# Architecture — MF Facts

**A RAG FAQ assistant for 5 HDFC Mutual Fund schemes.**
Organised around two questions: *what does the pipeline do at each stage?* and
*where in the pipeline is each product constraint enforced?*

Companion docs: [../PRD.md](../PRD.md) (requirements) · [CHUNKING.md](CHUNKING.md)
(chunking decision record) · [DISCLAIMER.md](DISCLAIMER.md) (exact copy) ·
[../README.md](../README.md) (setup)

> **Verification status:** written and designed, not yet executed. No Python runtime is
> installed on this machine, so `ingest → build → eval` has not run end to end.
> See [§11](#14-verification-status).

---

## 1. Pipeline at a glance

```
┌─ OFFLINE ─ build once, or on refresh ───────────────────────────────────────┐
│                                                                            │
│  [1] LOADING       5 allowlisted URLs → verbatim HTML + manifest            │
│       ↓                                                                    │
│  [2] CLEANING      HTML → one clean markdown doc per scheme                  │
│       ↓            (3 extraction channels merged)                          │
│  [3] CHUNKING      markdown → header-aware chunks, tables never split       │
│       ↓                                                                    │
│  [4] EMBEDDING     all-MiniLM-L6-v2 → 384-dim, CPU, normalised             │
│       ↓                                                                    │
│  [5] VECTOR STORE  ChromaDB persistent, 9 flat scalar metadata fields      │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
                                   ↓  data/chroma/  +  data/chunks.jsonl
┌─ ONLINE ─ per question ────────────────────────────────────────────────────┐
│                                                                            │
│  [6]  GUARDRAILS    6a PII screen → 6b intent/refusal check                │
│        ↓                    (before retrieval: 0 ms, 0 tokens on refusal)   │
│  [7]  QUERY EMBED   all-MiniLM-L6-v2                                        │
│        ↓                                                                   │
│  [8]  RETRIEVAL     unfiltered top-k → optional hard scheme filter →       │
│        ↓           similarity floor                                         │
│  [9]  GENERATION    grounded prompt, temp 0, swappable LLM                 │
│        ↓                                                                   │
│  [10] POST-CHECK    ≤3 sentences · exactly 1 citation · last-updated ·     │
│        ↓           no advice leakage  (enforced, not prompted)             │
│  [11] RENDER        answer + link + date + disclaimer                       │
│        ↓                                                                   │
│  [12] LOG           JSONL trace (retrieved ids, scores, timings)           │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

**Inputs:** 5 public scheme pages, allowlisted in `sources.py`.
**Outputs:** one 3-sentence answer, one citation URL, one date, zero advice, zero PII.

---

## 2. System context

```
        ┌────────────────────┐
        │     End user       │  retail investor · support/content agent
        │  (no account, no   │  class evaluator
        │   PII, no history) │
        └─────────┬──────────┘
                  │  one natural-language question
                  v
   ┌──────────────────────────────────────────────┐
   │  Entry points                                 │
   │   • Streamlit UI      app/streamlit_app.py   │
   │   • CLI               run.py ask              │
   │   • Eval harness      eval/run_eval.py        │
   └──────────────────┬───────────────────────────┘
                      v
   ┌──────────────────────────────────────────────┐
   │  MF Facts pipeline (this document)           │
   │                                              │
   │   offline  load → clean → chunk → embed →    │
   │            store                             │
   │   online   guardrails → retrieve → generate →│
   │            enforce → render → log            │
   └────┬───────────────────────────┬─────────────┘
        │                           │
        v                           v
┌────────────────────┐   ┌────────────────────────────┐
│ ChromaDB on disk   │   │ Generation backend         │
│ 384-dim vectors    │   │  1. OpenAI   gpt-4o-mini   │
│ + 9 scalar metadata│   │  2. Gemini   1.5-flash     │
│ data/chroma/       │   │  3. Ollama   llama3.1      │
└────────────────────┘   │  4. extractive stub        │
        ▲                 └────────────────────────────┘
        │  built offline                      ▲
        │  via run.py ingest + build          │  HTTPS, optional
┌───────┴──────────────────────────────────┴───┐
│  5 public groww.in scheme pages                │
│  allowlisted in sources.py — the only URLs    │
│  the system will ever request                  │
└────────────────────────────────────────────────┘
```

**Trust boundaries.** Two exist, and both are load-bearing:

1. **The public web → our corpus.** Scraped text is untrusted. It is treated as data
   throughout: the system prompt labels retrieved blocks as untrusted reference text, so
   a prompt-injection payload inside a scraped page cannot become an instruction.
2. **Our corpus → the LLM.** The model is treated as an unreliable narrator. It is never
   the source of a URL, a date, or a fact that reaches the user unchecked — §9.2 and
   §9.4 exist because of this boundary.

The user is **never** a trust source for anything that gets persisted. Input is screened
and, on refusal, not written to disk at all.

---

## 3. Component map

```
nextleap/
├── config.py                 all tunables + all product copy
├── sources.py                canonical 5-source list  (single source of truth)
├── run.py                    CLI: ingest | build | ask | eval | sources | app
│
├── ingest/                   ── OFFLINE · stages 1–2 ──
│   ├── loader.py             fetch + snapshot + manifest
│   ├── clean.py              HTML → markdown
│   └── pipeline.py           orchestration
│
├── transform/                ── OFFLINE · stage 3 ──
│   └── chunker.py            markdown → chunks
│
├── index/                    ── OFFLINE · stages 4–5 ──
│   ├── embedder.py           all-MiniLM-L6-v2
│   ├── vectorstore.py        Chroma client, upsert, query
│   └── build_index.py        orchestration
│
├── rag/                      ── ONLINE · stages 6–12 ──
│   ├── prompts.py            grounded prompt + context assembly
│   ├── generator.py          Generator ABC + 4 backends
│   ├── retriever.py          scheme resolution + two-pass top-k
│   └── chain.py              the orchestrator
│
├── guardrails/               ── ONLINE · stages 6 & 10 ──
│   ├── pii.py                7 regex patterns
│   ├── intent.py             advice / performance / corpus
│   └── output.py             answer contract
│
├── app/streamlit_app.py      FR-10 UI
├── eval/                     20-question labelled set + scorer
├── tools/export_sources.py   generates sources.csv / sources.md
├── docs/                     architecture.md · CHUNKING.md · DISCLAIMER.md
└── data/                     raw/ · docs/ · chroma/ · logs/ · chunks.jsonl
```

**Dependency direction is strictly one-way.**

```
config, sources            (leaves — no internal deps)
    ↑
ingest ──→ transform ──→ index
                ↑            ↑
                └──── rag ───┘
                    ↑
              guardrails
                    ↑
        run.py · app/ · eval/     (entry points only)
```

| Rule | Why |
|---|---|
| `config` and `sources` are leaves | Every tunables decision is in one file, so the README can point at exactly one place |
| Nothing imports `app` | The pipeline is usable headless — CLI, eval harness and UI all call `rag.chain.ask` |
| `rag → guardrails` but never the reverse | Guards are policy; the chain is flow. Policy must not know about flow |
| `eval/` and `app/` are siblings of `rag/` | The pipeline has exactly one public entry point: `rag.chain.ask` |

Adding a second entry point (FastAPI, a notebook) means writing one thin adapter and
changing no pipeline code.

---

## 4. Stage contracts

Each stage's input, transformation, and output — the boundaries a new component must
satisfy.

| # | Stage | Input | Transformation | Output |
|---|---|---|---|---|
| 1 | **Loading** | 5 URLs from `sources.py` | fetch w/ retries · snapshot verbatim · stamp `fetched_at` | `data/raw/{slug}.html` + `manifest.json` |
| 2 | **Cleaning** | raw HTML | 3 independent extractions (`__NEXT_DATA__`, rendered body, `ld+json`) → nav/footer stripped → tables → markdown → performance fields dropped | `data/docs/{slug}.md` + `{slug}.meta.json` |
| 3 | **Chunking** | markdown + meta sidecar | split on ATX headings w/ breadcrumb · per-kind dispatch · tables on row boundaries · prose w/ 60-char overlap · drop <40-char stubs | `list[Chunk]` → `data/chunks.jsonl` |
| 4 | **Embedding** | `list[Chunk]` | whitespace-normalise → all-MiniLM-L6-v2 → 384-dim, L2-normalised, `float32` | `ndarray (n, 384)` |
| 5 | **Store** | chunks + vectors | reset collection · upsert ids/documents/metadatas/embeddings | Chroma `mf_scheme_chunks` |
| 6 | **Guardrails** | raw question | 7 PII regexes → 14+16 intent patterns → corpus check | answer · or refusal + kind |
| 7 | **Query embed** | question | same model, same normalisation | `ndarray (384,)` |
| 8 | **Retrieval** | query vector | unfiltered top-k → optional `scheme_name` filter → similarity floor | `list[RetrievedChunk]` + `ResolvedScheme` |
| 9 | **Generation** | system prompt + numbered context blocks | grounded, temp 0, `max_tokens` 300 | raw answer text |
| 10 | **Enforcement** | raw answer + `allowed_url` | ≤3 sentences · exactly 1 URL matching metadata · last-updated present · no advice leakage | compliant answer + `contract_problems` |
| 11 | **Render** | `RAGResponse` | split body/source/date → markdown + link + evidence expander | UI turn |
| 12 | **Log** | `RAGResponse` + chunks | one JSON object per query (PII redacted) | `data/logs/queries.jsonl` |

**The two contracts that must never be broken** are the `Chunk` schema (§10.2) and the
`RAGResponse` shape (§11). Every entry point, and the eval harness, depends on them.

---

## 5. Design principles, and the constraints that forced them

| Principle | Forced by |
|---|---|
| Every stage is a separate module with its own CLI verb | The milestone requires demonstrating each RAG stage, not hiding it in one chain function |
| Retrieval-time **metadata filtering**, never post-filtering | "Expense ratio of the ELSS" must not be answerable from a Small Cap chunk |
| The citation URL is read from **chunk metadata**, never from model output | A citation must be incapable of being hallucinated |
| Return/performance data is **never ingested** | "No performance claims" is best enforced by never giving the model the material |
| Guardrails run **before** retrieval | Refusals should cost nothing and be instant in a live demo |
| Contract violations are **auto-repaired** (trim, re-stamp footer) | ≤3 sentences and 1 citation are hard requirements, not suggestions |
| 4th generator backend requires no key and no Ollama | The demo machine may have neither |
| Every query writes a full trace | The brief forbids back-end screenshots, so logs are the evidence |

---

## 6. Offline pipeline

### 6.1 Stage 1 — Loading · `ingest/loader.py`

```
sources.py  (5 allowlisted URLs — the only URLs the system will ever fetch)
    │
    ▼
fetch_all()  ·  requests.Session, browser UA, Accept-Language: en-IN
    │          3 retries · 1.5× linear backoff · 30 s timeout
    │          1.0 s politeness delay between requests
    │          resp.content.decode("utf-8")   ← explicit; ₹ (U+20B9) survives
    ▼
data/raw/{slug}.html          verbatim snapshot (gitignored, rebuildable)
data/raw/manifest.json        provenance per document
```

The manifest is the record of truth for citations:

```jsonc
{ "slug": "hdfc-large-cap-fund-direct-growth",
  "scheme_name": "HDFC Large Cap Fund - Direct Growth",
  "category": "Large Cap",
  "url": "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
  "fetched_at": "2026-09-28T09:14:22+00:00",   // → "Last updated from sources:"
  "content_hash": "3f9a…",  "raw_path": "…", "http_status": 200, "bytes": 453_831 }
```

**Failure behaviour:** per-URL failures are collected and printed; `pipeline.run`
exits non-zero. A partial corpus is a loud error, never a silent state — because a
silently empty corpus produces a chatbot that *confidently answers nothing*.

### 6.2 Stage 2 — Cleaning · `ingest/clean.py`

The highest-leverage stage, because of what these pages actually are. Measured on a
real fetch:

```
                453,831 bytes of HTML
                          │
   ┌──────────────────────┼──────────────────────────────┐
   ▼                      ▼                              ▼
__NEXT_DATA__        rendered body              4× application/ld+json
230,057 B            ~76,900 B                  6,390 B
(50.7%)              (17%)                       (incl. 4,201 B FAQPage)
   │                      │                              │
   │            nav 11% + footer 38%                    │
   │            of extracted text                       │
   ▼                      ▼                              ▼
## Key facts        ## Scheme page                ## FAQ (from published
16 label:value      h1–h5 preserved as           structured data)
lines from          markdown; <nav>, <footer>    7 Q&A pairs of long-form
mfServerSideData    <script>, <header> dropped   prose present nowhere
   │                      │                        in the rendered text
   └──────────────────────┴──────────────────────────────┘
                          ▼
        data/docs/{slug}.md  +  {slug}.meta.json
