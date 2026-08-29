"""Phase 6 entrypoint: four sections, no invented themes, aggregates stay out of the corpus."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from src.common.db import init_db, upsert_documents, upsert_tags
from src.common.schemas import Document, DocumentTags, EvidenceSpan
from src.synthesize import run_synthesis
from src.tag.taxonomy import (
    TAXONOMY_VERSION,
    BlockerType,
    EvidenceTag,
    IntentClass,
    OutcomeMentioned,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

FIT = {
    "question": "How was the Product fit?",
    "options": {"Perfect": 40, "Loose": 12, "Tight": 30, "Too Loose": 3, "Too Tight": 12},
}
QUALITY = {
    "question": "How was the Product Quality?",
    "options": {"Excellent": 27, "Very Good": 29, "Average": 32, "Bad": 5, "Very Bad": 5},
}


def settings_for(tmp_path):
    return SimpleNamespace(
        project_root=tmp_path,
        interim_db=tmp_path / "data" / "interim" / "discovery.db",
        outputs_dir=tmp_path / "outputs",
        processed_dir=tmp_path / "data" / "processed",
        aggregates_dir=tmp_path / "data" / "aggregates",
        logs_dir=tmp_path / "logs",
        config_hash="synthtest0000abcdef",
    )


def document(doc_id: str, source: str, text: str, **overrides) -> Document:
    payload = {
        "doc_id": doc_id,
        "source": source,
        "source_native_id": f"native-{doc_id}",
        "text": text,
        "created_utc": NOW,
        "word_count": len(text.split()),
        "is_relevant": True,
        "ingested_at": NOW,
    }
    payload.update(overrides)
    return Document(**payload)


def tags(*blockers: BlockerType, quote: str) -> DocumentTags:
    return DocumentTags(
        is_relevant=True,
        wishlist_motivation=[],
        blocker_type=list(blockers),
        uncertainty_type=[],
        info_sought_elsewhere=[],
        segment_cue=[],
        intent_class=IntentClass.GENUINE_INTENT,
        outcome_mentioned=OutcomeMentioned.STILL_DECIDING,
        severity=4,
        actionability_non_monetary=1,
        confidence_pct=80,
        evidence=[
            EvidenceSpan(tag=EvidenceTag(blocker.value), quote=quote) for blocker in blockers
        ],
    )


def write_aggregate(directory, product_id: str, **overrides) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "ajio_aggregate",
        "product_id": product_id,
        "url": f"https://www.ajio.com/p/{product_id}",
        "extracted_at": "2026-08-23T12:26:01.336Z",
        "rating_distribution": {"5": 54, "4": 16, "3": 11, "2": 3, "1": 13},
        "opinions": [FIT, QUALITY],
    }
    payload.update(overrides)
    (directory / f"{product_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def write_scores(directory, rows: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "opportunity_scores.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def seed_corpus(settings, *, with_scores: bool = True):
    """A few tagged docs across two sources, two aggregate files, optional ranking."""
    conn = init_db(settings.interim_db)
    docs = [
        document(
            "aaaa111122223333",
            "youtube",
            "wishlisted this kurta but the size chart makes no sense to me",
        ),
        document(
            "bbbb222233334444",
            "youtube",
            "the fabric looks cheap in the haul, not sure about the quality",
        ),
        document(
            "cccc333344445555",
            "quora_manual",
            "Does this run small? I am usually a medium on AJIO.",
        ),
        document(
            "dddd444455556666",
            "quora_manual",
            "I keep adding dresses to my wishlist watching the price.",
        ),
    ]
    upsert_documents(conn, docs)
    kwargs = dict(taxonomy_version=TAXONOMY_VERSION, prompt_version="v1", model="test")
    upsert_tags(
        conn,
        "aaaa111122223333",
        tags(BlockerType.FIT_SIZE_UNCERTAINTY, quote="size chart makes no sense"),
        **kwargs,
    )
    upsert_tags(
        conn,
        "bbbb222233334444",
        tags(BlockerType.QUALITY_DOUBT, quote="not sure about the quality"),
        **kwargs,
    )
    upsert_tags(
        conn,
        "cccc333344445555",
        tags(BlockerType.FIT_SIZE_UNCERTAINTY, quote="Does this run small"),
        **kwargs,
    )
    upsert_tags(
        conn,
        "dddd444455556666",
        tags(BlockerType.QUALITY_DOUBT, quote="watching the price"),
        **kwargs,
    )
    conn.close()

    ajio = settings.aggregates_dir / "ajio"
    write_aggregate(ajio, "410334633")
    write_aggregate(ajio, "703592968")

    if with_scores:
        write_scores(
            settings.processed_dir,
            [
                {
                    "theme": "fit_size_uncertainty",
                    "score": "72.4",
                    "prevalence": "0.41",
                    "documents": "2",
                    "cooccurrence": "will_it_fit",
                    "n_docs_genuine": "2",
                    "opportunity_score_genuine": "50.0",
                },
                {
                    "theme": "quality_doubt",
                    "score": "55.1",
                    "prevalence": "0.22",
                    "documents": "2",
                    "cooccurrence": "is_quality_worth_it",
                    "n_docs_genuine": "1",
                    "opportunity_score_genuine": "80.0",
                },
            ],
        )
    else:
        settings.processed_dir.mkdir(parents=True, exist_ok=True)


def test_run_writes_all_four_sections_with_source_mix_and_a_cross_reference(tmp_path):
    settings = settings_for(tmp_path)
    seed_corpus(settings)

    summary = run_synthesis.run(settings)

    assert summary["status"] == "written"
    assert summary["written"] is True
    assert summary["quantify_status"] == "present"
    assert summary["documents"] == 4
    assert summary["sources"]["youtube"] == 2
    assert summary["sources"]["quora_manual"] == 2
    assert summary["aggregates"] == 2

    report = (tmp_path / "outputs" / "opportunity_report.md").read_text(encoding="utf-8")
    assert report.index("## Corpus summary") < report.index("## Opportunity areas")
    assert report.index("## Opportunity areas") < report.index("## AJIO on-site aggregates")
    assert report.index("## AJIO on-site aggregates") < report.index("## Limitations")
    assert report.rstrip().endswith("re-collection yields a fresh snapshot.")

    assert "`youtube`" in report and "`quora_manual`" in report
    assert "no longer YouTube-only" in report
    assert "### 1. fit_size_uncertainty" in report
    assert "opportunity score: 72.4" in report
    assert "genuine-intent documents: 2" in report
    assert "genuine-intent score: 50.0" in report
    assert "co-occurrence: will_it_fit" in report
    assert "### Full corpus vs genuine intent" in report
    assert report.index("### Full corpus vs genuine intent") < report.index("### 1. fit_size_uncertainty")
    assert "real purchase intent" in report
    assert "4-document" in report
    assert "down 1 (1 → 2)" in report
    assert "up 1 (2 → 1)" in report
    assert "`youtube` `aaaa111122223333`" in report
    assert "size chart makes no sense" in report
    assert "corroborated" in report
    assert "not corpus documents" in report
    assert "post-purchase and self-selected" in report
    assert "Hand-collected data" in report
    assert "## Discovery questions" in report
    assert "### Q1." in report
    assert "### Q10." in report
    assert report.index("## Opportunity areas") < report.index("## Discovery questions")
    assert report.index("## Discovery questions") < report.index("## AJIO on-site aggregates")
    assert "## Segment differences" in report
    assert "## Excluded by constraint" in report
    assert (tmp_path / "outputs" / "evidence_appendix.md").is_file()
    assert (tmp_path / "outputs" / "opportunity_scores.csv").is_file()
    appendix = (tmp_path / "outputs" / "evidence_appendix.md").read_text(encoding="utf-8")
    assert "# Pipeline run log" in appendix
    assert "`synthesize`" in appendix
    assert (tmp_path / "outputs" / "tagger_validation.md").is_file()
    validation = (tmp_path / "outputs" / "tagger_validation.md").read_text(encoding="utf-8")
    assert "No gold set is in this repository" in validation


def test_dry_run_writes_no_file(tmp_path):
    settings = settings_for(tmp_path)
    seed_corpus(settings)

    summary = run_synthesis.run(settings, dry_run=True)

    assert summary["status"] == "dry_run"
    assert summary["written"] is False
    assert not (tmp_path / "outputs" / "opportunity_report.md").exists()
    assert not (tmp_path / "outputs" / "evidence_appendix.md").exists()
    assert not (tmp_path / "outputs" / "opportunity_scores.csv").exists()
    assert not (tmp_path / "outputs" / "tagger_validation.md").exists()


def test_without_force_an_existing_report_is_not_clobbered(tmp_path):
    settings = settings_for(tmp_path)
    seed_corpus(settings)
    out = tmp_path / "outputs"
    out.mkdir()
    report = out / "opportunity_report.md"
    report.write_text("SENTINEL\n", encoding="utf-8")

    summary = run_synthesis.run(settings)

    assert summary["status"] == "exists"
    assert summary["written"] is False
    assert report.read_text(encoding="utf-8") == "SENTINEL\n"


def test_force_overwrites_an_existing_report(tmp_path):
    settings = settings_for(tmp_path)
    seed_corpus(settings)
    out = tmp_path / "outputs"
    out.mkdir()
    report = out / "opportunity_report.md"
    report.write_text("SENTINEL\n", encoding="utf-8")

    summary = run_synthesis.run(settings, force=True)

    assert summary["status"] == "written"
    text = report.read_text(encoding="utf-8")
    assert "SENTINEL" not in text
    assert "## Corpus summary" in text


def test_aggregates_never_land_in_documents_or_tags(tmp_path):
    settings = settings_for(tmp_path)
    seed_corpus(settings)
    run_synthesis.run(settings)

    conn = init_db(settings.interim_db)
    sources = [row[0] for row in conn.execute("SELECT DISTINCT source FROM documents")]
    tagged_sources = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT d.source FROM doc_tags t JOIN documents d ON d.doc_id = t.doc_id"
        )
    ]
    conn.close()

    assert "ajio_aggregate" not in sources
    assert "ajio_aggregate" not in tagged_sources


def test_scores_without_genuine_columns_still_render_and_omit_the_comparison(tmp_path):
    """A pre-Phase-5 CSV has no genuine_* fields; the rest of the section still renders."""
    settings = settings_for(tmp_path)
    seed_corpus(settings, with_scores=False)
    write_scores(
        settings.processed_dir,
        [
            {
                "theme": "fit_size_uncertainty",
                "score": "72.4",
                "prevalence": "0.41",
                "documents": "2",
            }
        ],
    )
    run_synthesis.run(settings)
    report = (tmp_path / "outputs" / "opportunity_report.md").read_text(encoding="utf-8")
    assert "### 1. fit_size_uncertainty" in report
    assert "opportunity score: 72.4" in report
    assert "genuine-intent" not in report
    assert "### Full corpus vs genuine intent" not in report


def test_quantify_absent_renders_a_c_d_and_marks_b_pending_without_inventing_themes(tmp_path):
    settings = settings_for(tmp_path)
    seed_corpus(settings, with_scores=False)

    summary = run_synthesis.run(settings)

    assert summary["quantify_status"] == "pending"
    assert summary["themes"] == 0
    report = (tmp_path / "outputs" / "opportunity_report.md").read_text(encoding="utf-8")
    assert "## Corpus summary" in report
    assert "pending — run Stage 4 (quantify) first" in report
    assert "### 1." not in report
    opportunity_block = report.split("## Opportunity areas")[1].split("## Discovery")[0]
    assert "fit_size_uncertainty" not in opportunity_block
    assert "### Full corpus vs genuine intent" not in report
    assert "## AJIO on-site aggregates" in report
    assert "## Limitations" in report
    assert report.index("## Opportunity areas") < report.index("## AJIO on-site aggregates")
    assert report.index("## AJIO on-site aggregates") < report.index("## Limitations")


def test_the_entrypoint_imports_the_aggregates_reader_not_the_json(tmp_path):
    """The guardrail, on the import graph: this stage must not re-read the grabs."""
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src" / "synthesize" / "run_synthesis.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            names.update(alias.name for alias in node.names)

    assert "src.store.aggregates" in modules
    assert "load_ajio_aggregates" in names
    assert "by_product_id" in names
    assert "summarize" in names
    assert not any(module.startswith("src.collect") for module in modules)
    assert "src.store.build_corpus" not in modules
    assert "src.tag.run_tagging" not in modules
    assert "upsert_documents" not in names
    assert "upsert_tags" not in names


def test_header_only_scores_still_generate_a_report(tmp_path):
    """Edge-case §6.1: quantify ran, nothing ranked — do not crash, do not invent."""
    settings = settings_for(tmp_path)
    seed_corpus(settings, with_scores=False)
    scores = tmp_path / "data" / "processed" / "opportunity_scores.csv"
    scores.parent.mkdir(parents=True, exist_ok=True)
    scores.write_text("theme,score,documents\n", encoding="utf-8")

    summary = run_synthesis.run(settings)
    report = (tmp_path / "outputs" / "opportunity_report.md").read_text(encoding="utf-8")

    assert summary["quantify_status"] == "present"
    assert summary["themes"] == 0
    assert "No opportunity cleared the reporting bar" in report
    assert "### 1." not in report
    assert "## Corpus summary" in report
    assert "## Limitations" in report


def test_ten_discovery_questions_each_carry_a_number_and_a_citation(tmp_path):
    settings = settings_for(tmp_path)
    seed_corpus(settings)
    run_synthesis.run(settings)
    report = (tmp_path / "outputs" / "opportunity_report.md").read_text(encoding="utf-8")
    discovery = report.split("## Discovery questions")[1].split("## AJIO")[0]
    for index in range(1, 11):
        heading = f"### Q{index}."
        assert heading in discovery
        rest = discovery.split(heading, 1)[1]
        block = rest.split("### Q")[0] if index < 10 else rest
        assert any(char.isdigit() for char in block), f"Q{index} has no number"
        assert "`" in block, f"Q{index} has no citation"


def test_price_constraint_section_lists_the_excluded_tags(tmp_path):
    settings = settings_for(tmp_path)
    seed_corpus(settings)
    run_synthesis.run(settings)
    report = (tmp_path / "outputs" / "opportunity_report.md").read_text(encoding="utf-8")
    block = report.split("## Excluded by constraint")[1].split("## Limitations")[0]
    for label in ("price_absolute", "price_expectation", "price_watch", "budget_timing"):
        assert f"`{label}`" in block
    assert "tagged documents" in block


def test_segment_matrix_cells_above_lift_two_are_listed(tmp_path):
    settings = settings_for(tmp_path)
    seed_corpus(settings)
    write_scores = tmp_path / "data" / "processed" / "segment_matrix.csv"
    write_scores.write_text(
        "segment,blocker_type,n_docs,lift\n"
        "budget_conscious,fit_size_uncertainty,2,3.1\n"
        "menswear,quality_doubt,1,1.1\n",
        encoding="utf-8",
    )
    run_synthesis.run(settings)
    report = (tmp_path / "outputs" / "opportunity_report.md").read_text(encoding="utf-8")
    block = report.split("## Segment differences")[1].split("## Excluded")[0]
    assert "`budget_conscious`" in block
    assert "3.1" in block
    assert "menswear" not in block
    assert "affected segments: `budget_conscious`" in report


def test_tag_sample_denominators_are_read_from_run_log(tmp_path):
    settings = settings_for(tmp_path)
    seed_corpus(settings)
    conn = init_db(settings.interim_db)
    conn.execute(
        "CREATE TABLE tag_sample (doc_id TEXT PRIMARY KEY, source TEXT, drawn TEXT)"
    )
    conn.execute(
        "INSERT INTO tag_sample (doc_id, source, drawn) VALUES (?, ?, ?)",
        ("aaaa111122223333", "youtube", "2026-08-25"),
    )
    conn.execute(
        "INSERT INTO tag_sample (doc_id, source, drawn) VALUES (?, ?, ?)",
        ("cccc333344445555", "quora_manual", "2026-08-25"),
    )
    from src.common.db import run_log

    with run_log(conn, "sampletest", "tag_sample", settings.config_hash) as entry:
        entry.records_out = 2
        entry.note(
            json.dumps(
                {
                    "seed": 42,
                    "target": 800,
                    "census_sources": ["quora_manual"],
                    "sampled_total": 2,
                    "sampled_by_source": {"youtube": 1, "quora_manual": 1},
                }
            )
        )
    conn.close()

    run_synthesis.run(settings)
    report = (tmp_path / "outputs" / "opportunity_report.md").read_text(encoding="utf-8")
    assert "Tagging denominators:" in report
    assert "seed `42`" in report
    assert "target 800" in report
    assert "`quora_manual`" in report
    assert "Phase 4 tagged a sample" in report


def test_stale_scores_are_refused(tmp_path):
    settings = settings_for(tmp_path)
    seed_corpus(settings)
    conn = init_db(settings.interim_db)
    conn.execute("UPDATE doc_tags SET tagged_at = '2099-01-01T00:00:00+00:00'")
    conn.close()

    from src.synthesize.report import StaleScoresError
    import pytest

    with pytest.raises(StaleScoresError, match="older than the newest"):
        run_synthesis.run(settings)
    assert not (tmp_path / "outputs" / "opportunity_report.md").exists()


def test_no_ajio_aggregate_figures_leak_into_non_aggregate_sections(tmp_path):
    """Exit criterion: aggregate numbers stay inside their own section."""
    settings = settings_for(tmp_path)
    seed_corpus(settings)
    run_synthesis.run(settings)
    report = (tmp_path / "outputs" / "opportunity_report.md").read_text(encoding="utf-8")
    start = report.index("## AJIO on-site aggregates")
    end = report.index("## Segment differences")
    elsewhere = report[:start] + report[end:]
    assert "mean misfit" not in elsewhere
    assert "Bad + Very Bad" not in elsewhere
    assert "derived here as the weighted mean" not in elsewhere

