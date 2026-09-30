"""Stage 3: structure-aware chunking.

Every chunk carries scheme_name, source_url and text, plus a section breadcrumb
and the fetch date. Splitting is heading-first: the corpus is a clean markdown
document, and a fact's heading (Expense Ratio, Exit Load, Minimum SIP, ...) is
the natural boundary. A chunk therefore reads "### Expense Ratio\\n1.03%" rather
than a sentence fragment with no label.

Three rules, in order of importance:

  1. Never split a table row. Fee and spec blocks are atomic; a chunk reading
     "1% if redeemed within" with no label is worse than no chunk at all.
  2. Give prose a small overlap so a sentence is not cut in half.
  3. Drop stubs, but never drop a facts section, however short.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "data" / "docs"
CHUNKS_PATH = ROOT / "data" / "chunks.jsonl"

CHUNK_SIZE = 400
CHUNK_OVERLAP = 60
MIN_CHUNK_CHARS = 40
# ". " is deliberately absent. The FAQ answers are numbered lists ("1. Log on ...
# 2. Invest ..."), and splitting on ". " treats the list digit as its own piece, so
# the digit is dropped and the next chunk opens on an orphaned ". ". Sentence ends
# are still reachable: splitting falls through to " ", which is a word boundary and
# never cuts a token.
SEPARATORS = ("\n\n", "\n", "; ", ", ", " ", "")

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
FRONTMATTER_RE = re.compile(r"^\*\*([A-Za-z ]+):\*\*\s*(.*)$")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_RULE_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

# Provenance lines the index must not carry: the source URL is already a chunk
# field, and the fetch date is metadata. Everything else in the frontmatter is a
# real fact about the scheme and is kept.
FRONTMATTER_SKIP = {"source url", "facts last updated from sources"}

# Frontmatter keys promoted to their own atomic fact chunk. Keyed by the label as
# it appears in the document.
FRONTMATTER_FACTS = {"amc": "AMC", "category": "Category", "plan": "Plan"}

# A section under one of these is a single atomic fact and is never subdivided.
FACT_SECTIONS = {
    "scheme facts",
    "benchmark",
    "expense ratio",
    "base expense ratio",
    "exit load",
    "stamp duty",
    "minimum sip",
    "minimum lumpsum",
    "lock-in period",
    "fund size (aum)",
    "nav",
    "nav date",
    "portfolio turnover",
    "launch date",
    "isin",
    "groww rating",
    "fund manager",
    "fund manager since",
}


@dataclass
class Chunk:
    chunk_id: str
    scheme_name: str
    source_url: str
    section: str
    text: str
    fetched_at: str
    category: str
    kind: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Section:
    headings: list[str]
    body: str
    kind: str

    @property
    def breadcrumb(self) -> str:
        return " > ".join(self.headings) if self.headings else "Page"


def _classify(headings: list[str], body: str) -> str:
    leaf = headings[-1].strip().lower() if headings else ""
    if leaf in FACT_SECTIONS:
        return "fact"
    if leaf == "faq" or any(h.strip().lower() == "faq" for h in headings):
        return "faq"
    if TABLE_ROW_RE.search(body) and body.count("|") > 6:
        return "table"
    return "prose"


def split_sections(markdown: str) -> list[Section]:
    """Split on ATX headings, tracking the heading breadcrumb as we descend."""
    sections: list[Section] = []
    headings: list[str] = []
    buffer: list[str] = []
    root_level: int | None = None

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append(
                Section(
                    headings=list(headings),
                    body=body,
                    kind=_classify(headings, body),
                )
            )
        buffer.clear()

    for line in markdown.splitlines():
        match = HEADING_RE.match(line)
        if match:
            flush()
            level, title = len(match.group(1)), match.group(2).strip()
            if root_level is None:
                root_level = level
            relative = max(level - root_level, 0)
            headings = headings[:relative]
            headings.append(title)
        else:
            buffer.append(line)
    flush()
    return sections


def parse_frontmatter(body: str) -> tuple[list[tuple[str, str]], str]:
    """Pull the **Label:** value pairs off the top of a document.

    Returns the kept (label, value) pairs plus the body with those lines removed.
    The source URL and the fetch date are dropped because both already live in
    chunk metadata; duplicating them in the indexed text would only give the
    retriever a second, noisier copy of the same fact.
    """
    facts: list[tuple[str, str]] = []
    kept: list[str] = []
    for line in body.splitlines():
        match = FRONTMATTER_RE.match(line.strip())
        if match:
            label = match.group(1).strip().lower()
            value = match.group(2).strip()
            if label not in FRONTMATTER_SKIP and value:
                facts.append((FRONTMATTER_FACTS.get(label, label), value))
            continue
        kept.append(line)
    return facts, "\n".join(kept).strip()


def _split_table(body: str, size: int) -> list[str]:
    """Split a table on row boundaries, repeating the header on each chunk."""
    if len(body) <= size:
        return [body]

    header: list[str] = []
    rows: list[str] = []
    for line in body.splitlines():
        if not TABLE_ROW_RE.match(line):
            if not header:
                header = [line.strip()]
            continue
        if TABLE_RULE_RE.match(line):
            header.append(line.strip())
            continue
        rows.append(line)

    if not rows:
        return [body]

    preamble = "\n".join(header)
    chunks: list[str] = []
    current = preamble
    for row in rows:
        candidate = f"{current}\n{row}"
        if len(candidate) > size and current != preamble:
            chunks.append(current.strip())
            current = f"{preamble}\n{row}"
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _split_prose(body: str, size: int, overlap: int, separators=SEPARATORS) -> list[str]:
    """Recursively split on the earliest separator that yields pieces under size."""
    text = body.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    for index, separator in enumerate(separators):
        if not separator or separator not in text:
            continue
        pieces: list[str] = []
        for position, part in enumerate(text.split(separator)):
            # str.split() consumes the separator *between* pieces, so only the
            # pieces after the first get it back. Restoring it on the first piece
            # too prepends a separator the text never had, which is what left
            # chunks opening on a bare ", " or ". ".
            if index == 0 or position == 0:
                piece = part
            else:
                piece = separator + part
            if not piece.strip():
                continue
            if len(piece) > size:
                pieces.extend(_split_prose(piece, size, overlap, separators[index + 1 :]))
            else:
                pieces.append(piece)
        return _merge(pieces, size, overlap, separator)
    return [text]


def _carry_tail(current: str, overlap: int) -> str:
    """The overlap to carry into the next chunk, snapped to a word boundary.

    A raw `current[-overlap:]` slice starts mid-word, so the next chunk opens on
    "d take a few minutes to complete" instead of "take a few minutes". Snapping
    forward to the next word start costs a few characters and never produces a
    broken token. Orphaned leading punctuation is stripped because a cut list
    marker ("1. You can...") otherwise leaves a bare "." at the head of a chunk.
    """
    if not overlap:
        return ""
    tail = current[-overlap:]
    boundary = re.search(r"\s\S", tail)
    if boundary:
        tail = tail[boundary.end() - 1 :]
    return tail.lstrip(" .,;:)-")


def _merge(pieces: list[str], size: int, overlap: int, separator: str) -> list[str]:
    """Re-join small pieces up to size, carrying a tail overlap between them."""
    merged: list[str] = []
    current = ""
    for piece in pieces:
        candidate = current + piece
        if current and len(candidate) > size:
            merged.append(current.strip())
            current = _carry_tail(current, overlap) + piece
        else:
            current = candidate
    if current.strip():
        merged.append(current.strip())
    return [m for m in merged if m]


def _chunk_id(scheme_name: str, source_url: str, section: str, text: str) -> str:
    digest = hashlib.sha256(
        f"{source_url}|{section}|{text}".encode("utf-8")
    ).hexdigest()
    return digest[:16]


def chunk_markdown(
    markdown: str,
    *,
    scheme_name: str,
    source_url: str,
    category: str = "",
    fetched_at: str = "",
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[Chunk]:
    """Split one markdown document into chunks that keep their provenance."""
    if not source_url:
        raise ValueError(f"{scheme_name or 'document'}: source_url is required")

    chunks: list[Chunk] = []
    promoted: list[tuple[str, str]] = []
    sections = split_sections(markdown)

    for section in sections:
        facts, body = parse_frontmatter(section.body)
        promoted.extend(facts)
        if not body:
            continue
        atomic = section.kind == "fact"
        if len(body) < MIN_CHUNK_CHARS and not atomic:
            continue

        if section.kind == "table":
            pieces = _split_table(body, size)
        else:
            pieces = _split_prose(body, size, overlap)

        for piece in pieces:
            if not piece.strip():
                continue
            if len(piece) < MIN_CHUNK_CHARS and not atomic:
                continue
            text = f"{section.breadcrumb}\n\n{piece}".strip()
            chunks.append(
                Chunk(
                    chunk_id=_chunk_id(scheme_name, source_url, section.breadcrumb, text),
                    scheme_name=scheme_name,
                    source_url=source_url,
                    section=section.breadcrumb,
                    text=text,
                    fetched_at=fetched_at,
                    category=category,
                    kind=section.kind,
                )
            )

    # The document title is the fund's own name, and the frontmatter holds its
    # category, AMC and plan. Neither reaches the retriever through the body
    # chunks - the title only ever appears as a breadcrumb, and the frontmatter is
    # stripped - so "what is the fund name/category" had nothing to retrieve.
    # Promoting each to its own atomic fact keeps them answerable and independently
    # scorable, exactly like the Scheme Facts values.
    root = sections[0].breadcrumb if sections else "Page"
    for label, value in [("Fund Name", scheme_name)] + promoted:
        promoted_section = f"{root} > {label}"
        text = f"{promoted_section}\n\n{value}"
        chunks.append(
            Chunk(
                chunk_id=_chunk_id(scheme_name, source_url, promoted_section, text),
                scheme_name=scheme_name,
                source_url=source_url,
                section=promoted_section,
                text=text,
                fetched_at=fetched_at,
                category=category,
                kind="fact",
            )
        )

    return chunks


def chunk_documents(
    documents: list[dict],
    *,
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[Chunk]:
    """Chunk a list of records as returned by load.load_all()."""
    chunks: list[Chunk] = []
    for document in documents:
        produced = chunk_markdown(
            document["markdown"],
            scheme_name=document["scheme_name"],
            source_url=document["source_url"],
            category=document.get("category", ""),
            fetched_at=document.get("fetched_at", ""),
            size=size,
            overlap=overlap,
        )
        print(f"  {document['slug']}: {len(produced)} chunks")
        chunks.extend(produced)
    return chunks


def chunk_docs_dir(docs_dir: Path = DOCS_DIR) -> list[Chunk]:
    """Chunk every cleaned document on disk, using its .meta.json sidecar."""
    if not docs_dir.exists():
        raise FileNotFoundError(
            f"{docs_dir} does not exist. Run src/ingest/load.py first."
        )

    documents: list[dict] = []
    for markdown_path in sorted(docs_dir.glob("*.md")):
        meta_path = markdown_path.with_suffix("").with_suffix(".meta.json")
        if not meta_path.exists():
            raise FileNotFoundError(
                f"Missing provenance sidecar {meta_path.name}; refusing to chunk."
            )
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        documents.append({**meta, "markdown": markdown_path.read_text(encoding="utf-8")})
    return chunk_documents(documents)


def write_chunks(chunks: list[Chunk], path: Path = CHUNKS_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")
    return path


def read_chunks(path: Path = CHUNKS_PATH) -> list[Chunk]:
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist.")
    return [
        Chunk(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def summarise(chunks: list[Chunk]) -> str:
    """Human-readable distribution, for eyeballing a build."""
    by_kind: dict[str, int] = {}
    for chunk in chunks:
        by_kind[chunk.kind] = by_kind.get(chunk.kind, 0) + 1
    lengths = sorted(len(c.text) for c in chunks)
    if not lengths:
        return "no chunks"
    median = lengths[len(lengths) // 2]
    over = sum(1 for length in lengths if length > CHUNK_SIZE * 1.5)
    return (
        f"{len(chunks)} chunks | kinds={by_kind} | "
        f"chars min={lengths[0]} p50={median} max={lengths[-1]} | over 1.5x={over}"
    )


if __name__ == "__main__":
    produced = chunk_docs_dir()
    target = write_chunks(produced)
    print(f"\n{summarise(produced)}")
    print(f"written -> {target}")