```

**The finding that dictates this design:** a naive `get_text()` reaches only **3.9%**
of page bytes, and **misses `benchmark`, `expense_ratio`, `exit_load` and `lock_in`
entirely** — those four fields exist *only* in the `__NEXT_DATA__` JSON. "Benchmark" in
particular never appears as body text; it exists solely as an `h4` heading. So:

- **A blanket "strip all `<script>`" rule would gut the corpus.** The JSON and the
  rendered body are extracted by independent passes and concatenated.
- **~49% of naive extracted text is boilerplate** (nav mega-menu + an SEO link farm of
  26 single-letter MF links). `<nav>`/`<footer>`/`<header>`/`<script>` are removed at
  the tag level, with a line-pattern filter as a second defence.
- **Tables are converted to markdown**, because the corpus is table-heavy and a fee table
  cut mid-row becomes an unlabelled fragment.

**Fields deliberately excluded** (`_EXCLUDED_KEYS` + `_FACT_FIELDS` whitelist):
`sip_return`, `simple_return`, `return_stats`, `stats`, `holdings`, `peerComparison`,
`historic_exit_loads`, `sharpe_ratio`, `beta`, `alpha`, and `nfo_risk`.

- The first group is **performance data**. The brief forbids performance claims, so the
  strongest possible guard is that the model never sees the numbers.
- `nfo_risk` is dropped for a different reason: it is a legacy NFO-era field that
  *contradicts* the live riskometer on pre-2015 funds. The current rating comes from the
  `Very High Risk` chip and the About text instead.

**Provenance sidecar:** every cleaned doc is written with a `.meta.json`. The chunker
raises if a sidecar is missing, which makes it structurally impossible for a chunk to
exist without a source URL.

### 6.3 Stage 3 — Chunking · `transform/chunker.py`

```
data/docs/{slug}.md + {slug}.meta.json
    │
    ▼
