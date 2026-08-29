"""The collection runner: skip-if-collected, caps, isolation, and the stage split.

Driven entirely through the manual Quora collector, which makes no network calls,
so the orchestration is exercised end to end offline: config in, JSONL and a
manifest out, run_log row written.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.collect import run_collection
from src.collect.base import ZeroYieldError, has_manifest
from src.common.schemas import SOURCE_STAGE, PurchaseStage

THREAD = """Why do I keep saving dresses on Ajio and never buying them?

The sizes are never consistent between brands, so I keep them saved and then the
occasion passes and I never actually place the order at all.

I wait for a sale every single time and by the time the discount arrives the size
I wanted has already gone out of stock completely.
"""


@pytest.fixture
def settings(tmp_path):
    """A minimal stand-in for ``Settings``: only what the runner actually touches."""
    quora_dir = tmp_path / "data" / "manual" / "quora"
    quora_dir.mkdir(parents=True)
    (quora_dir / "wishlist-thread.txt").write_text(THREAD, encoding="utf-8")

    return SimpleNamespace(
        project_root=tmp_path,
        raw_dir=tmp_path / "data" / "raw",
        interim_db=tmp_path / "data" / "interim" / "discovery.db",
        logs_dir=tmp_path / "logs",
        config_hash="testhash0000",
        credentials=SimpleNamespace(has_reddit=False),
        run=SimpleNamespace(
            collection=SimpleNamespace(
                quora_manual=SimpleNamespace(enabled=True, import_dir="data/manual/quora"),
                max_requests_per_run=100,
                respect_robots_txt=True,
                per_domain_delay_seconds=0.0,
                scraper_user_agent="test-agent",
            ),
            # The runner scores each record against Phase 3's exclusions as it is
            # written, so it can report a floor in the unit that survives.
            filters=SimpleNamespace(
                min_words=3,
                exclude_emoji=True,
                excluded_languages=["hi"],
                language_confidence=0.7,
                language_min_words=8,
            ),
        ),
    )


def collect(settings, **kwargs):
    defaults = dict(
        session_factory=lambda **_: None,
        run_date="2026-08-19",
        force=False,
        max_records=None,
    )
    defaults.update(kwargs)
    return run_collection.collect_source("quora_manual", settings, **defaults)


# --- the happy path -------------------------------------------------------


def test_a_source_writes_jsonl_and_a_manifest(settings):
    result = collect(settings)

    assert result.status == "complete"
    assert result.written == 2
    assert result.stages == {"pre_purchase": 2}

    part = settings.raw_dir / "quora_manual" / "2026-08-19" / "part-000.jsonl"
    assert part.is_file()
    assert len(part.read_text(encoding="utf-8").strip().splitlines()) == 2

    manifest = json.loads(
        (settings.raw_dir / "quora_manual" / "2026-08-19" / "_manifest.json").read_text("utf-8")
    )
    assert manifest["record_count"] == 2
    assert manifest["config_hash"] == "testhash0000"
    assert manifest["purchase_stage_counts"] == {"pre_purchase": 2}
    # Both units land in the manifest, so the corpus can be scored later without
    # re-reading and re-classifying every part file.
    assert manifest["eligible_documents"] == 2
    assert manifest["eligible_stage_counts"] == {"pre_purchase": 2}


def test_eligibility_is_scored_with_phase_3s_real_rules(settings):
    """A record is not a document, and the runner has to know the difference.

    The floor that mattered most in Phase 2 was counted in records and passed at
    4,494 while 180 documents reached the corpus (plan §3.3). So the runner scores
    each record against the actual exclusion rules rather than approximating them,
    and a short answer proves the two counts can diverge.
    """
    quora_dir = settings.project_root / "data" / "manual" / "quora"
    (quora_dir / "short-answers.txt").write_text(
        "Is Ajio sizing reliable?\n\n"
        "Sizes run small, so order one size up than you usually would here.\n\n"
        "This answer is long enough to clear the forty-character import floor and "
        "well past the three-word gate, so it becomes a document.\n",
        encoding="utf-8",
    )
    quora_dir.joinpath("wishlist-thread.txt").unlink()

    result = collect(settings)
    assert result.written == 2
    assert result.eligible == 2

    # Now a record that is a legitimate import but not a legitimate document. The
    # length gate no longer separates the two at this size, so the divergence is
    # shown with the emoji rule — the point being that the runner applies *all* of
    # Phase 3's rules, not that any one of them is the interesting one.
    for path in quora_dir.glob("*.txt"):
        path.unlink()
    (quora_dir / "terse.txt").write_text(
        "Does Ajio sizing run small?\n\n"
        "Absolutely, unquestionably, disproportionately small sizing here 😭\n",
        encoding="utf-8",
    )
    second = collect(settings, force=True)
    assert second.written == 1
    assert second.eligible == 0
    assert second.eligible_stages == {}


def test_every_written_line_validates_as_a_raw_record(settings):
    """A Phase 2 exit criterion: the JSONL is a contract, not a dump."""
    from src.collect.base import read_records

    collect(settings)
    part = settings.raw_dir / "quora_manual" / "2026-08-19" / "part-000.jsonl"
    records = list(read_records(part))
    assert len(records) == 2
    assert all(record.source == "quora_manual" for record in records)
    assert all(record.collector_version for record in records)


# --- idempotency ----------------------------------------------------------


def test_rerunning_without_force_does_no_work(settings):
    """The "re-runnable without re-scraping" guarantee, stated as a test."""
    collect(settings)
    assert has_manifest(settings.raw_dir, "quora_manual", "2026-08-19")

    second = collect(settings)
    assert second.status == "skipped"
    assert second.written == 0
    assert "no network calls" in second.detail

    parts = list((settings.raw_dir / "quora_manual" / "2026-08-19").glob("part-*.jsonl"))
    assert len(parts) == 1


def test_force_recollects_into_a_new_part_file(settings):
    collect(settings)
    result = collect(settings, force=True)
    assert result.status == "complete"

    parts = sorted(p.name for p in (settings.raw_dir / "quora_manual" / "2026-08-19").glob("part-*"))
    assert parts == ["part-000.jsonl", "part-001.jsonl"]


def test_max_records_caps_the_source_and_is_noted(settings):
    result = collect(settings, max_records=1)
    assert result.written == 1

    manifest = json.loads(
        (settings.raw_dir / "quora_manual" / "2026-08-19" / "_manifest.json").read_text("utf-8")
    )
    assert any("max-records" in note for note in manifest["notes"])


# --- failure isolation ----------------------------------------------------


def test_a_failing_source_is_recorded_rather_than_raising(settings, monkeypatch):
    """One broken selector must not cost the other eight sources their run."""

    class Exploding:
        collector_version = "1.0.0"
        rejected = 0
        rejection_reasons: dict[str, int] = {}

        def fetch(self, cfg):
            raise ZeroYieldError("page parsed to zero items")

        def check_yield(self, written):
            pass

    monkeypatch.setattr(
        run_collection, "build_collector", lambda *a, **k: Exploding()
    )
    result = collect(settings)

    assert result.status == "zero_yield"
    assert "zero items" in result.detail

    manifest = json.loads(
        (settings.raw_dir / "quora_manual" / "2026-08-19" / "_manifest.json").read_text("utf-8")
    )
    assert manifest["status"] == "zero_yield"


def test_quota_exhaustion_keeps_what_was_collected(settings, monkeypatch):
    """Edge case 1.1.1: a daily-limit stop is a normal free-tier outcome."""
    from src.collect.base import QuotaExhausted
    from src.common.schemas import RawRecord
    from datetime import datetime, timezone

    class Partial:
        collector_version = "1.0.0"
        rejected = 0
        rejection_reasons: dict[str, int] = {}

        def fetch(self, cfg):
            yield RawRecord(
                source="quora_manual",
                source_native_id="a1",
                text="i keep saving kurtas and never buying them because of sizing",
                collected_at=datetime.now(timezone.utc),
                collector_version="1.0.0",
            )
            raise QuotaExhausted("quota gone; resume tomorrow")

        def check_yield(self, written):
            pass

    monkeypatch.setattr(run_collection, "build_collector", lambda *a, **k: Partial())
    result = collect(settings)

    assert result.status == "quota_exhausted"
    assert result.written == 1  # the record collected before the stop survives


# --- CLI surface ----------------------------------------------------------


def test_source_order_covers_every_known_source():
    """Adding a collector without ordering it would silently skip it in every run."""
    assert set(run_collection.SOURCE_ORDER) == set(SOURCE_STAGE)


def test_ajio_is_collected_first_because_it_is_the_riskiest_and_most_valuable():
    order = run_collection.SOURCE_ORDER
    assert order[0] == "ajio_onsite"
    # The manual fallback sits immediately behind the source it stands in for, so
    # the block and its remedy are adjacent in the summary table rather than
    # separated by four sources that had nothing to do with either.
    assert order[1] == "ajio_manual"

    leading = order[:4]
    assert set(leading) == {"ajio_onsite", "ajio_manual", "youtube", "quora_manual"}
    assert not [
        name for name in leading if SOURCE_STAGE[name] is PurchaseStage.POST_PURCHASE
    ]


def test_argument_defaults_and_parsing():
    args = run_collection.parse_args([])
    assert args.sources is None and args.force is False and args.max_records is None
    assert len(args.run_date) == 10

    args = run_collection.parse_args(
        ["--sources", "youtube,mouthshut", "--force", "--max-records", "25"]
    )
    assert args.sources == "youtube,mouthshut"
    assert args.force is True
    assert args.max_records == 25


def test_summary_reports_the_stage_split_and_warns_when_lopsided(capsys, tmp_path):
    """A post-purchase-only corpus must be visible here, not at synthesis."""
    results = [
        run_collection.SourceResult("complaints_board", "complete", written=900,
                                    stages={"post_purchase": 900},
                                    eligible=880,
                                    eligible_stages={"post_purchase": 880}),
        run_collection.SourceResult("youtube", "complete", written=100,
                                    stages={"pre_purchase": 100},
                                    eligible=25,
                                    eligible_stages={"pre_purchase": 25}),
    ]
    run_collection.print_summary(results, raw_dir=tmp_path)

    output = capsys.readouterr().out
    assert "post_purchase" in output and "90.0%" in output
    assert "25 pre-purchase documents, below the floor of 2,000" in output
    # The record counts are still reported, but as context and clearly labelled,
    # because their unlabelled version certified a phase the next stage undid.
    assert "Leading indicators, for context only" in output
    assert "1,000 raw records against a target of 15,000" in output


def test_the_document_floor_gates_where_the_record_floor_passed(capsys, tmp_path):
    """The §3.3 regression, stated as a test.

    4,494 pre-purchase records cleared a floor of 2,000 while 180 pre-purchase
    documents reached the corpus. Any floor that can be satisfied by records the
    next stage deletes is not a floor, so the verdict has to be the document count
    even when — especially when — the record count looks comfortable.
    """
    class Floors:
        pre_purchase_records = 2000
        total_records = 15000
        pre_purchase_documents = 2000
        total_documents = 1500

    results = [
        run_collection.SourceResult(
            "youtube", "complete", written=4494, stages={"pre_purchase": 4494},
            eligible=180, eligible_stages={"pre_purchase": 180},
        ),
        run_collection.SourceResult(
            "play_store", "complete", written=11000, stages={"mixed": 11000},
            eligible=900, eligible_stages={"mixed": 900},
        ),
    ]
    run_collection.print_summary(results, raw_dir=tmp_path, floors=Floors())
    output = capsys.readouterr().out

    # Both record floors are met — 15,494 raw and 4,494 pre-purchase — so the old
    # summary printed no warning at all here.
    assert "Leading indicators" not in output
    assert "180 pre-purchase documents, below the floor of 2,000" in output
    assert "short by 1,820" in output
    assert "1080 projected documents, below the floor of 1,500" in output


def test_pre_purchase_shortfall_names_the_sources_that_could_have_supplied_it():
    """"You are short 1,900" is not actionable; "ajio_onsite is blocked" is."""
    results = [
        run_collection.SourceResult("ajio_onsite", "blocked", written=0, stages={}),
        run_collection.SourceResult("youtube", "complete", written=100,
                                    stages={"pre_purchase": 100},
                                    eligible=25,
                                    eligible_stages={"pre_purchase": 25}),
        run_collection.SourceResult("complaints_board", "complete", written=900,
                                    stages={"post_purchase": 900}),
    ]
    lines = "\n".join(run_collection._pre_purchase_shortfall(results))

    assert "ajio_onsite" in lines and "[blocked]" in lines
    # Both units per source: 25 of 100 says the source is being collected and its
    # content is short, which is a different problem from collecting nothing.
    assert "youtube" in lines and "25 documents" in lines and "100 records" in lines
    # Post-purchase-only sources are omitted: naming them would send whoever is
    # reading to fix a source that was never going to close the gap.
    assert "complaints_board" not in lines
    # quora_manual can supply pre-purchase records but did not run at all.
    assert "quora_manual" in lines and "not run" in lines


def test_a_route_disabled_in_config_reads_as_intentional_not_as_a_gap(capsys, tmp_path):
    """``ajio_manual`` is off because AJIO carries no on-site free text, sitewide.

    Printed as "not run" it reads as a collection task nobody has got to yet, which
    is the wrong instruction: there is no review or Q&A prose on the site to
    hand-collect, so the route is closed for the same structural reason
    ``ajio_onsite`` is blocked. The distinction comes from the settings' own
    enabled-source list rather than from a second copy of the config here.
    """
    results = [
        run_collection.SourceResult("youtube", "complete", written=100,
                                    stages={"pre_purchase": 100},
                                    eligible=25,
                                    eligible_stages={"pre_purchase": 25}),
    ]
    enabled = ["youtube", "quora_manual", "ajio_onsite"]

    by_source = {
        line.split()[0]: line
        for line in run_collection._pre_purchase_shortfall(results, enabled=enabled)
    }
    assert "disabled in config" in by_source["ajio_manual"]
    assert "AJIO has no on-site free text" in by_source["ajio_manual"]
    assert "not run" not in by_source["ajio_manual"]
    # An enabled route that did not run still says so; that difference is the point.
    assert "not run" in by_source["quora_manual"]
    assert "not run" in by_source["ajio_onsite"]

    run_collection.print_summary(results, raw_dir=tmp_path, enabled=enabled)
    output = capsys.readouterr().out
    assert "ajio_manual" in output and "disabled in config" in output
    assert "no free text to hand-" in output


def test_floors_come_from_config_rather_than_being_hardcoded(capsys, tmp_path):
    class Floors:
        pre_purchase_records = 10
        total_records = 20
        pre_purchase_documents = 8
        total_documents = 6

    results = [
        run_collection.SourceResult("youtube", "complete", written=5,
                                    stages={"pre_purchase": 5},
                                    eligible=3,
                                    eligible_stages={"pre_purchase": 3}),
    ]
    run_collection.print_summary(results, raw_dir=tmp_path, floors=Floors())

    output = capsys.readouterr().out
    assert "3 pre-purchase documents, below the floor of 8" in output
    assert "short by 5" in output
    assert "3 projected documents, below the floor of 6" in output
    assert "5 raw records against a target of 20" in output


def test_a_partial_run_says_its_totals_are_not_the_corpus(capsys, tmp_path):
    """Re-collecting one source must not print a corpus-wide verdict for one source.

    Without this the common case — fixing one broken collector — prints a full
    shortfall warning computed from a single source, which reads as a catastrophe
    and teaches the reader to skip the block that matters most.
    """
    results = [
        run_collection.SourceResult("youtube", "complete", written=100,
                                    stages={"pre_purchase": 100},
                                    eligible=30,
                                    eligible_stages={"pre_purchase": 30}),
        run_collection.SourceResult("play_store", "skipped"),
    ]
    run_collection.print_summary(
        results, raw_dir=tmp_path, enabled=["youtube", "play_store", "app_store"]
    )

    output = capsys.readouterr().out
    assert "covered 1 of 3 enabled source(s)" in output
    assert "app_store" in output and "play_store" in output
    assert "audit_collection.py" in output


def test_a_run_that_collected_nothing_still_warns(capsys, tmp_path):
    """The zero case is exactly when the shortfall most needs saying out loud."""
    results = [run_collection.SourceResult("ajio_onsite", "blocked", written=0)]
    run_collection.print_summary(results, raw_dir=tmp_path)

    output = capsys.readouterr().out
    assert "0 pre-purchase documents" in output
    assert "ajio_onsite" in output


def test_a_fully_skipped_run_reports_no_shortfall(capsys, tmp_path):
    """Attempting nothing is not the same as collecting nothing.

    The second re-run of a completed date skips every source, and printing a
    corpus-wide shortfall of zero against a floor of 2,000 there says nothing true.
    A source that *tried* and got nothing still warns — see the test above.
    """
    results = [
        run_collection.SourceResult("youtube", "skipped"),
        run_collection.SourceResult("play_store", "skipped"),
    ]
    run_collection.print_summary(results, raw_dir=tmp_path, enabled=["youtube", "play_store"])

    output = capsys.readouterr().out
    assert "covered 0 of 2 enabled source(s)" in output
    assert "below the floor" not in output
    assert "Leading indicators" not in output


def test_an_empty_manual_import_is_not_reported_as_complete(settings):
    """Plan §2: "the quietest failure of the run".

    ``quora_manual`` wrote a 0-byte part file and a manifest reading ``complete``
    on two consecutive run dates while being one of only three live pre-purchase
    routes. A source that yields nothing produces no funnel loss to notice, so the
    status has to carry the distinction that the record count cannot.
    """
    for path in (settings.project_root / "data" / "manual" / "quora").glob("*.txt"):
        path.unlink()

    result = collect(settings)
    assert result.status == "empty_import"
    assert result.written == 0
    assert "pre-purchase" in result.detail

    manifest = json.loads(
        (settings.raw_dir / "quora_manual" / "2026-08-19" / "_manifest.json").read_text("utf-8")
    )
    assert manifest["status"] == "empty_import"
    # A manifest is still written, so a re-run without --force stays a no-op: the
    # absence of one means "never attempted", and this was attempted.
    assert has_manifest(settings.raw_dir, "quora_manual", "2026-08-19")
