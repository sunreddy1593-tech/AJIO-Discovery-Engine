"""AJIO's aggregate numbers: derived averages, tolerant loading, and the wall.

These records are the one input that is deliberately *not* a document. The last
test in this file is the important one: it asserts ``ajio_aggregate`` never
becomes a collect source, because the failure it guards against is silent — a
single row summarising hundreds of raters would be counted as one voice and would
inflate whatever it agreed with, with nothing in the funnel to show it.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.store.aggregates import (
    AjioAggregate,
    by_product_id,
    load_ajio_aggregates,
    scan_ajio_aggregates,
    summarize,
)

FIT = {
    "question": "How was the Product fit?",
    "options": {"Perfect": 65, "Loose": 12, "Tight": 9, "Too Loose": 3, "Too Tight": 9},
}
QUALITY = {
    "question": "How was the Product Quality?",
    "options": {"Excellent": 27, "Very Good": 29, "Average": 32, "Bad": 5, "Very Bad": 5},
}


def record(product_id="410334633", **overrides):
    payload = {
        "source": "ajio_aggregate",
        "product_id": product_id,
        "product_title": "Anouk Women Straight Kurta",
        "url": f"https://www.ajio.com/p/{product_id}",
        "extracted_at": "2026-08-23T17:56:20Z",
        "average_rating": None,
        "rating_count": 59,
        "rating_distribution": {"5": 54, "4": 16, "3": 11, "2": 3, "1": 13},
        "opinions": [FIT, QUALITY],
    }
    payload.update(overrides)
    return payload


def write(directory: Path, name: str, payload) -> Path:
    path = directory / name
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )
    return path


# --- the derived average --------------------------------------------------


def test_average_is_derived_from_the_star_distribution():
    """The weighted mean, divided by the buckets' actual sum rather than by 100.

    AJIO rounds each bucket on its own, so they sum to 97 here, not 100. Dividing
    by 100 would treat the missing 3% as ratings of zero stars — impossible on a
    1–5 scale — and pull every average down about 0.1 in the same direction, a
    one-sided bias with nothing recording that it happened.

    5*54 + 4*16 + 3*11 + 2*3 + 1*13 = 386, over 97, is 3.979 -> 4.0.
    """
    aggregate = AjioAggregate.model_validate(record())

    assert aggregate.average_rating == 4.0
    assert aggregate.average_rating_source == "distribution"


def test_a_reported_average_is_never_overwritten():
    """A figure AJIO published outranks anything derived from the buckets."""
    aggregate = AjioAggregate.model_validate(record(average_rating=3.4))

    assert aggregate.average_rating == 3.4
    assert aggregate.average_rating_source == "reported"


def test_no_average_and_no_distribution_stays_unknown():
    """Absence is reported as absence, not as a zero or a guess."""
    aggregate = AjioAggregate.model_validate(
        record(average_rating=None, rating_distribution={})
    )

    assert aggregate.average_rating is None
    assert aggregate.average_rating_source is None


def test_an_all_zero_distribution_does_not_divide_by_zero():
    aggregate = AjioAggregate.model_validate(
        record(rating_distribution={"5": 0, "4": 0, "3": 0, "2": 0, "1": 0})
    )

    assert aggregate.average_rating is None
    assert aggregate.average_rating_source is None


# --- loading a directory --------------------------------------------------


def test_one_bad_file_costs_itself_and_nothing_else(tmp_path):
    """Both failure shapes already on disk: a 0-byte grab and a doubled grab.

    Fifty good products must not be lost to one bad one, so the loader skips and
    warns per file. This is the same blast-radius rule the manual loader needed.
    """
    write(tmp_path, "410334633.json", record("410334633"))
    # A grab that ran twice into one file: two objects, so "Extra data".
    write(tmp_path, "703592968.json", json.dumps(record("703592968")) + "}")
    write(tmp_path, "703654593.json", "")

    result = scan_ajio_aggregates(tmp_path)

    assert len(result.aggregates) == 1
    assert result.aggregates[0].product_id == "410334633"
    assert len(result.warnings) == 2
    assert sorted(result.files_skipped) == ["703592968.json", "703654593.json"]
    joined = " | ".join(result.warnings)
    assert "703654593.json" in joined and "empty" in joined
    assert "703592968.json" in joined
    assert load_ajio_aggregates(tmp_path)[0].product_id == "410334633"


def test_readme_and_non_json_files_are_ignored_without_a_warning(tmp_path):
    """Docs beside data are normal; a warning per run would train the reader to skip."""
    write(tmp_path, "410334633.json", record())
    (tmp_path / "README.md").write_text("# schema notes\n", encoding="utf-8")
    (tmp_path / "grab.log").write_text("noise\n", encoding="utf-8")

    result = scan_ajio_aggregates(tmp_path)

    assert len(result.aggregates) == 1
    assert result.warnings == []


def test_a_regrabbed_product_keeps_the_newest_extraction(tmp_path):
    """Re-grabbing must be safe, so identity is the product and recency wins."""
    write(tmp_path, "old.json", record(extracted_at="2026-08-01T09:00:00Z", rating_count=10))
    write(tmp_path, "new.json", record(extracted_at="2026-08-23T21:00:00Z", rating_count=59))

    result = scan_ajio_aggregates(tmp_path)

    assert len(result.aggregates) == 1
    assert result.aggregates[0].rating_count == 59
    assert result.superseded == ["410334633"]


def test_a_missing_directory_warns_rather_than_raising(tmp_path):
    result = scan_ajio_aggregates(tmp_path / "not_here")

    assert result.aggregates == []
    assert len(result.warnings) == 1
    assert "does not exist" in result.warnings[0]


def test_a_record_without_a_product_id_is_refused(tmp_path):
    """Unattributable numbers would carry a dead citation URL into the report."""
    write(tmp_path, "broken.json", record(product_id=""))

    result = scan_ajio_aggregates(tmp_path)

    assert result.aggregates == []
    assert "broken.json" in result.warnings[0]


# --- the report helpers ---------------------------------------------------


def test_fit_and_quality_shares_read_the_right_prompt():
    """Prompts are matched by wording, never by position in the list."""
    aggregate = AjioAggregate.model_validate(record(opinions=[QUALITY, FIT]))

    assert aggregate.top_fit_option() == "Perfect"
    assert aggregate.misfit_share() == 33  # 12 + 3 loose, 9 + 9 tight
    # "Average" is AJIO's own middle option and is not a bad verdict.
    assert aggregate.bad_quality_share() == 10


def test_summary_counts_products_and_keeps_denominators_apart():
    tight = record("1", opinions=[{"question": "How was the fit?",
                                  "options": {"Tight": 40, "Perfect": 35, "Loose": 22}}])
    quality_only = record("2", opinions=[QUALITY])
    summary = summarize([
        AjioAggregate.model_validate(record("0")),
        AjioAggregate.model_validate(tight),
        AjioAggregate.model_validate(quality_only),
    ])

    assert summary.products == 3
    assert summary.products_with_fit == 2
    assert summary.products_with_quality == 2
    assert summary.top_fit_is_tight == 1
    assert summary.top_fit_is_loose == 0
    assert summary.misfit_share_of_products == 0.5
    assert summary.ratings_derived == 3
    assert summary.ratings_reported == 0


def test_by_product_id_indexes_the_batch():
    aggregates = [AjioAggregate.model_validate(record(str(i))) for i in range(3)]
    assert sorted(by_product_id(aggregates)) == ["0", "1", "2"]


# --- the wall -------------------------------------------------------------


def test_ajio_aggregate_is_not_a_collect_source():
    """The guardrail, stated as a test.

    One aggregate row summarises hundreds of raters. Registered as a source it
    would be written to ``data/raw``, given a purchase stage, deduped and counted
    as a single voice — inflating whatever it agreed with, invisibly, because a
    number that validates as a record produces no funnel loss to notice.
    """
    from src.collect.run_collection import SOURCE_ORDER
    from src.common.config import load_run_config
    from src.common.schemas import KNOWN_SOURCES, SOURCE_STAGE, STAGE_BY_CONTENT_TYPE

    assert "ajio_aggregate" not in KNOWN_SOURCES
    assert "ajio_aggregate" not in SOURCE_STAGE
    assert "ajio_aggregate" not in STAGE_BY_CONTENT_TYPE
    assert "ajio_aggregate" not in SOURCE_ORDER

    run_config, _ = load_run_config()
    assert "ajio_aggregate" not in run_config.collection.enabled_sources()
    assert not hasattr(run_config.collection, "ajio_aggregate")


def test_the_reader_imports_no_collector_or_corpus_code():
    """A standalone side-channel: no path from these numbers into the corpus.

    Asserted on the import graph rather than trusted, because the damage would be
    done by a convenience import someone added later — ``build_corpus`` to reuse a
    loader, say — and the corpus would absorb the numbers without a visible step.
    """
    import ast

    source = Path(__file__).resolve().parents[1] / "src" / "store" / "aggregates.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    forbidden = {
        "src.collect.manual",
        "src.collect.base",
        "src.collect.ajio_manual",
        "src.collect.ajio_onsite",
        "src.collect.run_collection",
        "src.store.build_corpus",
        "src.store.dedupe",
        "src.store.relevance",
        "src.common.schemas",
    }
    assert not (modules & forbidden), modules & forbidden
    assert not any(module.startswith("src.collect") for module in modules)
    assert not any(module.startswith("src.tag") for module in modules)


def test_the_production_aggregates_load(tmp_path):
    """The ~51 records already on disk have to work retroactively, unmodified."""
    from src.common.config import load_run_config

    run_config, _ = load_run_config()
    directory = (
        Path(__file__).resolve().parents[1] / run_config.paths.aggregates_dir / "ajio"
    )
    if not directory.is_dir():  # a clone without the side-channel is legitimate
        return

    result = scan_ajio_aggregates(directory)
    assert result.aggregates, "no AJIO aggregate loaded from the production directory"
    assert all(a.source == "ajio_aggregate" for a in result.aggregates)
    assert all(a.product_id for a in result.aggregates)
    # Every average currently comes from the distribution, since AJIO reports none.
    assert all(a.average_rating_source in {"reported", "distribution"} for a in result.aggregates)