split_markdown_sections()   split on ATX headings, track breadcrumb
    │                       → Section(headers[], body, kind)
    │                       kind ∈ { facts · faq · table · prose }
    ▼
dispatch by kind ───────────────────────────────────────────────────────┐
  facts   → emit whole. Atomic key:value block; never split.          │
  table   → _table_chunks(): split on ROW boundaries only, repeat    │
           the header row on every emitted chunk.                    │
  prose   → _split_on_separator():  \n\n → \n → ". " → "; " → ", "  │
           → " " → ""                                                │
           then a 60-char sliding-window overlap                    │
  faq     → same as prose                                            │
    │                                                                 │
    ▼  <──────────────────────────────────────────────────────────────┘
drop stubs < 40 chars (facts exempt)
    │
    ▼
prefix breadcrumb:  "## Fund benchmark > #### Exit load"
    │
    ▼
Chunk(chunk_id, text, source_url, scheme_name, category, slug,
      section, fetched_at, content_hash, kind)
    │
    ├──► data/chunks.jsonl        human-inspectable, eval-friendly
    └──► Chroma                   ids / documents / metadatas / embeddings
```

**Why header-aware, not fixed-size:** headings are present in the static HTML, form a
clean shallow hierarchy (32 heading nodes/page, no JS-toggled tabs — `role="tab"`,
`aria-selected`, `aria-expanded` all return 0 matches), and are *free semantic
boundaries*. A section-aware chunk keeps "Exit load" attached to its label; a
fixed-size split reliably produces `1% if redeemed within` with no label.

**The breadcrumb is not decoration.** `all-MiniLM-L6-v2` has no cross-chunk awareness.
A chunk reading `- **Expense ratio:** 1.03%` embeds very differently from one reading
`## Key facts\n\n- **Expense ratio:** 1.03%`. The breadcrumb is the only context signal
the embedding model receives.

