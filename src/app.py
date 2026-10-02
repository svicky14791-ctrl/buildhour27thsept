"""Stage 11: the Streamlit surface.

This file is the presentation layer and nothing else. Everything below the
CSS block that touches the retrieval pipeline - prewarm(), ask_cached(),
respond(), and the session_state contract they maintain - is backend and is
deliberately unchanged. The redesign is CSS, layout, and the render helpers.

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
from html import escape
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
# It is persistent and not dismissible per the PRD, but it renders as a quiet
# caption beside the chat input rather than a banner above the page - a banner
# that size reads as the main content, and it pushed the examples and the
# transcript below the fold on a phone.
STANDING_NOTE = "Facts only. No investment advice."
COLD_START_NOTE = (
    "Waking up. First load downloads the embedding model and opens the vector "
    "index, so this can take up to 30 seconds."
)
# Below this, prewarm() hit a warm cache and the note would only flash, which
# reads as a glitch rather than as progress.
COLD_START_FLASH_LIMIT = 1.0

# --- brand copy for the redesigned surface ----------------------------------
BRAND_SUBTITLE = "Mutual Fund Knowledge Assistant"
KB_STATUS_READY = "RAG connected"
HERO_KICKER = "Facts, not forecasts"
EMBED_MODEL = "all-MiniLM-L6-v2"
VECTOR_STORE = "ChromaDB"
EMBED_DIMS = "384-dim"

WELCOME = (
    "Explore scheme details, fund information and key facts through a simple "
    "conversational experience."
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

# ---------------------------------------------------------------------------
# Design tokens and component styles.
#
# One block, injected once at the top of the run. Every colour the app draws
# with is a token here rather than a hex literal in a render helper, so the
# surface stays consistent and .streamlit/config.toml only has to set the same
# handful of values for Streamlit's own widgets.
#
# The media queries at the bottom are the mobile half of the layout. Streamlit
# collapses columns to one below ~640px on its own, but it keeps the desktop
# gutters, the 16px base font and the 38px button height, none of which suit a
# phone.
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
    :root {
        --hdfc-navy: #0F3D56;
        --hdfc-navy-soft: #1B5A7A;
        --hdfc-red: #C41E3A;
        --ink: #1F2937;
        --ink-muted: #64748B;
        --ink-faint: #94A3B8;
        --surface: #FFFFFF;
        --surface-sunken: #F5F5F5;
        --line: #E2E8F0;
        --ok: #0F9D58;
    }

    /* Page chrome --------------------------------------------------------- */
    .block-container {
        padding-top: 2.2rem;
        padding-bottom: 7rem;   /* clears the pinned chat input */
        max-width: 60rem;
    }

    /* Header -------------------------------------------------------------- */
    h1 {
        color: var(--hdfc-navy);
        font-size: 2.1rem;
        font-weight: 700;
        letter-spacing: -0.02em;
        margin: 0 0 0.15rem 0;
    }
    .brandsub {
        color: var(--ink-muted);
        font-size: 0.95rem;
        margin-bottom: 0.35rem;
    }
    .statuspill {
        display: inline-flex;
        align-items: center;
        gap: 0.45rem;
        padding: 0.3rem 0.75rem;
        border-radius: 999px;
        background: #EAF6EF;
        border: 1px solid #C7E6D4;
        color: #0B6B3D;
        font-size: 0.8rem;
        font-weight: 600;
        white-space: nowrap;
    }
    .statuspill .dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background: var(--ok);
        box-shadow: 0 0 0 3px rgba(15, 157, 88, 0.18);
    }
    .statuswrap { text-align: right; padding-top: 0.9rem; }

    /* Hero ---------------------------------------------------------------- */
    .herokicker {
        color: var(--hdfc-red);
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        margin-bottom: 0.35rem;
    }
    /* Section labels ------------------------------------------------------ */
    .sectionlabel {
        color: var(--ink-muted);
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        margin: 1.6rem 0 0.5rem 0;
    }

    /* Question cards ------------------------------------------------------
       Real st.button elements, restyled. Buttons are the click target Streamlit
       gives us for free, and replacing them with HTML would mean hand-rolling
       click handling that the framework already does correctly. The overrides
       undo the parts of the default button that make it read as a button:
       fixed height, ellipsised label, centred text, grey fill. */
    .stButton > button {
        height: auto;
        min-height: 0;
        white-space: normal;
        text-align: left;
        padding: 0.85rem 1rem;
        border: 1px solid var(--line);
        border-left: 3px solid var(--hdfc-navy);
        border-radius: 12px;
        background: var(--surface);
        color: var(--ink);
        box-shadow: 0 1px 2px rgba(15, 61, 86, 0.06);
        transition: transform 0.12s ease, box-shadow 0.12s ease,
                    border-color 0.12s ease;
    }
    .stButton > button:hover {
        border-color: var(--hdfc-navy);
        border-left-color: var(--hdfc-red);
        box-shadow: 0 6px 18px rgba(15, 61, 86, 0.13);
        transform: translateY(-2px);
    }
    .stButton > button:focus-visible {
        outline: 2px solid var(--hdfc-navy-soft);
        outline-offset: 2px;
    }
    .stButton > button p {
        white-space: normal;
        overflow: visible;
        text-overflow: clip;
        font-weight: 500;
        font-size: 0.92rem;
        line-height: 1.45;
        /* Long scheme names must wrap rather than force the page wider than the
           viewport, which is what causes horizontal scroll on a phone. */
        overflow-wrap: anywhere;
    }

    /* Conversation -------------------------------------------------------- */
    [data-testid="stChatMessageContent"] p { line-height: 1.6; }
    [data-testid="stChatMessage"] {
        border-radius: 14px;
        padding: 0.4rem 0.7rem;
    }
    .stUserMessage {
        background: var(--hdfc-navy);
        border: none;
    }
    .stUserMessage p { color: #FFFFFF; font-weight: 500; }

    /* Answer card ---------------------------------------------------------
       st.container(border=True) renders as a bordered wrapper; these rules give
       it the rounded, padded card the redesign wants and lift it off the
       sunken sidebar/background. */
    [data-testid="stVerticalBlockBorderWrapper"] {
        border-radius: 14px;
        background: var(--surface);
        box-shadow: 0 1px 3px rgba(15, 61, 86, 0.07);
        padding: 0.35rem 0.4rem;
    }
    [data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stAlert"]) {
        box-shadow: none;   /* refusals keep their own alert styling */
    }

    /* Source card --------------------------------------------------------- */
    .srclabel {
        color: var(--ink-faint);
        font-size: 0.68rem;
        font-weight: 700;
        letter-spacing: 0.12em;
        text-transform: uppercase;
    }
    .srcscheme { color: var(--ink); font-weight: 600; font-size: 0.9rem; }
    .srcmeta { color: var(--ink-muted); font-size: 0.78rem; }
    .srclink {
        display: inline-block;
        margin-top: 0.35rem;
        padding: 0.32rem 0.8rem;
        border-radius: 8px;
        background: var(--hdfc-navy);
        color: #FFFFFF !important;
        text-decoration: none;
        font-size: 0.82rem;
        font-weight: 600;
    }
    .srclink:hover { background: var(--hdfc-navy-soft); }

    /* Evidence expander ----------------------------------------------------
       The passages stay one click away, but the retrieval score is the least
       useful thing in them, so it is dropped to faint monospace instead of
       competing with the passage heading. */
    .stExpander summary { font-weight: 600; font-size: 0.88rem; }
    .stExpander code {
        font-size: 0.72rem !important;
        color: var(--ink-faint) !important;
        background: transparent !important;
        padding: 0 !important;
    }

    /* Sidebar ------------------------------------------------------------- */
    [data-testid="stSidebar"] { background: var(--surface-sunken); }
    [data-testid="stSidebar"] .block-container { padding-top: 1.2rem; }
    .sbbrand {
        color: var(--hdfc-navy);
        font-size: 1.15rem;
        font-weight: 700;
        letter-spacing: -0.01em;
    }
    .sbsub { color: var(--ink-muted); font-size: 0.8rem; margin-bottom: 0.4rem; }
    .sbsection {
        color: var(--ink-faint);
        font-size: 0.7rem;
        font-weight: 700;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        margin: 1.4rem 0 0.3rem 0;
    }
    [data-testid="stSidebar"] a { text-decoration: none; }
    [data-testid="stSidebar"] p { font-size: 0.88rem; }

    /* Cold start ---------------------------------------------------------- */
    .coldnote {
        border: 1px solid var(--line);
        border-left: 3px solid var(--hdfc-navy);
        border-radius: 10px;
        background: var(--surface);
        padding: 0.7rem 0.95rem;
        color: var(--ink-muted);
        font-size: 0.88rem;
        margin-bottom: 0.5rem;
    }
    .coldnote b { color: var(--hdfc-navy); }

    /* Mobile -------------------------------------------------------------- */
    @media (max-width: 640px) {
        .block-container {
            padding-left: 1rem;
            padding-right: 1rem;
            padding-top: 1.4rem;
        }
        h1 { font-size: 1.7rem; }
        .brandsub { font-size: 0.88rem; }
        .statuswrap { text-align: left; padding-top: 0.2rem; }
        .block-container p, .block-container li {
            font-size: 0.95rem;
            line-height: 1.55;
        }
        /* Buttons are the primary tap target on a phone. */
        .stButton > button {
            padding-top: 0.85rem;
            padding-bottom: 0.85rem;
        }
        /* The chat input is pinned to the bottom; at the default size its
           placeholder truncates to a few characters on a narrow screen. */
        [data-testid="stChatInput"] textarea { font-size: 1rem; }
        [data-testid="stChatMessage"] { padding: 0.3rem 0.4rem; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# --- backend: unchanged ------------------------------------------------------


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


# --- presentation helpers ----------------------------------------------------


def _kb_status() -> str:
    """Label for the header and sidebar pills.

    Reads warm_index(), which is the same cached accessor the sidebar chunk
    count already used, so the status cannot claim a connection the index does
    not have without a second lookup against the vector store.
    """
    return KB_STATUS_READY if warm_index() else "Index empty"


def header() -> None:
    """Wordmark, subtitle, and live RAG connection status."""
    st.title(APP_TITLE)
    left, right = st.columns([3, 1], vertical_alignment="bottom")
    with left:
        st.markdown(f'<div class="brandsub">{escape(BRAND_SUBTITLE)}</div>', unsafe_allow_html=True)
    with right:
        st.markdown(
            f'<div class="statuswrap"><span class="statuspill"><span class="dot"></span>'
            f"{escape(_kb_status())}</span></div>",
            unsafe_allow_html=True,
        )


def hero() -> None:
    """One card that introduces the assistant."""
    with st.container(border=True):
        st.markdown(f'<div class="herokicker">{escape(HERO_KICKER)}</div>', unsafe_allow_html=True)
        st.markdown(WELCOME)


def question_cards() -> None:
    """The three starting questions, restyled as cards by the CSS block."""
    st.markdown('<div class="sectionlabel">Try asking</div>', unsafe_allow_html=True)
    for column, example in zip(st.columns(len(EXAMPLE_QUESTIONS)), EXAMPLE_QUESTIONS):
        # width="stretch" replaces use_container_width, deprecated in Streamlit 1.49+.
        # The label is a full question, so the button must wrap rather than
        # ellipsise - the CSS above does that. Columns also collapse to a
        # single column on narrow viewports, which is the mobile case.
        if column.button(example, key=f"example-{example[:24]}", width="stretch"):
            respond(example)


def sidebar() -> None:
    with st.sidebar:
        st.markdown(
            f'<div class="sbbrand">{escape(APP_TITLE)}</div>'
            f'<div class="sbsub">{escape(BRAND_SUBTITLE)}</div>',
            unsafe_allow_html=True,
        )
        st.markdown(f'<div class="sbsection">Schemes</div>', unsafe_allow_html=True)
        # One entry per scheme: the name is the link, the category is a quiet
        # second line. Two rows per scheme, with an unlabelled category link,
        # read as ten items for five schemes.
        for source in SOURCES:
            st.markdown(f"[{source.scheme_name}]({source.url})")
            st.caption(source.category)

        st.markdown(f'<div class="sbsection">Knowledge base</div>', unsafe_allow_html=True)
        st.markdown(
            f'<span class="statuspill"><span class="dot"></span>'
            f"{escape(_kb_status())}</span>",
            unsafe_allow_html=True,
        )
        st.write(f"{warm_index()} chunks embedded")
        st.caption(f"{EMBED_MODEL} &middot; {EMBED_DIMS} &middot; {VECTOR_STORE}")

        st.markdown(f'<div class="sbsection">Disclaimer</div>', unsafe_allow_html=True)
        st.caption(DISCLAIMER)


def _used_schemes(retrieved: list) -> list[str]:
    """Distinct scheme names behind the answer, in retrieval order."""
    seen: list[str] = []
    for hit in retrieved:
        name = (hit.get("scheme_name") or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _source_card(message: dict, schemes: list[str], label: str) -> None:
    """The citation card: which scheme, when it was last updated, and the link.

    Every grounded turn needs one. A refusal gets the same card but with a
    different label, because its link came from a rule rather than a page and
    calling that a "Source" would imply otherwise.
    """
    name = schemes[0] if schemes else f"the {len(SOURCES)} HDFC schemes"
    with st.container(border=True):
        st.markdown(f'<div class="srclabel">{escape(label)}</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="srcscheme">{escape(name)}</div>', unsafe_allow_html=True)
        st.caption(f"Last updated from sources: {message['last_updated']}")
        # The link label is the thing you click, so it carries the name.
        st.markdown(
            f'<a class="srclink" href="{message["source_url"]}" target="_blank" '
            f'rel="noopener noreferrer">Open source page &rarr;</a>',
            unsafe_allow_html=True,
        )


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
        if message.get("source_url"):
            # Not a Source card: the page was not the basis for this reply, and
            # saying so on the card is clearer than a caption explaining it.
            _source_card(message, schemes, "Reference only - not the basis for this reply")
            # The same disclaimer as selectable text, so the claim that this
            # link is not the basis for the reply does not depend on styling.
            st.caption(
                "Reference only, not the basis for this reply: "
                f"[{schemes[0] if retrieved else 'the 5 HDFC schemes'}]({message['source_url']})"
            )
    else:
        with st.container(border=True):
            st.markdown(body)
        if schemes:
            # One quiet line, not a list. The full evidence is below on demand.
            label = (
                schemes[0]
                if len(schemes) == 1
                else f"{schemes[0]} +{len(schemes) - 1} more"
            )
            st.caption(f"From: {label}")
            _source_card(message, schemes, "Source")
            # The plain markdown link stays as the citation of record: it is the
            # one that is text, so it can be selected, read by a screen reader
            # and asserted on in tools/test_ui.py.
            st.markdown(f"[Source]({message['source_url']})")

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


# --- layout -----------------------------------------------------------------

sidebar()
header()
hero()

if "messages" not in st.session_state:
    st.session_state.messages = []

if st.session_state.messages:
    st.markdown('<div class="sectionlabel">Conversation</div>', unsafe_allow_html=True)
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            if message["role"] == "user":
                st.markdown(message["content"])
            else:
                render_assistant(message)
else:
    question_cards()

# st.chat_input is pinned to the bottom of the viewport, so there is no reliable
# way to place an element after it. This caption sits immediately above it, which
# keeps the standing note next to the input it qualifies - the PRD requires it to
# be persistent and not dismissible, and this is the one place on the page where
# it stays on screen while the user types.
st.caption(STANDING_NOTE)

prompt = st.chat_input("Ask a factual question about one of the 5 HDFC schemes")
if prompt:
    respond(prompt)