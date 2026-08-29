"""Quantify: tags -> processed CSVs (plan §5).

The evidence rule and the tagger are out of scope. These tests pin the
analyzable-set filter and the columns the score is built from.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.common.hashing import anonymous_author_hash
from src.common.db import init_db, upsert_documents, upsert_tags
from src.common.schemas import Document, DocumentTags, EvidenceSpan
from src.quantify.cooccurrence import lift
from src.quantify.metrics import MixedTaggingError, QuantifyKnobs, load_analyzable, quantify, score_opportunities
from src.quantify.run_quantify import (
    LIFT_NAME,
    PREVALENCE_NAME,
    SCORES_NAME,
    SEGMENT_NAME,
    dry_run,
    run,
    write_scores,
)
from src.quantify.scoring import opportunity_score
from src.tag.taxonomy import (
    TAXONOMY_VERSION,
    BlockerType,
    EvidenceTag,
    IntentClass,
    OutcomeMentioned,
    SegmentCue,
    UncertaintyType,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
NO_MERGE = QuantifyKnobs(cluster_jaccard_min=0)

_QUOTES = {
    BlockerType.FIT_SIZE_UNCERTAINTY: "size chart makes no sense",
    UncertaintyType.WILL_IT_FIT: "size chart",
    BlockerType.QUALITY_DOUBT: "fabric looks cheap",
    BlockerType.RETURN_FRICTION: "asked for a return",
    SegmentCue.FIRST_TIME_ONLINE_BUYER: "first time buying online",
}


def document(doc_id: str, source: str = "youtube", **overrides) -> Document:
    payload = {
        "doc_id": doc_id,
        "source": source,
        "source_native_id": f"cmt-{doc_id}",
        "text": "wishlisted this kurta but the size chart makes no sense to me",
        "created_utc": NOW,
        "word_count": 12,
        "ingested_at": NOW,
        "is_relevant": True,
    }
    payload.update(overrides)
    return Document(**payload)


def tags(
    *,
    blockers: list[BlockerType] | None = None,
    uncertainties: list[UncertaintyType] | None = None,
    segments: list[SegmentCue] | None = None,
    severity: int = 4,
    actionability: int = 1,
    confidence_pct: int = 80,
    intent: IntentClass = IntentClass.GENUINE_INTENT,
    quote_overrides: dict | None = None,
) -> DocumentTags:
    blockers = list(blockers or [])
    uncertainties = list(uncertainties or [])
    segments = list(segments or [])
    quotes = {**_QUOTES, **(quote_overrides or {})}
    evidence = [
        EvidenceSpan(tag=EvidenceTag(value.value), quote=quotes[value])
        for value in [*blockers, *uncertainties, *segments]
    ]
    return DocumentTags(
        is_relevant=True,
        wishlist_motivation=[],
        blocker_type=blockers,
        uncertainty_type=uncertainties,
        info_sought_elsewhere=[],
        segment_cue=segments,
        intent_class=intent,
        outcome_mentioned=OutcomeMentioned.STILL_DECIDING,
        severity=severity,
        actionability_non_monetary=actionability,
        confidence_pct=confidence_pct,
        evidence=evidence,
    )


def persist(conn, doc: Document, coding: DocumentTags | None = None) -> None:
    upsert_documents(conn, [doc])
    if coding is None:
        return
    upsert_tags(
        conn,
        doc.doc_id,
        coding,
        taxonomy_version=TAXONOMY_VERSION,
        prompt_version="v1",
        model="test",
    )


def fixture_corpus(conn) -> None:
    """Four analyzable tagged docs plus three that must not enter the denominator.

    Analyzable:
      yt-fit-a  youtube   fit_size + will_it_fit  sev=5 act=1 conf=80
      yt-fit-b  youtube   fit_size + will_it_fit  sev=3 act=1 conf=90
      q-qual    quora     quality_doubt           sev=5 act=0 conf=70
      yt-empty  youtube   (no theme labels)       sev=2 act=1 conf=50

    Excluded:
      irrel     tagged but is_relevant=0
      dup       tagged but is_duplicate_of yt-fit-a
      untagged  relevant, no tag row
    """
    persist(
        conn,
        document("yt-fit-a"),
        tags(
            blockers=[BlockerType.FIT_SIZE_UNCERTAINTY],
            uncertainties=[UncertaintyType.WILL_IT_FIT],
            severity=5,
            actionability=1,
            confidence_pct=80,
        ),
    )
    persist(
        conn,
        document("yt-fit-b"),
        tags(
            blockers=[BlockerType.FIT_SIZE_UNCERTAINTY],
            uncertainties=[UncertaintyType.WILL_IT_FIT],
            severity=3,
            actionability=1,
            confidence_pct=90,
        ),
    )
    persist(
        conn,
        document(
            "q-qual",
            source="quora_manual",
            text="the fabric looks cheap so I doubt the quality is worth it",
        ),
        tags(
            blockers=[BlockerType.QUALITY_DOUBT],
            severity=5,
            actionability=0,
            confidence_pct=70,
        ),
    )
    persist(conn, document("yt-empty"), tags(severity=2, actionability=1, confidence_pct=50))
    persist(
        conn,
        document("irrel", is_relevant=False, text="I asked for a return and they said no"),
        tags(blockers=[BlockerType.RETURN_FRICTION], severity=5),
    )
    persist(
        conn,
        document(
            "dup",
            is_duplicate_of="yt-fit-a",
            text="I asked for a return and they said no",
        ),
        tags(blockers=[BlockerType.RETURN_FRICTION], severity=5),
    )
    persist(conn, document("untagged"))


# --- aggregation ----------------------------------------------------------


def test_fixture_n_docs_prevalence_and_score_order(tmp_path):
    conn = init_db(tmp_path / "discovery.db")
    fixture_corpus(conn)
    docs = load_analyzable(conn)
    assert {d.doc_id for d in docs} == {"yt-fit-a", "yt-fit-b", "q-qual", "yt-empty"}
    rows, sources = score_opportunities(docs, knobs=NO_MERGE)
    conn.close()

    by_key = {(r["dimension"], r["label"]): r for r in rows}
    assert ("blocker_type", "return_friction") not in by_key  # excluded docs only
    fit = by_key[("blocker_type", "fit_size_uncertainty")]
    will = by_key[("uncertainty_type", "will_it_fit")]
    qual = by_key[("blocker_type", "quality_doubt")]

    assert fit["n_docs"] == 2
    assert fit["prevalence"] == 0.5  # 2 / 4; empty-theme doc is in the denom
    assert fit["mean_severity"] == 4.0  # (5 + 3) / 2, not diluted by yt-empty's 2
    assert fit["mean_actionability"] == 1.0
    assert fit["mean_confidence"] == 85.0  # (80 + 90) / 2
    assert fit["opportunity_score"] == pytest.approx(
        opportunity_score(
            fit["prevalence_norm"],
            fit["mean_severity"],
            fit["mean_actionability"],
            fit["evidence_confidence"],
        )
    )
    assert fit["prevalence_lo"] < fit["prevalence"] < fit["prevalence_hi"]
    assert fit["low_confidence"] is True  # n=2 < 20
    assert fit["post_purchase_only"] is False
    assert "yt-fit-a" in fit["supporting_doc_ids"]
    assert "uncertainty_type=will_it_fit" in fit["co_occurs_with"]

    assert will["n_docs"] == 2
    assert will["prevalence"] == 0.5
    assert will["opportunity_score"] == pytest.approx(fit["opportunity_score"])

    assert qual["n_docs"] == 1
    assert qual["prevalence"] == 0.25
    assert qual["mean_severity"] == 5.0
    assert qual["mean_actionability"] == 0.0
    assert qual["opportunity_score"] == 0.0  # actionability kills it

    scores = [r["opportunity_score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    # YouTube-vs-rest is a column, not a reweight: fit is 2/3 of YouTube, 0/1 of Quora.
    assert sources[0] == "youtube"
    assert fit["prevalence_youtube"] == 2 / 3
    assert fit["prevalence_quora_manual"] == 0.0
    assert qual["prevalence_quora_manual"] == 1.0


def test_empty_theme_lists_count_in_the_denominator_only(tmp_path):
    conn = init_db(tmp_path / "discovery.db")
    persist(conn, document("empty"), tags())
    persist(
        conn,
        document("fit"),
        tags(blockers=[BlockerType.FIT_SIZE_UNCERTAINTY], severity=5),
    )
    rows, _ = score_opportunities(load_analyzable(conn))
    conn.close()
    fit = next(r for r in rows if r["label"] == "fit_size_uncertainty")
    assert fit["n_docs"] == 1
    assert fit["prevalence"] == 0.5
    assert fit["mean_severity"] == 5.0  # empty doc's severity=4 is not in the mean


def test_csv_has_component_columns_and_is_sorted_by_score(tmp_path):
    conn = init_db(tmp_path / "discovery.db")
    fixture_corpus(conn)
    rows, sources = score_opportunities(load_analyzable(conn))
    conn.close()
    path = tmp_path / "processed" / SCORES_NAME
    write_scores(path, rows, sources)
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        written = list(reader)

    for required in (
        "dimension",
        "label",
        "n_docs",
        "prevalence",
        "mean_severity",
        "mean_actionability",
        "mean_confidence",
        "opportunity_score",
        "co_occurs_with",
        "prevalence_lo",
        "prevalence_hi",
        "prevalence_norm",
        "severity_norm",
        "evidence_confidence",
        "low_confidence",
        "reportable",
        "flagged_evidence_share",
        "post_purchase_only",
        "supporting_doc_ids",
        "prevalence_youtube",
        "prevalence_quora_manual",
    ):
        assert required in fieldnames
    scores = [float(r["opportunity_score"]) for r in written]
    assert scores == sorted(scores, reverse=True)
    assert written[0]["opportunity_score"] != written[-1]["opportunity_score"] or len(written) == 1
    # The ranking is auditable: every component sits next to the score.
    top = written[0]
    assert float(top["prevalence"]) > 0
    assert float(top["mean_severity"]) > 0
    assert top["n_docs"] == "2"


def test_run_writes_csv_and_run_log_without_touching_documents(tmp_path):
    db = tmp_path / "discovery.db"
    conn = init_db(db)
    fixture_corpus(conn)
    before_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    before_tags = conn.execute("SELECT COUNT(*) FROM doc_tags").fetchone()[0]
    conn.close()

    settings = SimpleNamespace(
        interim_db=db,
        processed_dir=tmp_path / "processed",
        logs_dir=tmp_path / "logs",
        config_hash="abc123def456",
    )
    summary = run(settings)
    assert summary["analyzable_docs"] == 4
    assert summary["rows"] > 0
    processed = tmp_path / "processed"
    assert (processed / SCORES_NAME).is_file()
    assert (processed / PREVALENCE_NAME).is_file()
    assert (processed / LIFT_NAME).is_file()
    assert (processed / SEGMENT_NAME).is_file()

    conn = init_db(db)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == before_docs
    assert conn.execute("SELECT COUNT(*) FROM doc_tags").fetchone()[0] == before_tags
    logged = conn.execute(
        "SELECT stage, records_in, records_out, notes FROM run_log WHERE stage = 'quantify'"
    ).fetchone()
    conn.close()
    assert logged["stage"] == "quantify"
    assert logged["records_in"] == 4
    assert logged["records_out"] == summary["rows"]
    assert "prevalence" in logged["notes"]


def test_dry_run_does_not_write_the_csv(tmp_path):
    db = tmp_path / "discovery.db"
    conn = init_db(db)
    fixture_corpus(conn)
    conn.close()
    settings = SimpleNamespace(
        interim_db=db,
        processed_dir=tmp_path / "processed",
        logs_dir=tmp_path / "logs",
        config_hash="abc123def456",
    )
    summary = dry_run(settings)
    assert summary["dry_run"] is True
    assert summary["analyzable_docs"] == 4
    assert not (tmp_path / "processed" / SCORES_NAME).exists()


def test_blocker_uncertainty_lift_is_p_both_over_p_a_p_b(tmp_path):
    conn = init_db(tmp_path / "discovery.db")
    fixture_corpus(conn)
    result = quantify(load_analyzable(conn))
    conn.close()
    pair = next(
        r
        for r in result.cooccurrence_lift
        if r["label_a"] == "fit_size_uncertainty" and r["label_b"] == "will_it_fit"
    )
    # Both tags on 2 of 4 docs: lift = 0.5 / (0.5 * 0.5) = 2
    assert pair["n_both"] == 2
    assert pair["lift"] == pytest.approx(lift(2, 2, 2, 4))
    assert pair["lift"] == pytest.approx(2.0)


def test_genuine_intent_subset_excludes_bookmark_only(tmp_path):
    conn = init_db(tmp_path / "discovery.db")
    persist(
        conn,
        document("g1"),
        tags(blockers=[BlockerType.FIT_SIZE_UNCERTAINTY], severity=4),
    )
    persist(
        conn,
        document("b1"),
        tags(
            blockers=[BlockerType.FIT_SIZE_UNCERTAINTY],
            severity=4,
            intent=IntentClass.BOOKMARK_ONLY,
        ),
    )
    persist(conn, document("g2"), tags())  # genuine, no label
    rows, _ = score_opportunities(load_analyzable(conn))
    conn.close()
    fit = next(r for r in rows if r["label"] == "fit_size_uncertainty")
    assert fit["n_docs"] == 2
    assert fit["n_docs_genuine"] == 1
    assert fit["prevalence"] == pytest.approx(2 / 3)
    assert fit["prevalence_genuine"] == pytest.approx(0.5)  # 1 of 2 genuine docs


def test_post_purchase_only_flag_when_no_pre_purchase_support(tmp_path):
    conn = init_db(tmp_path / "discovery.db")
    persist(
        conn,
        document(
            "c1",
            source="consumer_complaints_in",
            text="I asked for a return and they said no",
        ),
        tags(blockers=[BlockerType.RETURN_FRICTION], severity=5),
    )
    persist(conn, document("yt1"), tags())  # denom, pre-purchase, no labels
    rows, _ = score_opportunities(load_analyzable(conn))
    conn.close()
    ret = next(r for r in rows if r["label"] == "return_friction")
    assert ret["post_purchase_only"] is True
    assert ret["n_pre_purchase"] == 0
    assert ret["n_post_purchase"] == 1


def test_overlapping_tags_merge_into_one_opportunity(tmp_path):
    conn = init_db(tmp_path / "discovery.db")
    fixture_corpus(conn)
    rows, _ = score_opportunities(load_analyzable(conn))
    conn.close()
    labels = {(r["dimension"], r["label"]) for r in rows}
    assert ("uncertainty_type", "will_it_fit") not in labels
    fit = next(r for r in rows if r["label"] == "fit_size_uncertainty")
    assert "uncertainty_type=will_it_fit" in fit["cluster"]
    assert "uncertainty_type=will_it_fit" not in fit["co_occurs_with"]
    assert fit["n_docs"] == 2


def test_segment_cue_is_prevalence_not_an_opportunity(tmp_path):
    conn = init_db(tmp_path / "discovery.db")
    persist(
        conn,
        document("s1", text="first time buying online and the size chart makes no sense"),
        tags(
            blockers=[BlockerType.FIT_SIZE_UNCERTAINTY],
            segments=[SegmentCue.FIRST_TIME_ONLINE_BUYER],
        ),
    )
    persist(conn, document("s2"), tags())
    result = quantify(load_analyzable(conn), knobs=NO_MERGE)
    conn.close()
    opp_labels = {r["label"] for r in result.opportunities}
    prev_labels = {r["label"] for r in result.prevalence}
    assert "first_time_online_buyer" not in opp_labels
    assert "first_time_online_buyer" in prev_labels
    assert "fit_size_uncertainty" in opp_labels


def test_mixed_tagging_triples_are_refused(tmp_path):
    conn = init_db(tmp_path / "discovery.db")
    persist(conn, document("d1"), tags(blockers=[BlockerType.FIT_SIZE_UNCERTAINTY]))
    upsert_tags(
        conn,
        "d1",
        tags(blockers=[BlockerType.QUALITY_DOUBT]),
        taxonomy_version=TAXONOMY_VERSION,
        prompt_version="v2",
        model="test",
    )
    with pytest.raises(MixedTaggingError, match="refuses to mix"):
        load_analyzable(conn)
    conn.close()


def test_anonymous_sentinel_is_not_one_author(tmp_path):
    salt = "quantify-test-salt"
    sentinel = anonymous_author_hash("youtube", salt)
    conn = init_db(tmp_path / "discovery.db")
    for index in range(6):
        persist(
            conn,
            document(f"a{index}", author_hash=sentinel),
            tags(blockers=[BlockerType.FIT_SIZE_UNCERTAINTY], severity=4),
        )
    knobs = QuantifyKnobs(cluster_jaccard_min=0, author_salt=salt, min_distinct_authors=3)
    rows, _ = score_opportunities(load_analyzable(conn), knobs=knobs)
    conn.close()
    fit = next(r for r in rows if r["label"] == "fit_size_uncertainty")
    assert fit["n_authors"] == 6
    assert fit["reportable"] is True


def test_one_author_cluster_is_not_reportable(tmp_path):
    conn = init_db(tmp_path / "discovery.db")
    persist(
        conn,
        document("only", author_hash="abc" * 8),
        tags(blockers=[BlockerType.FIT_SIZE_UNCERTAINTY]),
    )
    persist(conn, document("other", author_hash="def" * 8), tags())
    rows, _ = score_opportunities(load_analyzable(conn), knobs=NO_MERGE)
    conn.close()
    fit = next(r for r in rows if r["label"] == "fit_size_uncertainty")
    assert fit["n_authors"] == 1
    assert fit["reportable"] is False
    assert fit["low_confidence"] is True


def test_screen_flags_do_not_drop_the_tag(tmp_path):
    conn = init_db(tmp_path / "discovery.db")
    persist(
        conn,
        document("flagged", text="I am still deciding about this wishlist save"),
        tags(
            blockers=[BlockerType.FIT_SIZE_UNCERTAINTY],
            quote_overrides={BlockerType.FIT_SIZE_UNCERTAINTY: "still deciding"},
        ),
    )
    persist(conn, document("empty"), tags())
    rows, _ = score_opportunities(load_analyzable(conn), knobs=NO_MERGE)
    conn.close()
    fit = next(r for r in rows if r["label"] == "fit_size_uncertainty")
    assert fit["n_docs"] == 1
    assert fit["flagged_evidence_share"] == 1.0
    assert fit["attribution_factor"] == 0.0
    assert fit["opportunity_score"] == 0.0


def test_quantify_modules_do_not_import_aggregates_or_collectors():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "quantify"
    banned = ("src.store.aggregates", "src.collect", "src.synthesize")
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        for module in modules:
            assert not any(module == banned_name or module.startswith(banned_name + ".") for banned_name in banned), (
                f"{path.name} imports {module}"
            )