Full measurement, alternatives considered, and known weaknesses: **[CHUNKING.md](CHUNKING.md)**.

### 6.4 Stage 4 — Embedding · `index/embedder.py`

| Property | Value |
|---|---|
| Model | `sentence-transformers/all-MiniLM-L6-v2` (mandated by the brief) |
| Dimensions | 384 |
| Device | CPU |
| `normalize_embeddings` | `True` → cosine distance = `1 − dot`, matching Chroma's `hnsw:space: cosine` |
| Batching | 64, `float32` output |
| Model residency | `@lru_cache(maxsize=1)` — loaded once per process, not per query |
| Pre-processing | whitespace-normalised, so vectors are stable regardless of upstream HTML wrapping |

### 6.5 Stage 5 — Vector store · `index/vectorstore.py`

`chromadb.PersistentClient` at `data/chroma/`, one collection `mf_scheme_chunks`,
`hnsw:space: cosine`.

All nine metadata fields are **flat and scalar** — Chroma cannot filter on nested
structures, and every one of these is a filter or a citation input:

| Field | Consumed by |
|---|---|
| `source_url` | citation resolution — the URL in every answer |
| `scheme_name` | **the hard retrieval filter** |
| `category` | UI + eval reporting |
| `section` | breadcrumb shown in the UI's evidence expander |
| `chunk_id` | trace → chunk lookup |
| `fetched_at` | `Last updated from sources:` |
| `kind` | reserved for the per-kind top-k refinement |
| `slug`, `content_hash` | rebuild detection, provenance |

`upsert_chunks()` **resets the collection first**, so re-running the build is idempotent
and can never leave stale vectors next to fresh ones. Chroma rejects empty-string
metadata, so falsy values are filtered out before write.

---

## 7. Online pipeline

### 7.1 Stage 6 — Guardrails, deliberately first

```
question
   │
   ├─▶ [6a] guardrails/pii.screen()
   │        7 patterns: PAN · Aadhaar · email · phone · card/long-number ·
   │        account number · OTP (context-anchored, so "3 years" isn't an OTP)
   │        hit ──▶ refuse · log "[withheld: PII detected]" · raw text never persisted
   │
   ├─▶ [6b] guardrails/intent.classify()
   │        out-of-corpus scheme → advice (16 patterns) → performance (14 patterns)
   │        refuse ──▶ polite redirect + 1 in-corpus citation + educational links
   ▼
```

**Why before retrieval:** a refusal short-circuits here, so the embedding model is never
loaded and no tokens are spent. Four of the twenty eval cases are refusals; each
completes in single-digit milliseconds. A "Should I buy the Small Cap Fund?" answered
instantly with a clear redirect is a *better demo moment* than a 4-second answer that
hedges.

**One deliberate exception.** `PERFORMANCE_PATTERNS` includes `\breturns?\b`, which also
matches the legitimate FAQ chunk *"What kind of returns does this fund provide?"*. A
narrow `_FACT_OVERRIDE` escape hatch lets a question that clearly asks for a static fact
(say, expense ratio) through even if it also mentions returns. This is the single most
likely place to need tuning against the eval set.

### 7.2 Stages 7–8 — Query embedding and retrieval · `rag/retriever.py`

```
        ┌─ PASS 1: unfiltered ─────────────────────────────┐
q ──▶   │  embed_query → chroma top-k (where=None)         │──┐
        └──────────────────────────────────────────────────┘  │
                                                                  │
        ┌─ PASS 2: only if resolve_scheme(q).confident ────┐  │
        │  chroma top-k with where={"scheme_name": …}       │◀─┘  chosen
        └──────────────────────────────────────────────────┘
                                  ▼
                  drop chunks with score < MIN_SIMILARITY (0.15)
                  keep top_k = 5
```

`resolve_scheme()` scores the question against scheme names **and aliases**
(`elss`, `tax saver`, `80c`, `flexi cap`, `balanced advantage`, …) while excluding
generic tokens (`fund`, `direct`, `growth`, `plan`) that would otherwise match
everything. Confidence ≥ 0.82 triggers the filtered pass.

Two design points:

- **The filter goes *into* Chroma, not onto the result set.** This is what makes a
  cross-scheme answer structurally impossible rather than merely unlikely — there is no
  code path where an unfiltered result reaches the generator.
