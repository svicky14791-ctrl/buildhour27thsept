# Chunking

How `data/chunks.jsonl` is produced, and why. Read `src/ingest/load.py` then
`src/ingest/chunk.py`.

## Inputs

Five Markdown files in `data/docs/`, one per allowlisted scheme page, each with a
`.meta.json` sidecar holding the source URL, category and `fetched_at` date. The
loader is strict: any URL that is not in `SOURCES` raises instead of being
processed, so a stray link can never enter the corpus.

## What is extracted

Under `## Scheme Facts`, each page carries these labelled values, verified present
on all five pages:

`Base Expense Ratio`, `Expense Ratio`, `Exit Load`, `Minimum SIP`,
`Minimum Lumpsum`, `Lock-in Period`, `Benchmark`, `Fund Size (AUM)`, `Stamp Duty`,
`ISIN`, `Launch Date`, `Portfolio Turnover`, `NAV`, `NAV Date`, `Groww Rating`

Plus the `How to Invest` / `How to Redeem` FAQ blocks and a few short prose
fragments that hold no number.

Lock-in is `3 year` for the ELSS Tax Saver and `None` for the other four. `None` is
a real answer, not a missing value, and is kept as text.

### Promoted from the document frontmatter

The `**AMC:**` / `**Category:**` / `**Plan:**` lines at the top of each document
used to be stripped along with the provenance lines, which left three real facts
unanswerable - "what is the fund category of the ELSS fund" retrieved the Exit Load
definition and the model replied that it could not find it. The document title had
the same problem in a milder form: it only ever appeared as a chunk breadcrumb, so
"what is the fund name" had no chunk to retrieve and the model inferred the answer
from its own prompt.

`parse_frontmatter()` now keeps those keys and `chunk_markdown()` promotes each to
its own atomic `fact` chunk, alongside an explicit `Fund Name` chunk:

| Promoted chunk | Source |
|---|---|
| `Fund Name` | the document's H1 title |
| `AMC`, `Category`, `Plan` | the frontmatter values |

`Source URL` and `Facts last updated from sources` are still dropped, because both
already exist as chunk metadata and a second copy in the indexed text would only
give the retriever a noisier duplicate.

**There is no `Fund Name` or `Fund Category` heading in the source pages.** An
earlier version of this document claimed both were present and verified on all five
pages; they were in neither the pages nor the index, and the claim was never true.

## What is deliberately dropped

Returns, ranking, riskometer and portfolio-holdings content is removed on **every
channel** — structured JSON, rendered body text, and FAQ answers. Dropping it in
one channel only is how this kind of leak happens, so `_is_performance()` is
applied at all three points in `load.py`.

Two traps worth recording:

- **Word choice.** The filter blocks `holdings`, `fund portfolio` and
  `portfolio holdings`, but **not** the bare word `portfolio` — because
  `Portfolio Turnover` is an allowed fact. The same reasoning applies in
  `guardrails.py`, where a bare `portfolio` advice pattern once refused a valid
  portfolio-turnover question.
- **Regex precision.** The first version of this filter matched `returns?`, which
  also hits "returns" inside ordinary prose. It is now anchored to figure-shaped
  patterns (percentages, `x`/`X` returns, ranks, "1 year return").

After chunking, `data/docs/` and `data/chunks.jsonl` are both grepped for return
and ranking language. Both are clean.

## Chunk shape

| Setting | Value |
|---|---|
| Target size | 400 chars |
| Overlap | 60 chars |
| Minimum chunk | 40 chars |
| Actual min / median / max | 54 / 89 / 474 chars |

Chunks are section-aware, not naive fixed-width windows. Splitting happens at
piece boundaries rather than at an exact character offset, so a number is never
cut in half. A fact chunk is normally one whole labelled value plus its units;
observed chunks are far smaller than the 400 target because the source pages have
one short line per fact and splitting them further would separate the label from
the value.

### Boundary defects that were fixed

