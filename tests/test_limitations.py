"""The limitations section: the hand-collection caveat, and its two live figures.

The product count and the snapshot range are asserted against the records rather
than against a literal, because typing either one in is how a limitation stops
being true without anyone noticing.
"""

from __future__ import annotations

from pathlib import Path

from src.store.aggregates import AjioAggregate
from src.synthesize.limitations import (
    MEASURED_SNAPSHOT,
    hand_collected_paragraph,
    render_section,
    snapshot_phrase,
    snapshot_range,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def aggregate(product_id="410334633", **overrides):
    payload = {
        "source": "ajio_aggregate",
        "product_id": product_id,
        "url": f"https://www.ajio.com/p/{product_id}",
        "extracted_at": "2026-08-23T12:26:01.336Z",
        "rating_distribution": {"5": 54, "4": 16, "3": 11, "2": 3, "1": 13},
        "opinions": [],
    }
    payload.update(overrides)
    return AjioAggregate.model_validate(payload)


def test_the_snapshot_range_is_read_from_the_records():
    """Earliest and latest stamp, shortened to the date part."""
    aggregates = [
        aggregate("1", extracted_at="2026-08-23T16:28:08.424Z"),
        aggregate("2", extracted_at="2026-08-23T12:26:01.336Z"),
    ]

    assert snapshot_range(aggregates) == ("2026-08-23", "2026-08-23")


def test_a_single_day_of_grabs_reads_as_one_date_not_a_null_looking_range():
    """"between X and X" reads like an unfilled template, so it collapses."""
    assert snapshot_phrase([aggregate()]) == "on 2026-08-23"
    assert "collected on 2026-08-23 (per each record" in hand_collected_paragraph([aggregate()])


def test_a_real_multi_day_range_still_renders_as_a_range():
    """The collapse is cosmetic; a second day of grabs must show as a span."""
    aggregates = [
        aggregate("1", extracted_at="2026-08-23T16:28:08.424Z"),
        aggregate("2", extracted_at="2026-09-02T09:00:00.000Z"),
    ]

    assert snapshot_phrase(aggregates) == "between 2026-08-23 and 2026-09-02"
    assert "collected between 2026-08-23 and 2026-09-02" in hand_collected_paragraph(aggregates)


def test_an_unstamped_batch_falls_back_rather_than_rendering_an_empty_range():
    assert snapshot_range([aggregate(extracted_at=None)]) == MEASURED_SNAPSHOT
    assert "collected on 2026-08-23" in hand_collected_paragraph([])


def test_the_product_count_comes_from_the_batch_not_a_literal():
    """N is the figure most likely to drift, since re-grabbing changes it."""
    paragraph = hand_collected_paragraph([aggregate(str(i)) for i in range(3)])

    assert "N=3 products" in paragraph
    assert "N=51" not in paragraph


def test_the_paragraph_states_every_caveat_it_is_there_for():
    paragraph = hand_collected_paragraph([aggregate()])

    assert "gathered manually rather than through the automated pipeline" in paragraph
    assert "purposive" in paragraph and "not a random or exhaustive sample" in paragraph
    # Buyers, not the population the study is about.
    assert "not the wishlist-abandoners this study targets" in paragraph
    assert "never as primary evidence" in paragraph
    assert "method-reproducible" in paragraph and "not\ncommand-reproducible" not in paragraph
    assert "command-reproducible" in paragraph


def test_the_hand_collection_caveat_is_always_last():
    """It qualifies the evidence base, not whichever finding precedes it."""
    out = render_section(["Corpus limitation A.", "Corpus limitation B."], aggregates=[aggregate()])

    assert out.startswith("## Limitations\n")
    assert out.index("Corpus limitation A.") < out.index("Corpus limitation B.")
    assert out.index("Corpus limitation B.") < out.index("**Hand-collected data")
    assert out.rstrip().endswith("re-collection yields a fresh snapshot.")


def test_the_section_renders_with_no_corpus_entries_yet():
    """Phase 6 is unbuilt, so the caveat has to stand on its own today."""
    out = render_section(aggregates=[aggregate()])

    assert "## Limitations" in out
    assert "**Hand-collected data" in out


def test_the_production_aggregates_fill_the_paragraph():
    """The real directory, so a stale date range fails here rather than in review."""
    from src.common.config import load_run_config

    run_config, _ = load_run_config()
    directory = PROJECT_ROOT / run_config.paths.aggregates_dir
    if not (directory / "ajio").is_dir():  # a clone without the side-channel
        return

    out = render_section(aggregates_dir=directory)

    assert "N=51 products" in out
    assert "collected on 2026-08-23" in out
