"""The single entry point. ask(question) -> RAGResponse.

Stage order is the contract. Guards run before retrieval, and the citation is
assembled from retrieved metadata in code, never taken from a model:

    trim -> screen_pii -> classify_intent -> retrieve -> generate
         -> enforce_answer_contract -> footer -> log

ask() never raises. Every path, including every refusal, returns a RAGResponse
carrying exactly one citation URL and a Last-updated line, so the output contract
holds for refusals too and the UI never has to special-case them.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.generate import (  # noqa: E402
    SYSTEM_PROMPT,
    Generator,
    StubGenerator,
    build_prompt,
    get_generator,
)
from src.guardrails import (  # noqa: E402
    LAST_UPDATED_PREFIX,
    FUND_MANAGER_CAVEAT,
    asks_about_fund_manager,
    classify_intent,
    enforce_answer_contract,
    refusal_for,
    screen_pii,
    split_answer_footer,
)
from src.retrieve import retrieve, resolve_scheme  # noqa: E402

LOG_DIR = ROOT / "data" / "logs"
LOG_PATH = LOG_DIR / "queries.jsonl"
DOCS_DIR = ROOT / "data" / "docs"

# Used only when nothing was retrieved, so a refusal still carries one source.
FALLBACK_URL = "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth"

PII_LOG_PLACEHOLDER = "[withheld: PII detected]"


@lru_cache(maxsize=1)
def corpus_date() -> str:
    """Most recent fetch date across the ingested corpus.

    A refusal still cites the corpus, so it reports the corpus date rather than
    today's, which would imply the pages were re-read on the day of the refusal.
    """
    dates: list[str] = []
    for meta_path in DOCS_DIR.glob("*.meta.json"):
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        fetched_at = payload.get("fetched_at")
        if fetched_at:
            dates.append(str(fetched_at)[:10])
    if dates:
        return max(dates)
    return datetime.now(timezone.utc).date().isoformat()


@dataclass
class RAGResponse:
    question: str
    answer: str
    refused: bool
    refusal_kind: str | None
    source_url: str | None
    last_updated: str | None
    scheme: str | None
    retrieved: list[dict] = field(default_factory=list)
    contract_problems: list[str] = field(default_factory=list)
    generator_model: str = ""
    embedding_model: str = ""
    latency_ms: int = 0


def _footer(url: str, date_value: str) -> str:
    return f"Source: {url}\n{LAST_UPDATED_PREFIX} {date_value}"


# A model-authored citation line, tolerant of the decoration a chat model adds:
# markdown emphasis, a leading bullet or heading marker, and any case.
MODEL_FOOTER_RE = re.compile(
    r"^[\s>*#-]*(?:\*\*|\*|__)?\s*(?:Source\s*:|"
    + re.escape(LAST_UPDATED_PREFIX)
    + r")",
    re.I,
)


def strip_model_footer(raw: str) -> str:
    """Remove any citation lines the model wrote, leaving only its prose.

    The citation URL and the Last-updated date must come from retrieved chunk
    metadata, never from a model, so anything the model wrote in that position is
    discarded and replaced by the code-built footer. A real LLM will emit its own
    "Source:" line because the system prompt asks it to, and it gets it wrong in
    ways a stub never could: gpt-oss-120b truncated the URL mid-string and omitted
    the date, which left two conflicting Source lines in the answer and failed the
    single-citation contract.
    """
    kept = [line for line in (raw or "").splitlines() if not MODEL_FOOTER_RE.match(line)]
    return "\n".join(kept).strip()


def _log(entry: dict) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:  # logging must never break an answer
        print(f"  ! could not write query log: {exc}")


def _respond(
    question: str,
    refusal_kind: str | None,
    generator: Generator,
    started: float,
    retrieved: list[dict] | None = None,
    source_url: str | None = None,
    last_updated: str | None = None,
    scheme: str | None = None,
    contract_problems: list[str] | None = None,
) -> RAGResponse:
    body = refusal_for(refusal_kind) if refusal_kind else ""
    # Refusal copy may carry its own Source line; strip it so the appended footer
    # is the only citation and the contract sees exactly one URL.
    body, _existing = split_answer_footer(body)
    url = source_url or FALLBACK_URL
    date_value = last_updated or corpus_date()
    answer = f"{body}\n\n{_footer(url, date_value)}".strip() if body else _footer(url, date_value)
    return RAGResponse(
        question=question,
        answer=answer,
        refused=bool(refusal_kind),
        refusal_kind=refusal_kind,
        source_url=url,
        last_updated=date_value,
        scheme=scheme,
        retrieved=retrieved or [],
        contract_problems=contract_problems or [],
        generator_model=generator.model_id,
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def ask(question: str, top_k: int = 5) -> RAGResponse:
    started = time.perf_counter()
    generator = get_generator()
    text = (question or "").strip()

    if not text:
        response = _respond(text, "no_context", generator, started)
        _log(_log_entry(text, response, None, None))
        return response

    pii = screen_pii(text)
    if pii.is_pii:
        response = _respond(text, "pii", generator, started)
        _log(_log_entry(pii_flagged(text), response, None, pii.kinds))
        return response

    intent = classify_intent(text)
    if not intent.allowed:
        response = _respond(text, intent.kind, generator, started)
        _log(_log_entry(text, response, intent.kind, None))
        return response

    resolution = resolve_scheme(text)
    chunks = retrieve(text, top_k=top_k)
    if not chunks:
        response = _respond(
            text, "no_context", generator, started, scheme=resolution.source
        )
        _log(_log_entry(text, response, "no_context", None))
        return response

    source_url = chunks[0].source_url
    last_updated = chunks[0].ingest_date or corpus_date()

    # A configured-but-broken backend (expired key, unreachable Ollama) must not
    # surface as a crash. Fall back to the deterministic stub, which cannot invent.
    fallback_note: str | None = None
    try:
        raw = generator.generate(SYSTEM_PROMPT, build_prompt(text, chunks, resolution.source))
    except Exception as exc:  # noqa: BLE001
        fallback_note = (
            f"generator {generator.model_id} failed "
            f"({type(exc).__name__}); answered with the extractive stub"
        )
        print(f"  {fallback_note}")
        generator = StubGenerator()
        raw = generator.generate(SYSTEM_PROMPT, build_prompt(text, chunks, resolution.source))

    body = strip_model_footer(raw)
    if asks_about_fund_manager(text):
        # Appending a manager name is the one answer that can be wrong in a way
        # that matters, so the answer carries a staleness pointer to the AMC/AMFI
        # factsheet. Runs after the model's own output is separated from the
        # code-built footer, so the caveat is counted by the sentence budget.
        body = f"{body} {FUND_MANAGER_CAVEAT}".strip()
    answer = f"{body}\n\n{_footer(source_url, last_updated)}".strip()

    # Contract-check the composed answer, not the model draft. The draft check was
    # validating a citation we then discard, so its "wrong URL" violation could
    # only ever be informational; checking what the user actually receives is
    # what the contract is for.
    result = enforce_answer_contract(answer, allowed_url=source_url)
    problems = list(result.violations)
    if fallback_note:
        problems.append(fallback_note)

    response = RAGResponse(
        question=text,
        answer=answer,
        refused=False,
        refusal_kind=None,
        source_url=source_url,
        last_updated=last_updated,
        scheme=resolution.source,
        retrieved=[c.as_log() for c in chunks],
        contract_problems=problems,
        generator_model=generator.model_id,
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    _log(_log_entry(text, response, None, None))
    return response


def pii_flagged(text: str) -> str:
    """What gets written to the log in place of a question containing PII."""
    return PII_LOG_PLACEHOLDER


def _log_entry(
    question: str, response: RAGResponse, refusal_kind: str | None, pii_kinds
) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "question": question,
        "refused": response.refused,
        "refusal_kind": response.refusal_kind or refusal_kind,
        "pii_kinds": list(pii_kinds) if pii_kinds else [],
        "detected_scheme": response.scheme,
        "retrieved_chunk_ids": [r["chunk_id"] for r in response.retrieved],
        "retrieved": response.retrieved,
        "top_score": response.retrieved[0]["score"] if response.retrieved else None,
        "answer": response.answer,
        "cited_url": response.source_url,
        "last_updated": response.last_updated,
        "contract_problems": response.contract_problems,
        "latency_ms": response.latency_ms,
        "generator_model": response.generator_model,
        "embedding_model": response.embedding_model,
    }


if __name__ == "__main__":
    demos = [
        "What is the expense ratio of the HDFC Large Cap Fund?",
        "Should I buy the HDFC Small Cap Fund?",
        "My PAN is ABCDE1234F, can you check my returns?",
        "What is the expense ratio of the Parag Parag Flexi Cap Fund?",
    ]
    for demo in demos:
        result = ask(demo)
        print("=" * 78)
        print(f"Q: {demo}")
        print(f"   refused={result.refused}  kind={result.refusal_kind}  "
              f"scheme={result.scheme}")
        print(f"   chunks={len(result.retrieved)}  latency={result.latency_ms}ms  "
              f"model={result.generator_model}")
        if result.contract_problems:
            print(f"   contract_problems: {result.contract_problems}")
        print("-" * 78)
        for line in result.answer.splitlines():
            print(f"   {line}")
        print()