- **Pass 1 exists so process questions still work.** "How do I download a capital gains
  statement?" is not scheme-scoped, so it must not be filtered to one scheme.

**Fallback ladder** (never widens scope silently):
filtered → unfiltered-filtered-by-scheme → `no_context` refusal.

### 7.3 Stage 9 — Generation · `rag/prompts.py` + `rag/generator.py`

`SYSTEM_PROMPT` encodes six absolute rules: context-only; no advice; no return/CAGR/
Sharpe/alpha figures; ≤3 sentences; verbatim figures with units; and a required two-line
footer.

**Context blocks are individually attributed:**

```
[SOURCE 1] HDFC Large Cap Fund - Direct Growth - Large Cap
Section: Key facts
URL: https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth
Retrieved on: 2026-09-28
CONTENT:
## Key facts
- **Expense ratio:** 1.03%
- **Exit load:** Exit load of 1% if redeemed within 1 year
```

Numbering lets the prompt say *cite the block you used*; attribution lets a human audit
which passage produced the answer.

**Retrieved text is labelled untrusted reference data**, so a prompt-injection attempt
planted in a scraped page is treated as content, not instructions.

| Backend | Class | Model | Requires | Priority |
|---|---|---|---|---|
| cloud | `OpenAIGenerator` | `gpt-4o-mini` | `OPENAI_API_KEY` | 1 |
| cloud | `GeminiGenerator` | `gemini-1.5-flash` | `GOOGLE_API_KEY` | 2 |
| local | `OllamaGenerator` | `llama3.1` | Ollama on :11434 | 3 |
| none | `StubGenerator` | extractive | nothing | 4 |

`get_generator()` degrades on any import or connection failure and prints which backend
it fell back to. **`StubGenerator` is a real baseline, not a placeholder:** it parses the
numbered blocks, ranks sentences by lexical overlap with the question, and emits the top
3 with a compliant footer. It keeps the demo and the eval harness runnable on a bare
machine, and gives a floor to measure the LLM backends against.

All backends: `temperature = 0.0`, `max_tokens = 300`.

### 7.4 Stage 10 — Contract enforcement · `guardrails/output.py`

The PRD's hard requirements made executable. Prompting alone is not enforcement.

| Constraint | Check | On violation |
|---|---|---|
| Exactly 1 citation | count URLs in the answer | flagged `no citation` / `N citations` |
| Citation is real | URL **== top retrieved chunk's `source_url`** | flagged; footer re-stamped from metadata |
| ≤3 sentences | `count_sentences()` (ignores footer lines) | `trim_to_sentences()` physically truncates |
| `Last updated from sources:` present | regex | re-appended by the chain |
| No advice leakage | scan for `you should`, `we recommend`, `consider investing`, … | flagged `advice language detected` |

Violations are recorded in `contract_problems` and rendered in the UI as a caption — so
the evaluator sees the guardrail *fire* rather than taking it on trust.

### 7.5 Stages 11–12 — Render and log

`app/streamlit_app.py` splits the answer into body / citation / date and renders the
citation as a clickable link, with the retrieved passages behind an `st.expander`
labelled as RAG evidence. The sidebar carries the scope, the 5 source links, the index
size, and the full disclaimer.

`rag/chain.py:_log()` appends one JSON object per query to `data/logs/queries.jsonl`:

```jsonc
{ "timestamp": "…", "question": "…",
  "detected_scheme": "HDFC Large Cap Fund - Direct Growth",
  "refused": false, "refusal_kind": null,
  "retrieved_chunk_ids": ["a3f1c9e2b7d04a15"],
  "retrieved": [{ "section": "Key facts", "score": 0.61 }],
  "top_score": 0.61, "answer": "…", "cited_url": "https://groww.in/…",
  "contract_problems": [], "latency_ms": 1840,
  "generator_model": "openai/gpt-4o-mini",
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
  "index_chunks": 214 }
```

**This is the substitute for back-end screenshots**, which the brief forbids: a complete,
inspectable audit trail that retrieval really ran.

---

## 8. Product constraints → enforcement points

| Product constraint | Enforced at | Mechanism | Failure is impossible? |
|---|---|---|---|
| **Public sources only** | `sources.py` (allowlist) + `ingest/loader.py` | Only the 5 hardcoded URLs are ever fetched; no search, no crawling | Yes — no other URL is reachable |
| **No third-party blogs** | `ingest/clean.py` | Corpus is built only from the allowlist; a blog URL cannot enter the index | Yes |
| **No performance claims** | `ingest/clean.py` (exclusion) **+** `guardrails/intent.py` (14 patterns) **+** system prompt rule 3 | Data never ingested; question refused; output scanned | Belt-and-braces at 3 layers |
| **No investment advice** | `guardrails/intent.py` (16 patterns) + system prompt rule 2 + `output.py` leakage scan | Refused pre-retrieval; output re-scanned | 3 layers |
| **No PII accepted or stored** | `guardrails/pii.py` | 7 regex patterns, screened pre-retrieval; raw input never written to the log | Yes — nothing downstream runs |
| **≤3 sentences** | `guardrails/output.py:trim_to_sentences` | Post-generation physical truncation | Yes |
| **Exactly 1 citation** | `rag/prompts.py` + `guardrails/output.py:enforce` | URL taken from chunk metadata and cross-checked; footer re-stamped if wrong | Yes — model cannot invent a URL |
| **`Last updated from sources:`** | `rag/chain.py:_footer` | Stamped from the chunk's `fetched_at` | Yes |
| **Cite the right scheme** | `rag/retriever.py` | Hard `scheme_name` filter passed into Chroma | Yes — filtered results are the only results |
| **Polite refusal, not a lecture** | `guardrails/intent.py` copy + `docs/DISCLAIMER.md` | Fixed, reviewed strings; refusals cite an official educational link | Yes |
| **Tiny UI** | `app/streamlit_app.py` | Welcome + 3 chips + disclaimer + transcript. No sidebar history, settings, or auth | — |
| **Clarify when not covered** | `rag/chain.py` `no_context` path | Falls back to an honest "couldn't find that in my sources" | Yes |
| **No back-end screenshots** | `rag/chain.py:_log` | JSONL traces substitute for screenshots | — |

