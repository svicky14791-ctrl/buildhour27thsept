"""Stage 1-2: load the 5 HDFC scheme pages and normalise each into markdown.

Fetch -> snapshot verbatim -> clean -> one markdown document per scheme, built
from three extraction channels:

  A. __NEXT_DATA__  (props.pageProps.mfServerSideData) - structured scheme facts
  B. rendered body  - h1..h5 preserved as markdown headings
  C. ld+json        - FAQPage prose

Only the 5 allowlisted URLs in SOURCES are ever fetched. Performance fields
(sip_return, simple_return, return_stats, holdings, peerComparison, ...) are
deliberately not extracted: the product forbids performance claims, so the
model is never placed in a position where it could quote a return.

`fund_manager_details` was previously on that exclusion list. It is now
extracted (see extract_manager_facts): names and start dates are facts, not
performance. The verbose prose block for the same section stays excluded via
PERFORMANCE_RE, so the education/experience/funds-managed paragraph is not
duplicated into the corpus.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DOCS_DIR = DATA_DIR / "docs"
PROBLEM_STATEMENT = ROOT / "Problemstatement.txt"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 30
RETRIES = 3
BACKOFF = 1.5
POLITENESS_DELAY = 1.0

URL_PATTERN = re.compile(r"https://groww\.in/mutual-funds/[a-z0-9\-]+")


@dataclass(frozen=True)
class Source:
    """One allowlisted scheme page."""

    slug: str
    scheme_name: str
    category: str
    url: str

    @property
    def display_name(self) -> str:
        return f"{self.scheme_name} ({self.category})"


# Problemstatement.txt is currently empty, so these are transcribed from the
# milestone brief. verify_sources() cross-checks them against the file as soon
# as it has content, and refuses to run on any URL it does not recognise.
SOURCES: tuple[Source, ...] = (
    Source(
        slug="hdfc-large-cap-fund-direct-growth",
        scheme_name="HDFC Large Cap Fund - Direct Growth",
        category="Large Cap",
        url="https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
    ),
    Source(
        slug="hdfc-equity-fund-direct-growth",
        scheme_name="HDFC Equity Fund - Direct Growth",
        category="Flexi Cap",
        url="https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth",
    ),
    Source(
        slug="hdfc-elss-tax-saver-fund-direct-plan-growth",
        scheme_name="HDFC ELSS Tax Saver Fund - Direct Plan - Growth",
        category="ELSS (Tax)",
        url=(
            "https://groww.in/mutual-funds/"
            "hdfc-elss-tax-saver-fund-direct-plan-growth"
        ),
    ),
    Source(
        slug="hdfc-small-cap-fund-direct-growth",
        scheme_name="HDFC Small Cap Fund - Direct Growth",
        category="Small Cap",
        url="https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth",
    ),
    Source(
        slug="hdfc-balanced-advantage-fund-direct-growth",
        scheme_name="HDFC Balanced Advantage Fund - Direct Growth",
        category="Balanced Advantage (Hybrid)",
        url=(
            "https://groww.in/mutual-funds/"
            "hdfc-balanced-advantage-fund-direct-growth"
        ),
    ),
)

ALLOWED_URLS = frozenset(s.url for s in SOURCES)
BY_SLUG = {s.slug: s for s in SOURCES}
BY_URL = {s.url: s for s in SOURCES}

# Fields present in mfServerSideData that must never be extracted.
EXCLUDED_KEYS = frozenset(
    {
        "sip_return",
        "simple_return",
        "return_stats",
        "stats",
        "holdings",
        "peerComparison",
        "historic_exit_loads",
        "investment_date_configs",
        "stp_details",
        "swp_details",
        "nfo_risk",
    }
)

# (heading, mfServerSideData key, formatter). Only keys observed in the live
# pages. A key that is absent is skipped, never defaulted.
KEY_FACTS: tuple[tuple[str, str, str], ...] = (
    ("Benchmark", "benchmark", "text"),
    ("Expense Ratio", "expense_ratio", "pct"),
    ("Base Expense Ratio", "base_expense_ratio", "pct"),
    ("Exit Load", "exit_load", "text"),
    ("Stamp Duty", "stamp_duty", "text"),
    ("Minimum SIP", "min_sip_investment", "inr"),
    ("Minimum Lumpsum", "min_investment_amount", "inr"),
    ("Lock-in Period", "lock_in", "lockin"),
    ("Fund Size (AUM)", "aum", "crore"),
    ("NAV", "nav", "inr"),
    ("NAV Date", "nav_date", "text"),
    ("Portfolio Turnover", "portfolio_turnover", "text"),
    ("Launch Date", "launch_date", "text"),
    ("ISIN", "isin", "text"),
    ("Groww Rating", "groww_rating", "text"),
)

HEADING_TAGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####"}
DROP_TAGS = {
    "script", "style", "noscript", "nav", "footer", "header", "svg",
    "button", "form", "iframe", "template",
}
# nav + footer account for roughly half of naive get_text() output, including an
# SEO link farm of single-letter mutual-fund links.
LINK_FARM_RE = re.compile(
    r"^(?:mutual funds|stock exchanges|calculators?)\s*:\s*[A-Z](?:\s+[A-Z]){3,}$",
    re.IGNORECASE,
)
NOISE_RE = re.compile(r"^(?:view details|know more|read more|show more|close|\W)*$", re.I)

# Performance, ranking and portfolio-holdings content is dropped on every channel,
# not just the mfServerSideData one. The product contract forbids return claims, so
# a figure that is never ingested is a figure that can never be quoted. Note that
# bare "portfolio" is deliberately absent: "Portfolio Turnover" is an allowed fact,
# whereas "holdings" only ever names a portfolio table or a peer-ranked return.
PERFORMANCE_RE = re.compile(
    r"(?i)(?:\breturns?\b|\bperformance\b|\branks?\b|\bpeer\b|\bcagr\b|"
    r"\bsharpe\b|\balpha\b|\bbeta\b|\bannualis\w*|\bannualiz\w*|"
    r"\bholdings\b|\bportfolio\s+holdings\b|\bfund\s+portfolio\b|"
    r"\basset\s+allocation\b|\bsector\s+allocation\b|\btop\s*10\b|"
    r"\bfund\s+manager\b|\bprice\s+to\s+(?:earnings|book)\b|"
    r"\bpe\b|\bpb\b)"
)
# Rendered tables arrive under a synthetic "Table" heading, so their content is the
# only available signal. Matched case-insensitively against the whole row block.
PERFORMANCE_TABLE_MARKERS = (
    "fund returns",
    "category average",
    "rank (",
    "category rank",
    "instruments",
    "peer comparison",
)


@dataclass
class Page:
    source: Source
    html: str
    fetched_at: str
    content_hash: str

    @property
    def scheme_name(self) -> str:
        return self.source.scheme_name

    @property
    def source_url(self) -> str:
        return self.source.url

    @property
    def fetched_date(self) -> str:
        return self.fetched_at[:10]


def problem_statement_urls() -> list[str]:
    """groww.in mutual-fund URLs found in Problemstatement.txt, if any."""
    if not PROBLEM_STATEMENT.exists():
        return []
    text = PROBLEM_STATEMENT.read_text(encoding="utf-8", errors="replace")
    seen: list[str] = []
    for url in URL_PATTERN.findall(text):
        if url not in seen:
            seen.append(url)
    return seen


def verify_sources() -> list[str]:
    """Return human-readable problems. Empty list means the source set is sound."""
    problems: list[str] = []
    if not PROBLEM_STATEMENT.exists():
        return [f"{PROBLEM_STATEMENT.name} is missing"]

    text = PROBLEM_STATEMENT.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return [
            f"{PROBLEM_STATEMENT.name} is empty - SOURCES is transcribed from the "
            "milestone brief, not read from the file"
        ]

    for url in problem_statement_urls():
        if url not in ALLOWED_URLS:
            problems.append(
                f"URL in {PROBLEM_STATEMENT.name} is not in the allowlist: {url}"
            )
    for url in ALLOWED_URLS:
        if url not in text:
            problems.append(f"allowlisted URL missing from the brief: {url}")
    return problems


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def fetch(url: str, session: requests.Session | None = None) -> str:
    """Fetch one page, returning HTML. Raises on terminal failure."""
    if url not in ALLOWED_URLS:
        raise ValueError(f"Refusing to fetch a non-allowlisted URL: {url}")

    session = session or requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        }
    )

    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            response = session.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            # Explicit decode: these pages carry U+20B9 and corrupt silently
            # through a non-UTF-8 locale.
            return response.content.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - retried, then reported
            last_error = exc
            if attempt < RETRIES:
                time.sleep(BACKOFF * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def fetch_all(delay: float = POLITENESS_DELAY) -> list[Page]:
    pages: list[Page] = []
    session = requests.Session()
    for index, source in enumerate(SOURCES, start=1):
        print(f"[{index}/{len(SOURCES)}] {source.display_name}")
        html = fetch(source.url, session)
        page = Page(
            source=source,
            html=html,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            content_hash=_content_hash(html),
        )
        pages.append(page)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_DIR / f"{source.slug}.html").write_text(page.html, encoding="utf-8")
        print(f"    {len(html):,} chars  hash={page.content_hash}  -> data/raw/{source.slug}.html")
        if index < len(SOURCES):
            time.sleep(delay)
    return pages


def _format(value: object, kind: str) -> str | None:
    """Render a value, or None when it cannot be rendered faithfully."""
    if value is None:
        return None
    if isinstance(value, dict):
        parts = [
            f"{int(v)} {k[:-1]}"
            for k, v in value.items()
            if isinstance(v, (int, float)) and v
        ]
        return " ".join(parts) if parts else "None"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v not in (None, "")) or None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool):
        return None
    if kind in {"pct", "inr", "crore"}:
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return str(value)
        if kind == "pct":
            return f"{number:.2f}%"
        if kind == "crore":
            return f"\u20b9{number:,.2f} Cr"
        return f"\u20b9{number:,.2f}"
    return str(value)


def extract_next_data(html: str) -> dict:
    """Return mfServerSideData. Empty dict when the schema is absent or changed."""
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag is None or not tag.string:
        return {}
    try:
        data = json.loads(tag.string)
    except json.JSONDecodeError:
        return {}
    result = data.get("props", {}).get("pageProps", {}).get("mfServerSideData", {})
    return result if isinstance(result, dict) else {}


def _manager_date(value: object) -> str | None:
    """'2023-06-21T18:30:00.000Z' -> '21 June 2023'."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return f"{parsed.day} {parsed.strftime('%B %Y')}"


