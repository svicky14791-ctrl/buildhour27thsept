"""Stage 11: the Streamlit surface.

This file is the presentation layer and nothing else. Everything below the
CSS block that touches the retrieval pipeline - prewarm(), ask_cached(),
respond(), and the session_state contract they maintain - is backend and is
deliberately unchanged. The MoneyChat restyle is CSS, layout, and the render
helpers only.

The visual language follows design/stitch.html: navy #004c8f + red #ED1c24,
Inter, rounded white cards and a soft-shadow header. Tailwind and inline JS
from the mock cannot run in Streamlit, so the look is reproduced with native
components plus a single custom-CSS block.

One Streamlit rule shapes the whole file: the script re-runs top to bottom on
every interaction, so ask() is only ever called from a button or chat_input
handler, never at module level. Everything expensive is therefore either cached
at module scope (the embedding model, in src/embed.py) or shown through
st.cache_resource.
"""

from __future__ import annotations

import base64
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

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
LOGO_PATH = ASSETS_DIR / "logo.png"

APP_TITLE = "MoneyChat"
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

# --- brand copy for the MoneyChat surface -----------------------------------
BRAND_SUBTITLE = "Mutual Fund Knowledge Assistant"
KB_STATUS_READY = "RAG connected"
HERO_KICKER = "Facts, not forecasts"
BANNER_TEXT = (
    "Answers are grounded strictly in the ingested HDFC Mutual Fund scheme "
    "pages. No forecasts, no rankings and no investment advice."
)
# Header for the demonstrated-query section shown above a grounded multi-scheme
# answer. The badge beside it carries real provenance, never a hardcoded month.
DEMO_QUERY_TITLE = "DEMONSTRATED GROUNDED QUERY"

WELCOME = (
    "Explore scheme details, fund information and key facts through a simple "
    "conversational experience."
)
EXAMPLE_QUESTIONS = [
    "What is the expense ratio of the HDFC Large Cap Fund?",
    "Who manages the HDFC ELSS Tax Saver Fund?",
    "What is the lock-in period for the ELSS tax saver fund?",
]
# Short labels rendered as the little category tag on each suggested-question
# card. They describe the question, not any fund fact, so nothing here is data.
EXAMPLE_CATEGORIES = ["Expense & Fees", "Fund Manager", "ELSS & Lock-in"]
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

st.set_page_config(
    page_title=APP_TITLE,
    page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else "\U0001f4b0",
    layout="centered",
)

