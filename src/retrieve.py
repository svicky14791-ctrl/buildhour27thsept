"""Stage 7-8: scheme resolution and top-k retrieval against the Chroma collection.

Two design decisions that matter:

1. Retrieval is filtered on `scheme_name` *inside* Chroma via a `where` clause, not
   by filtering Python-side afterwards. Post-filtering silently shrinks the
   candidate pool, so a question about one scheme can be answered with a weaker
   match from another.
2. MIN_SIMILARITY is a floor on similarity (score = 1 - cosine_distance) used to
    separate in-scope questions from out-of-scope ones. It is deliberately *not*
    the mechanism for picking the right chunk within a scheme, because it cannot
    be: every chunk on a page repeats the full fund name in its breadcrumb, so an
    unrelated chunk from the correct page still scores ~0.60 against a question
    about that page - far above any floor that still admits genuine matches. The
    attribute gate below handles within-scheme selection instead. Measured on
    this corpus: a "fund category" question scored 0.597 against the Exit Load
    chunk before the gate existed.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.embed import (  # noqa: E402
    embed_texts,
    get_client,
    get_collection,
    perf,
)

TOP_K = 5
MIN_SIMILARITY = 0.25
# Chunks fetched when both the scheme and the asked-about attribute are known.
# Above the largest per-scheme chunk count (34 today) so it always covers the
# whole scheme; the attribute gate trims it back to top_k.
SCHEME_WIDE_FETCH = 64
SCHEME_FILTER_THRESHOLD = 0.82

LARGE_CAP = "HDFC Large Cap Fund - Direct Growth"
FLEXI_CAP = "HDFC Equity Fund - Direct Growth"
ELSS = "HDFC ELSS Tax Saver Fund - Direct Plan - Growth"
SMALL_CAP = "HDFC Small Cap Fund - Direct Growth"
BALANCED = "HDFC Balanced Advantage Fund - Direct Growth"

# Distinctive aliases are scheme-specific, so a match is strong enough to filter
# on. Weak aliases are shared vocabulary and must not narrow the search.
DISTINCTIVE_ALIASES: dict[str, tuple[str, ...]] = {
    LARGE_CAP: ("large cap",),
    FLEXI_CAP: ("flexi cap",),
    ELSS: ("elss", "tax saver", "80c"),
    SMALL_CAP: ("small cap",),
    BALANCED: ("balanced advantage", "balanced advantage fund"),
}
WEAK_ALIASES: dict[str, tuple[str, ...]] = {
    LARGE_CAP: ("hdfc large",),
    FLEXI_CAP: ("hdfc equity", "equity fund"),
    ELSS: ("hdfc elss",),
    SMALL_CAP: ("hdfc small",),
    BALANCED: ("balanced fund",),
}

WORD_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "of", "for", "in", "on", "to", "and",
        "or", "my", "me", "i", "what", "whats", "how", "do", "does", "did", "can",
        "you", "your", "it", "its", "be", "this", "that", "with", "about", "tell",
    }
)

# A question that names a specific attribute of a scheme. The corpus holds one
# short labelled value per attribute, so a question asking for one of these
# should be answered by a chunk that actually carries that label.
#
# This exists because a semantic floor cannot do the job. Every chunk in a scheme
# repeats the full fund name in its breadcrumb, so an unrelated chunk from the
# right page scores ~0.60 against a question about that page - well above any
# floor that still admits genuine matches. "What is the fund category of the ELSS
# fund" was retrieving the Exit Load and Expense Ratio definitions. Previously
# that produced a correct answer only because the model happened to notice the
# context was irrelevant; the gate makes it an explicit no_context instead.
ATTRIBUTE_TERMS: dict[str, tuple[str, ...]] = {
    "amc": ("amc", "asset management company", "fund house", "fund company"),
    "category": ("category", "cat", "type of fund", "fund type"),
    "plan": ("plan", "growth plan", "direct plan"),
    "fund name": ("fund name", "name of the fund", "full name"),
    "exit load": ("exit load", "exit loads", "load on exit", "exit charge"),
    "expense ratio": ("expense ratio", "expense ratios", "ter", "total expense"),
    "base expense ratio": ("base expense", "basic expense"),
    "stamp duty": ("stamp duty",),
    "minimum sip": ("minimum sip", "min sip", "smallest sip", "sip minimum"),
    "minimum lumpsum": ("minimum lumpsum", "min lumpsum", "lump sum", "lumpsum"),
    "lock-in": ("lock-in", "lock in", "lockin", "locking"),
    "benchmark": ("benchmark", "index", "index tracked"),
    "aum": ("aum", "fund size", "corpus", "net worth"),
    "nav": ("nav", "net asset value", "unit value"),
    "portfolio turnover": ("portfolio turnover", "turnover"),
    "fund manager": (
        "fund manager", "fund managers", "manager", "managers", "manages",
        "managed", "managing", "management", "tenure", "who manages",
    ),
    "fund manager since": (
        "fund manager since", "manager since", "managing since", "tenure",
        "how long", "since when", "started managing", "managing the fund since",
    ),
    "launch date": ("launch date", "launched", "inception", "started", "start date"),
    "isin": ("isin",),
    "groww rating": ("groww rating", "rating", "star rating"),
}

# A chunk matches an attribute if the label appears in its section breadcrumb or
# its text. Both are checked because a value can be named in the body while the
# breadcrumb carries the generic parent ("... > Scheme Facts > Expense Ratio").
ATTRIBUTE_LABEL_RE = {
    "amc": re.compile(r"\bamc\b|asset management", re.I),
    "category": re.compile(r"\bcategory\b", re.I),
    "plan": re.compile(r"\bplan\b", re.I),
    "fund name": re.compile(r"\bfund name\b", re.I),
    "exit load": re.compile(r"\bexit\s*load\b", re.I),
    "expense ratio": re.compile(r"\bexpense\s*ratio\b", re.I),
    "base expense ratio": re.compile(r"\bbase\s*expense\b", re.I),
    "stamp duty": re.compile(r"\bstamp\s*duty\b", re.I),
    "minimum sip": re.compile(r"\bminimum\s+sip\b", re.I),
    "minimum lumpsum": re.compile(r"\bminimum\s*lump\s*sum\b", re.I),
    "lock-in": re.compile(r"\block[\s-]*in\b", re.I),
    "benchmark": re.compile(r"\bbenchmark\b", re.I),
    "aum": re.compile(r"\b(aum|fund size)\b", re.I),
    "nav": re.compile(r"\bnav\b|net asset value", re.I),
    "portfolio turnover": re.compile(r"\bportfolio\s*turnover\b", re.I),
    "launch date": re.compile(r"\blaunch(?:ed)?\b|\binception\b", re.I),
    "isin": re.compile(r"\bisin\b", re.I),
    "groww rating": re.compile(r"\bgroww\s*rating\b", re.I),
    # Chunk side is deliberately NOT as loose as ATTRIBUTE_TERMS. It must match
    # the section labels that actually exist in the corpus, nothing else. An
    # earlier version reused the question's manage-root ("manage/managed/..."),
    # which _matches_attribute also tests against chunk *text* - so any FAQ
    # containing the word "manage" matched, filled the top_k slots, and pushed
    # the real Fund Manager chunk out. Loose synonyms belong on the question side.
    "fund manager": re.compile(r"\bfund\s*managers?\b", re.I),
    "fund manager since": re.compile(r"\bfund\s*manager\s*since\b", re.I),
}


# Plan-name suffixes. "Direct Plan Growth" is part of a fund's proper name, not a
# question about the plan attribute, but it contains the word "plan". Left in, the
# gate treats it as an attribute request and every chunk on the page matches it
# (each repeats the name), so the OR across attributes buries the real answer:
# "who is the fund manager of HDFC flexi cap direct plan growth?" detected
# {fund manager, plan}, the 5 kept slots filled with higher-scoring name-bearing
# chunks, and the Fund Manager chunk at rank 14 was dropped despite being the
# answer. Blanked before attribute detection only - the original question text is
# still what gets embedded and shown.
PLAN_NAME_SUFFIX_RE = re.compile(
    r"(?i)\b(?:direct\s+plan\s+growth|growth\s+plan|direct\s+growth)\b"
)


def question_attributes(question: str) -> set[str]:
    """Which corpus attributes this question asks about, if any.

    Longest alias wins per attribute so "base expense ratio" is not captured as
    "expense ratio". Returns an empty set for a question that names no attribute,
    which leaves behaviour exactly as it was before the gate.
    """
    text = (question or "").lower()
    scrubbed = PLAN_NAME_SUFFIX_RE.sub(" ", text)
    found: set[str] = set()
    for attribute, aliases in ATTRIBUTE_TERMS.items():
        for alias in aliases:
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", scrubbed):
                found.add(attribute)
                break
    return found


def _matches_attribute(hit: "Retrieved", attribute: str) -> bool:
    pattern = ATTRIBUTE_LABEL_RE.get(attribute)
    if pattern is None:
        return True
    return bool(pattern.search(hit.section) or pattern.search(hit.text))


def _apply_attribute_gate(
    question: str, hits: list[Retrieved], top_k: int
) -> list[Retrieved]:
    """Keep only chunks that actually carry the attribute the question asks for.

    Returns an empty list when nothing matches, which the chain turns into a
    no_context refusal. That is the point: an honest refusal is a better outcome
    than handing the model five chunks about something else and trusting it to
    notice.
    """
    attributes = question_attributes(question)
    if not attributes or not hits:
        return hits
    kept = [h for h in hits if any(_matches_attribute(h, a) for a in attributes)]
    return kept[:top_k]


@dataclass(frozen=True)
class Retrieved:
    chunk_id: str
    scheme_name: str
    source_url: str
    section: str
    text: str
    score: float
    kind: str = ""
    ingest_date: str = ""

    def as_log(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "section": self.section,
            "scheme_name": self.scheme_name,
            "score": round(self.score, 4),
        }


@dataclass(frozen=True)
class SchemeResolution:
    source: str | None
    confidence: float
    is_confident: bool
    matched_alias: str | None = None


def _tokens(text: str) -> set[str]:
    return {t for t in WORD_RE.findall((text or "").lower()) if t not in STOPWORDS}


def resolve_scheme(question: str) -> SchemeResolution:
    """Identify which of the 5 schemes a question is about.

    Generic tokens (fund, direct, growth, plan, hdfc) are deliberately never
    aliases: they appear in every question about the corpus and would make
    filtering meaningless.
    """
    text = (question or "").lower()
    if not text.strip():
        return SchemeResolution(None, 0.0, False)

    for scheme, aliases in DISTINCTIVE_ALIASES.items():
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias)}\b", text):
                return SchemeResolution(scheme, 1.0, True, alias)

    for scheme, aliases in WEAK_ALIASES.items():
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias)}\b", text):
                return SchemeResolution(scheme, 0.6, False, alias)

    return SchemeResolution(None, 0.0, False)


def _query(collection, vector: list[float], top_k: int, where: dict | None) -> list[Retrieved]:
    kwargs: dict = {"where": where} if where else {}
    result = collection.query(
        query_embeddings=[vector],
        n_results=top_k,
        include=["metadatas", "distances", "documents"],
        **kwargs,
    )
    ids = (result.get("ids") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    documents = (result.get("documents") or [[]])[0]

    out: list[Retrieved] = []
    for chunk_id, meta, distance, document in zip(ids, metadatas, distances, documents):
        score = 1.0 - float(distance)
        if score < MIN_SIMILARITY:
            continue
        out.append(
            Retrieved(
                chunk_id=chunk_id,
                scheme_name=(meta or {}).get("scheme_name", ""),
                source_url=(meta or {}).get("source_url", ""),
                section=(meta or {}).get("section", ""),
                text=document or "",
                score=score,
                kind=(meta or {}).get("kind", ""),
                ingest_date=(meta or {}).get("ingest_date", "") or "",
            )
        )
    return out


def retrieve(question: str, top_k: int = TOP_K) -> list[Retrieved]:
    """Return ranked chunks, scheme-filtered when the question names a scheme."""
    with perf("retrieve: total"):
        return _retrieve_inner(question, top_k)


def _retrieve_inner(question: str, top_k: int) -> list[Retrieved]:
    with perf("retrieve: embed query"):
        vector = embed_texts([question or ""])[0].tolist()
    with perf("retrieve: chroma open"):
        collection = get_collection(get_client())
    resolution = resolve_scheme(question)

    # A question that names an attribute has its answer in a specific chunk. That
    # chunk may rank far below top_k on pure similarity - every chunk on the page
    # repeats the fund name, so the wrong ones crowd it out - so over-fetch before
    # the attribute gate gets a chance to select it.
    #
    # When the scheme also resolves confidently, fetch that scheme's whole chunk
    # set instead of a fixed multiple. 3x was not enough: "who manages the flexi
    # cap fund?" is short, scores low across the board (~0.53), and left the
    # Fund Manager chunk below rank 15 while a Fund Manager *Since* chunk survived
    # - so the answer came back "I could not find the manager's name" with the
    # names sitting a rank or two further down. The gate filters whatever comes
    # back, so over-fetching is free; and the per-scheme set is small (~34).
    attributes = question_attributes(question)
    confident_scheme = bool(resolution.is_confident and resolution.source)
    fetch = SCHEME_WIDE_FETCH if (attributes and confident_scheme) else (
        top_k * 3 if attributes else top_k
    )

    if not confident_scheme:
        return _apply_attribute_gate(question, _query(collection, vector, fetch, None), top_k)

    where = {"scheme_name": resolution.source}
    filtered = _query(collection, vector, fetch, where)
    if filtered:
        return _apply_attribute_gate(question, filtered, top_k)

    # Second rung: the same query unfiltered, then narrowed back to this scheme.
    # Narrowing is allowed; widening to another scheme is not, so the ladder ends
    # at an empty list. Returning the unfiltered pool here would answer "the ELSS
    # exit load" from a Large Cap chunk, which is the confident-wrong-answer bug
    # the whole filter exists to prevent.
    narrowed = [r for r in _query(collection, vector, fetch, None) if r.scheme_name == resolution.source]
    if narrowed:
        return _apply_attribute_gate(question, narrowed, top_k)
    return []


if __name__ == "__main__":
    questions = [
        "What is the expense ratio of the HDFC Large Cap Fund?",
        "What is the lock-in period for the ELSS tax saver fund?",
        "How do I download a capital gains statement?",
        "What is the minimum SIP?",
    ]
    for question in questions:
        resolution = resolve_scheme(question)
        print(f"\nQ: {question}")
        print(
            f"   scheme={resolution.source} conf={resolution.confidence} "
            f"confident={resolution.is_confident} alias={resolution.matched_alias!r}"
        )
        hits = retrieve(question)
        if not hits:
            print("   (no chunk cleared MIN_SIMILARITY)")
        for hit in hits:
            print(
                f"   {hit.score:.4f}  {hit.scheme_name[:34]:<34} "
                f"{hit.section.split('>')[-1].strip()[:40]}"
            )