def extract_manager_facts(facts: dict) -> list[tuple[str, str]]:
    """Fund-manager names and their start dates, for the page's own scheme.

    `fund_manager_details` is a *list* of co-manager records, not a scalar, so it
    cannot go through KEY_FACTS/_format. Four of the five schemes here are
    co-managed (Large Cap and Small Cap have 2, Balanced Advantage has 6), so a
    single "Fund Manager" value would be a lie. Every current record is emitted
    with its own start date.

    Tenure is deliberately stored as an absolute "managing since" date rather
    than a duration: a duration computed at build time silently rots, while a
    date stays true. Records with a `date_to` are ex-managers and are excluded -
    a departed manager is not a current fact, and surfacing one is the exact
    staleness failure this fact is most prone to.
    """
    records = facts.get("fund_manager_details")
    if not isinstance(records, list):
        return []

    names: list[str] = []
    since: list[str] = []
    for record in records:
        if not isinstance(record, dict) or record.get("date_to"):
            continue
        name = record.get("person_name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
        start = _manager_date(record.get("date_from"))
        if start:
            since.append(start)

    if not names:
        return []
    pairs = [("Fund Manager", ", ".join(names))]
    if since:
        pairs.append(("Fund Manager Since", ", ".join(since)))
    return pairs


def extract_key_facts(page: Page) -> list[tuple[str, str]]:
    """(heading, value) pairs for the facts we may answer questions with.

    A key that is missing from the page yields no line at all - the gap is left
    visible rather than filled with a plausible default.
    """
    facts = extract_next_data(page.html)
    if not facts:
        return []

    pairs: list[tuple[str, str]] = []
    for heading, key, kind in KEY_FACTS:
        if key in EXCLUDED_KEYS or key not in facts:
            continue
        rendered = _format(facts[key], kind)
        if rendered:
            pairs.append((heading, rendered))
    pairs.extend(extract_manager_facts(facts))
    return pairs


def extract_faq(html: str) -> list[tuple[str, str]]:
    """(question, answer) pairs from any application/ld+json FAQPage block."""
    soup = BeautifulSoup(html, "lxml")
    found: list[tuple[str, str]] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict) or "FAQPage" not in str(node.get("@type", "")):
                continue
            for entry in node.get("mainEntity") or []:
                if not isinstance(entry, dict):
                    continue
                question = (entry.get("name") or "").strip()
                answer_html = (entry.get("acceptedAnswer") or {}).get("text", "")
                answer = _inline_markdown(answer_html)
                if question and answer and not _is_performance(question):
                    found.append((question, answer))
    return found


