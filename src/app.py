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

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.chain import ask  # noqa: E402
from src.guardrails import split_answer_footer  # noqa: E402
from src.ingest.load import SOURCES  # noqa: E402

APP_TITLE = "MF Facts – RAG FAQ Assistant"
AMC = "HDFC Asset Management"
# Pinned verbatim by the Phase 7 spec, so it must not be reworded or extended.
STANDING_NOTE = "Facts only. No investment advice."
WELCOME = (
    f"Ask me facts about {len(SOURCES)} {AMC} schemes - expense ratio, exit load, "
    "minimum SIP, lock-in, benchmark, AUM, NAV, stamp duty and fund manager. I "
    "answer from public Groww scheme pages and cite one link. I never give advice "
    "and never state returns."
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
    """Make sure the index exists, building it once per process if it does not.

    Render's free tier has an ephemeral disk, so a restart or a fresh deploy can
    arrive with no data/chroma. Rebuilding from the tracked data/docs/ pages is
    cheap now that the embedder is ONNX (~5 s, no torch), which is far better
    than every question failing with a no_context refusal.
    """
    from src.embed import count

    existing = count()
    if existing:
        return existing
    print("No index found - building from data/docs ...")
    return _build()


@st.cache_resource(show_spinner=False)
def _build() -> int:
    from src.embed import build_index

    return build_index()


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
        for source in SOURCES:
            st.markdown(f"**{source.scheme_name}**")
            st.markdown(f"[{source.category}]({source.url})")
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
    with st.spinner("Thinking..."):
        result = ask_cached(question)
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
st.info(STANDING_NOTE)

if "messages" not in st.session_state:
    st.session_state.messages = []

for column, example in zip(st.columns(len(EXAMPLE_QUESTIONS)), EXAMPLE_QUESTIONS):
    # width="stretch" replaces use_container_width, deprecated in Streamlit 1.49+.
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

prompt = st.chat_input("Ask a factual question about one of the 5 HDFC schemes")
if prompt:
    respond(prompt)
