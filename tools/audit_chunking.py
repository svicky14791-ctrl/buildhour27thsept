"""Audit: is the whole corpus chunked, and does retrieval find the right chunks?"""
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingest.chunk import chunk_docs_dir, split_sections, read_chunks

DOCS = ROOT / "data" / "docs"
CHUNKS = ROOT / "data" / "chunks.jsonl"

# ---------- A. inventory ----------
chunks = read_chunks()
print("=" * 70)
print("A. CHUNK INVENTORY")
print("=" * 70)
by_kind = {}
for c in chunks:
    by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
print(f"total chunks: {len(chunks)}   kinds: {by_kind}")
ids = [c.chunk_id for c in chunks]
print(f"unique chunk_ids: {len(set(ids))}  (duplicates: {len(ids)-len(set(ids))})")
per_scheme = {}
for c in chunks:
    per_scheme[c.scheme_name] = per_scheme.get(c.scheme_name, 0) + 1
for k, v in sorted(per_scheme.items()):
    print(f"  {v:>3}  {k}")

# ---------- B. coverage ----------
print()
print("=" * 70)
print("B. COVERAGE: every section in every doc -> did it survive chunking?")
print("=" * 70)
chunk_sections = {(c.scheme_name, c.section) for c in chunks}
missing_total = 0
dropped_bodies = []
for md_path in sorted(DOCS.glob("*.md")):
    meta = json.loads(md_path.with_suffix("").with_suffix(".meta.json").read_text(encoding="utf-8"))
    scheme = meta["scheme_name"]
    text = md_path.read_text(encoding="utf-8")
    secs = split_sections(text)
    for s in secs:
        key = (scheme, s.breadcrumb)
        if key not in chunk_sections:
            missing_total += 1
            body = s.body.strip()
            dropped_bodies.append((scheme, s.breadcrumb, s.kind, len(body), body[:90]))
print(f"sections present in docs but ABSENT from chunks: {missing_total}")
for scheme, bc, kind, n, preview in dropped_bodies[:25]:
    print(f"  [{kind:5}] {scheme[:28]:30} | {bc[:60]:62} ({n} ch) {preview!r}")

# ---------- C. fact completeness ----------
EXPECTED = ["Fund Name", "Category", "AMC", "Plan", "Base Expense Ratio",
            "Expense Ratio", "Exit Load", "Minimum SIP", "Minimum Lumpsum",
            "Lock-in Period", "Benchmark", "Fund Size (AUM)", "Stamp Duty", "ISIN",
            "Launch Date", "Portfolio Turnover", "NAV", "NAV Date", "Groww Rating",
            "Fund Manager", "Fund Manager Since"]
print()
print("=" * 70)
print(f"C. FACT COMPLETENESS (all 5 schemes x {len(EXPECTED)} facts)")
print("=" * 70)
scheme_names = sorted(per_scheme)
grid_missing = []
for scheme in scheme_names:
    got = {c.section.split(">")[-1].strip().lower() for c in chunks if c.scheme_name == scheme}
    for fact in EXPECTED:
        if fact.lower() not in got:
            grid_missing.append((scheme, fact))
print(f"missing fact cells: {len(grid_missing)} of {len(scheme_names)*len(EXPECTED)}")
for s, f in grid_missing:
    print(f"  MISSING  {s[:30]:32} {f}")

# ---------- D. overlap artifacts (doc says 18/141 affected) ----------
print()
print("=" * 70)
print("D. OVERLAP ARTIFACTS (mid-word starts, duplicated runs)")
print("=" * 70)
midword = dup = 0
examples = []
for c in chunks:
    body = c.text.split("\n\n", 1)[-1]
    if re.match(r"^[a-z]", body) and len(body) > 3:
        midword += 1
        if len(examples) < 4:
            examples.append(("midword/orphan", c.section[-40:], body[:70]))
    words = body.split()
    for n in (6,):
        seen = set()
        for i in range(len(words) - n):
            run = " ".join(words[i:i+n]).lower()
            if run in seen:
                dup += 1
                if len(examples) < 8:
                    examples.append(("dup-run", c.section[-40:], run))
                break
            seen.add(run)
print(f"chunks starting mid-word / orphan punct: {midword}")
print(f"chunks with a duplicated 6-word run:    {dup}")
for tag, sec, prev in examples:
    print(f"  [{tag}] ...{sec} | {prev!r}")

# ---------- E. empty / value-less fact chunks ----------
print()
print("=" * 70)
print("E. FACT CHUNKS WITH NO VALUE (label but empty body)")
print("=" * 70)
bad = []
for c in chunks:
    if c.kind != "fact":
        continue
    body = c.text.split("\n\n", 1)[-1].strip() if "\n\n" in c.text else ""
    if not body:
        bad.append((c.scheme_name, c.section, "<EMPTY>"))
    elif body.lower() in {"none", "n/a", "na", "-"}:
        bad.append((c.scheme_name, c.section, body))
print(f"empty-or-'None' fact chunks: {len(bad)}")
for s, sec, b in bad[:20]:
    print(f"  {s[:28]:30} | {sec.split('>')[-1].strip()[:34]:36} | {b!r}")