# ---------------------------------------------------------------------------
# Design tokens and component styles.
#
# One block, injected once at the top of the run. The palette mirrors
# design/stitch.html: navy #004c8f primary, red #ED1c24 accent, Inter type and
# slate neutrals. Every colour the app draws with is a token here rather than a
# hex literal in a render helper.
#
# The media queries at the bottom are the mobile half of the layout. Streamlit
# collapses columns to one below ~640px on its own, but it keeps the desktop
# gutters, the 16px base font and the 38px button height, none of which suit a
# phone.
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    :root {
        --navy: #004c8f;
        --navy-dark: #002e6e;
        --accent: #ED1c24;
        --light-blue: #edf4fb;
        --canvas: #f8fafc;
        --ink: #0f172a;
        --ink-muted: #64748b;
        --ink-faint: #94a3b8;
        --surface: #ffffff;
        --line: #e2e8f0;
        --ok: #10b981;
    }

    /* Inter applies to normal text only. The old [class*="st-"] selector also
       caught Streamlit's icon spans (their emotion class starts with "st-"),
       overriding the ligature icon font and printing the raw glyph name
       ("keyboard_arrow_right") beside the expander label. */
    html, body, .stApp,
    p, div,
    span:not([data-testid*="Icon"]):not([class*="material"]):not([class*="Material"]),
    label, button, input, textarea, select, option,
    a, small, strong, em, li, summary, th, td,
    h1, h2, h3, h4, h5, h6 {
        font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
    }
    /* Restore the ligature icon font everywhere Streamlit uses it: expander
       chevrons, buttons with icons, and the sidebar collapse control. Every
       DynamicIcon carries an "Icon" testid (stIconMaterial is the default),
       so one selector covers all of them. */
    [data-testid="stIconMaterial"] {
        font-family: "Material Symbols Rounded" !important;
    }
    [data-testid*="Icon"],
    span[class*="material"], span[class*="Material"], i[class*="material"],
    .material-symbols-rounded, .material-symbols-outlined, .material-icons {
        font-family: "Material Symbols Rounded", "Material Symbols Outlined",
                     "Material Icons" !important;
    }
    .stApp { background: var(--canvas); }
    [data-testid="stHeader"] { background: transparent; box-shadow: none; }

    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: var(--canvas); }
    ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 9999px; }
    ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }

    /* Page chrome --------------------------------------------------------- */
    .block-container {
        padding-top: 1.6rem;
        padding-bottom: 7rem;   /* clears the pinned chat input */
        max-width: 62rem;
    }

    /* Header -------------------------------------------------------------- */
    .appheader {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 1rem;
        background: var(--surface);
        border: 1px solid var(--line);
        border-radius: 16px;
        padding: 0.7rem 1rem;
        margin-bottom: 1.5rem;
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.05);
    }
    .appheader-brand {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        min-width: 0;
    }
    .brandlogo {
        width: 46px;
        height: 46px;
        flex: 0 0 auto;
        object-fit: contain;
        border-radius: 10px;
        background: var(--surface);
    }
    .logofallback {
        display: flex;
        align-items: center;
        justify-content: center;
        background: var(--navy);
        color: #ffffff;
        font-weight: 800;
        font-size: 1.3rem;
    }
    .brandname {
        color: var(--navy);
        font-size: 1.05rem;
        font-weight: 800;
        letter-spacing: -0.01em;
        line-height: 1.1;
    }
    .brandsub {
        color: var(--ink-muted);
        font-size: 0.78rem;
        font-weight: 500;
        margin-top: 0.15rem;
    }
    .statuspill {
        display: inline-flex;
        align-items: center;
        gap: 0.45rem;
        padding: 0.3rem 0.75rem;
        border-radius: 999px;
        background: #ecfdf5;
        border: 1px solid #a7f3d0;
        color: #047857;
        font-size: 0.78rem;
        font-weight: 700;
        white-space: nowrap;
    }
    .statuspill .dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--ok);
        box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.18);
    }

    /* Hero ---------------------------------------------------------------- */
    .hero { text-align: center; margin: 0.25rem 0 1.2rem; }
    .herologo {
        width: 84px;
        height: 84px;
        object-fit: contain;
        display: block;
        margin: 0 auto 0.7rem;
    }
    .herotitle {
        color: var(--navy);
        font-size: 2rem;
        font-weight: 800;
        letter-spacing: -0.02em;
        line-height: 1.1;
    }
    .herosub {
        color: var(--ink-muted);
        font-size: 0.95rem;
        font-weight: 500;
        margin-top: 0.25rem;
    }
    .herointro {
        color: var(--ink-muted);
        font-size: 0.92rem;
        margin: 0.55rem auto 0;
        max-width: 34rem;
        line-height: 1.55;
    }

    /* Compliance banner --------------------------------------------------- */
    .factbanner {
        position: relative;
        display: flex;
        gap: 0.8rem;
        align-items: flex-start;
        background: var(--surface);
        border: 1px solid var(--line);
        border-left: 4px solid var(--accent);
        border-radius: 14px;
        padding: 0.9rem 1rem;
        margin-bottom: 1.4rem;
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.05);
    }
    .facticon {
        flex: 0 0 auto;
        width: 30px;
        height: 30px;
        border-radius: 9px;
        background: #fef2f2;
        color: var(--accent);
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 800;
        font-size: 1rem;
    }
    .facttitle {
        color: var(--accent);
        font-size: 0.72rem;
        font-weight: 800;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        margin-bottom: 0.15rem;
    }
    .facttext {
        color: var(--ink-muted);
        font-size: 0.84rem;
        line-height: 1.5;
        margin: 0;
    }

    /* Section labels ------------------------------------------------------ */
    .sectionlabel {
        color: var(--ink-faint);
        font-size: 0.72rem;
        font-weight: 800;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        margin: 0.2rem 0 0.6rem 0;
    }

    /* Demonstrated grounded query ----------------------------------------
       Sits at the top of a grounded, multi-scheme answer: a green status dot,
       the section label, the real provenance date, and the question that
       produced the answer. It is rendered only on the non-refusal branch, so
       it can never dress up a refusal as a demonstration. */
    .demoquery { margin: 0 0 0.7rem 0; }
    .demohead {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 0.5rem;
        margin-bottom: 0.55rem;
    }
    .demodot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--ok);
        box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.18);
        flex: 0 0 auto;
    }
    .demotitle {
        color: var(--ink);
        font-size: 0.72rem;
        font-weight: 800;
        letter-spacing: 0.12em;
        text-transform: uppercase;
    }
    .demobadge {
        display: inline-block;
        padding: 0.14rem 0.5rem;
        border-radius: 6px;
        background: var(--light-blue);
        color: var(--navy);
        font-size: 0.62rem;
        font-weight: 800;
        letter-spacing: 0.04em;
        white-space: nowrap;
    }
    .demoq {
        display: flex;
        align-items: flex-start;
        gap: 0.6rem;
        background: var(--surface);
        border: 1px solid var(--line);
        border-radius: 14px;
        padding: 0.7rem 0.85rem;
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
    }
    .demoqmark {
        flex: 0 0 auto;
        color: var(--navy);
        font-weight: 800;
        font-size: 0.95rem;
        line-height: 1.5;
    }
    .demoqtext {
        color: var(--ink);
        font-size: 0.9rem;
        font-weight: 600;
        line-height: 1.5;
    }

    /* Suggested-question cards -------------------------------------------
       Real st.button elements, restyled. Buttons are the click target
       Streamlit gives us for free; the category tag makes the surrounding
       column read as a card, so the button itself is stripped back to plain
       text inside it. .stColumn is the stable testid Streamlit emits for a
       column, and :has() lets us card only the columns that hold a .qtag. */
    .stColumn:has(.qtag) {
        background: var(--surface);
        border: 1px solid var(--line);
        border-radius: 16px;
        padding: 0.85rem 0.9rem 0.7rem;
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
        position: relative;
        overflow: hidden;
        transition: box-shadow 0.15s ease, border-color 0.15s ease,
                    transform 0.15s ease;
    }
    .stColumn:has(.qtag)::before {
        content: "";
        position: absolute;
        left: 0;
        top: 0.9rem;
        bottom: 0.9rem;
        width: 3px;
        border-radius: 0 3px 3px 0;
        background: var(--navy);
    }
    .stColumn:has(.qtag):hover {
        border-color: rgba(0, 76, 143, 0.45);
        box-shadow: 0 8px 20px rgba(0, 76, 143, 0.12);
        transform: translateY(-2px);
    }
    .stColumn:has(.qtag) [data-testid="stMarkdownContainer"] p { margin: 0; }
    .stColumn:has(.qtag) [data-testid="stVerticalBlock"] { gap: 0.35rem; }
    .qtag {
        display: inline-block;
        padding: 0.14rem 0.5rem;
        border-radius: 6px;
        background: var(--light-blue);
        color: var(--navy);
        font-size: 0.62rem;
        font-weight: 800;
        letter-spacing: 0.06em;
        text-transform: uppercase;
    }
    .stColumn:has(.qtag) .stButton > button {
        border: none !important;
        background: transparent !important;
        box-shadow: none !important;
        padding: 0.3rem 0 0 0 !important;
        color: var(--ink) !important;
        text-align: left !important;
        font-size: 0.88rem !important;
        font-weight: 600 !important;
        line-height: 1.4 !important;
        min-height: 0 !important;
        height: auto !important;
        white-space: normal !important;
    }
    .stColumn:has(.qtag) .stButton > button:hover {
        color: var(--navy) !important;
    }
    .stColumn:has(.qtag) .stButton > button:focus-visible {
        outline: 2px solid var(--navy);
        outline-offset: 2px;
    }
    .stColumn:has(.qtag) .stButton > button p {
        white-space: normal;
        overflow: visible;
        text-overflow: clip;
        overflow-wrap: anywhere;
    }

    /* Conversation -------------------------------------------------------- */
    [data-testid="stChatMessageContent"] p { line-height: 1.6; }
    [data-testid="stChatMessage"] {
        border-radius: 14px;
        padding: 0.4rem 0.7rem;
    }
    .stUserMessage {
        background: var(--navy);
        border: none;
    }
    .stUserMessage p { color: #ffffff; font-weight: 500; }

    /* Answer card ---------------------------------------------------------
       st.container(border=True) renders as a bordered wrapper; these rules give
       it the rounded, padded card the design wants and lift it off the canvas. */
    [data-testid="stVerticalBlockBorderWrapper"] {
        border-radius: 14px;
        background: var(--surface);
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.07);
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
        background: var(--navy);
        color: #ffffff !important;
        text-decoration: none;
        font-size: 0.82rem;
        font-weight: 600;
    }
    .srclink:hover { background: var(--navy-dark); }

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

    /* Chat input ---------------------------------------------------------- */
    [data-testid="stChatInput"] {
        border: 1px solid var(--line);
        border-radius: 16px;
        background: var(--surface);
        box-shadow: 0 6px 20px rgba(15, 23, 42, 0.09);
        overflow: hidden;
    }
    [data-testid="stChatInput"] textarea { background: transparent; }
    [data-testid="stChatInputSubmitButton"] {
        background: var(--navy) !important;
        color: #ffffff !important;
        border-radius: 10px !important;
    }
    [data-testid="stChatInputSubmitButton"]:hover { background: var(--navy-dark) !important; }

    /* Sidebar ------------------------------------------------------------- */
    [data-testid="stSidebar"] {
        background: var(--surface);
        border-right: 1px solid var(--line);
    }
    [data-testid="stSidebar"] .block-container { padding-top: 1.2rem; }
    .sbbrand {
        color: var(--navy);
        font-size: 1.15rem;
        font-weight: 800;
        letter-spacing: -0.01em;
    }
    .sbsub { color: var(--ink-muted); font-size: 0.8rem; margin-bottom: 0.4rem; }
    .sbsection {
        color: var(--ink-faint);
        font-size: 0.7rem;
        font-weight: 800;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        margin: 1.4rem 0 0.3rem 0;
    }
    [data-testid="stSidebar"] a {
        display: block;
        padding: 0.4rem 0.55rem;
        border-radius: 9px;
        color: var(--navy) !important;
        text-decoration: none;
        font-size: 0.86rem;
        font-weight: 600;
        transition: background 0.12s ease, color 0.12s ease;
    }
    [data-testid="stSidebar"] a:hover {
        background: var(--light-blue);
        color: var(--accent) !important;
    }
    [data-testid="stSidebar"] p { font-size: 0.86rem; }

    /* Mobile -------------------------------------------------------------- */
    @media (max-width: 640px) {
        .block-container {
            padding-left: 1rem;
            padding-right: 1rem;
            padding-top: 1rem;
        }
        .appheader { flex-wrap: wrap; }
        .brandname { font-size: 1rem; }
        .herologo { width: 64px; height: 64px; }
        .herotitle { font-size: 1.6rem; }
        .stColumn:has(.qtag) { padding: 0.8rem; }
        .block-container p, .block-container li {
            font-size: 0.95rem;
            line-height: 1.55;
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


@st.cache_data(show_spinner=False)
def _logo_data_uri() -> str:
    """The local logo as a downscaled inline data URI.

    Streamlit cannot serve a repo-relative image from inside an HTML block, and
    the source PNG is ~400 KB, so the image is thumbnailed before it is base64
    encoded. A missing asset returns an empty string and the caller falls back
    to a text mark rather than taking the page down.
    """
    if not LOGO_PATH.exists():
        return ""
    try:
        from io import BytesIO

        from PIL import Image

        image = Image.open(LOGO_PATH).convert("RGBA")
        image.thumbnail((160, 160))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:  # noqa: BLE001 - the logo must never break the page
        return ""


def header() -> None:
    """MoneyChat wordmark and subtitle, with the live RAG status on the right."""
    logo = _logo_data_uri()
    if logo:
        logo_html = f'<img class="brandlogo" src="{logo}" alt="MoneyChat logo">'
    else:
        logo_html = '<div class="brandlogo logofallback">M</div>'
    st.markdown(
        f'<div class="appheader">'
        f'<div class="appheader-brand">{logo_html}'
        f'<div><div class="brandname">{escape(APP_TITLE)}</div>'
        f'<div class="brandsub">{escape(BRAND_SUBTITLE)}</div></div></div>'
        f'<span class="statuspill"><span class="dot"></span>'
        f"{escape(_kb_status())}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )


def hero() -> None:
    """Centered brand lockup that introduces the assistant."""
    logo = _logo_data_uri()
    logo_html = f'<img class="herologo" src="{logo}" alt="MoneyChat logo">' if logo else ""
    st.markdown(
        f'<div class="hero">{logo_html}'
        f'<div class="herotitle">{escape(APP_TITLE)}</div>'
        f'<div class="herosub">{escape(BRAND_SUBTITLE)}</div>'
        f'<div class="herointro">{escape(WELCOME)}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )


def fact_banner() -> None:
    """The red-bordered FACTS, NOT FORECASTS compliance banner."""
    st.markdown(
        f'<div class="factbanner">'
        f'<div class="facticon">!</div>'
        f"<div>"
        f'<div class="facttitle">{escape(HERO_KICKER)}</div>'
        f'<p class="facttext">{escape(BANNER_TEXT)}</p>'
        f"</div></div>",
        unsafe_allow_html=True,
    )


def _sync_badge(message: dict) -> str:
    """The demo badge: real provenance date, or a bare label when absent."""
    date = (message.get("last_updated") or "").strip()
    return f"Verified Sync \u00b7 {date}" if date else "Verified Sync"


def demonstrated_query(question: str, message: dict) -> None:
    """Render the demonstrated grounded query above a grounded answer.

    Called only from render_assistant's non-refusal branch, when the answer was
    grounded in two or more scheme pages. Explicit "vs"/"compare" questions are
    refused by src/guardrails.py before they reach the answer path, so this never
    manufactures a comparison - it only labels a query the chain really grounded.
    """
    st.markdown(
        f'<div class="demoquery">'
        f'<div class="demohead"><span class="demodot"></span>'
        f'<span class="demotitle">{escape(DEMO_QUERY_TITLE)}</span>'
        f'<span class="demobadge">{escape(_sync_badge(message))}</span></div>'
        f'<div class="demoq"><span class="demoqmark">Q</span>'
        f'<span class="demoqtext">{escape(question)}</span></div>'
        f"</div>",
        unsafe_allow_html=True,
    )


def question_cards() -> None:
    """The starting questions, each a card with a small category tag.

    Real st.button elements, not HTML. A button is the click target Streamlit
    already wires up for us, including keyboard activation and the widget state
    that survives a rerun; hand-rolling a div would mean reimplementing that and
    getting the accessibility behaviour wrong. The category tag sits above the
    button inside the column, and the CSS cards the column around both.
    """
    st.markdown('<div class="sectionlabel">Suggested questions</div>', unsafe_allow_html=True)
    columns = st.columns(len(EXAMPLE_QUESTIONS), gap="small")
    for index, (column, example) in enumerate(zip(columns, EXAMPLE_QUESTIONS)):
        tag = EXAMPLE_CATEGORIES[index] if index < len(EXAMPLE_CATEGORIES) else "Fact"
        with column:
            st.markdown(f'<span class="qtag">{escape(tag)}</span>', unsafe_allow_html=True)
            # width="stretch" replaces use_container_width, deprecated in
            # Streamlit 1.49+. The label is a full question, so the button must
            # wrap rather than ellipsise - the CSS above does that. Columns
            # collapse to one on narrow viewports, which is the mobile case.
            if st.button(example, key=f"example-{example[:24]}", width="stretch"):
                respond(example)


def sidebar() -> None:
    with st.sidebar:
        st.markdown(
            f'<div class="sbbrand">{escape(APP_TITLE)}</div>'
            f'<div class="sbsub">{escape(BRAND_SUBTITLE)}</div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div class="sbsection">Supported schemes</div>', unsafe_allow_html=True)
        # Names only: the five allowlisted schemes, no counts or category tags.
        for source in SOURCES:
            st.markdown(f"[{source.scheme_name}]({source.url})")

        st.markdown('<div class="sbsection">Disclaimer</div>', unsafe_allow_html=True)
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


def render_assistant(message: dict, question: str = "") -> None:
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
        # A grounded answer that spans two or more scheme pages is the live
        # equivalent of the design's comparison example. Refusals never reach
        # here, and an explicit "vs"/"compare" question is refused upstream, so
        # this only ever labels a query the chain actually grounded.
        if question and len(schemes) >= 2:
            demonstrated_query(question, message)
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
fact_banner()

if "messages" not in st.session_state:
    st.session_state.messages = []

if st.session_state.messages:
    st.markdown('<div class="sectionlabel">Conversation</div>', unsafe_allow_html=True)
    # Track the user turn as we walk the transcript so each assistant answer can
    # show the question that produced it. The transcript alternates user/assistant,
    # so the last user message is the one this answer belongs to.
    question = ""
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            if message["role"] == "user":
                question = message["content"]
                st.markdown(message["content"])
            else:
                render_assistant(message, question)
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
