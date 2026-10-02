r"""Headless UI tests for src/app.py, using Streamlit's own AppTest harness.

    .\.venv\Scripts\python.exe tools\test_ui.py

AppTest executes the real script, so these cover the wiring that unit tests
cannot: that st.chat_input actually reaches chain.ask(), that a refusal renders
as a refusal rather than an empty bubble, and that the citation is only attached
to answers that have grounding.

Runs against GENERATOR_BACKEND=stub, so the suite is offline, deterministic and
costs no tokens. The stub is extractive rather than an LLM, which is what makes
stable assertions possible - it is not a stand-in for model quality, and
src/verify.py remains the check on that. The two suites cover different things:

    src/guardrails.py   refusal ladder, answer contract, PII, perf
    tools/test_ui.py    rendering, attribution, citation honesty
    src/verify.py       live model quality end to end

Exit code is non-zero on any failure, so this can gate a build.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Must be set before the app imports src.chain -> src.generate. get_generator()
# reads this at call time, but pinning it here also keeps a developer's real
# .env from leaking a paid model into a test run.
os.environ["GENERATOR_BACKEND"] = "stub"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from streamlit.testing.v1 import AppTest  # noqa: E402

from src.ingest.load import SOURCES  # noqa: E402

APP = str(ROOT / "src" / "app.py")
TIMEOUT = 240

_cases: list[tuple[str, bool, str]] = []


def check(label: str, got: object, want: object) -> None:
    _cases.append((label, got == want, f"got={got!r} want={want!r}"))


def check_that(label: str, condition: bool, detail: str = "") -> None:
    _cases.append((label, bool(condition), detail if not condition else ""))


def run() -> AppTest:
    return AppTest.from_file(APP, default_timeout=TIMEOUT).run()


def no_exceptions(at: AppTest, where: str) -> None:
    detail = "; ".join(str(e.value)[:200] for e in at.exception)
    check_that(f"no exception ({where})", not at.exception, detail)


def text_of(message) -> str:
    """Everything rendered inside one chat_message, flattened."""
    parts = [m.value for m in message.markdown]
    parts += [i.value for i in message.info]
    parts += [w.value for w in message.warning]
    parts += [c.value for c in message.caption]
    parts += [e.label for e in message.expander]
    return "\n".join(parts)


def scheme_links(block) -> set[str]:
    """Markdown link values inside a block, whitespace-normalised.

    Streamlit wraps long values in the element proto, so a scheme name arrives
    split across lines and never matches its counterpart in SOURCES verbatim.
    Collapsing runs of whitespace makes the comparison meaningful without
    loosening it - the URL still has to be the exact allowlisted one.
    """
    return {
        re.sub(r"\s+", " ", m.value).strip()
        for m in block.markdown
        if "groww.in" in m.value
    }


# --- 1. initial render -------------------------------------------------------
at = run()
no_exceptions(at, "initial render")
check_that(
    "MoneyChat brand in header",
    any("MoneyChat" in m.value for m in at.markdown),
    "",
)
check("3 example buttons", len(at.button), 3)
check_that(
    "chat input present", len(at.chat_input) == 1, f"got {len(at.chat_input)}"
)
check_that(
    "no messages before first question", len(at.chat_message) == 0,
    f"got {len(at.chat_message)}",
)
check_that("sidebar lists 5 schemes", len(scheme_links(at.sidebar)) == len(SOURCES), "")
check_that(
    "RAG status badge rendered",
    any("RAG connected" in m.value for m in at.markdown),
    "",
)
check_that(
    "compliance banner rendered",
    any("FACTS, NOT FORECASTS" in m.value.upper() for m in at.markdown),
    "",
)
# Done gate: the standing note is pinned verbatim by the Phase 7 spec. It renders
# as a caption beside the chat input, not as the banner it used to be, so this
# asserts the string and its absence from st.info rather than the element type.
check(
    "standing note is the literal spec string",
    [c.value for c in at.caption if c.value == "Facts only. No investment advice."],
    ["Facts only. No investment advice."],
)
check("no standing-note banner", len(at.info), 0)
check_that(
    "full disclaimer in sidebar",
    any("not investment advice" in c.value for c in at.sidebar.caption),
    "",
)
# One clean entry per scheme: the name is the link, the category sits under it.
# These were two rows each, with the category link unlabelled.
links = scheme_links(at.sidebar)
check("one link per scheme in sidebar", len(links), len(SOURCES))
check_that(
    "every scheme link carries its name and URL",
    all(
        f"[{source.scheme_name}]({source.url})" in links
        for source in SOURCES
    ),
    "",
)
check_that(
    "no category captions left in the sidebar",
    not any(
        source.category in c.value
        for c in at.sidebar.caption
        for source in SOURCES
    ),
    "",
)
check(
    "no bare category links left in the sidebar",
    [
        value
        for value in links
        if any(source.category == re.sub(r"^\[(.*)\]\(.*\)$", r"\1", value) for source in SOURCES)
    ],
    [],
)

# --- 2. factual answer: rendered, attributed, cited -------------------------
at = run()
at.chat_input[0].set_value("What is the expense ratio of the HDFC Large Cap Fund?").run()
no_exceptions(at, "factual answer")
check("user + assistant bubbles", len(at.chat_message), 2)
check("user role", at.chat_message[0].name, "user")
check("assistant role", at.chat_message[-1].name, "assistant")
body = text_of(at.chat_message[-1])
check_that("answer is not a refusal", "I only answer factual questions" not in body, body[:120])
check_that(
    "expense ratio answered from corpus", "1.03%" in body or "expense ratio" in body.lower(),
    body[:160],
)
check_that("source shown as From:", any("From:" in c.value for c in at.chat_message[-1].caption), body[:160])
check_that(
    "as-of date shown",
    any("Last updated from sources:" in c.value for c in at.chat_message[-1].caption),
    "",
)
check_that(
    "citation link present",
    any("Source](https://groww.in" in m.value for m in at.chat_message[-1].markdown),
    "",
)
check_that(
    "evidence collapsible present",
    any("RAG evidence" in e.label for e in at.chat_message[-1].expander),
    "",
)
check_that(
    "no contract problems on a clean answer",
    not any("contract check" in c.value for c in at.chat_message[-1].caption),
    "",
)
check_that("no warning block", len(at.chat_message[-1].warning) == 0, "")

# --- 3. advice refusal: st.warning, and a working link that isn't a "Source" --
at = run()
at.chat_input[0].set_value("Should I buy the HDFC Small Cap Fund?").run()
no_exceptions(at, "advice refusal")
last = at.chat_message[-1]
body = text_of(last)
check("rendered as st.warning", len(last.warning), 1)
check("no info block", len(last.info), 0)
check_that(
    "polite refusal copy", "can't recommend a scheme" in body, body[:160]
)
check_that(
    "refusal link is not labelled Source",
    not any("Source](https://" in m.value for m in last.markdown),
    "",
)
check_that(
    "refusal link still clickable",
    any("](https://groww.in" in c.value for c in last.caption),
    "",
)
check_that(
    "refusal link disclaims being the basis",
    any("not the basis for this reply" in c.value for c in last.caption),
    "",
)
check_that("no evidence expander on a refusal", len(last.expander) == 0, "")

# --- 4. out-of-corpus refusal ------------------------------------------------
at = run()
at.chat_input[0].set_value(
    "What is the expense ratio of the Parag Parag Flexi Cap Fund?"
).run()
no_exceptions(at, "out-of-corpus refusal")
body = text_of(at.chat_message[-1])
check_that(
    "out-of-corpus copy", "I only have facts for 5 HDFC" in body, body[:160]
)

# --- 5. no-context: guardrails allow it, retrieval finds nothing -------------
at = run()
at.chat_input[0].set_value("What is the airspeed velocity of a swallow?").run()
no_exceptions(at, "no-context path")
last = at.chat_message[-1]
body = text_of(last)
check_that(
    "no-context copy shown", "I couldn't find that in the 5 HDFC scheme pages" in body,
    body[:200],
)
check_that(
    "no empty bubble on no-context", not any(len(m.value.strip()) == 0 for m in last.markdown),
    "",
)

# --- 6. fund manager: co-managed answer stays a single grounded answer -------
at = run()
at.chat_input[0].set_value("Who manages the HDFC ELSS Tax Saver Fund?").run()
no_exceptions(at, "fund manager")
body = text_of(at.chat_message[-1])
check_that(
    "both co-managers named",
    "Amar Kalkundrikar" in body and "Dhruv Muchhal" in body,
    body[:200],
)
check_that(
    "staleness caveat present", "point-in-time snapshot" in body, body[:200]
)

# --- 7. example button drives a full turn ------------------------------------
at = run()
label = at.button[0].label
at.button[0].click().run()
no_exceptions(at, "example button")
check("button produced 2 bubbles", len(at.chat_message), 2)
check("button echoed the example", at.chat_message[0].markdown[0].value, label)
check_that(
    "button produced a grounded answer",
    len(at.chat_message[-1].expander) == 1,
    "no evidence expander",
)

# --- 8. transcript persists across reruns -----------------------------------
at = run()
at.chat_input[0].set_value("What is the minimum SIP of the small cap fund?").run()
at.chat_input[0].set_value("What is the stamp duty on the ELSS fund?").run()
no_exceptions(at, "two-turn transcript")
check("4 bubbles after 2 questions", len(at.chat_message), 4)
check(
    "first question still first",
    "minimum SIP" in at.chat_message[0].markdown[0].value,
    True,
)
check(
    "second question present",
    "stamp duty" in at.chat_message[2].markdown[0].value,
    True,
)

# --- 9. empty input must not create a turn ----------------------------------
at = run()
at.chat_input[0].set_value("   ").run()
no_exceptions(at, "whitespace input")
check_that("whitespace input ignored", len(at.chat_message) == 0, f"got {len(at.chat_message)}")

# --- 10. PII never reaches the model or the transcript ----------------------
at = run()
at.chat_input[0].set_value("My PAN is ABCDE1234F, tell me the NAV").run()
no_exceptions(at, "pii")
body = text_of(at.chat_message[-1])
check_that("PAN not echoed back", "ABCDE1234F" not in body, body[:200])
check_that("PII refusal copy", "personal or financial account information" in body, body[:200])

# --- report -----------------------------------------------------------------
width = max(len(label) for label, _ok, _detail in _cases) + 2
for label, ok, detail in _cases:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:<{width}}{detail if not ok else ''}")
passed = sum(1 for _l, ok, _d in _cases if ok)
print(f"\n{passed}/{len(_cases)} passed")
raise SystemExit(0 if passed == len(_cases) else 1)
