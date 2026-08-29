"""Smoke tests for the read-only explorer loaders and Phase 8 navigation gates."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.ask import answer_question, build_snapshot, snapshot_contains_forbidden_ids
from app.data import (
    compact_score_rows,
    default_paths,
    filter_scores,
    load_opportunity_scores,
    load_tagged_documents,
    open_corpus_readonly,
    parse_evidence_appendix,
    rank_movement,
    rank_view,
    source_names,
)
from src.common.config import PROJECT_ROOT

FIXTURE_CSV = """dimension,label,n_docs,prevalence,opportunity_score,opportunity_score_genuine,n_docs_genuine,prevalence_genuine,reportable,post_purchase_only,low_confidence,cluster,supporting_doc_ids,prevalence_youtube,prevalence_play_store,n_authors,mean_severity,co_occurs_with
blocker_type,return_friction,195,0.24375,21.10,17.48,46,0.4259,true,false,false,blocker_type=return_friction,deadbeef12345678;cafebabe87654321,0.14,0.30,192,4.01,blocker_type=delivery_uncertainty
blocker_type,fit_size_uncertainty,90,0.1125,4.04,3.47,47,0.4352,true,false,false,blocker_type=fit_size_uncertainty,aaaabbbbccccdddd,0.18,0.02,83,3.07,
info_sought_elsewhere,brand_site_size_chart,9,0.01125,0.37,5.10,8,0.0741,true,false,true,info_sought_elsewhere=brand_site_size_chart,1111222233334444,0.021,0.0,8,2.33,
"""

APPENDIX = """# Evidence appendix

## return_friction

