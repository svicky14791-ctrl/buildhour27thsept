# MF Facts — HDFC Scheme FAQ

A grounded RAG FAQ assistant for **5 HDFC Mutual Fund schemes**. It answers
factual questions from five fixed public pages, cites one link, and refuses
everything else.

**Facts only. No investment advice.**

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

.\.venv\Scripts\python.exe src\ingest\load.py     # fetch the 5 pages
.\.venv\Scripts\python.exe src\ingest\load.py --offline   # replay data/raw, keep dates
.\.venv\Scripts\python.exe src\ingest\chunk.py    # -> data/chunks.jsonl (166)
.\.venv\Scripts\python.exe src\embed.py          # -> data/chroma (166 vectors)

.\.venv\Scripts\python.exe src\guardrails.py      # 53/53 self-tests
.\.venv\Scripts\python.exe tools\test_ui.py       # 47/47 headless UI tests
.\.venv\Scripts\python.exe src\verify.py          # 22 cases -> data/eval_results.json

.\.venv\Scripts\streamlit.exe run src\app.py      # the UI
```

Three suites, deliberately covering different things:

| Suite | Covers | Backend |
|---|---|---|
| `src/guardrails.py` | refusal ladder, answer contract, PII, performance | none |
| `tools/test_ui.py` | rendering, attribution, citation honesty | `stub` |
| `src/verify.py` | live answer quality end to end | real model |

`tools/test_ui.py` drives `src/app.py` through Streamlit's own `AppTest` harness,
so it needs no browser and no server. It pins `GENERATOR_BACKEND=stub` to stay
offline and deterministic, which is what makes assertions like "both co-managers
named" stable. It covers the wiring unit tests cannot see: that a refusal renders
as a refusal instead of an empty bubble, and that a citation is only ever attached
to an answer that has grounding. Exits non-zero on failure, so it can gate a build.

Use `load.py --offline` for any cleaning or formatting change. Plain `load.py`
refetches and stamps `fetched_at` with the current time, so re-running it to pick
up a parser fix silently re-dates the whole corpus; `--offline` replays the cached
snapshots in `data/raw/` and reuses the recorded date.

The chain also runs standalone, with one printed example per behaviour:

```powershell
.\.venv\Scripts\python.exe src\chain.py
```

## Deploy

Render, via the Blueprint in `render.yaml`:

| | |
|---|---|
| Build | `pip install --no-cache-dir -r requirements.txt` |
| Start | `streamlit run src/app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true --browser.gatherUsageStats false` |
| Health | `/_stcore/health` |

Steps: push the repo to GitHub, create a Blueprint from it in the Render
dashboard, and set `GROQ_API_KEY` when prompted (`sync: false` keeps it out of
the repo). That key is the only secret the app needs on a remote instance.

Three things make this deployable without a build-time crawl:

- `data/chroma/` and `data/docs/` are **committed**, not ignored. A deploy serves
  from a fresh clone, so the index and the `*.meta.json` files that carry the
  corpus date are what let it boot with a working index and no network.
- `.streamlit/config.toml` sets `headless` and binds `0.0.0.0`; the port comes
  from `$PORT` via the start command, not the config file.
- Query logging degrades gracefully. `data/logs/` stays ignored and the
  container filesystem is ephemeral, so logs are lost on restart by design.

The trade-off of shipping a committed index: **the deployed app answers as of
the committed corpus date** and says so in every answer footer. To refresh, run
the pipeline locally and redeploy:

```powershell
.\.venv\Scripts\python.exe src\ingest\load.py     # refetch, re-stamps fetched_at
.\.venv\Scripts\python.exe src\ingest\chunk.py
.\.venv\Scripts\python.exe src\embed.py
git add data/chroma data/docs && git commit -m "Refresh corpus"
```

Embeddings run through `fastembed` on ONNX Runtime rather than torch, so there
is no multi-gigabyte wheel to install. A cold build is minutes, and a cold start
pays roughly 5 s to load the model, after which it stays cached in the instance.

Set `RAG_PERF=1` to get per-stage timings in the logs:

```
[perf] embed 1 text(s): 25 ms
[perf] retrieve: total: 103 ms
[perf] groq: groq/openai/gpt-oss-120b: 1192 ms
```

If the free tier's ephemeral disk drops `data/chroma/`, the app rebuilds it from
the tracked `data/docs/` pages on first use rather than failing every question
with a `no_context` refusal.

## What it does

| Ask | Result |
|---|---|
| "What is the expense ratio of the HDFC Large Cap Fund?" | Answered, cited, dated |
| "What is the lock-in period for the HDFC ELSS Tax Saver Fund?" | Answered — 3 year |
| "Should I buy the HDFC Small Cap Fund?" | Refused — advice |
| "Which of these five funds has the best returns?" | Refused — performance |
| "How do I download a capital gains statement?" | Refused — not in corpus |
| "What is the expense ratio of the Parag Parag Flexi Cap Fund?" | Refused — out of corpus |
| "My PAN is ABCDE1234F, can you tell me the NAV?" | Refused — PII, not logged |

## Architecture

```
Groww page -> load.py -> chunk.py -> embed.py -> ChromaDB
                                                 |