---

## 9. Deep dive: the three hardest constraints

### 9.1 "No performance claims"

The tempting implementation is a post-hoc filter. The correct one is three layers,
because a post-hoc filter cannot catch a number the model *paraphrases*:

1. **Never ingest returns.** `mfServerSideData` carries `sip_return` (60+ fields),
   `simple_return`, `return_stats`, `sharpe_ratio`, `beta`, `alpha`, `rank3yr`, plus a
   51-row returns table and a 7-row peer-comparison table in the body. All excluded. The
   model is never placed in a position where it could quote a return.
2. **Refuse the question.** 14 patterns: `returns?`, `performance`, `cagr`, `ranking`,
   `how good is`, `worth investing`, `sip calculat`, `projected value`, …
3. **Scan the output.** `output.py` flags advice-flavoured leakage; the prompt restates
   the rule.

A return question is therefore refused with a pointer to the official factsheet — which
is precisely what the brief asks for.

### 9.2 "Every answer must include one source link"

The link is **never** a model output. It is `retrieved[0].source_url`, read from Chroma
metadata. The model is asked to echo it, `output.enforce` checks that whatever it echoed
matches, and if it does not, `_footer()` overwrites it. A citation can only ever point at
a page we actually ingested — a hallucinated URL is not merely discouraged, it is
unreachable by construction.

### 9.3 "No PII"

Screening happens at stage 6a, *before* embedding, retrieval, generation, and logging.
On a pattern hit the query text is replaced with `[withheld: PII detected]` in the trace,
so the identifier never reaches disk in any form. `redact()` exists as a second-line
defence if a PII-tripping line ever must be written. The OTP pattern is context-anchored
(`otp is 1234`, `verification code 1234`) precisely so that the string `3 years` in a
lock-in answer is not mistaken for a 4-digit code.

---

## 10. Data contracts

### 10.1 Corpus document — `data/docs/{slug}.md`

```markdown
# HDFC Large Cap Fund - Direct Growth
**AMC:** HDFC Mutual Fund
**Category:** Large Cap
**Plan:** Direct Growth
**Source URL:** https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth
**Facts last updated from sources:** 2026-09-28

## Key facts
- **Category:** Equity · Large Cap
- **Benchmark:** NIFTY 100 TRI
- **Base expense ratio:** 0.84%
- **Expense ratio:** 1.03%
- **Exit load:** Exit load of 1% if redeemed within 1 year
- **Stamp duty on investment:** 0.005% (from July 1st, 2020)
- **Minimum SIP investment:** ₹100.00
- **Minimum lumpsum investment:** ₹100.00
- **Lock-in period:** None
- **AUM:** ₹39,933.37 Cr
- **NAV:** ₹1,189.08
…

## FAQ (from published structured data)
### How much expense ratio is charged by HDFC Large Cap Fund Direct Growth?
…

## Scheme page (verbatim sections)
### Exit Load
#### Exit load
…
```

The `**Label:**` frontmatter is stripped from the text that gets embedded
(`_body_for_embedding`) but retained in the sidecar, so the embedding does not spend
tokens on provenance it will never be asked about.

### 10.2 Chunk

```jsonc
{ "chunk_id": "a3f1c9e2b7d04a15",              // sha256(url|section|text)[:16]
  "text": "## Key facts\n\n- **Expense ratio:** 1.03%",
  "source_url": "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
  "scheme_name": "HDFC Large Cap Fund - Direct Growth",
  "category": "Large Cap",  "slug": "hdfc-large-cap-fund-direct-growth",
  "section": "Key facts",   "fetched_at": "2026-09-28T09:14:22+00:00",
  "content_hash": "…",      "kind": "facts" }
```

### 10.3 Invariants any new source or backend must preserve

1. Every `Chunk` carries `source_url`, `scheme_name`, and `fetched_at`.
2. No performance data enters the corpus.
3. The citation URL is read from metadata, never from model output.
4. `kind ∈ {facts, faq, table, prose}` — the per-kind top-k refinement depends on it.

---

## 11. Response type matrix

Every path produces a response. A refusal still carries exactly one citation, so the
"every answer has a source" rule holds without the model inventing a URL.