- `app_store` `211b72eefa267e33` ([source](https://example.com/review)): "Their customer service, exchange and returns are disappointing."
- `play_store` `7d7676d9650bbcc6`: "refund nahin diya"

## fit_size_uncertainty

- `quora_manual` `69b854d91b105b4e` ([source](https://www.quora.com/x)): "I want to try it on and see how it fits me"

# Pipeline run log

Should not be parsed as a theme.
"""


def test_default_paths_do_not_need_credentials():
    paths = default_paths()
    assert paths.root == PROJECT_ROOT
    assert paths.processed_dir == PROJECT_ROOT / "data" / "processed"
    assert paths.interim_db == PROJECT_ROOT / "data" / "interim" / "discovery.db"


def test_load_scores_parses_booleans_and_numbers(tmp_path: Path):
    path = tmp_path / "opportunity_scores.csv"
    path.write_text(FIXTURE_CSV, encoding="utf-8")
    frame = load_opportunity_scores(path)
    assert list(frame["label"]) == [
        "return_friction",
        "fit_size_uncertainty",
        "brand_site_size_chart",
    ]
    assert bool(frame.loc[0, "reportable"]) is True
    assert bool(frame.loc[0, "post_purchase_only"]) is False
    assert frame.loc[0, "opportunity_score"] == 21.10
    assert "youtube" in source_names(frame)
    assert "lo" not in source_names(frame)


def test_header_only_scores_are_empty_not_an_error(tmp_path: Path):
    path = tmp_path / "opportunity_scores.csv"
    path.write_text("dimension,label,opportunity_score\n", encoding="utf-8")
    frame = load_opportunity_scores(path)
    assert frame.empty


def test_alias_columns_theme_score_documents(tmp_path: Path):
    path = tmp_path / "opportunity_scores.csv"
    path.write_text("theme,score,documents\nfit,1.5,4\n", encoding="utf-8")
    frame = load_opportunity_scores(path)
    assert frame.loc[0, "label"] == "fit"
    assert frame.loc[0, "opportunity_score"] == 1.5
    assert frame.loc[0, "n_docs"] == 4


def test_parse_appendix_stops_at_run_log():
    quotes = parse_evidence_appendix(APPENDIX)
    assert [q.theme for q in quotes] == [
        "return_friction",
        "return_friction",
        "fit_size_uncertainty",
    ]
    assert quotes[0].doc_id == "211b72eefa267e33"
    assert quotes[0].source == "app_store"
    assert "disappointing" in quotes[0].text
    assert quotes[0].url == "https://example.com/review"
    assert all("Pipeline" not in q.theme for q in quotes)


def test_rank_view_genuine_promotes_size_chart(tmp_path: Path):
    path = tmp_path / "opportunity_scores.csv"
    path.write_text(FIXTURE_CSV, encoding="utf-8")
    frame = load_opportunity_scores(path)
    full = rank_view(frame, genuine=False)
    genuine = rank_view(frame, genuine=True)
    assert full.iloc[0]["label"] == "return_friction"
    size_full = int(full.loc[full["label"] == "brand_site_size_chart", "rank"].iloc[0])
    size_gen = int(genuine.loc[genuine["label"] == "brand_site_size_chart", "rank"].iloc[0])
    assert size_gen < size_full
    movers = rank_movement(frame)
    delta = int(movers.loc[movers["label"] == "brand_site_size_chart", "delta"].iloc[0])
    assert delta > 0


def test_filter_by_source_keeps_themes_present_in_that_source(tmp_path: Path):
    path = tmp_path / "opportunity_scores.csv"
    path.write_text(FIXTURE_CSV, encoding="utf-8")
    frame = load_opportunity_scores(path)
    youtube = filter_scores(frame, sources=["youtube"])
    assert set(youtube["label"]) == {
        "return_friction",
        "fit_size_uncertainty",
        "brand_site_size_chart",
    }
    play = filter_scores(frame, sources=["play_store"])
    assert "brand_site_size_chart" not in set(play["label"])
    assert "return_friction" in set(play["label"])


def test_compact_score_rows_omit_supporting_doc_ids(tmp_path: Path):
    path = tmp_path / "opportunity_scores.csv"
    path.write_text(FIXTURE_CSV, encoding="utf-8")
    frame = load_opportunity_scores(path)
    rows = compact_score_rows(frame)
    blob = json.dumps(rows)
    assert "supporting_doc_ids" not in blob
    assert "deadbeef12345678" not in blob
    snapshot = build_snapshot(
        scores=rows,
        quotes=[{"theme": "return_friction", "source": "app_store", "doc_id": "abcd", "text": "x"}],
        segments=[],
        ajio={"provenance": "side-channel", "products": 51},
        tagged=800,
        genuine_intent=108,
    )
    assert snapshot["tagged_documents"] == 800
    assert snapshot["genuine_intent_documents"] == 108
    assert snapshot_contains_forbidden_ids(snapshot) is False


def test_missing_db_does_not_create_a_file(tmp_path: Path):
    missing = tmp_path / "nope" / "discovery.db"
    assert open_corpus_readonly(missing) is None
    assert not missing.exists()
    assert load_tagged_documents(missing).empty


def test_pretty_label_and_top_source():
    from app.data import pretty_label, source_display_name, top_source_for_row

    assert pretty_label("return_friction") == "Return Friction"
    assert source_display_name("quora_manual") == "Quora"
    assert top_source_for_row({"prevalence_youtube": 0.1, "prevalence_play_store": 0.4}) == "play_store"
    assert top_source_for_row({"prevalence_lo": 0.9, "prevalence_youtube": 0.1}) == "youtube"


def test_live_scores_load_when_present():
    path = PROJECT_ROOT / "data" / "processed" / "opportunity_scores.csv"
    if not path.is_file():
        path = PROJECT_ROOT / "outputs" / "opportunity_scores.csv"
    if not path.is_file():
        return
    frame = load_opportunity_scores(path)
    assert "label" in frame.columns
    assert "opportunity_score" in frame.columns
    assert len(frame) >= 1
    assert "supporting_doc_ids" not in json.dumps(compact_score_rows(frame))


def test_app_modules_do_not_call_get_settings_or_pipeline_writers():
    """Phase 8: explorer starts without get_settings and never tags or scores."""
    forbidden = {
        "get_settings",
        "run_tagging",
        "run_quantification",
        "upsert_documents",
        "upsert_tags",
    }
    for path in (PROJECT_ROOT / "app").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.alias):
                names.add(node.name.split(".")[-1])
        hit = names & forbidden
        assert not hit, f"{path.name} references {sorted(hit)}"


def test_ask_is_disabled_without_a_groq_key(monkeypatch):
    monkeypatch.setattr("app.ask.groq_api_key", lambda: None)
    with pytest.raises(RuntimeError, match="disabled"):
        answer_question(
            "what blocks wishlist conversion?",
            build_snapshot(
                scores=[],
                quotes=[],
                segments=[],
                ajio={},
                tagged=800,
                genuine_intent=108,
            ),
        )


def test_overview_html_does_not_use_stitch_placeholder_kpis():
    from app.ui import overview_html, qp_href

    html = overview_html(
        tagged=800,
        analyzable=7127,
        n_sources=6,
        n_areas=24,
        genuine_n=108,
        top_label="return_friction",
        top_href=qp_href({}, page="detail", theme="return_friction"),
        top_rows=[{"title": "Return Friction", "href": "/?page=detail", "blurb": "refund"}],
        quotes=["refund nahin diya"],
        source_mix=[("YouTube", 420, 0.525)],
    )
    assert "12,480" not in html
    assert "800" in html
    assert "7,127" in html
    assert "Reddit" not in html
    assert html.startswith("\n<div") or "<div" in html


def test_qp_href_is_a_root_relative_query():
    from app.ui import qp_href

    assert qp_href({"page": "overview"}, page="map").startswith("/?")
    assert "page=map" in qp_href({"page": "overview"}, page="map")
    assert "theme" not in qp_href({"page": "detail", "theme": "x"}, page="map", theme=None)


def test_query_only_connection_rejects_writes(tmp_path: Path):
    import sqlite3

    from src.common.db import init_db

    db = tmp_path / "t.db"
    conn = init_db(db)
    conn.close()
    ro = open_corpus_readonly(db)
    assert ro is not None
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("CREATE TABLE should_not_exist (a INTEGER)")
    ro.close()


def test_navigation_query_params_open_each_screen():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "explorer.py"), default_timeout=60)
    at.run()
    assert not list(at.exception)
    html = "".join(getattr(block, "value", "") or "" for block in at.markdown)
    assert "12,480" not in html
    for page, needle in (
        ("map", "Opportunity Map"),
        ("evidence", "Evidence Explorer"),
        ("segments", "Segments"),
        ("ajio", "Corroboration"),
        ("ask", "Ask the Discovery Engine"),
    ):
        at.query_params["page"] = page
        at.run()
        assert not list(at.exception), page
        body = "".join(getattr(block, "value", "") or "" for block in at.markdown)
        assert needle in body, page
    # Streamlit sidebar buttons are the navigation the reviewer actually clicks
    at.query_params["page"] = "overview"
    at.run()
    labels = [btn.label for btn in at.sidebar.button]
    assert "Opportunity Map" in labels
    assert "Ask the Engine" in labels
    next(btn for btn in at.sidebar.button if btn.label == "Opportunity Map").click().run()
    assert not list(at.exception)
    mapped = "".join(getattr(block, "value", "") or "" for block in at.markdown)
    assert "Opportunity Map" in mapped
    assert at.query_params.get("page") in ("map", ["map"])