question -> guardrails (PII, intent) -> retrieve.py -> generate.py -> contract check -> log
```

| Module | Role |
|---|---|
| `src/ingest/load.py` | Strict allowlisted fetch, cleaning, performance removal |
| `src/ingest/chunk.py` | Section-aware chunking to the frozen `Chunk` |
| `src/embed.py` | `all-MiniLM-L6-v2` embeddings into `hdfc_mf_facts` |
| `src/guardrails.py` | PII screen, intent classify, sentence trim, contract check |
| `src/retrieve.py` | Scheme resolution, filtered search, similarity cutoff |
| `src/generate.py` | LiteLLM, or a deterministic extractive stub with no API key |
| `src/chain.py` | Orchestration and JSONL logging |
| `src/app.py` | Streamlit UI |
| `src/verify.py` | End-to-end evaluation |

Design detail: [architecture.md](architecture.md). Chunking detail:
[docs/CHUNKING.md](docs/CHUNKING.md). Full disclaimer:
[docs/DISCLAIMER.md](docs/DISCLAIMER.md). Sample output:
[docs/SAMPLE_QA.md](docs/SAMPLE_QA.md). Sources: [sources.md](sources.md).

## Results

20 labelled cases, from `python src/verify.py`:

| Metric | Value | Target |
|---|---|---|
| Pass rate | 100% (20/20) | — |
| Answer rate on in-scope | 100% | ≥ 90% |
| Top-k retrieval hit | 100% | ≥ 90% |
| Refusal recall | 100% | 100% |
| Refusal precision | 100% | — |
| Single allowlisted citation | 100% | 100% |
| PII rejection | 100% | 100% |
| Latency p50 / p95 / max | 30 ms / 173 ms / 238 ms | p95 < 8 s |
| Index size | 166 chunks | — |

The embedding model takes ~26 s to load once per process; `verify.py` warms it
before timing, so per-query latency measures retrieval and generation only. p50
is stable around 30 ms; p95 moves between runs because a couple of queries pay an
extra embedding call, so treat the p95 as order-of-magnitude, not a tight bound.

**No API key is required.** With none set, `generate.py` uses an extractive stub
that returns source sentences verbatim and cannot invent anything. This checkout is
configured for **Groq**: put `GROQ_API_KEY` in `.env` and the generator resolves to
Groq automatically, or force it with `GENERATOR_BACKEND=groq`. The default model is
`groq/openai/gpt-oss-120b`; override with `GROQ_MODEL`. `llama-3.3-70b-versatile`
is decommissioned on Groq and now 404s — other current models are
`qwen/qwen3.8-27b` and `allam-2-7b`. OpenAI, Gemini and Ollama remain reachable the
same way as before (`OPENAI_API_KEY`, `GOOGLE_API_KEY`, `OLLAMA_MODEL`); there is
no automatic network probe for Ollama, so you must set `OLLAMA_MODEL` to opt in.
`.env` is read by `load_dotenv()` inside `get_generator()`. `.env.example` documents
exactly the variables the code reads, and `python src/generate.py` asserts that
contract in both directions, so a name can no longer drift (it caught
`GEMINI_MODEL` in the example where the code reads `GOOGLE_MODEL`, and
`OLLAMA_BASE_URL` in the example where no code path read it at all).

If a configured backend fails at request time — expired key, unreachable daemon —
`chain.ask()` does not crash. It logs the failure in `contract_problems` and
answers from the stub instead, so the UI degrades rather than showing a
traceback. Watch the `model:` line in `tools/probe.py` output to confirm which
backend actually served an answer. Backend resolution is silent by default because
it runs on every question; set `GENERATOR_VERBOSE=1` to get the
`Generator: ...` line back.

## Design decisions

**The corpus is 5 URLs and nothing else.** The five Groww scheme pages came from
the brief. When a question needs a page that is not on the list — a statement
download guide, the SEBI riskometer — the correct answer is a refusal, not the
nearest allowed fact. Adding URLs was considered and declined; `implementation.md`
records the reasoning.

**Performance data is removed at ingest, not at prompt time.** Return, ranking
and riskometer content is dropped from structured JSON, rendered body and FAQ
answers alike, then grepped to confirm. The model never sees it, so no prompt can
leak it.

**One question, one log line.** `chain.ask()` returns a frozen `RAGResponse` and
appends one JSON object to `data/logs/queries.jsonl`, PII-redacted.

**Refusals are first-class.** Advice, performance, out-of-corpus and unsupported
questions each have their own copy and their own `refusal_kind`. There is no
single generic "I can't help" catch-all.

**Fund managers are answered, with a staleness caveat.** Manager names and start
dates are indexed as facts (`Fund Manager`, `Fund Manager Since`) and a
manager question gets a deterministic "confirm on the AMC/AMFI factsheet" line
appended to the answer body. This was previously a hard refusal. Two things make
it safe enough to answer rather than refuse:

- The corpus carries **all** co-managers. Four of the five schemes are co-managed
  (Large Cap and Small Cap have 2, Balanced Advantage has 6), so a single-name
  answer would be wrong. The prompt requires reproducing every one.
- Tenure is stored as an absolute **"managing since" date**, never a duration. A
  duration computed at build time rots silently; a date stays true.

The residual risk is inherent, not fixable in code: a person can change role at
any time, and this is a point-in-time snapshot.

**The UI is minimal on purpose.** Welcome line, 3 example buttons, transcript, a
sidebar listing the 5 sources and the full disclaimer, and a RAG-evidence
expander per answer. No history sidebar, settings, auth or theming.

## Known limitations

1. **Broker pages, not official documents.** The corpus is public Groww pages, not
   HDFC AMC, SEBI or AMFI. Confirm any figure that matters against the official
   factsheet.
2. **No statements or tax documents.** Download guidance lives on non-allowlisted
   Groww help pages.
3. **No SEBI riskometer.** The pages carry `Groww Rating`, a different scale. It is
   never presented as the riskometer.
4. **Direct Growth only.** Other plan variants are out of scope.
5. **Point-in-time figures.** Every answer shows the source fetch date.
6. **English only.**
7. **The extractive stub is verbatim, not fluent.** It quotes source sentences
   rather than paraphrasing them. Real generation is smoother but riskier.
8. **No evaluation on a real LLM.** The 20 cases run against the stub. They verify
   retrieval, routing, citations and refusals — not generated-text quality. A real
   backend opens a fourth failure mode: a fabricated number matching no chunk.
   `enforce_answer_contract` catches advice/performance leakage, URL violations and
   length, but cannot catch a wrong figure.
9. **Overlap still duplicates text, by design.** The 60-char overlap is snapped to a
   word boundary, so no chunk opens mid-word or on orphaned punctuation, but 13 of
   166 chunks still contain a repeated 6-word run — almost all of them the scheme
   name appearing in both the breadcrumb and the body. A real LLM sees raw chunk
   text and could echo a phrase twice. The three boundary defects that used to
   corrupt chunk starts are fixed; see [docs/CHUNKING.md](docs/CHUNKING.md).
10. **A model can still write its own citation line.** The system prompt asks for
    one, and a real LLM complies — sometimes wrongly. `gpt-oss-120b` truncated the
    URL mid-string and dropped the date, which put two conflicting `Source:` lines
    in an answer and failed the single-citation contract. `strip_model_footer()`
    now discards anything the model wrote in that position and appends the
    code-built footer, so the citation always comes from chunk metadata.
11. **Retrieval is lexical-plus-embedding, no reranking.** `MIN_SIMILARITY` is tuned
    on a small sample, so the 0.25 cutoff is measured, not validated at scale. An
    attribute gate in `retrieve.py` rejects chunks that do not carry the attribute
    a question names, but it matches on labels, not meaning.
11. **Freshness is manual.** `load.py` refetches on demand; nothing re-runs nightly,
    so `Last updated` can drift as the real pages change.
12. **No multi-turn memory.** Each question stands alone.
13. **Logs are plain JSONL** in `data/logs/`, unencrypted and unrotated.
14. **The UI is local and single-user.** `st.session_state` only; no persistence,
    no auth.
15. **Not SEBI-registered advice.** This is a demonstration assistant and is not a
    registered investment adviser.

## Development tools

`tools/probe.py` traces one question end to end without the UI:

```powershell
.\.venv\Scripts\python.exe tools\probe.py "exit load of the ELSS fund"   # retrieval only
.\.venv\Scripts\python.exe tools\probe.py -g "who manages the flexi cap fund?"  # + generation
.\.venv\Scripts\python.exe tools\probe.py -g -s "what is the NAV?"      # + source documents
.\.venv\Scripts\python.exe tools\probe.py -g -f "minimum sip"            # + full chunk text
```

`-g` runs the generator only when the question would actually be answered. A PII
hit, an advice/performance/unsupported intent, or a retrieval that clears
nothing prints the refusal copy and stops — the same decision `chain.ask()` makes,
so a probe can never show an answer the product would refuse. `src/guardrails.py`
asserts that parity on 6 inputs so the two ladders cannot drift.

`tools/audit_chunking.py` is read-only: it reports chunk inventory, whether every
document section survived chunking, fact-cell coverage, overlap artifacts, and
fact chunks that carry no value.

## Layout

```
data/docs/        5 fetched pages + .meta.json sidecars      (committed)
data/chunks.jsonl 166 chunks                                 (ignored, build-time)
data/chroma/      persistent Chroma index                    (committed)
data/logs/        queries.jsonl, PII-redacted                (ignored, runtime)
data/eval_results.json
src/              ingest/ + the pipeline above
tools/            probe.py, audit_chunking.py, test_ui.py
docs/             CHUNKING, DISCLAIMER, SAMPLE_QA
render.yaml       Render Blueprint: build + start commands
.streamlit/       config.toml: headless, 0.0.0.0 bind
sources.csv, sources.md
PRD.md, implementation.md, architecture.md, Problemstatement.txt
```

Legacy scaffold was removed after a verified backup; see
[docs/DISCLAIMER.md](docs/DISCLAIMER.md) for the accuracy limits it now covers.