def _is_performance(text: str) -> bool:
    """True when text names or contains returns, ranking or portfolio holdings."""
    if PERFORMANCE_RE.search(text or ""):
        return True
    lowered = (text or "").lower()
    return any(marker in lowered for marker in PERFORMANCE_TABLE_MARKERS)


def _inline_markdown(fragment: str) -> str:
    """Flatten a small HTML fragment to plain text without inventing line breaks."""
    if not fragment:
        return ""
    soup = BeautifulSoup(fragment, "lxml")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with(" ")
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _table_to_markdown(table: Tag) -> str:
    rows: list[list[str]] = []
    for row in table.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def extract_body_sections(html: str) -> list[tuple[str, str]]:
    """(heading, body) pairs from the rendered page, boilerplate removed.

    Returns tables as a body under a synthetic "Table" heading, so the chunker
    can keep them intact.
    """
    soup = BeautifulSoup(html, "lxml")
    root = BeautifulSoup(str(soup.body or soup), "lxml")
    for tag in root.find_all(list(DROP_TAGS)):
        tag.decompose()

    sections: list[tuple[str, str]] = []
    seen: set[str] = set()

    def push(heading: str, body: str) -> None:
        cleaned = re.sub(r"[ \t]+", " ", body).strip()
        if not cleaned or NOISE_RE.match(cleaned) or LINK_FARM_RE.match(cleaned):
            return
        if _is_performance(heading) or _is_performance(cleaned):
            return
        key = cleaned.lower()
        if key in seen:  # hero values are repeated in several places on the page
            return
        seen.add(key)
        sections.append((heading, cleaned))

    current = "Page"
    buffer: list[str] = []
    skipping = False
    for node in root.find_all(list(HEADING_TAGS) + ["p", "li", "table", "dd", "dt"]):
        if node.name in HEADING_TAGS:
            if buffer:
                push(current, "\n".join(buffer))
                buffer = []
            current = node.get_text(" ", strip=True) or current
            # A performance heading silences its own section, up to the next heading.
            skipping = bool(PERFORMANCE_RE.search(current))
        elif skipping:
            continue
        elif node.name == "table":
            if buffer:
                push(current, "\n".join(buffer))
                buffer = []
            push("Table", _table_to_markdown(node))
        elif node.name in {"dd", "dt"}:
            push(current, node.get_text(" ", strip=True))
        elif not node.find_parent(list(HEADING_TAGS)):
            buffer.append(node.get_text(" ", strip=True))
    if buffer:
        push(current, "\n".join(buffer))

    return [(h, b) for h, b in sections if b]


