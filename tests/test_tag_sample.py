"""The tagging sample: reproducible, proportional, and additive to the corpus.

Sampling exists because 7,127 relevant documents is 23 free-tier days (plan §0.2),
and the cheapest wrong way to do it — flipping ``is_relevant`` to 0 on the
documents nobody can afford to tag — would store a budget decision in the column
that records a triage decision. So the sample is a side table, and these tests are
mostly about that: the corpus is unchanged, the draw is reproducible from its seed,
and an absent or empty table tags everything exactly as before.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.build_tag_sample import (
    CENSUS_SOURCES,
    SampleExistsError,
    allocate,
    draw_sample,
    existing_rows,
    taggable_by_source,
    write_sample,
)
from src.common.db import init_db, upsert_documents
from src.common.schemas import Document
from src.tag.run_tagging import _relevant_documents

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

#: Shaped like the real corpus: one dominant source, two mid-sized ones, and the
#: three small sources the census exists to protect.
CORPUS = {
    "youtube": 500,
    "play_store": 200,
    "app_store": 100,
    "consumer_complaints_in": 30,
    "quora_manual": 20,
    "complaints_board": 5,
}


def _document(source: str, index: int, **overrides) -> Document:
    payload = {
        "doc_id": f"{source}-{index:04d}",
        "source": source,
        "source_native_id": f"{source}-native-{index}",
        "text": "wishlisted this kurta but the size chart makes no sense to me",
        "is_relevant": True,
        "ingested_at": NOW,
    }
    payload.update(overrides)
    return Document(**payload)


@pytest.fixture
def conn(tmp_path):
    connection = init_db(tmp_path / "discovery.db")
    upsert_documents(
        connection,
        [
            _document(source, i)
            for source, count in CORPUS.items()
            for i in range(count)
        ],
    )
    yield connection
    connection.close()


# --- the universe ---------------------------------------------------------


def test_the_universe_is_the_taggers_own_predicate(conn):
    """Sampling from a wider pool than the tagger reads would draw untaggable ids."""
    upsert_documents(conn, [_document("youtube", 900, is_relevant=False)])
    upsert_documents(conn, [_document("youtube", 901)])
    conn.execute(
        "UPDATE documents SET is_duplicate_of = ? WHERE doc_id = ?",
        ("youtube-0000", "youtube-0901"),
    )

    universe = taggable_by_source(conn)
    assert sum(len(ids) for ids in universe.values()) == sum(CORPUS.values())
    assert "youtube-0900" not in universe["youtube"]  # is_relevant = 0
    assert "youtube-0901" not in universe["youtube"]  # a near-duplicate


def test_doc_ids_are_sorted_so_the_draw_does_not_depend_on_row_order(conn):
    universe = taggable_by_source(conn)
    for ids in universe.values():
        assert ids == sorted(ids)


# --- reproducibility ------------------------------------------------------


def test_the_same_seed_and_target_draw_the_same_documents(conn):
    universe = taggable_by_source(conn)
    first = draw_sample(universe, target=400, seed=42)
    second = draw_sample(universe, target=400, seed=42)
    assert first.doc_ids() == second.doc_ids()


def test_two_builds_write_the_same_doc_id_set(tmp_path, conn):
    """Reproducible through the table too, not just in memory."""
    universe = taggable_by_source(conn)
    write_sample(conn, draw_sample(universe, target=400, seed=42))
    first = {row[0] for row in conn.execute("SELECT doc_id FROM tag_sample")}

    write_sample(conn, draw_sample(universe, target=400, seed=42), force=True)
    second = {row[0] for row in conn.execute("SELECT doc_id FROM tag_sample")}
    assert first == second


def test_a_different_seed_draws_a_different_sample(conn):
    universe = taggable_by_source(conn)
    assert draw_sample(universe, target=400, seed=42).doc_ids() != draw_sample(
        universe, target=400, seed=43
    ).doc_ids()


# --- the two strata -------------------------------------------------------


def test_every_census_document_is_in_the_sample(conn):
    """The small sources are taken whole; sampling them buys nothing and costs voice."""
    universe = taggable_by_source(conn)
    sample = draw_sample(universe, target=400, seed=42)
    for source in CENSUS_SOURCES:
        assert set(sample.selected[source]) == set(universe[source])


def test_non_census_sources_are_sampled_in_proportion(conn):
    universe = taggable_by_source(conn)
    sample = draw_sample(universe, target=400, seed=42)

    census = sum(CORPUS[s] for s in CENSUS_SOURCES)
    budget = 400 - census
    pool = sum(CORPUS[s] for s in CORPUS if s not in CENSUS_SOURCES)
    for source in ("youtube", "play_store", "app_store"):
        expected = budget * CORPUS[source] / pool
        assert abs(len(sample.selected[source]) - expected) <= 1


def test_the_total_lands_on_the_target(conn):
    universe = taggable_by_source(conn)
    sample = draw_sample(universe, target=400, seed=42)
    assert abs(sample.total_selected - 400) <= 2


def test_a_target_above_the_corpus_takes_everything(conn):
    universe = taggable_by_source(conn)
    sample = draw_sample(universe, target=99_000, seed=42)
    assert sample.total_selected == sum(CORPUS.values())


def test_a_source_is_never_asked_for_more_than_it_has():
    """Largest-remainder must cap per source or a thin source over-draws."""
    allocation = allocate({"big": 1000, "small": 3}, 900)
    assert allocation["small"] <= 3
    assert sum(allocation.values()) == 900


def test_allocation_sums_to_the_budget_rather_than_near_it():
    allocation = allocate({"a": 7, "b": 11, "c": 13}, 10)
    assert sum(allocation.values()) == 10


def test_a_census_larger_than_the_target_still_keeps_the_census(conn):
    universe = taggable_by_source(conn)
    sample = draw_sample(universe, target=10, seed=42)
    assert sample.total_selected == sum(CORPUS[s] for s in CENSUS_SOURCES)
    assert sample.selected.get("youtube", []) == []


# --- the sample is additive ----------------------------------------------


def test_building_a_sample_changes_no_document_row(conn):
    before = [
        tuple(row)
        for row in conn.execute(
            "SELECT doc_id, is_relevant, is_duplicate_of FROM documents ORDER BY doc_id"
        )
    ]
    write_sample(conn, draw_sample(taggable_by_source(conn), target=400, seed=42))
    after = [
        tuple(row)
        for row in conn.execute(
            "SELECT doc_id, is_relevant, is_duplicate_of FROM documents ORDER BY doc_id"
        )
    ]
    assert before == after


def test_an_existing_sample_is_not_replaced_without_force(conn):
    universe = taggable_by_source(conn)
    write_sample(conn, draw_sample(universe, target=400, seed=42))
    with pytest.raises(SampleExistsError):
        write_sample(conn, draw_sample(universe, target=100, seed=7))


def test_force_redraws_the_table(conn):
    universe = taggable_by_source(conn)
    write_sample(conn, draw_sample(universe, target=400, seed=42))
    written = write_sample(conn, draw_sample(universe, target=100, seed=7), force=True)
    assert existing_rows(conn) == written


def test_existing_rows_is_zero_when_the_table_was_never_created(conn):
    assert existing_rows(conn) == 0


# --- the selection hook ---------------------------------------------------


def test_without_the_table_the_tagger_reads_the_whole_relevant_corpus(conn):
    """Backward compatibility: absent table means tag everything, as before."""
    assert len(_relevant_documents(conn)) == sum(CORPUS.values())


def test_an_empty_table_also_means_tag_everything(conn):
    conn.execute("CREATE TABLE tag_sample (doc_id TEXT PRIMARY KEY, source TEXT, drawn TEXT)")
    assert len(_relevant_documents(conn)) == sum(CORPUS.values())


def test_a_populated_table_narrows_the_tagger_to_the_sample(conn):
    sample = draw_sample(taggable_by_source(conn), target=400, seed=42)
    write_sample(conn, sample)

    selected = _relevant_documents(conn)
    assert {d["doc_id"] for d in selected} == set(sample.doc_ids())
    assert len(selected) < sum(CORPUS.values())
    assert all(d["text"] for d in selected)


def test_a_sampled_document_that_stops_being_relevant_is_still_excluded(conn):
    """The sample narrows the taggable set; it cannot widen it past the predicate."""
    sample = draw_sample(taggable_by_source(conn), target=400, seed=42)
    write_sample(conn, sample)
    victim = sample.doc_ids()[0]
    conn.execute("UPDATE documents SET is_relevant = 0 WHERE doc_id = ?", (victim,))

    assert victim not in {d["doc_id"] for d in _relevant_documents(conn)}


def test_dropping_the_table_restores_the_full_job(conn):
    write_sample(conn, draw_sample(taggable_by_source(conn), target=400, seed=42))
    conn.execute("DROP TABLE tag_sample")
    assert len(_relevant_documents(conn)) == sum(CORPUS.values())


# --- what the report has to be able to say --------------------------------


def test_the_spec_records_enough_to_redraw_the_sample(conn):
    sample = draw_sample(taggable_by_source(conn), target=400, seed=42)
    spec = sample.spec()
    assert spec["seed"] == 42
    assert spec["target"] == 400
    assert spec["taggable_total"] == sum(CORPUS.values())
    assert spec["sampled_total"] == sample.total_selected
    assert spec["sampled_by_source"]["quora_manual"] == CORPUS["quora_manual"]
    assert sorted(spec["census_sources"]) == sorted(CENSUS_SOURCES)


def test_every_written_row_names_the_source_it_came_from(conn):
    write_sample(conn, draw_sample(taggable_by_source(conn), target=400, seed=42))
    rows = conn.execute("SELECT doc_id, source, drawn FROM tag_sample").fetchall()
    assert all(row["doc_id"].startswith(row["source"]) for row in rows)
    assert all(row["drawn"] for row in rows)
