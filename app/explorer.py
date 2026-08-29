"""AJIO Discovery Explorer — Stitch UI over frozen pipeline outputs.

Launch from the project root:

    .venv\\Scripts\\python.exe -m streamlit run app/explorer.py

Does not collect, tag, or re-score. Ask is one optional Groq call over the CSV
snapshot. Visual language follows the Stitch screens (editorial light, AJIO red);
placeholder mock numbers in those HTML files are not shown.
"""

from __future__ import annotations

import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st

from app.ask import (
    MAX_QUESTION_CHARS,
    answer_question,
    ask_model_name,
    build_snapshot,
    groq_api_key,
)
from app.data import (
    APPENDIX_NAME,
    SCORES_NAME,
    SEGMENTS_NAME,
    compact_score_rows,
    corroboration_rows,
    corpus_facts,
    default_paths,
    filter_scores,
    live_quotes_for_theme,
    load_ajio_side_channel,
    load_evidence_appendix,
    load_opportunity_scores,
    load_segment_matrix,
    load_tagged_documents,
    pretty_label,
    quotes_by_theme,
    rank_view,
    source_display_name,
    source_names,
    top_source_for_row,
)
from app.ui import (
    CHROME_CSS,
    ajio_html,
    ask_answer_html,
    ask_intro_html,
    detail_html,
    evidence_html,
    map_html,
    missing_html,
    overview_html,
    qp_href,
    segments_html,
)

SAMPLE_QUESTIONS = (
    "What blocks wishlist conversion?",
    "How does genuine-intent ranking differ from the full tagged set?",
    "What do the AJIO on-site aggregates say about fit?",
    "Which themes are post-purchase only?",
)

PAGES = {
    "overview": "Overview",
    "map": "Opportunity Map",
    "evidence": "Evidence Explorer",
    "segments": "Segments",
    "ajio": "AJIO Corroboration",
    "ask": "Ask the Engine",
}


def _paths():
    return default_paths()


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
def _docs_cached(path: str) -> pd.DataFrame:
    return load_tagged_documents(Path(path))


@st.cache_data(show_spinner=False)
def _facts_cached(path: str) -> dict:
    return corpus_facts(Path(path))


@st.cache_data(show_spinner=False)
def _ajio_cached(path: str) -> dict:
    payload = dict(load_ajio_side_channel(Path(path)))
    payload.pop("aggregates", None)
    payload.pop("summary", None)
    return payload


@st.cache_resource(show_spinner=False)
def _ajio_aggregates(path: str):
    from src.store.aggregates import load_ajio_aggregates

    return load_ajio_aggregates(Path(path) / "ajio")


@st.cache_data(show_spinner=False)
def _quotes_cached(db_path: str, label: str, dimension: str, cluster: str, supporting: str):
    return live_quotes_for_theme(
        Path(db_path),
        label=label,
        dimension=dimension or None,
        cluster=cluster or None,
        supporting_doc_ids=supporting or None,
        limit=4,
    )


def _q() -> dict[str, str]:
    return {key: str(value) for key, value in st.query_params.items() if value is not None}


def _goto(**updates) -> None:
    """Write query params and rerun. ``None`` deletes a key. Streamlit widgets call this."""
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