def to_markdown(page: Page) -> str:
    """Render one fetched page as a single markdown document."""
    parts = [
        f"# {page.source.scheme_name}",
        "**AMC:** HDFC Mutual Fund",
        f"**Category:** {page.source.category}",
        "**Plan:** Direct Growth",
        f"**Source URL:** {page.source_url}",
        f"**Facts last updated from sources:** {page.fetched_date}",
    ]

    facts = extract_key_facts(page)
    if facts:
        parts.append("## Scheme Facts")
        parts.extend(f"### {heading}\n{value}" for heading, value in facts)

    faq = extract_faq(page.html)
    if faq:
        parts.append("## FAQ")
        parts.extend(f"### {question}\n{answer}" for question, answer in faq)

    body = extract_body_sections(page.html)
    if body:
        parts.append("## Page Sections")
        parts.extend(f"### {heading}\n{content}" for heading, content in body)

    if not facts and not faq and not body:
        raise ValueError(
            f"Nothing extractable from {page.source_url}. The page structure may "
            "have changed, or the fetch returned a JavaScript-only shell."
        )

    return "\n\n".join(parts).strip() + "\n"


def pages_from_raw() -> list[Page]:
    """Rebuild Page objects from the cached snapshots in data/raw.

    `fetch_all` always hits the network and stamps `fetched_at` with the current
    time, so re-running it to pick up a cleaning change silently re-dates the
    whole corpus and makes the rebuild unreproducible. This replays the stored
    HTML instead and reuses the `fetched_at` already recorded in the sidecar
    meta, so a cleaning change moves chunk counts without moving the as-of date.

    Raises if a snapshot is missing: silently skipping one would quietly shrink
    the corpus from 5 schemes to 4.
    """
    pages: list[Page] = []
    for source in SOURCES:
        html_path = RAW_DIR / f"{source.slug}.html"
        meta_path = DOCS_DIR / f"{source.slug}.meta.json"
        if not html_path.exists() or not meta_path.exists():
            raise FileNotFoundError(
                f"No cached snapshot for {source.slug}. Run `python src/ingest/load.py` "
                f"once to populate data/raw/ and data/docs/."
            )
        html = html_path.read_text(encoding="utf-8", errors="replace")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        page = Page(
            source=source,
            html=html,
            fetched_at=meta["fetched_at"],
            content_hash=meta.get("content_hash") or _content_hash(html),
        )
        if page.content_hash != _content_hash(html):
            print(f"    ! {source.slug}: stored content_hash differs from snapshot")
        pages.append(page)
        print(f"    replayed {source.slug}: {len(html):,} chars  fetched_at={page.fetched_at}")
    return pages


