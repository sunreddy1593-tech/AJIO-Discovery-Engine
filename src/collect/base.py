"""Collection plumbing shared by every source (`architecture.md` §5).

Five concerns live here so that no collector reimplements them, and so that a
compliance or privacy guarantee holds for all nine sources rather than for the
ones whose author remembered:

1. **Manifests and idempotency.** ``RawWriter`` owns the on-disk layout, the
   per-record flush, and the manifest. Every collector inherits
   skip-if-already-collected behaviour from :func:`has_manifest`.
2. **Text cleaning at the boundary.** HTML entities, stray tags, and zero-width
   characters are removed once, here, rather than discovered in Phase 3 as
   strange word counts (`edge-case.md` §1.2.5).
3. **PII redaction at collection.** Complaint boards embed order ids and phone
   numbers in the body. Redacting at collection *and* again at render is
   deliberate belt-and-braces: the raw JSONL is the thing most likely to be
   copied around by hand (§1.2.10).
4. **Politeness.** ``RateLimiter`` and ``RequestBudget`` are shared so a per-run
   request ceiling means something across sources rather than per collector.
5. **Rejection accounting.** ``Collector.build()`` funnels every record through
   ``RawRecord`` validation and counts what it drops, so an empty source is
   distinguishable from a source whose records were all malformed (§1.2.1).

Nothing here imports a HTTP client: ``scraping.py`` sits on top of this module,
which is what lets ``quora_manual.py`` inherit the plumbing while provably making
no network calls (§1.1.12).
"""

from __future__ import annotations

import html as html_module
import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

from pydantic import ValidationError

from src.common.logging import get_logger
from src.common.schemas import RawRecord

logger = get_logger("collect.base")

#: Stamped into every ``RawRecord``. Bump when a collector's parsing changes in a
#: way that would produce different text from the same page, so the corpus can be
#: partitioned by what produced it.
COLLECTOR_VERSION = "1.0.0"

MANIFEST_NAME = "_manifest.json"


# --------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------


class CollectionError(RuntimeError):
    """Base class for structural collection failures.

    The rule from `edge-case.md`: fail loudly on structural problems, degrade
    gracefully on data problems. Everything deriving from this is structural.
    """


class ZeroYieldError(CollectionError):
    """A page that should contain records parsed to none of them (§1.1.7).

    Raised rather than logged because a redesigned site looks exactly like a
    quiet one: the corpus would silently lose a whole population and every
    prevalence figure computed from it would still look plausible.
    """


class EmptyImportError(CollectionError):
    """A manual-import source has no files to import (plan §2, "the quietest failure").

    Raised rather than returned empty because the two are indistinguishable in a
    summary table, and one of them is a problem. ``quora_manual`` wrote a 0-byte
    part file and a manifest reading ``complete`` on two consecutive run dates
    while being one of only three live pre-purchase routes: a source that yields
    nothing produces no funnel loss for anyone to notice, so the absence has to
    announce itself here or it is never announced at all.

    Distinct from :class:`ZeroYieldError`, which means a page that should have had
    records parsed to none — a broken selector or a blocked client. This one means
    the hand-collection step has not been done, which no code change can fix.
    """


class QuotaExhausted(CollectionError):
    """A provider's daily quota ran out mid-run (§1.1.1).

    Not a failure: the caller writes the manifest for what was collected and
    exits 0 with resume instructions, because a multi-day collection is the
    expected shape of a free-tier run.
    """


class RequestBudgetExhausted(CollectionError):
    """The per-run request ceiling from ``config.yaml`` was reached."""


# --------------------------------------------------------------------------
# Text cleaning
# --------------------------------------------------------------------------

#: Invisible characters that break word counts and duplicate detection without
#: ever being visible in a log. ZWJ (U+200D) and ZWNJ (U+200C) are deliberately
#: **not** here: ZWJ composes emoji sequences that Phase 3 must still detect
#: (§3.2.3), and ZWNJ is meaningful in Devanagari.
_INVISIBLE_CHARS = {
    ord("\u200b"): None,  # zero-width space
    ord("\u2060"): None,  # word joiner
    ord("\ufeff"): None,  # BOM
    ord("\u00ad"): None,  # soft hyphen
    ord("\u200e"): None,  # LRM
    ord("\u200f"): None,  # RLM
}

