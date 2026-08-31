"""Evaluator Streamlit UI — Stitch chrome over the existing discovery tagger.

Does not collect, retag the corpus, or write documents / doc_tags / quantify.
Sample cards read frozen tagger output bundled below. Live mode wraps
``TaggingClient.tag_batch`` for a single pasted (or uploaded) text string.

Visual language follows the Stitch screens in
``stitch_ajio_intelligence_engine`` (Test the Engine, results, methodology,
revised overview). Placeholder mock KPIs from those HTML files are not shown.
"""

from __future__ import annotations

import json
import sys
from html import escape
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from app.data import (
    APPENDIX_NAME,
    OVERVIEW_SUMMARY_NAME,
    SCORES_NAME,
    SEGMENTS_NAME,
    default_paths,
    load_evidence_appendix,
    load_opportunity_scores,
    load_overview_summary,
    load_segment_matrix,
    pretty_label,
    source_display_name,
)
from src.tag.taxonomy import MULTI_LABEL_DIMENSIONS

PROMPT_PATH = ROOT / "src" / "tag" / "prompts" / "tagging_v1.md"

PAGES = (
    ("overview", "dashboard", "Overview"),
    ("test", "rocket_launch", "Test the Engine"),
    ("map", "map", "Opportunity Map"),
    ("evidence", "find_in_page", "Evidence Explorer"),
    ("segments", "pie_chart", "Segments"),
    ("methodology", "settings_accessibility", "Methodology"),
)

INTENT_META = {
    "genuine_intent": ("Genuine Intent", "verified", "#107C10"),
    "bookmark_only": ("Bookmark Only", "bookmark", "#0067B8"),
    "ambiguous": ("Ambiguous", "help", "#636262"),
}

DIM_META = {
    "blocker_type": ("Purchase Blockers", "block", "blockers"),
    "uncertainty_type": ("Unresolved Uncertainties", "help", "uncertainties"),
    "wishlist_motivation": ("Wishlist Motivations", "favorite", "wishlist"),
    "info_sought_elsewhere": ("Info Sought Elsewhere", "travel_explore", "info"),
    "segment_cue": ("Segment Cues", "person_search", "segment"),
}

SAMPLE_ICONS = {
    "return_complaint": "keyboard_return",
    "fit_size_question": "straighten",
    "wishlist_bookmark": "bookmark",
}

SOURCE_OPTIONS = (
    "play_store",
    "app_store",
    "youtube",
    "quora_manual",
    "reddit",
    "consumer_complaints_in",
)

# Frozen engine output from data/interim/discovery.db doc_tags (read-only snapshot).
DEMO_EXAMPLES: list[dict] = [
    {
        "id": "return_complaint",
        "title": "Return Friction",
        "blurb": (
            "I ordered 2 items and initiated returns… QC failed and a re-pickup "
            "has been initiated. Customer care is not responding."
        ),
        "source": "play_store",
        "doc_id": "8cb290a270922f1c",
        "text": (
            "I ordered 2 items and initiated returns,but mistakenly one product was "
            "replaced with the other.I don't understand how the QC was done and they "
            "refunded me for one item. Yesterday,the other item was picked up,but now "
            "it's showing that the quality check failed and a re-pickup has been "
            "initiated.I don't have any product with me.Customer care is not responding "
            "since 2 days and there is no real human intervention.Also mailed the "
            "customergrievance team plz slove ASAP."
        ),
        "result": {
            "is_relevant": True,
            "wishlist_motivation": [],
            "blocker_type": ["return_friction", "quality_doubt", "delivery_uncertainty"],
            "uncertainty_type": ["can_i_return", "is_quality_worth_it"],
            "info_sought_elsewhere": [],
            "segment_cue": [],
            "intent_class": "ambiguous",
            "outcome_mentioned": "still_deciding",
            "severity": 4,
            "actionability_non_monetary": 1,
            "confidence_pct": 80,
            "evidence": [
                {"tag": "return_friction", "quote": "customer care is not responding since 2 days"},
                {"tag": "quality_doubt", "quote": "I don't understand how the QC was done"},
                {
                    "tag": "delivery_uncertainty",
                    "quote": "now it's showing that the quality check failed and a re-pickup has been initiated",
                },
                {"tag": "can_i_return", "quote": "I was forced to return the order"},
                {"tag": "is_quality_worth_it", "quote": "I don't understand how the QC was done"},
            ],
        },
    },
    {
        "id": "fit_size_question",
        "title": "Fit & Size Uncertainty",
        "blurb": (
            "Merko bhai shoe size 7 aata hai agar mai 7.5 mangau to kya jada "
            "difference rahega…"
        ),
        "source": "youtube",
        "doc_id": "79ecf661760429ee",
        "text": (
            "Merko bhai shoe size 7 aata hai agar mai 7.5 mangau to kya jada "
            "difference rahega yaa fir pata bhi nahi chalega shoe pehenai k baad"
        ),
        "result": {
            "is_relevant": True,
            "wishlist_motivation": [],
            "blocker_type": ["fit_size_uncertainty"],
            "uncertainty_type": ["will_it_fit"],
            "info_sought_elsewhere": [],
            "segment_cue": [],
            "intent_class": "genuine_intent",
            "outcome_mentioned": "not_stated",
            "severity": 3,
            "actionability_non_monetary": 1,
            "confidence_pct": 80,
            "evidence": [
                {
                    "tag": "fit_size_uncertainty",
                    "quote": "shoe size 7 aata hai agar mai 7.5 mangau to kya jada difference rahega",
                },
                {
                    "tag": "will_it_fit",
                    "quote": "kya jada difference rahega yaa fir pata bhi nahi chalega shoe pehenai k baad",
                },
            ],
        },
    },
    {
        "id": "wishlist_bookmark",
        "title": "Wishlist as Bookmark",
        "blurb": (
            "It's a good way to \"bookmark\" something you might like to purchase "
            "in the future. Also, it allows you to \"sleep on it\"…"
        ),
        "source": "quora_manual",
        "doc_id": "cb9fd6691717983c",
        "text": (
            "It's a good way to \"bookmark\" something you might like to purchase in "
            "the future. Also, it allows you to \"sleep on it\" and come back the next "
            "day (or whenever) and ask yourself do you really need it, or even want it? "
            "I've come back to my wish list more than once and looked at something "
            "there and thought, \"Now what on earth did I put that there for?\""
        ),
        "result": {
            "is_relevant": True,
            "wishlist_motivation": ["decide_later"],
            "blocker_type": [],
            "uncertainty_type": [],
            "info_sought_elsewhere": [],
            "segment_cue": [],
            "intent_class": "bookmark_only",
            "outcome_mentioned": "still_deciding",
            "severity": 1,
            "actionability_non_monetary": 0,
            "confidence_pct": 80,
            "evidence": [
                {
                    "tag": "decide_later",
                    "quote": "It's a good way to \"bookmark\" something you might like to purchase in the future",
                }
            ],
        },
    },
]


