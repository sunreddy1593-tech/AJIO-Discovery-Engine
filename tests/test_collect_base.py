"""Phase 2 plumbing: cleaning, redaction, manifests, and idempotency.

Every test here runs offline. The collection layer's job is to be boring and
correct, and the failures that actually happen — a duplicate written twice, a
phone number surviving into the corpus, a truncated line killing a reader — are
all reachable without a network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.collect.base import (
    Collector,
    RateLimiter,
    RawWriter,
    RequestBudget,
    RequestBudgetExhausted,
    ZeroYieldError,
    clean_text,
    has_manifest,
    manifest_path,
    parse_date,
    read_records,
    redact_pii,
    stage_counts,
)
from src.common.schemas import RawRecord

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


class DummyCollector(Collector):
    source = "mouthshut"
    min_expected_records = 5

    def fetch(self, cfg):  # pragma: no cover - not exercised
        return iter(())


# --- text cleaning --------------------------------------------------------


def test_html_entities_are_unescaped_including_double_escapes():
    """Scraped pages are routinely double-escaped; one pass leaves &gt; visible."""
    assert clean_text("size &amp; fit") == "size & fit"
    assert clean_text("quality &amp;gt; price") == "quality > price"


def test_break_tags_become_newlines_and_known_tags_are_stripped():
    cleaned = clean_text("<p>first line<br/>second line</p>")
    assert "first line" in cleaned and "second line" in cleaned
    assert "<" not in cleaned


def test_user_text_containing_angle_brackets_survives():
    """A general <[^>]+> strip would eat this; the tag list is deliberately narrow."""
    assert clean_text("waist < 30 inches > useless for me") == "waist < 30 inches > useless for me"


def test_invisible_characters_are_removed_but_zwj_is_kept():
    """ZWJ composes emoji sequences Phase 3 must still detect (edge-case 3.2.3)."""
    assert clean_text("wish\u200blist") == "wishlist"
    assert "\u200d" in clean_text("family \U0001f468\u200d\U0001f469\u200d\U0001f467 emoji")


def test_paragraph_breaks_survive_because_quora_splits_on_them():
    cleaned = clean_text("question here\n\n\n\nanswer here")
    assert cleaned == "question here\n\nanswer here"


@pytest.mark.parametrize(
    "separator",
    ["\u2028", "\u2029", "\u0085", "\u000b", "\u000c", "\u001c", "\u001d", "\u001e"],
)
def test_every_line_separator_becomes_a_newline_not_just_crlf(separator):
    """The characters that split a JSONL line without a serializer escaping them.

    ``str.splitlines()`` treats all eight as line breaks; ``json`` escapes none of
    them, because JSON only requires escaping below U+0020. So a record carrying
    one is written as a single line and read back as several — which is exactly
    what happened to a YouTube comment that laid a numbered list out with U+2028.
    """
    assert clean_text(f"first{separator}second") == "first\nsecond"


def test_currency_and_typographic_symbols_are_untouched():
    """₹ must reach Phase 3 intact: the emoji rule is specified to keep it."""
    assert clean_text("worth ₹1299 — really?") == "worth ₹1299 — really?"


# --- PII redaction (edge-case 1.2.10) -------------------------------------


def test_email_and_phone_are_redacted():
    text = "mail me at asha.rao+ajio@gmail.com or call 9876543210 please"
    redacted = redact_pii(text)
    assert "asha.rao+ajio@gmail.com" not in redacted
    assert "9876543210" not in redacted
    assert "[email]" in redacted and "[phone]" in redacted


def test_phone_with_country_code_is_redacted():
    assert "[phone]" in redact_pii("reach me on +91 9876543210 today")


def test_labelled_order_id_keeps_its_label():
    """The report should be able to say an order id was cited without printing it."""
    redacted = redact_pii("my order id 4051234567 was never delivered")
    assert "4051234567" not in redacted
    assert "order" in redacted and "[id]" in redacted


def test_prices_and_sizes_are_not_mistaken_for_identifiers():
    """Price talk is a taxonomy dimension, so it must survive redaction intact."""
    kept = redact_pii("I paid ₹1299 for a size 32 kurta and waited 15 days")
    assert "1299" in kept and "32" in kept and "15" in kept


def test_social_handles_are_redacted_but_emails_are_not_double_handled():
    assert redact_pii("dm @ajiocare about it") == "dm [handle] about it"


def test_redaction_runs_inside_build_for_every_source():
    """The guarantee has to live in the base class, not in each collector."""
    collector = DummyCollector()
    record = collector.build(
        source_native_id="r1",
        text="Terrible service, call me on 9876543210 about order id 4051234567 now",
    )
    assert record is not None
    assert "9876543210" not in record.text
    assert "4051234567" not in record.text


# --- record construction --------------------------------------------------


def test_build_counts_rejections_instead_of_raising():
    """A single bad review must never end a three-day run (edge-case 1.2.1)."""
    collector = DummyCollector()
    assert collector.build(source_native_id="r1", text="   ") is None
    assert collector.build(source_native_id="r2", text="\n\n") is None
    assert collector.rejected == 2
    assert sum(collector.rejection_reasons.values()) == 2


def test_check_yield_raises_below_the_floor():
    """A redesigned page looks exactly like a quiet source (edge-case 1.1.7)."""
    collector = DummyCollector()
    collector.check_yield(10)
    with pytest.raises(ZeroYieldError, match="below its floor"):
        collector.check_yield(2)


# --- dates ----------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-05-11T08:14:00Z", datetime(2026, 5, 11, 8, 14, tzinfo=timezone.utc)),
        ("11 May 2026", datetime(2026, 5, 11, tzinfo=timezone.utc)),
        ("May 11, 2026", datetime(2026, 5, 11, tzinfo=timezone.utc)),
        ("2026-05-11", datetime(2026, 5, 11, tzinfo=timezone.utc)),
    ],
)
def test_absolute_dates_parse_to_utc(value, expected):
    assert parse_date(value) == expected


def test_relative_dates_are_resolved_against_a_reference():
    assert parse_date("3 days ago", now=NOW) == NOW - timedelta(days=3)
    assert parse_date("a month ago", now=NOW) == NOW - timedelta(days=30.44)
    assert parse_date("yesterday", now=NOW) == NOW - timedelta(days=1)


def test_unparseable_date_returns_none_rather_than_now():
    """Defaulting to now would win every recency weighting (edge-case 1.2.2)."""
    assert parse_date("some time last winter") is None
    assert parse_date("") is None
    assert parse_date(None) is None


# --- politeness -----------------------------------------------------------


def test_rate_limiter_waits_only_when_needed_and_tracks_per_domain():
    clock = {"t": 0.0}
    slept: list[float] = []

    def fake_sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    limiter = RateLimiter(3.0, sleep=fake_sleep, monotonic=lambda: clock["t"])
    limiter.wait("a.com")
    assert slept == []

    limiter.wait("b.com")  # a different domain must not be penalised
    assert slept == []

    limiter.wait("a.com")
    assert slept == [3.0]


def test_request_budget_is_shared_and_hard():
    budget = RequestBudget(2)
    budget.spend()
    budget.spend()
    assert budget.remaining == 0
    with pytest.raises(RequestBudgetExhausted, match="max_requests_per_run"):
        budget.spend()


# --- writer and manifests -------------------------------------------------


def record(native_id: str = "c1", **overrides) -> RawRecord:
    payload = {
        "source": "youtube",
        "source_native_id": native_id,
        "text": "this kurta has been in my wishlist for a month and I cannot decide",
        "created_utc": NOW,
        "collected_at": NOW,
        "collector_version": "1.0.0",
    }
    payload.update(overrides)
    return RawRecord(**payload)


@pytest.fixture
def writer(tmp_path):
    return RawWriter(
        raw_dir=tmp_path,
        source="youtube",
        run_date="2026-08-19",
        config_hash="abc123",
    )


def test_records_are_written_as_one_json_object_per_line(writer, tmp_path):
    assert writer.write(record("c1")) is True
    assert writer.write(record("c2")) is True
    writer.close()

    records = list(read_records(writer.path))
    assert [r.source_native_id for r in records] == ["c1", "c2"]


def test_a_record_is_one_line_under_splitlines_too(writer):
    """"One JSON object per line" has to hold for the strictest reader in the project.

    A YouTube comment used U+2028 to lay out a numbered list. Pydantic wrote it
    unescaped, so the record was one line to a reader splitting on ``\\n`` and six
    lines to anything calling ``str.splitlines()`` — which ``build_corpus`` did.
    It was counted as six malformed lines and dropped, silently, with only a number
    in the funnel to show for it.

    The record here is constructed directly rather than through ``build()``, which
    is the point: the guarantee belongs to the writer, so it cannot depend on the
    text having been cleaned first.
    """
    laid_out = (
        "Links are not working.\u2028"
        "1\ufe0f\u20e3 Go to my profile\u2028"
        "2\ufe0f\u20e3 Swipe left on the top menu"
    )
    assert writer.write(record("c1", text=laid_out)) is True
    writer.close()

    body = writer.path.read_text(encoding="utf-8").strip()
    assert len(body.splitlines()) == 1
    assert len(body.split("\n")) == 1

    # Escaped, not folded: at write time the text is final, so the round trip is
    # lossless and the corpus still says exactly what was collected.
    [restored] = list(read_records(writer.path))
    assert restored.text == laid_out


def test_duplicate_native_id_is_refused_within_a_run(writer):
    """Pagination overlap is normal; the DB constraint is the backstop, not this."""
    assert writer.write(record("c1")) is True
    assert writer.write(record("c1")) is False
    assert writer.written == 1
    assert writer.duplicates == 1


def test_manifest_records_counts_window_and_status(writer, tmp_path):
    writer.write(record("c1", created_utc=NOW - timedelta(days=5)))
    writer.write(record("c2", created_utc=NOW))
    path = writer.close(status="complete", extra={"pages_fetched": 3})

    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["record_count"] == 2
    assert payload["status"] == "complete"
    assert payload["config_hash"] == "abc123"
    assert payload["pages_fetched"] == 3
    assert payload["window"]["earliest_created_utc"].startswith("2026-08-14")
    assert payload["window"]["latest_created_utc"].startswith("2026-08-19")
    assert payload["parts"] == ["part-000.jsonl"]


def test_manifest_is_written_even_for_a_failed_run(writer):
    """An absent manifest means "never attempted"; that distinction drives re-runs."""
    writer.write(record("c1"))
    path = writer.close(status="quota_exhausted")
    import json

    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "quota_exhausted"


def test_second_run_on_the_same_date_appends_a_new_part(tmp_path):
    """Edge case 0.7: a same-day re-run must not overwrite part-000."""
    first = RawWriter(raw_dir=tmp_path, source="youtube", run_date="2026-08-19", config_hash="h")
    first.write(record("c1"))
    first.close()

    second = RawWriter(raw_dir=tmp_path, source="youtube", run_date="2026-08-19", config_hash="h")
    second.write(record("c2"))
    second.close()

    assert first.path.name == "part-000.jsonl"
    assert second.path.name == "part-001.jsonl"
    assert first.path.exists()


def test_has_manifest_is_what_makes_a_rerun_free(tmp_path, writer):
    assert has_manifest(tmp_path, "youtube", "2026-08-19") is False
    writer.close()
    assert has_manifest(tmp_path, "youtube", "2026-08-19") is True
    assert manifest_path(tmp_path, "youtube", "2026-08-19").name == "_manifest.json"


def test_max_records_cap_stops_the_writer(tmp_path):
    capped = RawWriter(
        raw_dir=tmp_path, source="youtube", run_date="2026-08-19", config_hash="h", max_records=2
    )
    assert capped.write(record("c1")) is True
    assert capped.write(record("c2")) is True
    assert capped.full is True
    assert capped.write(record("c3")) is False
    capped.close()


def test_truncated_final_line_is_tolerated_by_the_reader(writer):
    """A run killed by Ctrl-C or a sleeping laptop leaves exactly this (edge-case 0.3)."""
    writer.write(record("c1"))
    writer.close()
    with writer.path.open("a", encoding="utf-8") as handle:
        handle.write('{"source": "youtube", "source_nat')

    records = list(read_records(writer.path))
    assert len(records) == 1


# --- stage split ----------------------------------------------------------


def test_stage_counts_split_pre_and_post_purchase():
    """The lopsided-corpus warning in the summary depends on this."""
    records = [
        record("c1"),  # youtube: pre
        RawRecord(
            source="ajio_onsite",
            source_native_id="qa-1",
            text="does this kurta run small for a medium size person",
            meta={"content_type": "qa"},
            collected_at=NOW,
            collector_version="1.0.0",
        ),
        RawRecord(
            source="complaints_board",
            source_native_id="x1",
            text="my refund has not arrived after three weeks of waiting",
            collected_at=NOW,
            collector_version="1.0.0",
        ),
    ]
    counts = stage_counts(records)
    assert counts == {"pre_purchase": 2, "post_purchase": 1}