| Path | Trigger | `refused` | `refusal_kind` | Citation |
|---|---|---|---|---|
| Answer | supported factual question | `false` | `null` | `retrieved[0].source_url` |
| PII | `pii.screen` hit | `true` | `pii` | SEBI investor-education URL |
| Advice | intent advice hit | `true` | `advice` | first in-corpus page + 3 educational links |
| Performance | intent performance hit | `true` | `performance` | first in-corpus page + 3 educational links |
| Out of corpus | scheme not in the 5 | `true` | `out_of_corpus` | first in-corpus page + 3 educational links |
| No context | nothing above the similarity floor | `true` | `no_context` | first in-corpus page + 3 educational links |

---

## 12. Tuning knobs

All in `config.py`. The eval loop is: edit → `run.py build` → `run.py eval` → record.

| Knob | Default | Raise / lower when |
|---|---|---|
| `CHUNK_SIZE` | 400 | Sections are being cut in half → raise. Retrieval feels noisy → lower |
| `CHUNK_OVERLAP` | 60 | Prose answers are truncated mid-thought → raise. Recall too broad → lower |
| `TOP_K` | 5 | `Key facts` chunks are being crowded out of context → lower to 3 |
| `MIN_SIMILARITY` | 0.15 | Irrelevant chunks reach the prompt → raise toward 0.3 |
| `SCHEME_FILTER_THRESHOLD` | 0.82 | Ambiguous questions filter to the wrong scheme → lower |
| `TEMPERATURE` | 0.0 | **Do not raise.** Non-determinism buys nothing here |
| `MAX_SENTENCES` | 3 | Product constraint. Do not change |

Highest-value next refinement: a **`kind == "facts"` boost** for fee/lock-in questions,
which are the highest-frequency class and always live in a single chunk.

---

## 13. Performance budget

| Stage | Budget | Notes |
|---|---|---|
| Embedding model load | 2–4 s, **once per process** | `@lru_cache`; not per query |
| Guardrails (6a + 6b) | < 1 ms | regex + phrase banks; a refusal ends here |
| Query embed (7) | 5–20 ms | 384-dim, CPU |
| Chroma query (8) | 1–5 ms | in-process `PersistentClient` |
| Generation (9) | 0.8–6 s | backend-dependent; `max_tokens=300` bounds the tail |
| **Total, answering path** | **< 8 s p95** | NFR-1 |
| **Total, refusal path** | **< 20 ms** | never reaches embedding |

**Cold start.** `run.py ingest` + `run.py build` target < 5 min on a laptop, but the
first `pip install` dominates — `torch` and `sentence-transformers` are ~2 GB. Record
that in the demo plan: install dependencies before the class, not during it.

**Where the 8 s goes, in the common case:** generation is 85–95% of it. Retrieval,
embedding and guardrails together are typically under 30 ms. This is why the refusal
path being instant is a genuine architectural win rather than a micro-optimisation.

**Scaling reality.** The design is deliberately demo-shaped. 5 documents → ~200–300
chunks → one in-process Chroma instance is exactly right. At corpus scale the changes
would be: a hosted vector store, a cross-encoder rerank between stages 8 and 9, and
batched embedding. None of those touch the contract in §4.

---

## 14. Failure modes

| Failure | Handling | User-visible effect |
|---|---|---|
| Fetch fails | 3 retries, per-URL record, non-zero exit | build-time error naming the URL |
| HTML/`__NEXT_DATA__` structure changes | `clean_html` raises if nothing extractable | loud failure, never a silent empty corpus |
| `.meta.json` missing | `chunk_docs_dir` raises | build refuses to run |
| Filtered retrieval empty | unfiltered → scheme-filtered → `no_context` | honest "couldn't find that" |
| Model exceeds 3 sentences | `trim_to_sentences` truncates | capped answer + contract note in UI |
| Model cites a wrong URL | `enforce` flags, footer re-stamped | correct citation regardless |
| No key, no Ollama | falls back to `StubGenerator` | demo runs, extractive answers |
| PII typed live | rejected at 6a | refusal; raw text absent from disk |
| Newline/encoding corruption | explicit `utf-8` decode on fetch, read, and write | ₹ renders correctly |

---

## 15. Extension points

Ranked by value per unit of effort. Each notes what it would invalidate.

| # | Extension | Where | Effort | Would invalidate |
|---|---|---|---|---|
| 1 | **Add schemes or a second AMC** | `sources.py` only | very low | nothing — every stage is data-driven off the source list |
| 2 | **Per-kind top-k** (`facts` 2, `prose` 4) | `rag/retriever.py:retrieve` | low | nothing — `kind` is already in metadata |
| 3 | **`kind == "facts"` boost** for fee/lock-in questions | `rag/retriever.py` | low | nothing |
| 4 | **Conversation memory** | add to `rag/chain.py:ask`; UI already holds `st.session_state` | low | the "one question, one answer" framing in §11 |
| 5 | **New LLM backend** | subclass `Generator`, add a branch in `get_generator` | low | nothing |
| 6 | **Cross-encoder rerank** between 8 and 9 | new module + one call site in `chain.py` | medium | latency budget in §13 |
| 7 | **Official PDFs as corpus (v2)** | new `ingest/pdf_loader.py`; reuse the `Chunk` contract | medium | the broker-aggregated source note in README |
| 8 | **LLM-based refusal classifier** | replace `guardrails/intent.py:classify` | medium | nothing, but the reason for the escape hatch in §7.1 changes |
| 9 | **FastAPI + React UI** | new `app/` adapter calling `rag.chain.ask` | medium | nothing |