def _fmt(value, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{float(value):.{digits}f}"


def _pct(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{100 * float(value):.1f}%"


def _priority(row) -> str:
    if bool(row.get("post_purchase_only")):
        return "Watch"
    score = float(row.get("view_score") or 0)
    if bool(row.get("reportable")) and score >= 4:
        return "Critical"
    if bool(row.get("reportable")):
        return "High"
    return "Medium"


def _evidence_volume(n) -> str:
    try:
        count = int(n)
    except (TypeError, ValueError):
        return "—"
    if count >= 40:
        return f"High · {count} docs"
    if count >= 10:
        return f"Medium · {count} docs"
    return f"Thin · {count} docs"


def _chip_row(options: list[tuple[str, str]], current: str, param: str, href) -> str:
    bits = []
    for value, label in options:
        on = (current == value) or (value == "" and not current)
        nxt = None if on or value == "" else value
        cls = "ad-chip on" if on else "ad-chip"
        bits.append(f'<a class="{cls}" href="{escape(href(**{param: nxt}), quote=True)}">{escape(label)}</a>')
    return "".join(bits)


def _render_sidebar(*, page: str, corpus_ok: bool, all_sources: list[str], source_filter: list[str]) -> None:
    with st.sidebar:
        st.markdown(
            '<p style="font-family:Inter,sans-serif;font-size:24px;font-weight:700;'
            'color:#9e0000;letter-spacing:-0.01em;margin:0">AJIO Discovery</p>'
            '<p style="font-family:JetBrains Mono,monospace;font-size:12px;letter-spacing:0.05em;'
            'text-transform:uppercase;color:#5f5e5e;margin:4px 0 16px">Discovery Engine</p>',
            unsafe_allow_html=True,
        )
        if st.button("Ask the Engine", type="primary", width="stretch"):
            _goto(page="ask", theme=None)
        st.divider()
        for key, label in PAGES.items():
            active = page == key or (page == "detail" and key == "map")
            if st.button(label, type="primary" if active else "secondary", width="stretch", key=f"nav_{key}"):
                _goto(page=key, theme=None, q=None)
        if all_sources:
            selected = st.multiselect(
                "Theme appears in source",
                all_sources,
                default=source_filter or all_sources,
                format_func=source_display_name,
            )
            current = source_filter or all_sources
            if selected and set(selected) != set(current):
                joined = None if set(selected) >= set(all_sources) else ",".join(sorted(selected))
                _goto(sources=joined)
        st.caption("v1.0 · Evidence-backed")
        st.caption("Corpus loaded" if corpus_ok else "Corpus missing")


def main() -> None:
    st.set_page_config(
        page_title="AJIO Discovery Engine",
        page_icon="◆",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(CHROME_CSS, unsafe_allow_html=True)

    paths = _paths()
    page = st.query_params.get("page", "overview")
    if page not in set(PAGES) | {"detail"}:
        page = "overview"
    genuine = st.query_params.get("view", "full") == "genuine"
    source_filter = [s for s in (st.query_params.get("sources") or "").split(",") if s]

    scores_path = paths.first_existing(SCORES_NAME)
    facts = _facts_cached(str(paths.interim_db))
    corpus_ok = facts.get("available", False)

    def href(**updates) -> str:
        return qp_href(_q(), **updates)

    scores = (
        _scores_cached(str(scores_path))
        if scores_path is not None
        else pd.DataFrame()
    )
    all_sources = source_names(scores) if not scores.empty else []
    _render_sidebar(
        page=page,
        corpus_ok=corpus_ok,
        all_sources=all_sources,
        source_filter=source_filter,
    )

    view_choice = st.segmented_control(
        "Ranking population",
        options=["Full Corpus", "Genuine Intent"],
        default="Genuine Intent" if genuine else "Full Corpus",
        key="ranking_view",
        help="Genuine intent re-ranks the same Stage 4 scores on the genuine_intent subset. It does not re-score.",
    )
    if view_choice is not None:
        want_genuine = view_choice == "Genuine Intent"
        if want_genuine != genuine:
            _goto(view="genuine" if want_genuine else "full")

    if scores_path is None:
        st.markdown(
            missing_html(str(paths.processed_dir), str(paths.outputs_dir)),
            unsafe_allow_html=True,
        )
        return

    appendix_path = paths.first_existing(APPENDIX_NAME)
    appendix = _appendix_cached(str(appendix_path)) if appendix_path else []
    appendix_map = quotes_by_theme(appendix)
    segments_path = paths.first_existing(SEGMENTS_NAME)
    segments = _segments_cached(str(segments_path)) if segments_path else pd.DataFrame()
    ajio = _ajio_cached(str(paths.aggregates_dir))
    tagged_docs = _docs_cached(str(paths.interim_db)) if corpus_ok else pd.DataFrame()
    tagged_n = facts["tagged"] or len(tagged_docs)
    genuine_n = facts["genuine_intent"]

    filtered = filter_scores(
        scores,
        sources=source_filter or None,
    )
    ranked = rank_view(filtered, genuine=genuine) if not filtered.empty else filtered

    if page == "overview":
        _page_overview(ranked, tagged_docs, tagged_n, genuine_n, facts, appendix_map, href)
    elif page == "map":
        _page_map(ranked, href)
    elif page == "detail":
        _page_detail(ranked, appendix_map, paths, href)
    elif page == "evidence":
        _page_evidence(tagged_docs, ranked, href)
    elif page == "segments":
        _page_segments(segments)
    elif page == "ajio":
        _page_ajio(scores, ajio, paths)
    else:
        _page_ask(ranked, appendix, segments, ajio, tagged_n, genuine_n, href)


def _page_overview(ranked, tagged_docs, tagged_n, genuine_n, facts, appendix_map, href) -> None:
    top = ranked.iloc[0] if not ranked.empty else None
    top_label = str(top["label"]) if top is not None else ""
    top_rows = []
    quotes = []
    for i, row in enumerate(ranked.head(5).itertuples(index=False), start=1):
        rec = row._asdict() if hasattr(row, "_asdict") else dict(row)
        label = rec["label"]
        blurb = ""
        frozen = appendix_map.get(label) or []
        if frozen:
            blurb = frozen[0].text
            if i <= 3:
                quotes.append(frozen[0].text)
        top_rows.append(
            {
                "title": pretty_label(label),
                "href": href(page="detail", theme=label),
                "blurb": blurb if i == 1 else "",
            }
        )
    mix: list[tuple[str, int, float]] = []
    if not tagged_docs.empty:
        counts = tagged_docs["source"].value_counts()
        total = int(counts.sum()) or 1
        mix = [(source_display_name(src), int(n), n / total) for src, n in counts.items()]
    st.markdown(
        overview_html(
            tagged=tagged_n,
            analyzable=facts["analyzable"],
            n_sources=len(mix),
            n_areas=len(ranked),
            genuine_n=genuine_n,
            top_label=top_label,
            top_href=href(page="detail", theme=top_label) if top_label else "",
            top_rows=top_rows,
            quotes=quotes,
            source_mix=mix,
        ),
        unsafe_allow_html=True,
    )


def _page_map(ranked, href) -> None:
    if not ranked.empty:
        labels = ranked["label"].tolist()
        pending = st.session_state.get("map_theme_pick")
        if pending and pending != "(select a theme)":
            st.session_state.map_theme_pick = "(select a theme)"
            _goto(page="detail", theme=pending)
            return
        st.selectbox(
            "Open a theme",
            ["(select a theme)"] + labels,
            format_func=lambda value: pretty_label(value) if value != "(select a theme)" else value,
            key="map_theme_pick",
        )
    table_rows = []
    points = []
    max_score = float(ranked["view_score"].max()) if not ranked.empty else 1.0
    max_score = max(max_score, 0.01)
    for rec in ranked.to_dict(orient="records"):
        label = rec["label"]
        n = rec.get("view_n_docs")
        n_g = rec.get("n_docs_genuine")
        prev = rec.get("view_prevalence") or 0.0
        try:
            genuine_share = float(n_g) / float(n) if n and float(n) else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            genuine_share = 0.0
        score = float(rec.get("view_score") or 0)
        srcs = []
        for key, value in rec.items():
            if str(key).startswith("prevalence_") and key.split("prevalence_", 1)[-1] not in {
                "lo",
                "hi",
                "norm",
                "genuine",
                "lo_genuine",
                "hi_genuine",
            }:
                try:
                    if float(value) > 0:
                        srcs.append(source_display_name(key.removeprefix("prevalence_")))
                except (TypeError, ValueError):
                    pass
        table_rows.append(
            {
                "title": pretty_label(label),
                "dimension": rec.get("dimension") or "",
                "href": href(page="detail", theme=label),
                "score": _fmt(score),
                "evidence": _evidence_volume(n),
                "genuine": f"{int(n_g) if pd.notna(n_g) else 0} docs · {_pct(rec.get('prevalence_genuine'))}",
                "sources": ", ".join(srcs[:4]) or "—",
                "priority": _priority(rec),
            }
        )
        points.append(
            {
                "title": f"{pretty_label(label)} (score {score:.2f})",
                "href": href(page="detail", theme=label),
                "x": float(prev) if pd.notna(prev) else 0.0,
                "y": min(1.0, genuine_share),
                "r": 6 + 22 * (score / max_score),
            }
        )
    st.markdown(map_html(table_rows=table_rows, points=points), unsafe_allow_html=True)


def _page_detail(ranked, appendix_map, paths, href) -> None:
    if st.button("Back to map"):
        _goto(page="map", theme=None)
    theme = st.query_params.get("theme", "")
    if ranked.empty:
        st.markdown('<p class="ad-muted">No themes match the current filters.</p>', unsafe_allow_html=True)
        return
    labels = ranked["label"].tolist()
    if theme not in set(labels):
        _goto(page="detail", theme=str(ranked.iloc[0]["label"]))
        return
    picked = st.selectbox(
        "Theme",
        labels,
        index=labels.index(theme),
        format_func=pretty_label,
    )
    if picked != theme:
        _goto(page="detail", theme=picked)
        return
    row = ranked.loc[ranked["label"] == theme].iloc[0]
    rec = row.to_dict()
    frozen = appendix_map.get(theme) or []
    live = _quotes_cached(
        str(paths.interim_db),
        str(rec.get("label") or ""),
        str(rec.get("dimension") or ""),
        str(rec.get("cluster") or ""),
        str(rec.get("supporting_doc_ids") or ""),
    )
    quotes = []
    seen = set()
    for item in frozen:
        quotes.append({"meta": f"{item.source} · {item.doc_id}", "text": item.text})
        seen.add(item.text)
    for item in live:
        if item["text"] in seen:
            continue
        quotes.append({"meta": f"{item['source']} · {item['doc_id']}", "text": item["text"]})
    top_src = top_source_for_row(rec)
    n = rec.get("view_n_docs")
    n_g = rec.get("n_docs_genuine")
    flags = []
    if rec.get("reportable"):
        flags.append("reportable")
    if rec.get("low_confidence"):
        flags.append("low confidence")
    if rec.get("post_purchase_only"):
        flags.append("post-purchase only")
    summary_parts = [
        f"{pretty_label(theme)} ranks in the current view with opportunity score {_fmt(rec.get('view_score'))} "
        f"on {int(n) if pd.notna(n) else '—'} tagged documents "
        f"({_pct(rec.get('view_prevalence'))} of the ranking population).",
    ]
    if pd.notna(n_g):
        summary_parts.append(
            f"Genuine-intent support: {int(n_g)} documents, score {_fmt(rec.get('opportunity_score_genuine'))}."
        )
    if rec.get("co_occurs_with"):
        summary_parts.append(f"Co-occurs with {rec['co_occurs_with']}.")
    if flags:
        summary_parts.append("Flags: " + ", ".join(flags) + ".")
    summary_parts.append("Score is the Stage 4 formula, not a 0–100 index.")
    conv = "Watch" if rec.get("post_purchase_only") else ("High" if rec.get("reportable") else "Limited")
    st.markdown(
        detail_html(
            {
                "back": href(page="map", theme=None),
                "title": pretty_label(theme),
                "score": _fmt(rec.get("view_score")),
                "summary": " ".join(summary_parts),
                "cards": [
                    {
                        "lbl": "Total volume",
                        "val": str(int(n) if pd.notna(n) else "—"),
                        "hint": "Supporting tagged documents in this view",
                    },
                    {
                        "lbl": "Genuine intent %",
                        "val": _pct(rec.get("prevalence_genuine")),
                        "hint": "Share of the genuine-intent subset",
                        "accent": True,
                    },
                    {
                        "lbl": "Top source",
                        "val": source_display_name(top_src) if top_src else "—",
                        "hint": "Highest per-source prevalence",
                    },
                    {
                        "lbl": "Conv. relevance",
                        "val": conv,
                        "hint": "Reportable pre-purchase support vs post-purchase-only",
                    },
                ],
                "quotes": quotes[:6],
            }
        ),
        unsafe_allow_html=True,
    )


def _page_evidence(docs, ranked, href) -> None:
    if docs.empty:
        st.markdown(
            '<div class="ad-page"><h2 class="ad-display">Evidence Explorer</h2>'
            "<p class=\"ad-sub\">No tagged documents in the local corpus database.</p></div>",
            unsafe_allow_html=True,
        )
        return
    sources = sorted(docs["source"].dropna().unique().tolist())
    dimensions = [""] + sorted(ranked["dimension"].dropna().unique().tolist()) if not ranked.empty else [""]
    src_q = st.query_params.get("esrc", "")
    intent_q = st.query_params.get("intent", "")
    dim_q = st.query_params.get("dim", "")
    query = st.query_params.get("q", "")

    src_options = [""] + sources
    intent_options = ["", "genuine_intent", "bookmark_only", "ambiguous"]
    dim_options = [""] + [d for d in dimensions if d]
    intent_labels = {
        "": "All",
        "genuine_intent": "Genuine intent",
        "bookmark_only": "Bookmark",
        "ambiguous": "Ambiguous",
    }
    c1, c2, c3 = st.columns(3)
    with c1:
        src_pick = st.selectbox(
            "Source",
            src_options,
            index=src_options.index(src_q) if src_q in src_options else 0,
            format_func=lambda s: "All sources" if s == "" else source_display_name(s),
        )
    with c2:
        intent_pick = st.selectbox(
            "Intent",
            intent_options,
            index=intent_options.index(intent_q) if intent_q in intent_options else 0,
            format_func=lambda s: intent_labels.get(s, pretty_label(s)),
        )
    with c3:
        dim_pick = st.selectbox(
            "Dimension",
            dim_options,
            index=dim_options.index(dim_q) if dim_q in dim_options else 0,
            format_func=lambda s: "All dimensions" if s == "" else pretty_label(s),
        )
    if (src_pick or "") != (src_q or "") or (intent_pick or "") != (intent_q or "") or (dim_pick or "") != (dim_q or ""):
        _goto(esrc=src_pick or None, intent=intent_pick or None, dim=dim_pick or None)

    view = docs
    if src_q:
        view = view[view["source"] == src_q]
    if intent_q:
        view = view[view["intent_class"] == intent_q]
    if dim_q and dim_q in view.columns:
        pass
    if dim_q:
        col = {
            "blocker_type": "blocker_type",
            "uncertainty_type": "uncertainty_type",
            "wishlist_motivation": "wishlist_motivation",
            "info_sought_elsewhere": "info_sought_elsewhere",
            "segment_cue": "segment_cue",
        }.get(dim_q, "blocker_type" if dim_q == "blocker_type" else None)
        if col and col in view.columns:
            view = view[view[col].fillna("").astype(str).str.len() > 0]
    needle = (query or "").strip().casefold()
    if needle:
        hay = (
            view["preview"].fillna("").str.casefold()
            + " "
            + view["blocker_type"].fillna("").str.casefold()
            + " "
            + view["uncertainty_type"].fillna("").str.casefold()
        )
        view = view[hay.str.contains(needle, regex=False)]

    src_chips = _chip_row([("", "All")] + [(s, source_display_name(s)) for s in sources], src_q, "esrc", href)
    intent_chips = _chip_row(
        [("", "All"), ("genuine_intent", "Genuine intent"), ("bookmark_only", "Bookmark"), ("ambiguous", "Ambiguous")],
        intent_q,
        "intent",
        href,
    )
    dim_chips = _chip_row(
        [("", "All")] + [(d, pretty_label(d)) for d in dimensions if d],
        dim_q,
        "dim",
        href,
    )

    with st.form("evidence_search", border=False):
        typed = st.text_input("Search quotes or keywords", value=query, label_visibility="collapsed")
        submitted = st.form_submit_button("Search")
    if submitted:
        st.query_params["q"] = typed
        st.rerun()

    cards = []
    for rec in view.head(40).to_dict(orient="records"):
        blockers = rec.get("blocker_type") or rec.get("uncertainty_type") or rec.get("wishlist_motivation") or ""
        tag = str(blockers).split(";")[0].strip() or rec.get("intent_class") or "tagged"
        cards.append(
            {
                "tag": pretty_label(tag),
                "source": source_display_name(rec.get("source") or ""),
                "doc_id": rec.get("doc_id") or "",
                "when": (rec.get("created_utc") or "")[:10],
                "text": rec.get("preview") or "",
                "intent": pretty_label(rec.get("intent_class") or ""),
                "theme": pretty_label(tag),
            }
        )
    st.markdown(
        evidence_html(
            chips_source=src_chips,
            chips_intent=intent_chips,
            chips_dim=dim_chips,
            cards=cards,
            n_shown=len(view),
            n_total=len(docs),
        ),
        unsafe_allow_html=True,
    )


def _page_segments(segments) -> None:
    rows = []
    if not segments.empty:
        for rec in segments.to_dict(orient="records"):
            rows.append(
                {
                    "segment": pretty_label(rec.get("segment") or ""),
                    "blocker": pretty_label(rec.get("blocker_type") or ""),
                    "n": str(int(rec["n_docs"]) if pd.notna(rec.get("n_docs")) else "—"),
                    "lift": _fmt(rec.get("lift")),
                }
            )
    st.markdown(segments_html(rows), unsafe_allow_html=True)


def _page_ajio(scores, ajio, paths) -> None:
    aggregates = _ajio_aggregates(str(paths.aggregates_dir))
    table = corroboration_rows(scores, aggregates)
    rows = table.to_dict(orient="records") if not table.empty else []
    for row in rows:
        row["label"] = pretty_label(row["label"])
        row["detail"] = str(row.get("detail") or "")
        row["kind"] = str(row.get("kind") or "")
    misfit = "—" if ajio.get("mean_misfit_pct") is None else str(ajio["mean_misfit_pct"])
    quality = "—" if ajio.get("mean_bad_quality_pct") is None else str(ajio["mean_bad_quality_pct"])
    rating = "—" if ajio.get("mean_average_rating") is None else str(ajio["mean_average_rating"])
    caption = (
        f"Fit prompt on {ajio.get('products_with_fit') or 0} products "
        f"({ajio.get('top_fit_is_loose') or 0} skew loose, {ajio.get('top_fit_is_tight') or 0} tight). "
        f"Quality prompt on {ajio.get('products_with_quality') or 0}. "
        f"Averages: {ajio.get('ratings_reported') or 0} reported by AJIO, "
        f"{ajio.get('ratings_derived') or 0} derived from the star distribution."
    )
    st.markdown(
        ajio_html(
            {
                "provenance": ajio.get("provenance") or "AJIO aggregates are not corpus documents.",
                "products": ajio.get("products") or 0,
                "misfit": misfit,
                "quality": quality,
                "rating": rating,
                "caption": caption,
            },
            rows,
        ),
        unsafe_allow_html=True,
    )


def _page_ask(ranked, appendix, segments, ajio, tagged_n, genuine_n, href) -> None:
    chips = [(q, href(page="ask", q=q)) for q in SAMPLE_QUESTIONS]
    st.markdown(ask_intro_html(chips), unsafe_allow_html=True)
    key = groq_api_key()
    prefill = st.query_params.get("q", "")
    if not key:
        st.info("Ask needs GROQ_API_KEY in `.env`. Every other screen works without it.")
        return
    st.caption(f"Model `{ask_model_name()}`. Grounded in this snapshot only.")
    with st.form("ask_form", border=False):
        question = st.text_input(
            "Question",
            value=prefill,
            max_chars=MAX_QUESTION_CHARS,
            placeholder="Ask a question about the corpus…",
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button("Ask")
    quotes_payload = [
        {"theme": q.theme, "source": q.source, "doc_id": q.doc_id, "text": q.text} for q in appendix
    ]
    snapshot = build_snapshot(
        scores=compact_score_rows(ranked.drop(columns=["supporting_doc_ids"], errors="ignore")),
        quotes=quotes_payload,
        segments=segments.to_dict(orient="records") if not segments.empty else [],
        ajio=ajio,
        tagged=tagged_n,
        genuine_intent=genuine_n,
    )
    asked = (question or "").strip()
    if submitted and not asked:
        st.warning("Type a question, or pick one of the examples.")
        return
    if submitted and asked:
        with st.spinner("Grounding an answer in the snapshot…"):
            try:
                reply = answer_question(asked, snapshot)
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))
                return
        related = []
        for rec in ranked.head(4).to_dict(orient="records"):
            related.append(
                {
                    "title": pretty_label(rec["label"]),
                    "hint": f"Score {_fmt(rec.get('view_score'))} · {int(rec['view_n_docs']) if pd.notna(rec.get('view_n_docs')) else '—'} docs",
                    "href": href(page="detail", theme=rec["label"]),
                }
            )
        ev = []
        for q in appendix[:4]:
            ev.append({"meta": f"{q.source} · {q.doc_id}", "text": q.text})
        paragraphs = "".join(f"<p>{escape(line)}</p>" for line in reply.split("\n") if line.strip())
        st.markdown(
            ask_answer_html("Grounded answer", paragraphs, ev, related),
            unsafe_allow_html=True,
        )


if __name__ == "__main__":
    main()