#: Only tags a review body plausibly contains are stripped. A general
#: ``<[^>]+>`` would eat user text such as "waist < 30 inches > useless".
_TAG_RE = re.compile(
    r"</?(?:br|p|div|span|b|i|u|strong|em|a|li|ul|ol|small|sub|sup|font)\b[^>]*>",
    re.IGNORECASE,
)
_BREAK_RE = re.compile(r"<(?:br\s*/?|/p|/div|/li)\s*>", re.IGNORECASE)
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_SPACES_RE = re.compile(r"[ \t\u00a0\u2000-\u200a]{2,}")

#: Line separators that ``str.splitlines()`` honours but a JSON serializer does
#: not escape, so a record containing one is written as a single line and read
#: back as several. A YouTube comment using U+2028 to lay out a numbered list did
#: exactly that: one record became six unparseable fragments, which every reader
#: dutifully counted as "malformed" and skipped. Folded into ``\n`` here, where
#: the JSON escaping then makes them unambiguous, because the alternative is for
#: every reader in the project to remember to split on ``\n`` alone.
_LINE_SEPARATORS_RE = re.compile("[\u000b\u000c\u001c\u001d\u001e\u0085\u2028\u2029]")


def clean_text(raw: str) -> str:
    """Normalize collected text without changing what it says.

    HTML entities are unescaped twice because scraped pages are routinely
    double-escaped (``&amp;gt;`` for ``>``), and a single pass leaves a visible
    ``&gt;`` in the corpus. Paragraph structure is preserved — the manual Quora
    importer splits on blank lines, so collapsing them would merge answers
    (§1.2.9).

    Every character a reader might treat as a line break becomes ``\\n``, not just
    the CRLF pair: see :data:`_LINE_SEPARATORS_RE` for the one that reached the
    corpus and what it cost.

    Deliberately *not* NFKC-normalized: that would rewrite ``™`` to ``TM`` and
    fold characters Phase 3's exclusion rules are specified against. NFKC happens
    only in ``hashing.normalize_for_fingerprint``, where the text is not kept.
    """
    if not raw:
        return ""

    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _LINE_SEPARATORS_RE.sub("\n", text)
    text = _BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)

    unescaped = html_module.unescape(text)
    if "&" in unescaped:
        unescaped = html_module.unescape(unescaped)
    text = unescaped.translate(_INVISIBLE_CHARS)

    text = _SPACES_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# PII redaction (§1.2.10)
# --------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")

#: Labelled identifiers keep their label so the sentence still reads, and so a
#: reader of the report can see that an order id *was* cited without seeing it.
_LABELLED_ID_RE = re.compile(
    r"\b(order|awb|tracking|docket|invoice|ref|reference|complaint|ticket|txn|transaction|shipment)"
    r"(\s*(?:id|no\.?|number|#)?\s*[:#\-]?\s*)"
    r"([A-Za-z0-9][A-Za-z0-9/\-]{5,})",
    re.IGNORECASE,
)

#: Indian mobile numbers, optionally +91-prefixed. Ten digits starting 6-9 cannot
#: collide with a price: ``₹1299`` is four digits and survives untouched, which
#: matters because price talk is a taxonomy dimension.
_PHONE_RE = re.compile(r"(?<![\d])(?:\+?91[\s\-]?)?[6-9]\d{9}(?![\d])")

#: Bare long digit runs after phones are handled: AWB and order numbers pasted
#: without a label. Ten digits is above any plausible price or size.
_LONG_DIGITS_RE = re.compile(r"(?<![\d])\d{10,}(?![\d])")

_HANDLE_RE = re.compile(r"(?<![\w/@.])@([A-Za-z0-9_.]{3,30})\b")

REDACTIONS = ("[email]", "[phone]", "[id]", "[handle]")


def redact_pii(text: str) -> str:
    """Replace personal identifiers with fixed tokens.

    Order matters: emails contain digits that the phone pattern would otherwise
    bite into, and labelled ids are matched before bare digit runs so the label
    survives. Each replacement is a single token, so word counts move by at most
    one and the Phase 3 length gate stays meaningful.
    """
    if not text:
        return text

    text = _EMAIL_RE.sub("[email]", text)
    text = _LABELLED_ID_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[id]", text)
    text = _PHONE_RE.sub("[phone]", text)
    text = _LONG_DIGITS_RE.sub("[id]", text)
    text = _HANDLE_RE.sub("[handle]", text)
    return text


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%b %d %Y",
)