### 15.1 The v2 corpus problem, stated honestly

The strongest critique of this design is the source choice: the brief asked for
AMC/SEBI/AMFI pages and we use groww.in. Moving to official PDFs is the right v2 and is
mostly a loader problem, **not** a pipeline problem — because §4's contract stops at
`list[Chunk]`, and a PDF loader would satisfy it.

The work is real, though: PDF text extraction is noisier than a clean HTML template,
fee tables lose their structure, and a 5-scheme corpus becomes a much larger set of
documents. The chunker in §6.3 is heading-aware, and PDF headings are a different and
less reliable signal — that is the piece most likely to need rework.

### 15.2 What to add before calling this production

Outside the milestone, in priority order:

1. Real refusal classifier, so "would you personally put money in this?" is caught by
   meaning rather than by phrase bank.
2. Freshness policy. Right now the corpus is a snapshot and re-ingest is manual. A
   published figure that has changed upstream is still answered from the stale snapshot.
3. Multi-turn scope locking, so a follow-up cannot silently widen the scheme scope.
4. Automated re-ingest with a diff report on what changed.
5. Retrieval eval at corpus scale — 20 questions tells you little about 200 documents.

---

## 16. PRD traceability

| PRD ref | Requirement | Component | Stage | §|
|---|---|---|---|---|
| FR-1 | Corpus ingestion | `ingest/loader.py`, `ingest/pipeline.py` | 1 | 6.1 |
| FR-2 | Cleaning & normalization | `ingest/clean.py` | 2 | 6.2 |
| FR-3 | Data-driven chunking | `transform/chunker.py` | 3 | 6.3, CHUNKING.md |
| FR-4 | Embedding (all-MiniLM-L6-v2) | `index/embedder.py` | 4 | 6.4 |
| FR-5 | Vector store (ChromaDB) | `index/vectorstore.py` | 5 | 6.5 |
| FR-6 | Retrieval | `rag/retriever.py` | 8 | 7.2 |
| FR-7 | Grounded generation | `rag/prompts.py`, `rag/generator.py` | 9 | 7.3 |
| FR-8 | Refusal & scope guardrail | `guardrails/intent.py` | 6b | 7.1, 8 |
| FR-9 | PII guard | `guardrails/pii.py` | 6a | 7.1, 9.3 |
| FR-10 | Minimal UI | `app/streamlit_app.py` | 11 | 7.5 |
| FR-11 | Eval set & quality gate | `eval/run_eval.py` | — | 12 |
| FR-12 | Observability | `rag/chain.py:_log` | 12 | 7.5 |
| NFR-1 | p95 latency < 8 s | budget, not a code path | — | 13 |
| NFR-3 | ≤3 sentences, enforced | `output.trim_to_sentences` | 10 | 7.4 |
| NFR-4 | Citation integrity | `output.enforce` | 10 | 7.4, 9.2 |
| NFR-5 | Determinism | `TEMPERATURE = 0.0` | 9 | 12 |
| NFR-6 | Reproducibility | `run.py` verbs, `data/raw/` snapshots | — | 6.1 |
| NFR-8 | PII safety | screened pre-retrieval, never logged | 6a | 9.3 |
| D1 | Prototype | `app/streamlit_app.py` | — | 7.5 |
| D2 | Source list CSV/MD | `tools/export_sources.py` | — | 10.1 |
| D4 | Sample Q&A | `run.py ask` | — | 11 |
| D5 | Disclaimer snippet | `config.DISCLAIMER_*`, DISCLAIMER.md | 11 | 7.5 |
| D6 | Eval results | `data/eval_results.json` | — | 12 |
| D7 | RAG evidence (no screenshots) | `data/logs/queries.jsonl` | 12 | 7.5 |
| — | Public sources only, no blogs | `sources.py` allowlist | 1 | 2, 8 |

---

## 17. Verification status

| Check | Status |
|---|---|
| PRD written | done — [../PRD.md](../PRD.md) |
| Full scaffold written | done — 24 Python modules, 3 docs |
| `run.py ingest` | **not run** — no Python runtime on this machine |
| `run.py build` | **not run** |
| `run.py eval` | **not run** — measured metrics still blank in README |
| `run.py app` | **not run** |

Likely first-run fixes (unverifiable without execution):

1. `transform/chunker.py:_merge()` has a redundant ternary — both branches produce
   `f"{current}{piece}"`. Harmless, but simplify.
2. `guardrails/intent.py:detect_out_of_corpus` has a second guard over multi-token
   scheme names that may over-match. Eval case 19 (`Parag Parag Flexi Cap Fund`) is the test.
3. `PERFORMANCE_PATTERNS`' `\breturns?\b` will also match the legitimate FAQ chunk
   *"What kind of returns does this fund provide?"* — the `_FACT_OVERRIDE` escape hatch
   mitigates it but needs tuning.
4. `rag/chain.py:_refusal_response` derives `date` from a redundant
   `SOURCES[0].url and …` expression. Works; simplify.
5. `vectorstore.upsert_chunks` filters falsy metadata, so a chunk with an empty
   `fetched_at` would silently lose that field rather than erroring.
6. Eval case 12 ("What is the NAV…") intentionally asserts only retrieval and citation —
   the NAV figure changes daily and is not asserted.