def load_all(save: bool = True, offline: bool = False) -> list[dict]:
    """Fetch and clean every source. Returns one record per document.

    offline=True replays data/raw instead of hitting the network, preserving the
    recorded fetched_at. Use it for cleaning/formatting changes only.
    """
    problems = verify_sources()
    for problem in problems:
        print(f"  ! {problem}")

    pages = pages_from_raw() if offline else fetch_all()
    documents: list[dict] = []
    for page in pages:
        markdown = to_markdown(page)
        if save:
            DOCS_DIR.mkdir(parents=True, exist_ok=True)
            (DOCS_DIR / f"{page.source.slug}.md").write_text(markdown, encoding="utf-8")
            (DOCS_DIR / f"{page.source.slug}.meta.json").write_text(
                json.dumps(
                    {
                        "slug": page.source.slug,
                        "scheme_name": page.scheme_name,
                        "category": page.source.category,
                        "source_url": page.source_url,
                        "fetched_at": page.fetched_at,
                        "content_hash": page.content_hash,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        print(f"    cleaned {page.source.slug}: {len(markdown):,} chars")
        documents.append(
            {
                "slug": page.source.slug,
                "scheme_name": page.scheme_name,
                "category": page.source.category,
                "source_url": page.source_url,
                "fetched_at": page.fetched_at,
                "markdown": markdown,
            }
        )
    return documents


if __name__ == "__main__":
    offline = "--offline" in sys.argv
    if offline:
        print("Replaying cached snapshots in data/raw (no network, dates preserved)")
    loaded = load_all(offline=offline)
    print(f"\nLoaded {len(loaded)}/{len(SOURCES)} documents into {DOCS_DIR}")
    raise SystemExit(0 if len(loaded) == len(SOURCES) else 1)