_RELATIVE_RE = re.compile(
    r"(?:(\d+)|an?)\s+(minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE
)
_UNIT_DAYS = {
    "minute": 1 / 1440,
    "hour": 1 / 24,
    "day": 1.0,
    "week": 7.0,
    "month": 30.44,
    "year": 365.25,
}


def parse_date(value: str | None, *, now: datetime | None = None) -> datetime | None:
    """Best-effort UTC datetime from the date strings review sites render.

    Returns ``None`` rather than guessing when nothing matches. That is the
    correct outcome per §1.2.2 — quantification treats a missing timestamp as the
    corpus median, whereas defaulting to "now" would wrongly boost it to the top
    of every recency weighting.
    """
    if not value:
        return None

    text = " ".join(value.split())
    if not text:
        return None

    reference = now or datetime.now(timezone.utc)

    lowered = text.lower()
    if lowered in {"today", "just now", "a moment ago"}:
        return reference
    if lowered == "yesterday":
        return reference - timedelta(days=1)

    relative = _RELATIVE_RE.search(lowered)
    if relative:
        count = int(relative.group(1)) if relative.group(1) else 1
        days = _UNIT_DAYS[relative.group(2)] * count
        return reference - timedelta(days=days)

    candidate = text.replace("Sept ", "Sep ").rstrip(".")
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    try:  # ISO-8601 with offsets that strptime is fussy about
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Politeness
# --------------------------------------------------------------------------


class RateLimiter:
    """Minimum wall-clock gap between actions, tracked per key (usually a domain).

    ``sleep`` is injected so tests exercise the arithmetic without spending the
    delay, and so a caller can substitute an interruptible sleep.
    """

    def __init__(self, delay_seconds: float, *, sleep=time.sleep, monotonic=time.monotonic):
        self.delay_seconds = max(0.0, delay_seconds)
        self._sleep = sleep
        self._monotonic = monotonic
        self._last: dict[str, float] = {}

    def wait(self, key: str = "_default") -> float:
        """Block until ``delay_seconds`` has passed since the last call for ``key``."""
        now = self._monotonic()
        previous = self._last.get(key)
        waited = 0.0
        if previous is not None:
            remaining = self.delay_seconds - (now - previous)
            if remaining > 0:
                self._sleep(remaining)
                waited = remaining
                now = self._monotonic()
        self._last[key] = now
        return waited


