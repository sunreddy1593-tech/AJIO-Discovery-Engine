"""Blind gold worksheet and scorer: independent labels, no writes to doc_tags."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from scripts.build_gold_worksheet import (
    EMPTY_ROW_LABELS,
    MULTI_LABEL_FIELDS,
    allocate_with_source_floor,
    draw,
    empty_row,
    write_worksheet,
)
from scripts.score_gold_set import (
    binary_prf,
    evidence_precision,
    macro_f1,
    per_label_scores,
    score,
)
from src.common.db import init_db, upsert_documents, upsert_tags
from src.common.schemas import Document, DocumentTags, EvidenceSpan
from src.tag.taxonomy import TAXONOMY_VERSION, BlockerType, EvidenceTag, IntentClass, OutcomeMentioned


NOW = datetime(2026, 8, 28, tzinfo=timezone.utc)


def test_floor_gives_every_source_at_least_one_when_n_allows():
    counts = {"youtube": 420, "play_store": 94, "complaints_board": 5, "quora_manual": 107}
    allocation = allocate_with_source_floor(counts, 40)
    assert allocation["complaints_board"] >= 1
    assert sum(allocation.values()) == 40
    for source, take in allocation.items():
        assert 0 < take <= counts[source]


def test_same_seed_draws_the_same_doc_ids():
    universe = {
        "youtube": [(f"yt{i:02d}", f"text {i}") for i in range(20)],
        "quora_manual": [(f"q{i:02d}", f"quora {i}") for i in range(8)],
        "play_store": [(f"ps{i:02d}", f"play {i}") for i in range(6)],
    }
    a = [row[0] for row in draw(universe, n=10, seed=7)]
    b = [row[0] for row in draw(universe, n=10, seed=7)]
    c = [row[0] for row in draw(universe, n=10, seed=8)]
    assert a == b
    assert a != c


def test_worksheet_rows_have_empty_labels_and_no_tagger_fields(tmp_path):
    path = tmp_path / "gold_worksheet.jsonl"
    write_worksheet(path, [("aaaa1111bbbb2222", "youtube", "does this run small?")], n=1, seed=7)
    lines = path.read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0])
    row = json.loads(lines[1])
    assert meta["blind"] is True
    assert meta["seed"] == 7
    assert row["doc_id"] == "aaaa1111bbbb2222"
    assert row["text"] == "does this run small?"
    for field in MULTI_LABEL_FIELDS:
        assert row[field] == []
    assert row["intent_class"] == ""
    assert row["evidence"] == []
    assert "tags_json" not in row
    assert "blocker_type" in EMPTY_ROW_LABELS
    dumped = json.dumps(empty_row("x", "youtube", "hi"))
    assert "fit_size_uncertainty" not in dumped


def test_perfect_match_macro_f1_is_one():
    labels = ["fit_size_uncertainty", "return_friction"]
    gold = [{"fit_size_uncertainty"}, {"return_friction"}]
    pred = [{"fit_size_uncertainty"}, {"return_friction"}]
    per_label = per_label_scores(gold, pred, labels)
    assert macro_f1(per_label) == 1.0


def test_unused_labels_are_excluded_from_macro():
    labels = ["fit_size_uncertainty", "return_friction", "choice_overload"]
    gold = [{"fit_size_uncertainty"}]
    pred = [{"fit_size_uncertainty"}]
    per_label = per_label_scores(gold, pred, labels)
    assert per_label["choice_overload"]["support"] == 0
    assert per_label["choice_overload"]["fp"] == 0
    assert macro_f1(per_label) == 1.0


def test_binary_prf_zero_support():
    assert binary_prf(0, 0, 0) == (0.0, 0.0, 0.0)
    p, r, f1 = binary_prf(1, 1, 0)
    assert p == 0.5
    assert r == 1.0
    assert f1 == 2 * 0.5 * 1.0 / 1.5


def test_evidence_precision_counts_quotes_in_the_document():
    texts = {"d1": "the size chart is wrong and returns take weeks"}
    preds = [
        {
            "doc_id": "d1",
            "evidence": [
                {"tag": "fit_size_uncertainty", "quote": "size chart is wrong"},
                {"tag": "return_friction", "quote": "not in the document at all"},
            ],
        }
    ]
    result = evidence_precision(preds, texts)
    assert result["total"] == 2
    assert result["hits"] == 1
    assert result["precision"] == 0.5


def test_score_compares_gold_to_predictions_without_inventing_tags():
    gold_rows = [
        {
            "doc_id": "d1",
            "source": "youtube",
            "intent_class": "genuine_intent",
            "blocker_type": ["fit_size_uncertainty"],
            "uncertainty_type": [],
            "wishlist_motivation": [],
            "info_sought_elsewhere": [],
            "segment_cue": [],
        }
    ]
    predictions = {
        "d1": {
            "doc_id": "d1",
            "blocker_type": ["fit_size_uncertainty"],
            "uncertainty_type": [],
            "wishlist_motivation": [],
            "info_sought_elsewhere": [],
            "segment_cue": [],
            "intent_class": "genuine_intent",
            "evidence": [{"tag": "fit_size_uncertainty", "quote": "runs small"}],
        }
    }
    result = score(gold_rows, predictions, {"d1": "this kurta runs small"})
    assert result["n"] == 1
    assert result["dimensions"]["blocker_type"]["macro_f1"] == 1.0
    assert result["evidence"]["precision"] == 1.0
    assert result["intent_accuracy"] == 1.0


def test_score_refuses_empty_intent_class():
    gold_rows = [{"doc_id": "d1", "intent_class": "", "blocker_type": []}]
    result = score(gold_rows, {"d1": {}}, {"d1": "x"})
    assert result["error"] == "unlabelled"


def test_scorer_reads_doc_tags_and_does_not_insert(tmp_path):
    """A tagged fixture is readable; row counts do not change."""
    conn = init_db(tmp_path / "discovery.db")
    upsert_documents(
        conn,
        [
            Document(
                doc_id="a" * 16,
                source="youtube",
                source_native_id="c1",
                text="size chart makes no sense",
                created_utc=NOW,
                ingested_at=NOW,
            )
        ],
    )
    upsert_tags(
        conn,
        "a" * 16,
        DocumentTags(
            is_relevant=True,
            wishlist_motivation=[],
            blocker_type=[BlockerType.FIT_SIZE_UNCERTAINTY],
            uncertainty_type=[],
            info_sought_elsewhere=[],
            segment_cue=[],
            intent_class=IntentClass.GENUINE_INTENT,
            outcome_mentioned=OutcomeMentioned.NOT_STATED,
            severity=3,
            actionability_non_monetary=1,
            confidence_pct=80,
            evidence=[
                EvidenceSpan(tag=EvidenceTag.FIT_SIZE_UNCERTAINTY, quote="size chart makes no sense")
            ],
        ),
        taxonomy_version=TAXONOMY_VERSION,
        prompt_version="v1",
        model="openai/gpt-oss-120b",
    )
    before_tags = conn.execute("SELECT COUNT(*) FROM doc_tags").fetchone()[0]
    before_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    from scripts.score_gold_set import load_predictions

    preds = load_predictions(conn, ["a" * 16])
    assert "a" * 16 in preds
    assert preds["a" * 16]["blocker_type"] == ["fit_size_uncertainty"]
    assert conn.execute("SELECT COUNT(*) FROM doc_tags").fetchone()[0] == before_tags
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == before_docs
    conn.close()
