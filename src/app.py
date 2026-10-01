"""Stage 11: the Streamlit surface.

Deliberately the smallest UI the brief asks for: a welcome line, 3 example
questions, a facts-only note, and a transcript. No history sidebar, settings,
auth, theming or animations.

One Streamlit rule shapes the whole file: the script re-runs top to bottom on
every interaction, so ask() is only ever called from a button or chat_input
handler, never at module level. Everything expensive is therefore either cached
at module scope (the embedding model, in src/embed.py) or shown through
st.cache_resource.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.chain import ask  # noqa: E402
from src.guardrails import split_answer_footer  # noqa: E402
from src.ingest.load import SOURCES  # noqa: E402

APP_TITLE = "HDFC BOT"
AMC = "HDFC Asset Management"
# Pinned verbatim by the Phase 7 spec, so it must not be reworded or extended.
# It is persistent and not dismissible per the PRD, but it now renders as a quiet
# caption beside the chat input rather than a full-width banner above the page -
# a banner that size reads as the main content, and it pushed the examples and
# the transcript below the fold on a phone.
STANDING_NOTE = "Facts only. No investment advice."
COLD_START_NOTE = (
    "Waking up. First load downloads the embedding model and opens the vector "
    "index, so this can take up to 30 seconds."
)
# Below this, prewarm() hit a warm cache and the note would only flash, which
# reads as a glitch rather than as progress.
COLD_START_FLASH_LIMIT = 1.0
WELCOME = (
    f"Ask me facts about {len(SOURCES)} HDFC schemes - expense ratio, exit load, "
    "lock-in, AUM, NAV, stamp duty and fund manager. I answer from public Groww "
    "pages with one link, and never give advice or state returns."
)
EXAMPLE_QUESTIONS = [
    "What is the expense ratio of the HDFC Large Cap Fund?",
    "Who manages the HDFC ELSS Tax Saver Fund?",
    "What is the lock-in period for the ELSS tax saver fund?",
]
DISCLAIMER = (
    "This assistant provides factual information about 5 HDFC Mutual Fund schemes "
    "sourced from public Groww pages. It is not investment advice, and it does not "
    "state, calculate or compare returns, rankings or risk-adjusted performance. "
    "Fund manager names and start dates are a point-in-time snapshot - people "
    "change roles, so confirm the current manager on the official AMC or AMFI "
    "factsheet. Figures may be stale; always confirm against the official AMC "
    "factsheet before acting. Nothing here is a recommendation to buy, sell or "
    "hold any security."
)

st.set_page_config(page_title=APP_TITLE, layout="centered")

# st.button renders its label in a single line and clips it with an ellipsis when
# it overflows. The example labels are whole questions, so at three columns each
# one loses most of its text. This lets the label wrap and lets the button grow,
# so the question reads in full instead of "What is the expense...".
# Scoped to .stButton so nothing else in the app inherits the override.
#
# The media query is the mobile half of the layout: Streamlit collapses columns
# to one below ~640px on its own, but it keeps the desktop page gutters and 16px
# base font, which leaves a phone-width column with very little room per line.
st.markdown(
    """
    <style>
    .stButton > button p {
        white-space: normal;
        overflow: visible;
        text-overflow: clip;
    }
    .stButton > button {
        height: auto;
        white-space: normal;
        /* Long scheme names must wrap rather than force the page wider than
           the viewport, which is what causes horizontal scroll on a phone. */
        overflow-wrap: anywhere;
    }
    @media (max-width: 640px) {
        .block-container {
            padding-left: 1rem;
            padding-right: 1rem;
        }
        .block-container p,
        .block-container li {
            font-size: 0.95rem;
            line-height: 1.5;
        }
        h1 {
            font-size: 1.75rem;
        }
        /* Buttons are the primary tap target on a phone, so give them room
           rather than the 38px default. */
        .stButton > button {
            padding-top: 0.6rem;
            padding-bottom: 0.6rem;
        }
        /* The chat input is pinned to the bottom; without this its placeholder
           is truncated to a few characters on a narrow screen. */
        [data-testid="stChatInput"] textarea {
            font-size: 1rem;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource(show_spinner=False)
def index_size() -> int:
    """Chunk count for the sidebar.

    Cached because Streamlit re-runs this script on every keystroke in the chat
    input, and an uncached count() opened a fresh Chroma client each time.
    """
    from src.embed import count

    return count()


@st.cache_resource(show_spinner=False)
def warm_index() -> int:
    """Chunk count for the sidebar. The index itself is built by prewarm()."""
    return prewarm()["chunks"]


@st.cache_resource(show_spinner=False)
def _build() -> int:
    from src.embed import build_index

    return build_index()


def _log(event: str) -> None:
    """One line per startup step, so a stall is visible in Render's log.

    Streamlit captures print() from the script run, which is why the sidebar
    count already showed up. This adds the stages behind it: without these you
    cannot tell a 40 s model download from a 40 s hung Chroma open.
    """
    print(f"[startup] {event}", flush=True)


@st.cache_resource(show_spinner=False)
def prewarm() -> dict:
    """Pay every cold-start cost before the user's first question.

    The ONNX session costs ~176 MB and the ONNX weights are an 86 MB download.
    Doing that inside the first answer made the app look broken: the user typed
    a question and watched "Thinking..." for a minute. Doing it here means the
    page takes a few seconds longer to appear once per instance, and every
    subsequent question is fast.

    Each step logs before and after, so Render's log shows exactly which step
    is responsible if this is what the health check is waiting on.
    """
    from src.embed import count, embed_texts, get_client, get_collection, get_model

    t0 = time.perf_counter()
    _log("prewarm begin")
    _log(f"  python {sys.version.split()[0]}  pid {os.getpid()}  cwd {Path.cwd()}")

    total = count()
    _log(f"  index check: {total} chunk(s) in data/chroma")
    if not total:
        _log("  index missing - rebuilding from data/docs (this is the slow path)")
        total = _build()
        _log(f"  index rebuilt: {total} chunk(s) in {time.perf_counter() - t0:.1f}s")
    else:
        _log(f"  index reused ({time.perf_counter() - t0:.1f}s)")

    _log("  opening chroma client")
    collection = get_collection(get_client())
    _log(f"  chroma open, collection.count()={collection.count()}")

    _log("  loading ONNX model (first load on a cold instance downloads ~86 MB)")
    get_model()
    _log(f"  ONNX model ready ({time.perf_counter() - t0:.1f}s)")

    _log("  running a warm-up inference")
    embed_texts(["warm up"])
    _log(f"  warm-up inference done ({time.perf_counter() - t0:.1f}s)")

    # litellm is imported lazily inside generate(), which made the *first*
    # question absorb a ~20 s / ~160 MB import. Do it here instead, and only
    # when a real backend is selected - the stub needs none of it.
    from src.generate import LiteLLMGenerator, get_generator

    generator = get_generator()
    if isinstance(generator, LiteLLMGenerator):
        _log("  importing litellm (~160 MB, lazily imported by generate())")
        import litellm  # noqa: F401

        _log(f"  litellm {getattr(litellm, '__version__', '?')} imported")
    _log(f"  generator: {generator.model_id}")

    _log(f"prewarm complete in {time.perf_counter() - t0:.1f}s")
    return {"chunks": total, "generator": generator.model_id}


# Runs on every rerun, but the body runs once per process. Placed here rather
# than inside sidebar() so the cost is paid and logged even if the sidebar is
# not rendered.
#
# The placeholder is the cold-start affordance. A cached instance returns in
# well under COLD_START_FLASH_LIMIT and the note is swapped back out, so the
# steady state is a plain page. A cold instance holds the note on screen for the
# ~30 s while the model downloads, instead of showing a blank page that reads as
# a crash. The spinner does the same job for the wait but disappears the moment
# the script yields, which on a cold start is before the download finishes.
_cold_start = st.empty()
_cold_start.info(COLD_START_NOTE)
_cold_t0 = time.perf_counter()
prewarm()
if time.perf_counter() - _cold_t0 >= COLD_START_FLASH_LIMIT:
    _cold_start.success("Ready. Ask a question below.")
else:
    _cold_start.empty()


@st.cache_data(show_spinner=False)
def ask_cached(question: str):
    """Run the chain once per distinct question.

    Streamlit re-runs the whole script on every interaction. Without this, a
    rerun triggered by something unrelated - resizing the window, focusing the
    chat input - would re-run retrieval and re-bill the LLM for the same
    question. The cache is keyed on the question text and dropped when the
    session's transcript is cleared.
    """
    return ask(question)


def sidebar() -> None:
    with st.sidebar:
        st.subheader(AMC)
        st.caption(f"{len(SOURCES)} schemes in this corpus")
        # One entry per scheme, name as the link and the category as a quiet
        # second line. These were two separate st.markdown calls each, which
        # rendered as a name with an unlabelled category link under it: two rows
        # per scheme, and neither row said what the link was.
        for source in SOURCES:
            st.markdown(f"[{source.scheme_name}]({source.url})")
            st.caption(source.category)
        st.divider()
        st.subheader("Index")
        st.write(f"{warm_index()} chunks embedded")
        st.caption("all-MiniLM-L6-v2, ChromaDB")
        st.divider()
        st.subheader("Disclaimer")
        st.caption(DISCLAIMER)


def _used_schemes(retrieved: list) -> list[str]:
    """Distinct scheme names behind the answer, in retrieval order."""
    seen: list[str] = []
    for hit in retrieved:
        name = (hit.get("scheme_name") or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def render_assistant(message: dict) -> None:
    answer = message.get("answer") or ""
    body, _footer = split_answer_footer(answer)
    body = body.strip()
    retrieved = message.get("retrieved") or []
    # refused=True is the chain's own verdict. The other two conditions are belt
    # and braces: a future path that returns neither a refusal nor any grounding
    # would otherwise render as an empty bubble, which reads as a broken app
    # rather than an honest "I don't know".
    is_refusal = bool(message.get("refused")) or not body or not retrieved
    schemes = _used_schemes(retrieved)

    if is_refusal:
        st.warning(body or message.get("fallback_note") or "I don't have that in my sources.")
    else:
        with st.container(border=True):
            st.markdown(body)
            if schemes:
                # One quiet line, not a list. The full evidence is below on demand.
                label = schemes[0] if len(schemes) == 1 else f"{schemes[0]} +{len(schemes) - 1} more"
                st.caption(f"From: {label}")
                st.markdown(f"[Source]({message['source_url']})")
                st.caption(f"Last updated from sources: {message['last_updated']}")
    if is_refusal and message.get("source_url"):
        # Every turn still needs a working link, but a refusal came from a rule
        # rather than a page, so calling this "Source" would imply otherwise.
        # Link the scheme name so the label is the thing you click.
        with st.container(border=True):
            st.caption(
                "Reference only, not the basis for this reply: "
                f"[{schemes[0] if retrieved else 'the 5 HDFC schemes'}]({message['source_url']})"
            )

    if retrieved:
        with st.expander(f"RAG evidence ({len(retrieved)} passages)"):
            for hit in retrieved:
                leaf = (hit.get("section") or "").split(">")[-1].strip()
                st.markdown(f"**{leaf}**  `score={hit.get('score')}`")
                st.caption(hit.get("scheme_name", ""))

    for problem in message.get("contract_problems") or []:
        st.caption(f"contract check: {problem}")


def respond(question: str) -> None:
    """Answer a question, append it to the transcript, and redraw.

    This only writes state. Rendering belongs to the transcript loop further
    down, because the script re-runs from the top on every interaction: if this
    also drew the exchange inline, a question arriving from a button (which sits
    above the transcript) would be drawn twice on the same run. Handlers write,
    the transcript draws, and the rerun puts both turns in one place.
    """
    question = (question or "").strip()
    if not question:
        return
    # A button fires on every rerun while it stays True, so guard on the
    # question we have not answered yet rather than trusting the click.
    if question == st.session_state.get("last_answered"):
        return
    st.session_state.last_answered = question

    st.session_state.messages.append({"role": "user", "content": question})
    _log(f"question received: {question[:70]!r}")
    t0 = time.perf_counter()
    with st.spinner("Looking up facts..."):
        result = ask_cached(question)
    _log(
        f"question answered in {time.perf_counter() - t0:.1f}s "
        f"(refused={result.refused}, hits={len(result.retrieved)}, "
        f"model={result.generator_model})"
    )
    st.session_state.messages.append(
        {
            "role": "assistant",
            "answer": result.answer,
            "refused": result.refused,
            "refusal_kind": result.refusal_kind,
            "source_url": result.source_url,
            "last_updated": result.last_updated,
            "retrieved": result.retrieved,
            "contract_problems": result.contract_problems,
        }
    )
    st.rerun()


sidebar()

st.title(APP_TITLE)
st.write(WELCOME)

if "messages" not in st.session_state:
    st.session_state.messages = []

for column, example in zip(st.columns(len(EXAMPLE_QUESTIONS)), EXAMPLE_QUESTIONS):
    # width="stretch" replaces use_container_width, deprecated in Streamlit 1.49+.
    # The label is a full question, so the button must wrap rather than
    # ellipsise - the <style> block above does that. Columns also collapse to a
    # single column on narrow viewports, which is the mobile case.
    if column.button(example, key=f"example-{example[:24]}", width="stretch"):
        respond(example)

if st.session_state.messages:
    st.divider()
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            if message["role"] == "user":
                st.markdown(message["content"])
            else:
                render_assistant(message)

# st.chat_input is pinned to the bottom of the viewport, so there is no reliable
# way to place an element after it. This caption sits immediately above it, which
# keeps the standing note next to the input it qualifies - the PRD requires it to
# be persistent and not dismissible, and this is the one place on the page where
# it stays on screen while the user types.
st.caption(STANDING_NOTE)

prompt = st.chat_input("Ask a factual question about one of the 5 HDFC schemes")
if prompt:
    respond(prompt)
