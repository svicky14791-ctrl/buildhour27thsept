"""Development helper: probe retrieval and generation without the UI.

Not part of the pipeline and not imported by any src/ module. It exists so a
question can be traced by hand - scheme resolution, ranked chunks, the exact
prompt sent to the generator, and the contract verdict - without starting
Streamlit or writing a throwaway script each time.

    python tools/probe.py "exit load of the ELSS fund"     # retrieval only
    python tools/probe.py -g "exit load of the ELSS fund"  # + generation
    python tools/probe.py -g -f "what is the benchmark"    # + full chunk text
    python tools/probe.py -g -s "who manages the flexi cap fund?"  # + sources
    python tools/probe.py --top-k 3 "minimum sip"
    python tools/probe.py                                 # interactive REPL

Refusals are honoured exactly as src.chain.ask() applies them: a question the
guardrails refuse, or one that retrieves nothing, prints the refusal copy and
stops. The generator is not called, because a probe that generates past a refusal
reports an answer the real product would never give.

Read-only: it never writes to data/, and it does not append to the query log.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A Windows console defaults to cp1252, which cannot encode the narrow no-break
# spaces and en dashes that Groq models emit in otherwise normal answers. Without
# this, printing a valid answer dies with UnicodeEncodeError.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.generate import (  # noqa: E402
    SYSTEM_PROMPT,
    build_prompt,
    get_generator,
)
from src.guardrails import (  # noqa: E402
    classify_intent,
    enforce_answer_contract,
    refusal_for,
    screen_pii,
    split_answer_footer,
)
from src.retrieve import (  # noqa: E402
    MIN_SIMILARITY,
    SCHEME_FILTER_THRESHOLD,
    TOP_K,
    resolve_scheme,
    retrieve,
)

RULE = "=" * 78
SUBRULE = "-" * 78


def _header(question: str) -> None:
    print(RULE)
    print(f"Q: {question}")


def _refusal(kind: str, why: str) -> None:
    """Print the refusal the real chain would return, and why it stopped.

    The copy comes from refusal_for(), the same lookup chain.ask() uses, so a
    probe cannot show a different refusal than production.
    """
    print(f"   REFUSED kind={kind}  ({why})")
    print("   generator NOT called - the chain returns this without an LLM call")
    print("   --- refusal the chain would return ---")
    for line in refusal_for(kind).splitlines():
        print(f"     {line}")


def _guards(question: str) -> str | None:
    """Return a refusal kind, or None when the question may be answered."""
    if not (question or "").strip():
        # chain.ask() short-circuits an empty question before any screening.
        _refusal("no_context", "empty question")
        return "no_context"

    pii = screen_pii(question)
    if pii.is_pii:
        _refusal("pii", f"PII detected {pii.kinds}")
        return "pii"

    intent = classify_intent(question)
    if not intent.allowed:
        _refusal(intent.kind, intent.reason)
        return intent.kind

    print("   guardrails: allowed")
    return None


def _retrieval(question: str, top_k: int, full: bool) -> list:
    resolution = resolve_scheme(question)
    filter_state = (
        f"scheme_name == {resolution.source!r}"
        if resolution.is_confident
        else "none (no confident scheme named)"
    )
    print(f"   resolved: {resolution.source}  conf={resolution.confidence} "
          f"confident={resolution.is_confident} alias={resolution.matched_alias!r}")
    print(f"   where   : {filter_state}   (MIN_SIMILARITY={MIN_SIMILARITY}, "
          f"threshold={SCHEME_FILTER_THRESHOLD})")

    hits = retrieve(question, top_k=top_k)
    if not hits:
        # retrieve() already discards anything under MIN_SIMILARITY, so an empty
        # result means even the best chunk scored below the floor.
        _refusal("no_context", f"no chunk cleared MIN_SIMILARITY={MIN_SIMILARITY}")
        return hits

    print(f"   {len(hits)} chunk(s) returned:")
    for hit in hits:
        leaf = hit.section.split(">")[-1].strip()
        print(f"     {hit.score:.4f}  [{hit.kind:<5}] {hit.scheme_name}")
        print(f"             {leaf}")
        if full:
            for line in (hit.text or "").splitlines():
                print(f"             | {line}")
            print()
    return hits


def _sources(hits: list) -> None:
    """One clean line naming the documents the answer is drawn from."""
    seen: list[str] = []
    for hit in hits:
        if hit.scheme_name not in seen:
            seen.append(hit.scheme_name)
    print(f"   sources : {len(seen)} document(s) - " + "; ".join(seen))


def _generation(question: str, hits: list, resolution, show_sources: bool) -> None:
    prompt = build_prompt(question, hits, resolution.source)
    print("   --- prompt sent to the generator ---")
    for line in prompt.splitlines():
        print(f"     {line}")
    print(SUBRULE)

    generator = get_generator()
    answer = generator.generate(SYSTEM_PROMPT, prompt)
    body, footer = split_answer_footer(answer)
    result = enforce_answer_contract(answer, allowed_url=hits[0].source_url)

    print(f"   model: {generator.model_id}")
    if show_sources:
        _sources(hits)
    print("   --- answer body ---")
    for line in body.splitlines():
        print(f"     {line}")
    print("   --- footer ---")
    for line in footer.splitlines():
        print(f"     {line}")
    print(f"   contract: ok={result.ok}"
          + ("" if result.ok else f"  violations={result.violations}"))
    print(f"   cited url: {hits[0].source_url}")


def probe(
    question: str,
    top_k: int = TOP_K,
    generate: bool = False,
    full: bool = False,
    show_sources: bool = False,
) -> None:
    _header(question)
    refused = _guards(question)
    if refused:
        print()
        return

    hits = _retrieval(question, top_k, full)
    if not hits:
        print()
        return

    if generate:
        print(SUBRULE)
        _generation(question, hits, resolve_scheme(question), show_sources)
    print()


def _repl() -> None:
    print("Interactive probe. Ctrl-C or an empty line to exit.\n")
    while True:
        try:
            question = input("probe> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not question:
            return
        probe(question, generate=True, full=False, show_sources=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe.py",
        description="Probe retrieval and generation against the local Chroma index.",
    )
    parser.add_argument("question", nargs="*", help="the question to probe")
    parser.add_argument("-g", "--generate", action="store_true",
                        help="also run the generator and check the answer contract")
    parser.add_argument("-f", "--full", action="store_true",
                        help="print the full text of every retrieved chunk")
    parser.add_argument("-s", "--sources", action="store_true",
                        help="name the source documents the answer is drawn from")
    parser.add_argument("-k", "--top-k", type=int, default=TOP_K,
                        help=f"chunks to retrieve (default {TOP_K})")
    args = parser.parse_args(argv)

    question = " ".join(args.question).strip()
    if not question:
        _repl()
        return 0
    probe(question, top_k=args.top_k, generate=args.generate, full=args.full,
          show_sources=args.sources)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