class RequestBudget:
    """Hard ceiling on network requests for one run.

    Shared across collectors on purpose: a per-source budget would let nine
    sources each spend the "maximum" and produce nine times the intended load.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.spent = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def spend(self, count: int = 1) -> None:
        if self.spent + count > self.limit:
            raise RequestBudgetExhausted(
                f"per-run request budget of {self.limit} reached; "
                "raise collection.max_requests_per_run or narrow --sources"
            )
        self.spent += count


# --------------------------------------------------------------------------
# Raw JSONL output and manifests
# --------------------------------------------------------------------------


def is_import_documentation(path: Path) -> bool:
    """Whether a file in a manual import directory is instructions, not data.

    A README that documents the expected file format is, by construction, written
    in that format closely enough to parse — so without this check the import
    directory's own instructions become documents, and a manual source that has
    never been filled reports records anyway. Observed live: `data/manual/quora/`
    held nothing but its README and yielded nine pre-purchase records.

    Deliberately narrow. Only READMEs and the conventional "not data" prefixes
    are skipped, because a hand-collected file may legitimately be called
    anything else, and silently dropping real evidence is the worse error.
    """
    name = path.name
    return (
        name.startswith((".", "_"))
        or path.stem.lower() == "readme"
    )


def run_date_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def source_dir(raw_dir: Path, source: str, run_date: str) -> Path:
    return Path(raw_dir) / source / run_date


def manifest_path(raw_dir: Path, source: str, run_date: str) -> Path:
    return source_dir(raw_dir, source, run_date) / MANIFEST_NAME


def has_manifest(raw_dir: Path, source: str, run_date: str) -> bool:
    """Whether this source was already collected for ``run_date``.

    The basis of the "re-runnable without re-scraping" guarantee: ``--force`` is
    the only way past this check.
    """
    return manifest_path(raw_dir, source, run_date).is_file()


def _one_line(payload: str) -> str:
    """Escape any character a reader could mistake for the end of the record.

    ``clean_text`` already folds these into newlines, which the serializer then
    escapes properly, so this should never fire on a record built through
    :meth:`Collector.build`. It is here because "one JSON object per line" is the
    *file format's* guarantee and should not rest on a text cleaner upstream
    remembering: any ``RawRecord``, however constructed, gets written as one line.

    Escaping rather than folding, unlike ``clean_text``: at this point the text is
    final, so the character is preserved exactly and the round trip is lossless.
    """
    if not _LINE_SEPARATORS_RE.search(payload):
        return payload
    return _LINE_SEPARATORS_RE.sub(lambda m: f"\\u{ord(m.group()):04x}", payload)


def read_records(path: Path) -> Iterator[RawRecord]:
    """Yield validated records from a JSONL part file.

    A truncated final line is tolerated and logged rather than fatal: a run
    interrupted by Ctrl-C or a sleeping laptop leaves exactly that, and the
    records before it are perfectly good (§0.3).
    """
    with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("%s:%d is not valid JSON (truncated write?); skipped", path, number)
                continue
            try:
                yield RawRecord.model_validate(payload)
            except ValidationError as exc:
                logger.warning("%s:%d failed validation: %s", path, number, exc.error_count())


class RawWriter:
    """Append-only JSONL writer for one source and run date.

    Three behaviours are load-bearing rather than incidental:

    * **Per-record flush.** A three-day collection interrupted at any point
      leaves every record before the interruption readable (§0.3).
    * **Part rollover.** A second run on the same date writes ``part-001.jsonl``
      instead of overwriting ``part-000.jsonl`` (§0.7).
    * **In-run dedupe.** Pagination overlap is normal, so the writer refuses a
      ``source_native_id`` it has already written. The database unique constraint
      is the backstop, not the primary defence (§1.2.7).
    """

    def __init__(
        self,
        *,
        raw_dir: Path,
        source: str,
        run_date: str,
        config_hash: str,
        collector_version: str = COLLECTOR_VERSION,
        max_records: int | None = None,
    ):
        self.source = source
        self.run_date = run_date
        self.config_hash = config_hash
        self.collector_version = collector_version
        self.max_records = max_records
        self.dir = source_dir(Path(raw_dir), source, run_date)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self._next_part_path()

        self._handle = self.path.open("a", encoding="utf-8")
        self._seen: set[str] = set()
        self.written = 0
        self.duplicates = 0
        self.notes: list[str] = []
        self.started_at = datetime.now(timezone.utc)
        self.earliest: datetime | None = None
        self.latest: datetime | None = None

    def _next_part_path(self) -> Path:
        for index in range(1000):
            candidate = self.dir / f"part-{index:03d}.jsonl"
            if not candidate.exists():
                return candidate
        raise CollectionError(f"more than 1000 part files in {self.dir}")

    @property
    def full(self) -> bool:
        return self.max_records is not None and self.written >= self.max_records

    def note(self, message: str) -> None:
        """Record something the manifest should carry, e.g. a cap that was hit."""
        self.notes.append(message)
        logger.info("[%s] %s", self.source, message)

    def write(self, record: RawRecord) -> bool:
        """Persist one record. Returns False if it was a duplicate or the cap is hit."""
        if self.full:
            return False
        if record.source_native_id in self._seen:
            self.duplicates += 1
            return False

        self._seen.add(record.source_native_id)
        self._handle.write(_one_line(record.model_dump_json()) + "\n")
        self._handle.flush()
        self.written += 1

        if record.created_utc is not None:
            if self.earliest is None or record.created_utc < self.earliest:
                self.earliest = record.created_utc
            if self.latest is None or record.created_utc > self.latest:
                self.latest = record.created_utc
        return True

    def close(self, *, status: str = "complete", extra: dict[str, Any] | None = None) -> Path:
        """Flush, write the manifest, and return its path.

        The manifest is written even for a failed or quota-stopped run, with the
        status recorded. An absent manifest means "never attempted", and that
        distinction is what ``--force``-free re-runs depend on.
        """
        if not self._handle.closed:
            self._handle.close()

        payload: dict[str, Any] = {
            "source": self.source,
            "run_date": self.run_date,
            "status": status,
            "config_hash": self.config_hash,
            "collector_version": self.collector_version,
            "record_count": self.written,
            "duplicates_skipped": self.duplicates,
            "parts": sorted(p.name for p in self.dir.glob("part-*.jsonl")),
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "window": {
                "earliest_created_utc": self.earliest.isoformat() if self.earliest else None,
                "latest_created_utc": self.latest.isoformat() if self.latest else None,
            },
            "notes": self.notes,
        }
        if extra:
            payload.update(extra)

        target = self.dir / MANIFEST_NAME
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        return target


# --------------------------------------------------------------------------
# The collector contract
# --------------------------------------------------------------------------


class Collector(ABC):
    """One source of public conversation (`architecture.md` §5).

    Subclasses implement :meth:`fetch` and nothing else is required of them. They
    build records through :meth:`build`, which applies cleaning, redaction, and
    validation uniformly — the reason a new collector cannot accidentally skip
    the privacy step.
    """

    source: ClassVar[str]
    collector_version: ClassVar[str] = COLLECTOR_VERSION

    #: Below this, the source is assumed broken rather than quiet. Zero disables
    #: the check; set it per source where a plausible floor is known (§1.1.7).
    min_expected_records: ClassVar[int] = 0

    #: Whether this collector touches the network at all. ``quora_manual`` sets
    #: this False and a test asserts the module imports no HTTP client.
    makes_network_calls: ClassVar[bool] = True

    def __init__(self) -> None:
        self.log = get_logger(f"collect.{self.source}")
        self.rejected = 0
        self.rejection_reasons: dict[str, int] = {}

    @abstractmethod
    def fetch(self, cfg: Any) -> Iterator[RawRecord]:
        """Yield records for this source. Must not write to disk."""

    def build(
        self,
        *,
        source_native_id: str,
        text: str,
        url: str | None = None,
        author_raw: str | None = None,
        created_utc: datetime | str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> RawRecord | None:
        """Clean, redact, and validate one record.

        Returns ``None`` for a record the contract rejects — empty text, mojibake,
        a blank native id — after counting the reason. A single unparseable review
        must never end a run that has been going for days, but it must also never
        vanish without appearing in the funnel.
        """
        cleaned = redact_pii(clean_text(text))
        if isinstance(created_utc, str):
            created_utc = parse_date(created_utc)

        try:
            return RawRecord(
                source=self.source,
                source_native_id=str(source_native_id),
                url=url,
                author_raw=author_raw,
                created_utc=created_utc,
                text=cleaned,
                meta=meta or {},
                collected_at=datetime.now(timezone.utc),
                collector_version=self.collector_version,
            )
        except ValidationError as exc:
            reason = exc.errors()[0].get("msg", "invalid") if exc.errors() else "invalid"
            self.rejected += 1
            self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1
            self.log.debug("rejected %s/%s: %s", self.source, source_native_id, reason)
            return None

    def check_yield(self, written: int) -> None:
        """Raise when a source produced implausibly little (§1.1.7)."""
        if self.min_expected_records and written < self.min_expected_records:
            raise ZeroYieldError(
                f"{self.source} yielded {written} records, below its floor of "
                f"{self.min_expected_records}. This is the signature of a changed "
                "layout or a blocked client, not a quiet source."
            )


def stage_counts(records: Iterable[RawRecord]) -> dict[str, int]:
    """Pre/post-purchase split of a record iterable.

    Surfaced in the collection summary because a corpus that has quietly become
    all post-purchase produces a discovery engine that reports delivery
    complaints as wishlist blockers (`implementation-plan.md` §2.1).
    """
    counts: dict[str, int] = {}
    for record in records:
        stage = record.purchase_stage.value
        counts[stage] = counts.get(stage, 0) + 1
    return counts


__all__ = [
    "COLLECTOR_VERSION",
    "Collector",
    "CollectionError",
    "QuotaExhausted",
    "RateLimiter",
    "RawWriter",
    "RequestBudget",
    "RequestBudgetExhausted",
    "ZeroYieldError",
    "clean_text",
    "has_manifest",
    "is_import_documentation",
    "manifest_path",
    "parse_date",
    "read_records",
    "redact_pii",
    "run_date_utc",
    "source_dir",
    "stage_counts",
]
