"""Score Phase 2's six exit criteria against the corpus actually on disk.

    .venv\\Scripts\\python.exe scripts\\audit_collection.py
    .venv\\Scripts\\python.exe scripts\\audit_collection.py --no-language-check

Every criterion in `implementation-plan.md` §2 is about a *corpus*, and until this
script existed each one was scored by hand: someone opened the manifests, added the
record counts up, and wrote the total into the plan. That went wrong in both
directions. The plan carried 12,702 raw records for a day after the corpus had
grown past 50,000, and — the more expensive error — it recorded the pre-purchase
floor as *passed* at 4,494 records while 180 pre-purchase documents reached the
corpus, because the floor was counted one stage before the filters that removed 96%
of what it counted (§3.3).

So the numbers are computed here instead, from the files, on demand:

1. every enabled source has a run directory and a parseable ``_manifest.json``;
2. a re-run without ``--force`` would make zero network calls;
3. every line of every part file validates as a ``RawRecord``;
4. the corpus clears its size floors;
5. the corpus clears its pre-purchase floors;
6. a ``robots.txt`` policy file is on record for every scraped domain.

**Criteria 4 and 5 are scored in documents, not records.** A record is what a
collector wrote; a document is what survives Phase 3's hard exclusions and so is
the only thing the tagger and every downstream metric ever see. Both units are
printed, because the gap between them is itself a finding — it is where the
pre-purchase signal was lost — but only the document figure decides pass or fail.

Records are collapsed on ``(source, source_native_id)`` across run dates exactly as
``build_corpus.load_raw_records`` does, so a source collected twice is counted once
and the totals here match the funnel there rather than merely resembling it.

Exit code 0 means every criterion passed or was an accepted outcome. Accepted
outcomes are listed explicitly in :data:`ACCEPTED_ZERO_YIELD` rather than inferred,
because "this source yields nothing and that is fine" is exactly the claim that
should have to be written down and reviewed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import ValidationError  # noqa: E402

from src.collect.app_store import FEED_TEMPLATE  # noqa: E402
from src.collect.base import MANIFEST_NAME, has_manifest, run_date_utc  # noqa: E402
from src.collect.complaints_board import ComplaintsBoardCollector  # noqa: E402
from src.collect.consumer_complaints_in import ConsumerComplaintsInCollector  # noqa: E402
from src.collect.mouthshut import MouthShutCollector  # noqa: E402
from src.collect.run_collection import SOURCE_ORDER  # noqa: E402
from src.collect.trustpilot import TrustpilotCollector  # noqa: E402
from src.common.config import ConfigFileError, MissingConfigError, get_settings  # noqa: E402
from src.common.encoding import harden_stdio  # noqa: E402
from src.common.schemas import RawRecord, purchase_stage  # noqa: E402
from src.store.exclusions import classify_with_filters  # noqa: E402

#: Sources whose zero yield is a documented outcome rather than a fault, with the
#: reason each is accepted. Anything not listed here that yields nothing fails.
ACCEPTED_ZERO_YIELD: dict[str, str] = {
    "ajio_onsite": (
        "every content path is refused by an Akamai edge; defeating bot management is "
        "out of scope, and hand-collection is no longer a route around it — AJIO "
        "publishes no review or Q&A prose on site, so ajio_manual is disabled rather "
        "than awaiting a person (edge-case 1.1.13d)"
    ),
    "trustpilot": "robots.txt disallows /reviews/, so a compliant fetch returns nothing",
}

#: Sources that reach the network without a ``PoliteSession``, and so legitimately
#: have no ``robots.txt`` decision on record. ``play_store`` goes through
#: ``google-play-scraper`` and ``youtube`` through the Data API; both are documented
#: exceptions in their own modules.
NO_ROBOTS_GATE = frozenset({"play_store", "youtube", "reddit", "ajio_manual", "quora_manual"})


@dataclass
class Criterion:
    """One exit criterion and the evidence for how it scored."""

    name: str
    ok: bool
    detail: str
    accepted: bool = False
    lines: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.ok:
            return "PASS"
        return "ACCEPTED" if self.accepted else "FAIL"


@dataclass
class SourceTally:
    records: int = 0
    documents: int = 0
    stages: Counter = field(default_factory=Counter)
    document_stages: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)


@dataclass
class Scan:
    """The result of reading every part file once."""

    by_source: dict[str, SourceTally] = field(default_factory=dict)
    invalid_lines: list[str] = field(default_factory=list)
    superseded: int = 0
    files: int = 0

    def tally(self, source: str) -> SourceTally:
        return self.by_source.setdefault(source, SourceTally())

    @property
    def records(self) -> int:
        return sum(t.records for t in self.by_source.values())

    @property
    def documents(self) -> int:
        return sum(t.documents for t in self.by_source.values())

    def stage_records(self, stage: str) -> int:
        return sum(t.stages.get(stage, 0) for t in self.by_source.values())

    def stage_documents(self, stage: str) -> int:
        return sum(t.document_stages.get(stage, 0) for t in self.by_source.values())

    def reasons(self) -> Counter:
        total: Counter = Counter()
        for t in self.by_source.values():
            total.update(t.reasons)
        return total


# --------------------------------------------------------------------------
# Reading what is on disk
# --------------------------------------------------------------------------


def manifests(raw_dir: Path) -> dict[str, dict[str, Any]]:
    """The latest manifest per source, by run date.

    Latest rather than every one, because a source that failed on Monday and
    succeeded on Tuesday is a fixed source, not a failing one — the Monday manifest
    is history worth keeping on disk but it is not the current state.
    """
    latest: dict[str, tuple[str, dict[str, Any]]] = {}
    for path in sorted(raw_dir.glob(f"*/*/{MANIFEST_NAME}")):
        source, run_date = path.parent.parent.name, path.parent.name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            payload = {"status": "unreadable", "record_count": 0, "error": str(exc)}
        payload["_run_date"] = run_date
        payload["_path"] = str(path)
        known = latest.get(source)
        if known is None or run_date >= known[0]:
            latest[source] = (run_date, payload)
    return {source: payload for source, (_, payload) in latest.items()}


def scan_records(raw_dir: Path, filters: Any, *, check_language: bool) -> Scan:
    """Validate and tally every collected record, collapsing re-collected copies.

    Identity is ``(source, source_native_id)`` and files are read in date order, so
    a later run supersedes an earlier one and the counts here are the counts
    ``build_corpus`` will load — not an approximation of them.
    """
    scan = Scan()
    seen: dict[tuple[str, str], tuple[str, str | None]] = {}

    for path in sorted(raw_dir.glob("*/*/part-*.jsonl")):
        if "_compliance" in path.parts:
            continue
        scan.files += 1
        # Iterating the handle splits on newlines alone. ``splitlines()`` also breaks
        # on U+2028, U+2029, U+0085 and the C0 separators, which no JSON serializer
        # escapes — so it reports a perfectly good record as several invalid lines,
        # which is how this criterion first failed.
        with path.open("r", encoding="utf-8") as handle:
            numbered = list(enumerate(handle, start=1))
        for number, line in numbered:
            line = line.strip()
            if not line:
                continue
            try:
                record = RawRecord.model_validate_json(line)
            except ValidationError as exc:
                first = exc.errors()[0]
                scan.invalid_lines.append(
                    f"{path.relative_to(raw_dir)}:{number}  {first.get('msg', 'invalid')}"
                )
                continue

            key = (record.source, record.source_native_id)
            if key in seen:
                scan.superseded += 1

            try:
                stage = purchase_stage(record.source, record.meta).value
            except ValueError:
                # RawRecord already refuses a record whose stage cannot be resolved,
                # so reaching here means the mapping changed after collection.
                stage = "mixed"
            reason = classify_with_filters(record.text, filters) if check_language else _cheap(
                record.text, filters
            )
            seen[key] = (stage, reason)

    for (source, _), (stage, reason) in seen.items():
        tally = scan.tally(source)
        tally.records += 1
        tally.stages[stage] += 1
        if reason is None:
            tally.documents += 1
            tally.document_stages[stage] += 1
        else:
            tally.reasons[reason] += 1
    return scan


def _cheap(text: str, filters: Any) -> str | None:
    """The two deterministic rules only, for ``--no-language-check``.

    langdetect dominates the runtime on a corpus this size and moves the total by
    well under a percent, so skipping it is a useful fast path — but it makes the
    document count a slight overestimate, which the report says out loud.
    """
    from src.store.exclusions import emoji_is_the_substance, is_too_short

    if is_too_short(text, min_words=filters.min_words):
        return "too_short"
    if filters.exclude_emoji and emoji_is_the_substance(text, min_words=filters.min_words):
        return "contains_emoji"
    return None


def scraped_domains(settings: Any) -> dict[str, set[str]]:
    """The domains each enabled source fetches, derived from the collectors themselves.

    Asking the collector rather than hardcoding a table means a corrected URL in
    ``config.yaml`` is reflected here without a matching edit, which is the whole
    reason the URLs live in config.
    """
    collection = settings.run.collection
    listing_collectors = {
        "mouthshut": MouthShutCollector,
        "complaints_board": ComplaintsBoardCollector,
        "consumer_complaints_in": ConsumerComplaintsInCollector,
        "trustpilot": TrustpilotCollector,
    }

    domains: dict[str, set[str]] = {}
    for source in collection.enabled_sources():
        if source in NO_ROBOTS_GATE:
            continue
        cfg = getattr(collection, source)
        urls: list[str] = []
        if source in listing_collectors:
            # page_url and listings are pure; neither touches the session.
            collector = listing_collectors[source](None)  # type: ignore[arg-type]
            urls = [collector.page_url(listing, 1) for listing in collector.listings(cfg)]
        elif source == "ajio_onsite":
            urls = list(cfg.category_urls) + list(cfg.product_urls)
        elif source == "app_store":
            urls = [
                FEED_TEMPLATE.format(country=country, page=1, app_id=app_id)
                for app_id in cfg.app_ids
                for country in cfg.countries
            ]
        hosts = {urlparse(url).netloc for url in urls if urlparse(url).netloc}
        if hosts:
            domains[source] = hosts
    return domains


# --------------------------------------------------------------------------
# The six criteria
# --------------------------------------------------------------------------


def check_manifests(
    enabled: list[str], found: dict[str, dict[str, Any]], filters: Any
) -> Criterion:
    lines: list[str] = []
    failures: list[str] = []
    unfilled: list[str] = []
    accepted: list[str] = []

    for source in [s for s in SOURCE_ORDER if s in enabled]:
        manifest = found.get(source)
        if manifest is None:
            failures.append(source)
            lines.append(f"    {source:<24} no run directory")
            continue
        status = str(manifest.get("status", "unknown"))
        count = int(manifest.get("record_count", 0) or 0)
        note = f"{manifest['_run_date']}  {status:<15} {count:>7} records"
        if status == "complete" and count > 0:
            pass
        elif source in ACCEPTED_ZERO_YIELD:
            accepted.append(source)
            note += "  [accepted]"
        elif status == "empty_import":
            # Separated from a fault because the remedy is entirely different: no
            # code change fixes this, and reporting it as a broken collector sends
            # whoever is reading to debug a parser that works.
            unfilled.append(source)
            note += "  [needs hand-collection]"
        else:
            failures.append(source)
            note += "  [FAIL]"
        lines.append(f"    {source:<24} {note}")

    for source in accepted:
        lines.append(f"    accepted: {source} — {ACCEPTED_ZERO_YIELD[source]}")
    if unfilled:
        lines.append("")
        lines.append(
            f"    {', '.join(unfilled)} have nothing to import. These are pre-purchase"
        )
        lines.append(
            "    routes, so until they are filled the corpus is more post-purchase than"
        )
        lines.append(
            "    its stage split suggests. The format is in each directory's README;"
        )
        lines.append(
            f"    the length gate is now {filters.min_words} words, so short questions like"
        )
        lines.append('    "does this run small?" are worth collecting rather than skipping.')

    outstanding = failures + unfilled
    if outstanding:
        return Criterion(
            "every enabled source populated with a valid manifest",
            ok=False,
            detail=(
                f"{len(outstanding)} source(s) contributing nothing: "
                f"{', '.join(outstanding)}"
                + (f" ({len(unfilled)} awaiting hand-collection)" if unfilled else "")
            ),
            lines=lines,
        )
    return Criterion(
        "every enabled source populated with a valid manifest",
        ok=True,
        detail=f"{len(enabled)} enabled, {len(accepted)} accepted as zero-yield",
        lines=lines,
    )


def check_idempotent(
    raw_dir: Path, enabled: list[str], found: dict[str, dict[str, Any]]
) -> Criterion:
    """A manifest is what makes a re-run free, and it guards exactly one run date.

    Stated carefully because a loose reading of this criterion is false. Re-running
    *the same run date* is a no-op, which is the guarantee. Re-running *tomorrow*
    re-collects everything, because ``data/raw`` is date-partitioned and
    append-only by design — that is how a corpus grows rather than a bug. So the
    criterion is scored on the guarantee, and the cost of a run today is reported
    alongside it rather than folded into a pass or a fail.
    """
    without = [s for s in enabled if s not in found]
    if without:
        return Criterion(
            "a re-run of a collected date makes zero network calls",
            ok=False,
            detail=f"no manifest to skip on for: {', '.join(without)}",
        )

    today = run_date_utc()
    stale = [s for s in enabled if not has_manifest(raw_dir, s, today)]
    dates = sorted({m["_run_date"] for m in found.values()})
    lines: list[str] = []
    if stale:
        lines.append(
            f"    {len(stale)} source(s) have no manifest for {today}, so a run today"
        )
        lines.append(
            f"    would re-collect them: {', '.join(stale)}. That is the date partition"
        )
        lines.append(
            "    working as designed, not a broken skip — but it does cost requests, so"
        )
        lines.append("    pass --sources when you only meant to fix one of them.")
    return Criterion(
        "a re-run of a collected date makes zero network calls",
        ok=True,
        detail=(
            f"every enabled source has a manifest (run dates {dates[0]}..{dates[-1]}); "
            f"{len(enabled) - len(stale)} of {len(enabled)} also have one for {today}"
        ),
        lines=lines,
    )


def check_valid_records(scan: Scan) -> Criterion:
    if scan.invalid_lines:
        return Criterion(
            "every line in every JSONL file validates as a RawRecord",
            ok=False,
            detail=f"{len(scan.invalid_lines)} invalid line(s) across {scan.files} part files",
            lines=[f"    {line}" for line in scan.invalid_lines[:20]],
        )
    return Criterion(
        "every line in every JSONL file validates as a RawRecord",
        ok=True,
        detail=(
            f"{scan.records:,} records across {scan.files} part files, "
            f"{scan.superseded:,} re-collected copies superseded"
        ),
    )


def check_size_floor(scan: Scan, floors: Any) -> Criterion:
    documents, records = scan.documents, scan.records
    floor = floors.total_documents
    detail = (
        f"{documents:,} documents against a floor of {floor:,} "
        f"(from {records:,} raw records against a target of {floors.total_records:,})"
    )
    return Criterion("corpus size floor, in documents", ok=documents >= floor, detail=detail)


def check_pre_purchase_floor(scan: Scan, floors: Any) -> Criterion:
    documents = scan.stage_documents("pre_purchase")
    records = scan.stage_records("pre_purchase")
    floor = floors.pre_purchase_documents
    detail = (
        f"{documents:,} pre-purchase documents against a floor of {floor:,} "
        f"(from {records:,} raw records against a target of "
        f"{floors.pre_purchase_records:,})"
    )
    lines: list[str] = []
    if records and documents < floor:
        lines.append(
            f"    {documents / records:.1%} of pre-purchase records survive the hard "
            "exclusions, so collecting"
        )
        lines.append(
            f"    the shortfall of {floor - documents:,} takes roughly "
            f"{int((floor - documents) / max(documents / records, 0.001)):,} more records."
        )
    return Criterion(
        "pre-purchase floor, in documents",
        ok=documents >= floor,
        detail=detail,
        lines=lines,
    )


def check_compliance(raw_dir: Path, domains: dict[str, set[str]]) -> Criterion:
    compliance_dir = raw_dir / "_compliance"
    decisions = compliance_dir / "robots_decisions.jsonl"
    expected = {domain for hosts in domains.values() for domain in hosts}
    missing = [d for d in sorted(expected) if not (compliance_dir / f"robots_{d}.txt").is_file()]

    lines = [f"    {source:<24} {', '.join(sorted(hosts))}" for source, hosts in sorted(domains.items())]
    if not decisions.is_file():
        return Criterion(
            "a robots.txt policy file is on record for every scraped domain",
            ok=False,
            detail=f"{decisions} does not exist",
            lines=lines,
        )
    if missing:
        return Criterion(
            "a robots.txt policy file is on record for every scraped domain",
            ok=False,
            detail=f"no cached policy for: {', '.join(missing)}",
            lines=lines,
        )
    recorded = sum(1 for _ in decisions.open(encoding="utf-8"))
    return Criterion(
        "a robots.txt policy file is on record for every scraped domain",
        ok=True,
        detail=f"{len(expected)} domain(s), {recorded} decision(s) logged",
        lines=lines,
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def print_corpus_table(scan: Scan) -> None:
    width = max((len(s) for s in scan.by_source), default=10)
    print()
    print(f"  {'SOURCE'.ljust(width)}  RECORDS  DOCUMENTS  SURVIVE  SHORT  EMOJI  HINDI")
    print(f"  {'-' * width}  -------  ---------  -------  -----  -----  -----")
    for source in [s for s in SOURCE_ORDER if s in scan.by_source]:
        t = scan.by_source[source]
        rate = f"{t.documents / t.records:>6.1%}" if t.records else "     -"
        print(
            f"  {source.ljust(width)}  {t.records:>7}  {t.documents:>9}  {rate}  "
            f"{t.reasons.get('too_short', 0):>5}  {t.reasons.get('contains_emoji', 0):>5}  "
            f"{t.reasons.get('hindi_language', 0):>5}"
        )
    rate = f"{scan.documents / scan.records:>6.1%}" if scan.records else "     -"
    reasons = scan.reasons()
    print(f"  {'-' * width}  -------  ---------  -------  -----  -----  -----")
    print(
        f"  {'TOTAL'.ljust(width)}  {scan.records:>7}  {scan.documents:>9}  {rate}  "
        f"{reasons.get('too_short', 0):>5}  {reasons.get('contains_emoji', 0):>5}  "
        f"{reasons.get('hindi_language', 0):>5}"
    )

    print()
    print(f"  {'STAGE'.ljust(width)}  RECORDS  DOCUMENTS  SURVIVE")
    print(f"  {'-' * width}  -------  ---------  -------")
    for stage in ("pre_purchase", "mixed", "post_purchase"):
        records = scan.stage_records(stage)
        documents = scan.stage_documents(stage)
        rate = f"{documents / records:>6.1%}" if records else "     -"
        print(f"  {stage.ljust(width)}  {records:>7}  {documents:>9}  {rate}")


def main(argv: list[str] | None = None) -> int:
    harden_stdio()
    parser = argparse.ArgumentParser(
        description="Score Phase 2's exit criteria against the corpus on disk."
    )
    parser.add_argument(
        "--no-language-check",
        action="store_true",
        help="skip langdetect; faster, and makes the document count a slight overestimate",
    )
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
    except (MissingConfigError, ConfigFileError) as exc:
        print(f"\n  Configuration error: {exc}\n")
        return 1

    raw_dir = settings.raw_dir
    if not raw_dir.is_dir():
        print(f"\n  Nothing to audit: {raw_dir} does not exist. Run collection first.\n")
        return 1

    floors = settings.run.collection.floors
    enabled = settings.run.collection.enabled_sources()
    found = manifests(raw_dir)

    print()
    print("=" * 78)
    print(" PHASE 2 COLLECTION AUDIT")
    print("=" * 78)
    print(f"  raw_dir      {raw_dir}")
    print(f"  config_hash  {settings.config_hash[:12]}")
    print(f"  enabled      {', '.join(enabled)}")

    scan = scan_records(raw_dir, settings.run.filters, check_language=not args.no_language_check)
    print_corpus_table(scan)
    if args.no_language_check:
        print()
        print("  NOTE: --no-language-check was passed, so the Hindi rule was not applied and")
        print("  the HINDI column reads zero rather than 'none found'. DOCUMENTS is therefore")
        print("  an overestimate — on the order of a percent, and always in the direction of")
        print("  flattering a floor. Re-run without the flag before trusting a close verdict.")

    domains = scraped_domains(settings)
    criteria = [
        check_manifests(enabled, found, settings.run.filters),
        check_idempotent(raw_dir, enabled, found),
        check_valid_records(scan),
        check_size_floor(scan, floors),
        check_pre_purchase_floor(scan, floors),
        check_compliance(raw_dir, domains),
    ]

    print()
    print("=" * 78)
    print(" EXIT CRITERIA")
    print("=" * 78)
    for index, criterion in enumerate(criteria, start=1):
        print(f"  {criterion.label:<9} {index}. {criterion.name}")
        print(f"            {criterion.detail}")
        for line in criterion.lines:
            print(line)
        print()

    failed = [c for c in criteria if not c.ok]
    passed = len(criteria) - len(failed)
    print("=" * 78)
    print(f"  {passed} of {len(criteria)} criteria met.")
    if failed:
        print("  Outstanding:")
        for criterion in failed:
            print(f"    - {criterion.name}: {criterion.detail}")
    print("=" * 78)
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
