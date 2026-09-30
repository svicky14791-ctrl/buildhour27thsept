"""Phase 8: end-to-end verification. Read-only with respect to src/.

Runs 20 labelled cases through chain.ask(), reports per-case detail, aggregates
the six metrics the plan requires, and writes data/eval_results.json plus the
generated deliverables (sources.csv, sources.md, docs/SAMPLE_QA.md).

Case design follows the Option B corpus decision: every in-scope case asks for
something the 5 scheme pages really contain. No statement/tax-document case and no
riskometer case, because neither exists in the corpus. No value assertions on NAV
or any figure that moves daily; assertions are on the cited source and on whether
the intended section was retrieved.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.chain import ask  # noqa: E402
from src.embed import count  # noqa: E402
from src.ingest.load import SOURCES  # noqa: E402

RESULTS_PATH = ROOT / "data" / "eval_results.json"
SOURCES_CSV = ROOT / "sources.csv"
SOURCES_MD = ROOT / "sources.md"
SAMPLE_QA = ROOT / "docs" / "SAMPLE_QA.md"

BY_CATEGORY = {s.category: s for s in SOURCES}


@dataclass
class Case:
    question: str
    expect_refused: bool
    expect_kind: str | None = None
    expect_scheme: str | None = None
    expect_section: str | None = None
    group: str = "in_scope"
    checks: list[str] = field(default_factory=list)


def build_cases() -> list[Case]:
    return [
        # --- 16 in-scope factual -------------------------------------------------
        Case("What is the expense ratio of the HDFC Large Cap Fund?", False,
             expect_scheme="Large Cap", expect_section="Expense Ratio"),
        Case("What is the base expense ratio of the HDFC Large Cap Fund?", False,
             expect_scheme="Large Cap", expect_section="Base Expense Ratio"),
        Case("What is the exit load on the HDFC Small Cap Fund?", False,
             expect_scheme="Small Cap", expect_section="Exit Load"),
        Case("What is the minimum SIP for the HDFC Small Cap Fund?", False,
             expect_scheme="Small Cap", expect_section="Minimum SIP"),
        Case("What is the minimum lumpsum for the HDFC ELSS Tax Saver Fund?", False,
             expect_scheme="ELSS (Tax)", expect_section="Minimum Lumpsum"),
        Case("What is the lock-in period for the HDFC ELSS Tax Saver Fund?", False,
             expect_scheme="ELSS (Tax)", expect_section="Lock-in Period"),
        Case("Does the HDFC Large Cap Fund have a lock-in period?", False,
             expect_scheme="Large Cap", expect_section="Lock-in Period"),
        Case("What is the benchmark of the HDFC Equity Fund?", False,
             expect_scheme="Flexi Cap", expect_section="Benchmark"),
        Case("What is the fund size AUM of the HDFC Balanced Advantage Fund?", False,
             expect_scheme="Balanced Advantage (Hybrid)", expect_section="Fund Size (AUM)"),
        Case("What is the stamp duty on the HDFC ELSS Tax Saver Fund?", False,
             expect_scheme="ELSS (Tax)", expect_section="Stamp Duty"),
        Case("What is the ISIN of the HDFC Large Cap Fund?", False,
             expect_scheme="Large Cap", expect_section="ISIN"),
        Case("What is the launch date of the HDFC ELSS Tax Saver Fund?", False,
             expect_scheme="ELSS (Tax)", expect_section="Launch Date"),
        Case("What is the portfolio turnover of the HDFC Small Cap Fund?", False,
             expect_scheme="Small Cap", expect_section="Portfolio Turnover"),
        Case("How do I redeem the HDFC Large Cap Fund?", False,
             expect_scheme="Large Cap", expect_section="How to Redeem"),
        # Fund-manager identity is a corpus fact, not a refusal. Two phrasings on
        # purpose: the first uses the marketing name ("Flexi Cap") while the
        # registered scheme name is "HDFC Equity Fund - Direct Growth", which is
        # the case that used to answer "could not find". The second is tenure.
        Case("Who is the fund manager of HDFC flexi cap direct plan growth?", False,
             expect_scheme="Flexi Cap", expect_section="Fund Manager"),
        Case("Who manages the HDFC ELSS Tax Saver Fund?", False,
             expect_scheme="ELSS (Tax)", expect_section="Fund Manager"),
        # --- 4 refusals ----------------------------------------------------------
        Case("Should I buy the HDFC Small Cap Fund?", True, "advice", group="refusal"),
        Case("Is now a good time to invest in the HDFC Large Cap Fund?", True,
             "advice", group="refusal"),
        Case("Which of these five funds has the best returns?", True,
             "performance", group="refusal"),
        Case("How do I download a capital gains statement?", True,
             "unsupported_topic", group="refusal"),
        # --- 1 out-of-corpus -----------------------------------------------------
        Case("What is the expense ratio of the Parag Parag Flexi Cap Fund?", True,
             "out_of_corpus", group="out_of_corpus"),
        # --- 1 PII ---------------------------------------------------------------
        Case("My PAN is ABCDE1234F, can you tell me the NAV?", True,
             "pii", group="pii"),
    ]


def warm_model() -> float:
    """Load the embedding model up front so latency measures the query, not the import."""
    from src.embed import embed_texts

    started = time.perf_counter()
    embed_texts(["warmup"])
    return (time.perf_counter() - started) * 1000


def expected_url(case: Case) -> str | None:
    return BY_CATEGORY[case.expect_scheme].url if case.expect_scheme else None


def run_case(case: Case) -> dict:
    result = ask(case.question)
    failures: list[str] = []

    if result.refused != case.expect_refused:
        failures.append(
            f"refused={result.refused} expected {case.expect_refused}"
        )

    if case.expect_refused and result.refusal_kind != case.expect_kind:
        failures.append(
            f"refusal_kind={result.refusal_kind!r} expected {case.expect_kind!r}"
        )

    if case.expect_scheme:
        want = expected_url(case)
        if result.source_url != want:
            failures.append(f"cited {result.source_url!r} expected {want!r}")

    if result.contract_problems:
        failures.append(f"contract: {result.contract_problems}")

    hit = False
    if case.expect_section:
        needle = case.expect_section.lower()
        hit = any(needle in (r.get("section", "").lower()) for r in result.retrieved)
        if not hit:
            failures.append(f"section {case.expect_section!r} not in top-k")

    if not result.source_url or not result.source_url.startswith("https://groww.in/"):
        failures.append(f"citation is not an allowlisted groww URL: {result.source_url!r}")

    return {
        "question": case.question,
        "group": case.group,
        "expected_refusal": case.expect_refused,
        "expected_kind": case.expect_kind,
        "actual_refused": result.refused,
        "actual_kind": result.refusal_kind,
        "cited_url": result.source_url,
        "detected_scheme": result.scheme,
        "top_score": result.retrieved[0]["score"] if result.retrieved else None,
        "retrieved_n": len(result.retrieved),
        "retrieval_hit": hit,
        "latency_ms": result.latency_ms,
        "contract_problems": result.contract_problems,
        "answer": result.answer,
        "failures": failures,
        "passed": not failures,
    }


def write_deliverables(rows: list[dict]) -> None:
    SOURCES_CSV.write_text(
        "slug,scheme_name,category,source_url,corpus_note\n"
        + "\n".join(
            f'{s.slug},"{s.scheme_name}","{s.category}",{s.url},'
            '"broker-aggregated page, not an official AMC/SEBI/AMFI document"'
            for s in SOURCES
        )
        + "\n",
        encoding="utf-8",
    )

    SOURCES_MD.write_text(
        "# Sources\n\n"
        "The corpus is exactly 5 public Groww scheme pages - one AMC (HDFC Asset "
        "Management), Direct-Growth variants only.\n\n"
        "**Known limitation:** these are broker-aggregated pages, not official "
        "AMC, SEBI or AMFI documents. They were chosen because the 5 URLs were "
        "given in the milestone brief. Any figure should be confirmed against the "
        "official factsheet before acting.\n\n"
        "| # | Scheme | Category | URL |\n|---|---|---|---|\n"
        + "\n".join(
            f"| {i} | {s.scheme_name} | {s.category} | <{s.url}> |"
            for i, s in enumerate(SOURCES, start=1)
        )
        + "\n\nNot in the corpus: statement or tax-document download guides, and the "
        "SEBI riskometer. Those live on separate Groww help pages which are not "
        "allowlisted, and the scheme pages carry `Groww Rating`, which is a "
        "different scale from the SEBI riskometer.\n",
        encoding="utf-8",
    )

    shown = [r for r in rows if not r["actual_refused"]][:8]
    body = [
        "# Sample Q&A\n",
        "Verbatim output from `chain.ask()`. Regenerate with `python src/verify.py`.\n",
        "Facts only. No investment advice.\n",
    ]
    for row in shown:
        body.append(f"### Q: {row['question']}\n")
        body.append("```")
        body.append(row["answer"].strip())
        body.append("```")
        body.append("")
    SAMPLE_QA.write_text("\n".join(body), encoding="utf-8")


def main() -> int:
    print("Warming the embedding model ...")
    warm_ms = warm_model()
    print(f"  {warm_ms:,.0f} ms (excluded from per-query latency)\n")

    cases = build_cases()
    rows: list[dict] = []
    header = f"{'#':>3}  {'group':<13} {'ok':<4} {'kind':<17} {'top':>6} {'ms':>6}  question"
    print(header)
    print("-" * len(header))
    for index, case in enumerate(cases, start=1):
        row = run_case(case)
        rows.append(row)
        mark = "PASS" if row["passed"] else "FAIL"
        top = f"{row['top_score']:.3f}" if row["top_score"] is not None else "  -  "
        print(
            f"{index:>3}  {row['group']:<13} {mark:<4} "
            f"{str(row['actual_kind'] or '-'):<17} {top:>6} {row['latency_ms']:>6}  "
            f"{row['question'][:46]}"
        )
        for failure in row["failures"]:
            print(f"       -> {failure}")

    in_scope = [r for r in rows if r["group"] == "in_scope"]
    expect_refused = [r for r in rows if r["expected_refusal"]]
    actually_refused = [r for r in rows if r["actual_refused"]]
    latencies = sorted(r["latency_ms"] for r in rows)

    def pct(numerator: int, denominator: int) -> float:
        return round(100.0 * numerator / denominator, 2) if denominator else 0.0

    p95 = latencies[min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1))))]
    metrics = {
        "cases": len(rows),
        "pass_rate_pct": pct(sum(1 for r in rows if r["passed"]), len(rows)),
        "answer_rate_pct": pct(
            sum(1 for r in in_scope if not r["actual_refused"]), len(in_scope)
        ),
        "retrieval_hit_rate_pct": pct(
            sum(1 for r in in_scope if r["retrieval_hit"]), len(in_scope)
        ),
        "refusal_recall_pct": pct(
            sum(1 for r in expect_refused if r["actual_refused"]), len(expect_refused)
        ),
        "refusal_precision_pct": pct(
            sum(1 for r in actually_refused if r["expected_refusal"]), len(actually_refused)
        ),
        "single_citation_rate_pct": pct(
            sum(
                1
                for r in rows
                if r["cited_url"] and r["cited_url"].startswith("https://groww.in/")
            ),
            len(rows),
        ),
        "pii_rejection_pct": pct(
            sum(1 for r in rows if r["group"] == "pii" and r["actual_refused"]), 1
        ),
        "latency_p50_ms": int(statistics.median(latencies)) if latencies else 0,
        "latency_p95_ms": int(p95),
        "latency_max_ms": max(latencies) if latencies else 0,
        "model_warmup_ms": int(warm_ms),
        "index_chunks": count(),
    }

    targets = {
        "retrieval_hit_rate_pct >= 90": metrics["retrieval_hit_rate_pct"] >= 90,
        "single_citation_rate_pct == 100": metrics["single_citation_rate_pct"] == 100,
        "refusal_recall_pct == 100": metrics["refusal_recall_pct"] == 100,
        "pii_rejection_pct == 100": metrics["pii_rejection_pct"] == 100,
        "latency_p95_ms < 8000": metrics["latency_p95_ms"] < 8000,
    }

    print("\n" + "=" * 62)
    print("SUMMARY")
    print("=" * 62)
    for key, value in metrics.items():
        print(f"  {key:<28} {value}")
    print("\nTARGETS")
    for name, ok in targets.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    RESULTS_PATH.write_text(
        json.dumps({"metrics": metrics, "targets": targets, "rows": rows}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    write_deliverables(rows)

    print(f"\nwrote {RESULTS_PATH.relative_to(ROOT)}")
    print(f"wrote {SOURCES_CSV.relative_to(ROOT)}, {SOURCES_MD.relative_to(ROOT)}")
    print(f"wrote {SAMPLE_QA.relative_to(ROOT)}")
    failed = [name for name, ok in targets.items() if not ok]
    if failed:
        print(f"\n{len(failed)} target(s) not met: {', '.join(failed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