CHROME_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@500&family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0&display=swap');

:root {
  --bg: #F8F9FA;
  --surface: #ffffff;
  --on: #271815;
  --secondary: #5c5f60;
  --primary: #730000;
  --primary-container: #9e0000;
  --on-primary: #ffffff;
  --outline: #906f6a;
  --ghost: #E5E7EB;
  --outline-variant: #e4beb8;
  --container: #fff0ee;
  --container-high: #ffe2dd;
  --tertiary: #444c62;
  --tertiary-fixed: #dae2fd;
  --error: #ba1a1a;
  --error-container: #ffdad6;
  --blockers: #BA1A1A;
  --uncertainties: #5C647B;
  --intent-genuine: #107C10;
  --intent-bookmark: #0067B8;
  --intent-ambiguous: #636262;
  --sidebar: 260px;
}

[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
#MainMenu, footer, .stDeployButton,
.stAppDeployButton { display: none !important; }

/* stExpandSidebarButton is a child of stToolbar. Hiding the toolbar
   (display:none) also hides the expand control — do not hide stToolbar. */
header[data-testid="stHeader"] {
  background: transparent !important;
  border: none !important;
}
[data-testid="stToolbar"] {
  background: transparent !important;
  border: none !important;
}
header[data-testid="stHeader"]:has([data-testid="stExpandSidebarButton"]) {
  min-height: 3.5rem !important;
}

[data-testid="stExpandSidebarButton"] {
  display: inline-flex !important;
  visibility: visible !important;
  opacity: 1 !important;
  z-index: 1000000 !important;
  background: #fff8f6 !important;
  color: #730000 !important;
  border: 1px solid #e4beb8 !important;
  border-radius: 8px !important;
  width: 2.5rem !important;
  height: 2.5rem !important;
  box-shadow: 0 2px 10px rgba(0,0,0,0.06) !important;
}

.stApp, [data-testid="stAppViewContainer"], [data-testid="stAppViewContainer"] > .main {
  background: var(--bg) !important;
  color: var(--on);
}
.block-container {
  padding-top: 1.25rem !important;
  padding-bottom: 3rem !important;
  max-width: 1440px !important;
}
iframe[height="0"] {
  display: none !important;
  height: 0 !important;
}
[data-testid="stSidebar"] {
  background: #fff8f6 !important;
  border-right: 1px solid var(--outline-variant);
}
[data-testid="stSidebar"] button {
  justify-content: flex-start !important;
  font-family: Inter, system-ui, sans-serif !important;
  border-radius: 8px !important;
}
[data-testid="stSidebar"] button[kind="primary"] {
  background: var(--container) !important;
  color: var(--primary) !important;
  border-right: 2px solid var(--primary) !important;
  font-weight: 700 !important;
}
[data-testid="stSidebar"] button[kind="secondary"] {
  background: transparent !important;
  color: var(--secondary) !important;
  border: none !important;
}
[data-testid="stVerticalBlock"] { gap: 0.5rem !important; }

.stTextArea textarea {
  font-family: Inter, system-ui, sans-serif !important;
  font-size: 16px !important;
  line-height: 24px !important;
  border: 1px solid var(--outline-variant) !important;
  border-radius: 4px !important;
  background: #fff8f6 !important;
}
.stTextArea textarea:focus {
  border-color: var(--primary) !important;
  box-shadow: 0 0 0 1px var(--primary) !important;
}
.stSelectbox [data-baseweb="select"] > div {
  border-color: var(--outline-variant) !important;
  background: #fff8f6 !important;
}

.ad { font-family: Inter, system-ui, sans-serif; color: var(--on); }
.ad-kicker {
  font-family: "JetBrains Mono", monospace; font-size: 12px; letter-spacing: 0.05em;
  text-transform: uppercase; color: var(--secondary); margin: 0 0 8px;
}
.ad-display { font-size: 48px; line-height: 56px; font-weight: 700; letter-spacing: -0.02em; margin: 0; }
.ad-h { font-size: 24px; font-weight: 600; letter-spacing: -0.01em; margin: 0 0 8px; }
.ad-hsm { font-size: 18px; font-weight: 600; margin: 0 0 8px; }
.ad-sub { font-size: 16px; line-height: 24px; color: var(--secondary); margin: 8px 0 0; max-width: 48rem; }
.ad-muted { color: var(--secondary); font-size: 14px; }
.ad-card {
  background: var(--surface); border: 1px solid var(--outline-variant); border-radius: 12px;
  padding: 24px; box-shadow: 0 2px 10px rgba(0,0,0,0.02);
}
.ad-card:hover { box-shadow: 0 4px 20px rgba(0,0,0,0.05); }
.ad-grid-3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 24px; margin-bottom: 24px; }
.ad-grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
.ad-grid-bento { display: grid; grid-template-columns: 2fr 1fr; gap: 24px; }
.ad-grid-4 { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 24px; }
.ad-lbl {
  font-family: "JetBrains Mono", monospace; font-size: 12px; letter-spacing: 0.05em;
  text-transform: uppercase; color: var(--secondary);
}
.ad-num { font-size: 32px; line-height: 40px; font-weight: 700; letter-spacing: -0.02em; }
.ad-pill {
  display: inline-flex; align-items: center; padding: 2px 10px; border-radius: 9999px;
  font-family: "JetBrains Mono", monospace; font-size: 11px; letter-spacing: 0.05em; text-transform: uppercase;
  background: var(--tertiary); color: #fff;
}
.ad-chip {
  display: inline-flex; align-items: center; padding: 6px 12px; margin: 0 8px 8px 0;
  border-radius: 6px; font-size: 14px; border: 1px solid;
}
.ad-chip.blockers { background: rgba(186,26,26,0.10); color: var(--blockers); border-color: rgba(186,26,26,0.20); }
.ad-chip.uncertainties { background: rgba(92,100,123,0.10); color: var(--uncertainties); border-color: rgba(92,100,123,0.20); }
.ad-chip.wishlist { background: #fadcd7; color: #5b403c; border-color: var(--outline-variant); }
.ad-chip.info { background: #fff0ee; color: var(--secondary); border-color: var(--outline-variant); }
.ad-chip.segment { background: var(--tertiary-fixed); color: #131b2f; border-color: rgba(190,198,224,0.30); }
.ad-chip.empty { background: #f3f4f5; color: var(--secondary); border-color: var(--ghost); }
.ad-intent {
  display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px; border-radius: 9999px;
  font-size: 18px; font-weight: 700;
}
.ad-quote {
  font-size: 16px; line-height: 24px; font-style: italic; border-left: 4px solid var(--primary);
  padding: 4px 0 4px 16px; margin: 0;
}
.ad-bar-track { height: 6px; background: #ffe9e6; border-radius: 9999px; overflow: hidden; }
.ad-bar-fill { height: 6px; background: var(--tertiary); border-radius: 9999px; }
.ad-table { width: 100%; border-collapse: collapse; font-size: 14px; }
.ad-table th {
  text-align: left; font-family: "JetBrains Mono", monospace; font-size: 12px; letter-spacing: 0.05em;
  text-transform: uppercase; color: var(--secondary); padding: 16px; border-bottom: 1px solid var(--outline-variant);
}
.ad-table td { padding: 16px; border-bottom: 1px solid var(--outline-variant); }
.ad-funnel-row { display: flex; align-items: center; margin-bottom: 16px; }
.ad-funnel-lbl { width: 140px; text-align: right; padding-right: 16px; font-size: 14px; color: var(--secondary); flex-shrink: 0; }
.ad-funnel-bar {
  height: 48px; border-radius: 0 8px 8px 0; border: 1px solid var(--outline-variant);
  display: flex; align-items: center; padding: 0 16px; position: relative; min-width: 72px;
}
.ad-pipe { display: flex; gap: 12px; overflow-x: auto; padding: 8px 0 16px; }
.ad-pipe-step {
  width: 128px; height: 96px; flex-shrink: 0; background: #fff; border: 1px solid var(--outline-variant);
  border-radius: 8px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px;
}
.ad-pipe-step.accent { border-color: #ffb4a8; background: #ffe9e6; }
.ad-pipe-step.end { background: var(--tertiary); color: #fff; border: none; }
.ad-principle {
  background: #fff; border: 1px solid var(--outline-variant); border-radius: 12px; padding: 24px;
}
.ad-json {
  background: #1e1e1e; color: #d4d4d4; font-family: "JetBrains Mono", monospace; font-size: 13px;
  border-radius: 0 0 12px 12px; padding: 16px; overflow-x: auto; white-space: pre-wrap;
}
.ad-ev { background: #fff; border: 1px solid var(--outline-variant); border-radius: 12px; padding: 24px; margin-bottom: 12px; position: relative; }
.material-symbols-outlined { font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24; font-size: 20px; vertical-align: middle; }
@media (max-width: 960px) {
  .ad-display { font-size: 32px; line-height: 40px; }
  .ad-grid-3, .ad-grid-2, .ad-grid-bento, .ad-grid-4 { grid-template-columns: 1fr; }
}
</style>
"""

_OPEN_NAV_HTML = """
<script>
(function () {
  const doc = window.parent.document;
  function sidebarCollapsed() {
    const sb = doc.querySelector('[data-testid="stSidebar"]');
    if (!sb) return false;
    if (sb.getAttribute("aria-expanded") === "false") return true;
    return sb.getBoundingClientRect().width < 48;
  }
  function nativeExpand() {
    return doc.querySelector('[data-testid="stExpandSidebarButton"]');
  }
  function tick() {
    const collapsed = sidebarCollapsed();
    const native = nativeExpand();
    if (native) {
      native.style.setProperty("display", "inline-flex", "important");
      native.style.setProperty("visibility", "visible", "important");
      native.style.setProperty("opacity", "1", "important");
      const toolbar = native.closest('[data-testid="stToolbar"]');
      if (toolbar) {
        toolbar.style.setProperty("display", "flex", "important");
        toolbar.style.setProperty("visibility", "visible", "important");
      }
      const header = native.closest('[data-testid="stHeader"]');
      if (header) {
        header.style.setProperty("display", "flex", "important");
        header.style.setProperty("visibility", "visible", "important");
      }
    }
    let fab = doc.getElementById("ad-open-nav");
    const nativeVisible = !!(native && native.getBoundingClientRect().width > 0);
    if (!collapsed || nativeVisible) {
      if (fab) fab.style.display = "none";
      return;
    }
    if (!fab) {
      fab = doc.createElement("button");
      fab.id = "ad-open-nav";
      fab.type = "button";
      fab.setAttribute("aria-label", "Open navigation");
      fab.textContent = "\\u2630";
      Object.assign(fab.style, {
        position: "fixed",
        top: "12px",
        left: "12px",
        zIndex: "2147483647",
        width: "40px",
        height: "40px",
        borderRadius: "8px",
        border: "1px solid #e4beb8",
        background: "#fff8f6",
        color: "#730000",
        fontSize: "20px",
        cursor: "pointer",
        boxShadow: "0 2px 10px rgba(0,0,0,0.08)",
      });
      fab.onclick = function () {
        const b = nativeExpand();
        if (b) b.click();
      };
      doc.body.appendChild(fab);
    }
    fab.style.display = "flex";
    fab.style.alignItems = "center";
    fab.style.justifyContent = "center";
  }
  setInterval(tick, 250);
})();
</script>
"""


def _keep_sidebar_expandable() -> None:
    """Toolbar hosts Streamlit's expand control; this restores it if CSS hid a parent."""
    components.html(_OPEN_NAV_HTML, height=0, width=0)


def groq_key_from_secrets() -> str | None:
    """GROQ_API_KEY from Streamlit secrets only. Never reads the repo ``.env``."""
    try:
        secrets = st.secrets
    except Exception:
        return None
    try:
        value = secrets.get("GROQ_API_KEY")
    except Exception:
        return None
    if value is None:
        return None
    token = str(value).strip()
    return token or None


def tag_one_text(text: str, *, api_key: str):
    """Smallest reusable wrap of the batch tagger for one pasted string."""
    from pydantic import SecretStr

    from src.common.config import load_run_config
    from src.tag.llm_client import TaggingClient

    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("Paste a review, comment, or product question first.")

    run, _raw = load_run_config()
    settings = SimpleNamespace(
        run=run,
        credentials=SimpleNamespace(groq_api_key=SecretStr(api_key)),
    )
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    client = TaggingClient(settings=settings)
    tagged, _usage = client.tag_batch(prompt, [{"doc_id": "live-eval", "text": cleaned}])
    if not tagged:
        raise RuntimeError(
            "The tagger returned no usable coding for this text. Try a longer "
            "fashion-shopping review or question."
        )
    return tagged[0]


def tagged_to_display(tagged) -> dict:
    if isinstance(tagged, dict):
        return tagged
    payload = tagged.model_dump(mode="json")
    payload.pop("doc_id", None)
    return payload


def friendly_live_error(exc: BaseException) -> str:
    name = type(exc).__name__
    message = str(exc).lower()
    if name in {"DailyLimitReached"} or "rate" in message or "429" in message:
        return (
            "The tagging API is rate-limited right now. Wait a minute and try again, "
            "or stay on Try Sample Feedback (no API call)."
        )
    if "timeout" in name.lower() or "timeout" in message:
        return "The tagging request timed out. Check the network and try a shorter text."
    if name in {"TaggingFailedError", "SchemaNonCompliantError"}:
        return f"The tagger could not code this text: {exc}"
    if name in {"ValueError"}:
        return str(exc)
    return f"Tagging failed ({name}). Sample feedback still works without a key."


def first_text_from_jsonl(raw: bytes) -> str:
    """Take the first usable text field from a JSONL upload. No tagging here."""
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("The file is not UTF-8 text.") from exc
    for line in decoded.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("Each line must be a JSON object.") from exc
        if not isinstance(obj, dict):
            continue
        for key in ("text", "body", "review", "comment", "content"):
            value = obj.get(key)
            if value:
                return str(value).strip()
    raise ValueError("No text / body / review / comment / content field found in the file.")


def _q() -> dict[str, str]:
    return {key: str(value) for key, value in st.query_params.items() if value is not None}


def _goto(**updates) -> None:
    params = _q()
    nxt = dict(params)
    for key, value in updates.items():
        if value is None:
            nxt.pop(key, None)
        else:
            nxt[key] = str(value)
    if nxt == params:
        return
    st.query_params.from_dict(nxt)
    st.rerun()


@st.cache_data(show_spinner=False)
def _scores_cached(path: str) -> pd.DataFrame:
    return load_opportunity_scores(Path(path))


@st.cache_data(show_spinner=False)
def _segments_cached(path: str) -> pd.DataFrame:
    return load_segment_matrix(Path(path))


@st.cache_data(show_spinner=False)
def _appendix_cached(path: str):
    return load_evidence_appendix(Path(path))


@st.cache_data(show_spinner=False)
def _overview_cached(path: str) -> dict:
    return load_overview_summary(Path(path))


def _example(example_id: str) -> dict:
    return next(ex for ex in DEMO_EXAMPLES if ex["id"] == example_id)


def _clear_test() -> None:
    st.session_state.pop("test_result", None)
    st.session_state.pop("test_meta", None)
    st.session_state.pop("live_error", None)
    st.session_state["live_text"] = ""


def _show_sample(example_id: str) -> None:
    example = _example(example_id)
    st.session_state["test_result"] = tagged_to_display(example["result"])
    st.session_state["test_meta"] = {
        "source": example["source"],
        "text": example["text"],
        "mode": "demo",
        "title": example["title"],
        "doc_id": example["doc_id"],
    }
    st.session_state.pop("live_error", None)
    _goto(page="test")


def _render_sidebar(*, page: str, corpus_ok: bool) -> None:
    with st.sidebar:
        st.markdown(
            '<div class="ad">'
            '<p style="font-family:Inter,sans-serif;font-size:18px;font-weight:700;'
            'color:#730000;letter-spacing:-0.01em;margin:0">AJIO Discovery</p>'
            '<p style="font-family:JetBrains Mono,monospace;font-size:12px;letter-spacing:0.05em;'
            'text-transform:uppercase;color:#5c5f60;margin:4px 0 20px">Engine v2.4</p>'
            "</div>",
            unsafe_allow_html=True,
        )
        if st.button("Test the Engine", type="primary", width="stretch", key="cta_test"):
            _clear_test()
            _goto(page="test")
        st.divider()
        for key, _icon, label in PAGES:
            active = page == key
            if st.button(label, type="primary" if active else "secondary", width="stretch", key=f"nav_{key}"):
                _goto(page=key)
        st.caption("v1.0")
        st.caption("Corpus loaded" if corpus_ok else "Corpus missing")


def _chips(values: list, kind: str) -> str:
    if not values:
        return '<span class="ad-chip empty">None on this coding</span>'
    return "".join(
        f'<span class="ad-chip {escape(kind)}">{escape(pretty_label(str(value)))} '
        f'<span class="ad-muted">({escape(str(value))})</span></span>'
        for value in values
    )


def render_result_html(result: dict, meta: dict) -> str:
    intent_key = result.get("intent_class") or "ambiguous"
    intent_label, intent_icon, intent_color = INTENT_META.get(
        intent_key, (pretty_label(intent_key), "help", "#636262")
    )
    source = source_display_name(meta.get("source") or "unknown")
    confidence = result.get("confidence_pct")
    confidence_bit = (
        f'<span class="ad-muted">Match confidence: {int(confidence)}%</span>'
        if confidence is not None
        else ""
    )
    mode = "Sample (no API call)" if meta.get("mode") == "demo" else "Live tagger"
    extras = []
    if result.get("outcome_mentioned"):
        extras.append(f"outcome {pretty_label(result['outcome_mentioned'])}")
    if result.get("severity") is not None:
        extras.append(f"severity {result['severity']}")
    extra_line = " · ".join(extras)

    cards = []
    for name, _enum in MULTI_LABEL_DIMENSIONS:
        title, icon, kind = DIM_META[name]
        values = result.get(name) or []
        cards.append(
            f'<div class="ad-card">'
            f'<div class="ad-kicker" style="display:flex;align-items:center;gap:8px;'
            f'border-bottom:1px solid var(--outline-variant);padding-bottom:8px;margin-bottom:12px">'
            f'<span class="material-symbols-outlined">{icon}</span>{escape(title)}</div>'
            f'<div>{_chips(values, kind)}</div></div>'
        )

    evidence = result.get("evidence") or []
    quotes = []
    for span in evidence:
        quotes.append(
            f'<div class="ad-ev">'
            f'<div class="ad-kicker" style="margin-bottom:8px">{escape(pretty_label(span.get("tag", "")))} '
            f'<span class="ad-muted">({escape(str(span.get("tag", "")))})</span></div>'
            f'<p class="ad-quote">“{escape(span.get("quote", ""))}”</p></div>'
        )
    evidence_block = (
        "".join(quotes)
        if quotes
        else '<p class="ad-muted">No evidence spans on this coding.</p>'
    )

    payload = {
        "doc_id": meta.get("doc_id") or "live-eval",
        "intent_class": result.get("intent_class"),
        "confidence_pct": result.get("confidence_pct"),
        "source": meta.get("source"),
        "taxonomies": {name: result.get(name) or [] for name, _ in MULTI_LABEL_DIMENSIONS},
        "evidence": result.get("evidence") or [],
    }
    json_block = escape(json.dumps(payload, indent=2, ensure_ascii=False))

    return f"""
<div class="ad">
  <div class="ad-card" style="margin-bottom:24px">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:24px">
      <div>
        <p class="ad-kicker">Analysis complete</p>
        <h3 class="ad-h">Input processed</h3>
        <p class="ad-muted">{escape(mode)} · Source: {escape(source)}</p>
        <p class="ad-muted">{escape(extra_line)}</p>
      </div>
      <div style="text-align:right">
        <p class="ad-kicker">Primary intent</p>
        <div class="ad-intent" style="background:{intent_color}1a;color:{intent_color};border:1px solid {intent_color}33">
          <span class="material-symbols-outlined">{intent_icon}</span>{escape(intent_label)}
        </div>
      </div>
    </div>
  </div>
  <div class="ad-grid-2" style="margin-bottom:24px">{"".join(cards)}</div>
  <h3 class="ad-hsm">Evidence behind the analysis</h3>
  <div class="ad-card" style="margin-bottom:16px">
    <div style="display:flex;gap:12px;align-items:center;margin-bottom:12px">
      <span class="ad-pill" style="background:#fadcd7;color:#5b403c">Source: {escape(source)}</span>
      {confidence_bit}
    </div>
    {evidence_block}
  </div>
  <details class="ad-card" style="padding:0;overflow:hidden">
    <summary style="padding:16px 24px;cursor:pointer;font-size:18px;font-weight:600">View structured JSON</summary>
    <pre class="ad-json"><code>{json_block}</code></pre>
  </details>
</div>
"""


def overview_html(*, facts: dict, scores: pd.DataFrame) -> str:
    documents = int(facts.get("documents") or 0)
    analyzable = int(facts.get("analyzable") or 0)
    tagged = int(facts.get("tagged") or 0)
    genuine = int(facts.get("genuine_intent") or 0)

    intent_counts = facts.get("intent_class") or {}
    bookmark = int(intent_counts.get("bookmark_only") or 0)
    ambiguous = int(intent_counts.get("ambiguous") or 0)
    genuine_from_docs = int(intent_counts.get("genuine_intent") or genuine)
    intent_total = max(genuine_from_docs + bookmark + ambiguous, 1)

    source_counts = facts.get("sources") or {}
    source_total = sum(int(count) for count in source_counts.values()) or 1
    source_rows = []
    for source, count in list(source_counts.items())[:4]:
        pct = 100 * int(count) / source_total
        source_rows.append(
            f'<div style="margin-bottom:12px">'
            f'<div style="display:flex;justify-content:space-between;font-size:14px;margin-bottom:4px">'
            f"<span>{escape(source_display_name(str(source)))}</span><span>{int(count)} · {pct:.0f}%</span></div>"
            f'<div class="ad-bar-track"><div class="ad-bar-fill" style="width:{pct:.1f}%"></div></div></div>'
        )
    source_block = "".join(source_rows) or '<p class="ad-muted">No tagged documents in this checkout.</p>'

    def _bar(label: str, count: int, color: str, width_pct: float) -> str:
        return (
            f'<div class="ad-funnel-row"><div class="ad-funnel-lbl">{escape(label)}</div>'
            f'<div style="flex:1"><div class="ad-funnel-bar" style="width:{max(width_pct, 12):.0f}%;'
            f'background:{color}">{count:,}</div></div></div>'
        )

    denom = max(documents, 1)
    funnel = (
        _bar("Collected", documents, "#ffe2dd", 100)
        + _bar("Analyzable", analyzable, "#fff0ee", 100 * analyzable / denom)
        + _bar("Tagged", tagged, "#fadcd7", 100 * tagged / denom)
        + _bar("Genuine intent", genuine, "#d4edda", 100 * genuine / denom)
    )

    def _ibar(count: int, color: str) -> str:
        height = 8 + 40 * (count / intent_total)
        return (
            f'<div style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center">'
            f'<div style="width:100%;height:{height:.0f}px;background:{color};border-radius:2px 2px 0 0"></div>'
            f'<div class="ad-lbl" style="margin-top:6px">{count}</div></div>'
        )

    intent_bars = (
        '<div style="display:flex;align-items:flex-end;gap:16px;height:72px">'
        + _ibar(genuine_from_docs, "#107C10")
        + _ibar(bookmark, "#0067B8")
        + _ibar(ambiguous, "#636262")
        + "</div>"
        + '<div style="display:flex;justify-content:space-between;margin-top:4px" class="ad-lbl">'
        + "<span>Genuine</span><span>Bookmark</span><span>Ambiguous</span></div>"
    )

    table_rows = []
    if not scores.empty:
        view = scores.copy()
        if "opportunity_score" in view.columns:
            view = view.sort_values("opportunity_score", ascending=False)
        for i, row in enumerate(view.head(8).itertuples(index=False), start=1):
            mapping = row._asdict() if hasattr(row, "_asdict") else row._asdict()
            label = pretty_label(str(mapping.get("label", "")))
            dimension = pretty_label(str(mapping.get("dimension", "")))
            n_docs = mapping.get("n_docs")
            score = mapping.get("opportunity_score")
            n_txt = "—" if n_docs is None or pd.isna(n_docs) else f"{int(n_docs)} docs"
            score_txt = "—" if score is None or pd.isna(score) else f"{float(score):.2f}"
            table_rows.append(
                f"<tr><td class='ad-muted'>{i}</td>"
                f"<td><div style='font-weight:600'>{escape(label)}</div>"
                f"<div class='ad-muted'>{escape(dimension)}</div></td>"
                f"<td>{escape(n_txt)}</td><td style='text-align:right;font-weight:700;color:var(--primary)'>"
                f"{escape(score_txt)}</td></tr>"
            )
    table_body = "".join(table_rows) or "<tr><td colspan='4' class='ad-muted'>No opportunity_scores.csv in this checkout.</td></tr>"

    insights = [
        (
            "The engine codes conversations, not checkout events.",
            "Intent class is a tagger label (genuine / bookmark / ambiguous), not a conversion rate.",
        )
    ]
    if tagged:
        share = 100 * genuine / tagged
        insights.append(
            (
                f"{genuine:,} of {tagged:,} tagged documents are genuine_intent ({share:.0f}%).",
                "Bookmark-only and ambiguous remain in the full-corpus rank; they are not dropped.",
            )
        )
    if not scores.empty and "label" in scores.columns:
        top = scores.sort_values("opportunity_score", ascending=False).iloc[0]
        insights.append(
            (
                f"Highest-scoring theme in this run: {pretty_label(str(top['label']))}.",
                f"{int(top['n_docs'])} supporting documents · score {float(top['opportunity_score']):.2f}.",
            )
        )
    insight_html = "".join(
        f'<div style="display:flex;gap:12px;margin-bottom:16px">'
        f'<div style="width:6px;height:6px;border-radius:9999px;background:var(--primary);margin-top:8px;flex-shrink:0"></div>'
        f"<div><p class='ad-hsm' style='font-size:14px'>{escape(title)}</p>"
        f"<p class='ad-muted'>{escape(body)}</p></div></div>"
        for title, body in insights
    )

    return f"""
<div class="ad">
  <span class="ad-pill">Demonstration dataset</span>
  <h2 class="ad-display" style="margin-top:12px">AJIO Discovery Engine</h2>
  <p class="ad-sub">Discover what prevents wishlist intent from becoming purchase intent.</p>
  <div class="ad-grid-3" style="margin-top:32px">
    <div class="ad-card">
      <div class="ad-lbl">Conversations analysed</div>
      <div class="ad-num">{documents:,}</div>
      <p class="ad-muted" style="margin-top:8px">{analyzable:,} analyzable · {tagged:,} tagged</p>
    </div>
    <div class="ad-card">
      <div class="ad-lbl">Source distribution</div>
      {source_block}
    </div>
    <div class="ad-card">
      <div class="ad-lbl">Intent-class dist.</div>
      {intent_bars}
    </div>
  </div>
  <div class="ad-grid-bento">
    <div>
      <div class="ad-card" style="margin-bottom:24px">
        <h3 class="ad-hsm">Discovery funnel</h3>
        <p class="ad-muted" style="margin-bottom:16px">Corpus counts from this checkout — not a conversion model.</p>
        {funnel}
      </div>
      <div class="ad-card" style="padding:0;overflow:hidden">
        <div style="padding:20px 24px;border-bottom:1px solid var(--outline-variant)">
          <h3 class="ad-hsm" style="margin:0">Ranked opportunity areas</h3>
        </div>
        <table class="ad-table">
          <thead><tr><th>#</th><th>Theme</th><th>Documents</th><th style="text-align:right">Score</th></tr></thead>
          <tbody>{table_body}</tbody>
        </table>
      </div>
    </div>
    <div class="ad-card">
      <div class="ad-kicker" style="display:flex;align-items:center;gap:8px">
        <span class="material-symbols-outlined" style="color:var(--primary)">lightbulb</span>
        What the engine is telling us
      </div>
      {insight_html}
    </div>
  </div>
</div>
"""


def map_html(scores: pd.DataFrame) -> str:
    if scores.empty:
        return '<div class="ad"><h2 class="ad-display">Opportunity Map</h2><p class="ad-muted">opportunity_scores.csv was not found.</p></div>'
    view = scores.sort_values("opportunity_score", ascending=False) if "opportunity_score" in scores.columns else scores
    max_score = float(view["opportunity_score"].max() or 1) if "opportunity_score" in view.columns else 1
    rows = []
    for i, row in enumerate(view.head(16).itertuples(index=False), start=1):
        mapping = row._asdict()
        score = float(mapping.get("opportunity_score") or 0)
        width = 100 * score / max(max_score, 0.001)
        n_docs = mapping.get("n_docs")
        prev = mapping.get("prevalence")
        n_txt = "—" if n_docs is None or pd.isna(n_docs) else f"{int(n_docs)} docs"
        prev_txt = "—" if prev is None or pd.isna(prev) else f"{100 * float(prev):.1f}%"
        rows.append(
            f"<tr><td class='ad-muted'>{i}</td>"
            f"<td><div style='font-weight:600'>{escape(pretty_label(str(mapping.get('label', ''))))}</div>"
            f"<div class='ad-muted'>{escape(pretty_label(str(mapping.get('dimension', ''))))}</div>"
            f'<div class="ad-bar-track" style="margin-top:8px"><div class="ad-bar-fill" style="width:{width:.1f}%;background:#730000"></div></div></td>'
            f"<td>{escape(n_txt)}</td><td>{escape(prev_txt)}</td>"
            f"<td style='font-weight:700;color:var(--primary)'>{score:.2f}</td></tr>"
        )
    return f"""
<div class="ad">
  <h2 class="ad-display">Opportunity Map</h2>
  <p class="ad-sub">Ranked themes from Stage 4 opportunity_scores.csv — not a mock index.</p>
  <div class="ad-card" style="padding:0;overflow:hidden;margin-top:24px">
    <table class="ad-table">
      <thead><tr><th>#</th><th>Opportunity</th><th>Documents</th><th>Prevalence</th><th>Score</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </div>
</div>
"""


def evidence_html(quotes) -> str:
    if not quotes:
        return '<div class="ad"><h2 class="ad-display">Evidence Explorer</h2><p class="ad-muted">No evidence appendix in this checkout.</p></div>'
    cards = []
    for quote in quotes[:40]:
        cards.append(
            f'<div class="ad-ev">'
            f'<div class="ad-kicker" style="margin-bottom:8px">{escape(pretty_label(quote.theme))} · '
            f"{escape(source_display_name(quote.source))}</div>"
            f'<p class="ad-quote">“{escape(quote.text)}”</p>'
            f'<p class="ad-muted" style="margin-top:12px">{escape(quote.doc_id)}</p></div>'
        )
    return f"""
<div class="ad">
  <h2 class="ad-display">Evidence Explorer</h2>
  <p class="ad-sub">Verbatim quotes from the frozen evidence appendix. Each line is already PII-redacted.</p>
  <div style="margin-top:24px">{"".join(cards)}</div>
</div>
"""


def segments_html(frame: pd.DataFrame) -> str:
    if frame.empty:
        return '<div class="ad"><h2 class="ad-display">Segments</h2><p class="ad-muted">segment_matrix.csv was not found.</p></div>'
    rows = []
    show = frame.head(24)
    for row in show.itertuples(index=False):
        mapping = row._asdict()
        cells = "".join(f"<td>{escape(str(mapping.get(col, '—')))}</td>" for col in show.columns)
        rows.append(f"<tr>{cells}</tr>")
    heads = "".join(f"<th>{escape(str(col))}</th>" for col in show.columns)
    return f"""
<div class="ad">
  <h2 class="ad-display">Segments</h2>
  <p class="ad-sub">Read-only segment_matrix.csv. Lift is against the tagged corpus, not a survey weight.</p>
  <div class="ad-card" style="padding:0;overflow:auto;margin-top:24px">
    <table class="ad-table"><thead><tr>{heads}</tr></thead><tbody>{"".join(rows)}</tbody></table>
  </div>
</div>
"""


def methodology_html() -> str:
    steps = [
        ("01", "Collect", False),
        ("02", "Clean", False),
        ("03", "Validate", False),
        ("04", "Deduplicate", False),
        ("05", "Build Corpus", True),
        ("06", "Tag with AI", False),
        ("07", "Extract Evidence", False),
        ("08", "Quantify", False),
        ("09", "Rank Opportunities", False),
        ("10", "Synthesize Findings", "end"),
    ]
    pipe = []
    for num, name, flag in steps:
        cls = "ad-pipe-step"
        if flag is True:
            cls += " accent"
        elif flag == "end":
            cls += " end"
        pipe.append(f'<div class="{cls}"><span class="ad-lbl">{num}</span><span style="font-weight:600;text-align:center;padding:0 8px">{escape(name)}</span></div>')
    return f"""
<div class="ad">
  <h2 class="ad-display">Methodology</h2>
  <p class="ad-sub">How the AJIO Discovery Engine turns unstructured feedback into ranked opportunities.</p>
  <h3 class="ad-h" style="margin-top:40px">Data processing pipeline</h3>
  <div class="ad-pipe">{"".join(pipe)}</div>
  <h3 class="ad-h" style="margin-top:24px">Core principles</h3>
  <div class="ad-grid-3">
    <div class="ad-principle">
      <div class="ad-kicker">Taxonomy control</div>
      <p class="ad-muted">The tagger applies a fixed taxonomy (TAXONOMY_VERSION v1). Labels are not free text.</p>
    </div>
    <div class="ad-principle">
      <div class="ad-kicker">Intent classification</div>
      <p class="ad-muted">Purchase intent is coded separately as genuine_intent, bookmark_only, or ambiguous.</p>
    </div>
    <div class="ad-principle">
      <div class="ad-kicker">Evidence-backed</div>
      <p class="ad-muted">Every kept tag is supported by a verbatim span. Unevidenced tags are dropped.</p>
    </div>
  </div>
  <div class="ad-card" style="margin-top:24px;background:#fff0ee;display:flex;gap:16px;align-items:flex-start">
    <span class="material-symbols-outlined" style="color:var(--primary)">info</span>
    <div>
      <div class="ad-hsm">Analytical boundary</div>
      <p class="ad-muted">The system identifies patterns in tagged conversations. It does not prove causality or measure checkout conversion.</p>
    </div>
  </div>
</div>
"""


def _render_test_input(*, api_key: str | None) -> None:
    st.markdown(
        """
<div class="ad">
  <h2 class="ad-display">Test the Discovery Engine</h2>
  <p class="ad-sub">Analyse customer conversations to identify wishlist motivations, purchase barriers, unresolved uncertainties and supporting evidence.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    tabs = st.tabs(["Try Sample Feedback", "Paste Feedback", "Upload JSONL"])

    with tabs[0]:
        st.caption("Frozen tags from the existing tagger (`doc_tags`). Opening a card does not call the API.")
        cols = st.columns(3)
        for col, example in zip(cols, DEMO_EXAMPLES):
            with col:
                icon = SAMPLE_ICONS.get(example["id"], "chat")
                st.markdown(
                    f'<div class="ad-card" style="min-height:140px">'
                    f'<span class="material-symbols-outlined" style="color:var(--primary)">{icon}</span>'
                    f'<div class="ad-hsm" style="margin-top:8px">{escape(example["title"])}</div>'
                    f'<p class="ad-muted">{escape(example["blurb"])}</p></div>',
                    unsafe_allow_html=True,
                )
                if st.button("Open sample", width="stretch", key=f"sample_{example['id']}"):
                    _show_sample(example["id"])

    with tabs[1]:
        if not api_key:
            st.info(
                "Live analysis needs `GROQ_API_KEY` in Streamlit secrets. "
                "Try Sample Feedback still works with no key."
            )
        st.selectbox(
            "Source context",
            options=list(SOURCE_OPTIONS),
            format_func=source_display_name,
            key="live_source",
        )
        st.text_area(
            "Paste a fashion-shopping review, comment or conversation",
            key="live_text",
            height=200,
            placeholder="Paste a fashion-shopping review, comment or conversation here…",
        )
        left, right = st.columns([2, 1])
        with left:
            b1, b2 = st.columns(2)
            if b1.button("Load sample", width="stretch"):
                st.session_state["live_text"] = DEMO_EXAMPLES[0]["text"]
                st.session_state["live_source"] = DEMO_EXAMPLES[0]["source"]
                st.rerun()
            if b2.button("Clear", width="stretch"):
                st.session_state["live_text"] = ""
                st.rerun()
        with right:
            run = st.button("Run AI Analysis", type="primary", width="stretch", disabled=not api_key)
        if run and api_key:
            _run_live(api_key)

    with tabs[2]:
        if not api_key:
            st.info("Upload is live-only. Add `GROQ_API_KEY` under App settings → Secrets.")
        uploaded = st.file_uploader("JSONL with a text / body / review field", type=["jsonl", "json"])
        if uploaded is not None and st.button("Use first record", type="primary", disabled=not api_key):
            try:
                text = first_text_from_jsonl(uploaded.getvalue())
            except ValueError as exc:
                st.session_state["live_error"] = str(exc)
            else:
                st.session_state["live_text"] = text
                if api_key:
                    _run_live(api_key)


def _run_live(api_key: str) -> None:
    try:
        with st.spinner("Tagging…"):
            tagged = tag_one_text(st.session_state.get("live_text", ""), api_key=api_key)
        st.session_state["test_result"] = tagged_to_display(tagged)
        st.session_state["test_meta"] = {
            "source": st.session_state.get("live_source") or "play_store",
            "text": st.session_state.get("live_text", ""),
            "mode": "live",
            "doc_id": "live-eval",
        }
        st.session_state.pop("live_error", None)
    except Exception as exc:  # noqa: BLE001 — friendly message, never a traceback
        st.session_state.pop("test_result", None)
        st.session_state["live_error"] = friendly_live_error(exc)
    st.rerun()


def _render_test_results(*, api_key: str | None) -> None:
    top_l, top_r = st.columns([3, 2])
    with top_l:
        st.markdown(
            '<div class="ad"><h2 class="ad-h" style="color:var(--primary);margin:0">'
            "Test the Discovery Engine</h2></div>",
            unsafe_allow_html=True,
        )
    with top_r:
        c1, c2 = st.columns(2)
        if c1.button("Clear Test", width="stretch"):
            _clear_test()
            st.rerun()
        if c2.button("New Input", type="primary", width="stretch"):
            _clear_test()
            st.rerun()

    if st.session_state.get("live_error"):
        st.error(st.session_state["live_error"])

    result = st.session_state.get("test_result") or {}
    meta = st.session_state.get("test_meta") or {}
    st.markdown(render_result_html(result, meta), unsafe_allow_html=True)
    if meta.get("text"):
        with st.expander("Input text"):
            st.write(meta["text"])
    if not api_key and meta.get("mode") == "live":
        st.caption("This live result is from the current session only.")


def main() -> None:
    st.set_page_config(
        page_title="AJIO Discovery Engine",
        page_icon="◆",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(CHROME_CSS, unsafe_allow_html=True)
    _keep_sidebar_expandable()

    if "live_text" not in st.session_state:
        st.session_state["live_text"] = ""

    paths = default_paths()
    page = st.query_params.get("page", "test")
    if page not in {key for key, _icon, _label in PAGES}:
        page = "test"

    scores_path = paths.first_existing(SCORES_NAME)
    segments_path = paths.first_existing(SEGMENTS_NAME)
    appendix_path = paths.first_existing(APPENDIX_NAME)
    summary_path = paths.first_existing(OVERVIEW_SUMMARY_NAME)
    facts = _overview_cached(str(summary_path)) if summary_path is not None else load_overview_summary(Path())
    corpus_ok = bool(facts.get("available")) or scores_path is not None

    _render_sidebar(page=page, corpus_ok=corpus_ok)

    scores = _scores_cached(str(scores_path)) if scores_path is not None else pd.DataFrame()

    if page == "overview":
        st.markdown(overview_html(facts=facts, scores=scores), unsafe_allow_html=True)
        return

    if page == "test":
        api_key = groq_key_from_secrets()
        if st.session_state.get("test_result"):
            _render_test_results(api_key=api_key)
        else:
            if st.session_state.get("live_error"):
                st.error(st.session_state["live_error"])
            _render_test_input(api_key=api_key)
        return

    if page == "map":
        st.markdown(map_html(scores), unsafe_allow_html=True)
        if not scores.empty and {"label", "opportunity_score"}.issubset(scores.columns):
            chart = scores.sort_values("opportunity_score", ascending=False).head(10).set_index("label")[
                "opportunity_score"
            ]
            st.bar_chart(chart)
        return

    if page == "evidence":
        quotes = _appendix_cached(str(appendix_path)) if appendix_path is not None else []
        st.markdown(evidence_html(quotes), unsafe_allow_html=True)
        return

    if page == "segments":
        segments = _segments_cached(str(segments_path)) if segments_path is not None else pd.DataFrame()
        st.markdown(segments_html(segments), unsafe_allow_html=True)
        return

    if page == "methodology":
        st.markdown(methodology_html(), unsafe_allow_html=True)


if __name__ == "__main__":
    main()