Three separate bugs used to corrupt the first characters of a chunk. All three are
fixed, and the numbers below are measured on the current 166 chunks.

| Defect | Cause | Now |
|---|---|---|
| Chunk opened on a bare `. ` or `, ` | `str.split()` consumes the separator *between* pieces, but `_split_prose` restored it on the first piece too, prepending punctuation the text never had | 0 / 166 |
| Chunk opened mid-word (`thatd take a few`) | the 60-char overlap was a raw slice, `current[-60:]` | 0 / 166 |
| Numbered list markers lost their digit | `". "` was a split separator, so `2. Invest` split into `2` and `. Invest` | `". "` removed from `SEPARATORS` |

This last one is why the previous version of this document blamed "a stripped list
marker" for the leading `.`. The list markers in these FAQ answers are already
flattened to prose upstream; the `. ` was being manufactured by the splitter.

Two supporting changes:

- `_carry_tail()` snaps the overlap forward to the next word boundary
  (`re.search(r"\s\S", tail)`) and strips orphaned leading punctuation, so a carry
  costs a few characters but never breaks a token.
- `". "` is gone from `SEPARATORS`. Sentence ends are still reachable, because
  splitting falls through to `" "`, which is a word boundary and never cuts a
  token.

**Overlap still duplicates text by design.** 13 / 166 chunks contain a repeated
6-word run, almost all of them the scheme name appearing in both the breadcrumb and
the body. That is expected from a 60-character carry and is not a defect, but it is
why a real LLM can echo a phrase twice; `strip_model_footer()` in `src/chain.py`
removes model-written citation lines for the same reason the footer is built in
code rather than generated.

Verification: every one of the 166 chunks begins with a 4-word prefix that occurs
verbatim in its source document.

## Output

`data/chunks.jsonl` — one JSON object per line, 166 chunks:

| Kind | Count |
|---|---|
| `fact` | 120 |
| `faq` | 35 |
| `prose` | 11 |

Per scheme: Large Cap 34, Flexi Cap 34, Small Cap 34, ELSS 32, Balanced Advantage
32. The two 32s are the schemes whose FAQ answers are shorter after the `". "`
separator was removed, not missing content.

The 24 distinct `fact` leaves include four near-duplicate pairs that come from two
different headings on the page and are kept separate because they carry different
values: `Exit Load` / `Exit load`, `Expense Ratio` / `Expense ratio`, `Stamp Duty`
/ `Stamp duty`, and the prose `Table` section.

`Fund Manager` and `Fund Manager Since` must be listed in `FACT_SECTIONS` in
`src/ingest/chunk.py`, not just in the extractor's output. An unlisted leaf falls
through to the prose path, and anything under `MIN_CHUNK_CHARS` (40) is dropped
there — so `21 June 2023`, the entire Flexi Cap tenure value, was silently
discarded while Balanced Advantage's 6-name list survived. A fact that can be
shorter than the minimum chunk size has to be declared atomic.

Every chunk carries exactly the frozen `Chunk` fields and nothing else:

`chunk_id`, `scheme_name`, `source_url`, `section`, `text`, `fetched_at`,
`category`, `kind`

`chunk_id` is unique across all 166 rows, and every `source_url` is one of the five
allowlisted URLs. No table is stored, because the performance tables were removed
upstream.

## Rebuild

```
python src/ingest/load.py     # refetches the live pages
python src/ingest/chunk.py
python src/embed.py
```

`load.py` refetches the live pages, so a full rebuild re-reads Groww and can change
`fetched_at` and the corpus. To re-chunk and re-embed the **existing** documents
without touching the network, run only the last two steps. That is what produced
the current index, so the corpus dates are unchanged from the original fetch.

`chunk_id` is a content hash, so any change to chunk text changes every affected
id; the Chroma collection is dropped and rewritten by `embed.py`, and two
consecutive `embed.py` runs over unchanged chunks both produced the same count.

