"""Stage 1 entrypoint: collect every enabled source into ``data/raw/``.

    .venv\\Scripts\\python.exe -m src.collect.run_collection
    .venv\\Scripts\\python.exe -m src.collect.run_collection --sources youtube,mouthshut
    .venv\\Scripts\\python.exe -m src.collect.run_collection --force --max-records 50

Two behaviours here are the phase's exit criteria rather than conveniences.

**Re-running makes zero network calls.** A source with a manifest for today is
skipped unless ``--force`` is passed, which is what "re-runnable without
re-scraping" means in practice.

**The pre/post-purchase split is printed at the end.** The North Star metric is
about *pre-purchase* hesitation while most of this roster is post-purchase, so a
lopsided corpus has to be visible here, immediately, rather than discovered at
synthesis when the report already reads as if delivery complaints were the finding
(`implementation-plan.md` §2.1).

A source failing is not a run failing. Each source is isolated: a 403, a changed
layout, or an exhausted quota is recorded against that source and the others
continue, because losing a day of collection to one broken selector is a worse
outcome than an incomplete corpus you can see the shape of.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.collect.ajio_manual import AjioManualCollector
from src.collect.ajio_onsite import AjioBlockedError, AjioOnsiteCollector, browser_headers
from src.collect.app_store import AppStoreCollector
from src.collect.base import (
    Collector,
    CollectionError,
    EmptyImportError,
    QuotaExhausted,
    RawWriter,
    RequestBudget,
    RequestBudgetExhausted,
    ZeroYieldError,
    has_manifest,
    run_date_utc,
)
from src.collect.complaints_board import ComplaintsBoardCollector
from src.collect.consumer_complaints_in import ConsumerComplaintsInCollector
from src.collect.mouthshut import MouthShutCollector
from src.collect.play_store import PlayStoreCollector
from src.collect.quora_manual import QuoraManualCollector
from src.collect.scraping import PoliteSession, RobotsGate, ScrapingError
from src.collect.trustpilot import TrustpilotCollector
from src.collect.youtube import YouTubeCollector
from src.common.config import Settings, get_settings
from src.common.db import init_db, run_log
from src.common.logging import get_logger, new_run_id, setup_logging
from src.common.schemas import SOURCE_STAGE, PurchaseStage, purchase_stage

# Phase 3's rules, imported rather than reimplemented. This is the one place a
# collect-stage module reaches into the store stage, and it is deliberate: the
# alternative is a second copy of the length gate here, and a drifting copy would
# corrupt the exact number this phase is judged on (plan §3.3). The gate moving
# from 8 words to 3 is what that would have cost: the copy would still be scoring
# Phase 2 against a rule Phase 3 no longer applies.
from src.store.exclusions import survives_hard_exclusions

logger = get_logger("collect.run")

#: Order is evidential value for the research question, not ease of scraping.
#: AJIO Q&A is first because it is the richest pre-purchase source and the one
#: most likely to fail, so a failure surfaces while there is still time to react.
SOURCE_ORDER = (
    "ajio_onsite",
    "ajio_manual",
    "youtube",
    "quora_manual",
    "play_store",
    "app_store",
    "mouthshut",
    "complaints_board",
    "consumer_complaints_in",
    "trustpilot",
    "reddit",
)


@dataclass
class SourceResult:
    source: str
    status: str
    written: int = 0
    rejected: int = 0
    duplicates: int = 0
    requests: int = 0
    stages: dict[str, int] = field(default_factory=dict)
    #: Records that would survive Phase 3's hard exclusions, in total and by
    #: purchase stage. This is the unit the floors are judged in; ``written`` and
    #: ``stages`` are the leading indicator (plan §3.3).
    eligible: int = 0
    eligible_stages: dict[str, int] = field(default_factory=dict)
    detail: str = ""


def build_collector(
    source: str, settings: Settings, *, session_factory, force: bool
) -> Collector | None:
    """Instantiate one collector, or None when its credentials are absent."""
    collection = settings.run.collection

    if source == "ajio_onsite":
        cfg = collection.ajio_onsite
        # AJIO gets its own session: browser-grade headers are the whole point,
        # and sharing the generic scraper session would defeat them.
        session = session_factory(extra_headers=browser_headers(cfg.browser_user_agent))
        return AjioOnsiteCollector(
            session,
            review_api_template=cfg.review_api_template,
            qa_api_template=cfg.qa_api_template,
        )

    if source == "youtube":
        return YouTubeCollector(
            api_key=settings.credentials.youtube_api_key.get_secret_value(),
            cache_dir=settings.raw_dir,
            force_search=force,
        )

    if source == "ajio_manual":
        return AjioManualCollector(project_root=settings.project_root)

    if source == "quora_manual":
        return QuoraManualCollector(project_root=settings.project_root)

    if source == "play_store":
        return PlayStoreCollector()

    if source == "app_store":
        return AppStoreCollector(session_factory())

    if source == "mouthshut":
        return MouthShutCollector(session_factory())

    if source == "complaints_board":
        return ComplaintsBoardCollector(session_factory())

    if source == "consumer_complaints_in":
        return ConsumerComplaintsInCollector(session_factory())

    if source == "trustpilot":
        return TrustpilotCollector(session_factory())

    if source == "reddit":
        if not settings.credentials.has_reddit:
            logger.warning(
                "reddit is enabled in config.yaml but REDDIT_* credentials are absent; skipping"
            )
            return None
        from src.collect.reddit import RedditCollector, build_client

        return RedditCollector(
            build_client(
                settings.credentials.reddit_client_id.get_secret_value(),  # type: ignore[union-attr]
                settings.credentials.reddit_client_secret.get_secret_value(),  # type: ignore[union-attr]
                settings.credentials.reddit_user_agent or "",
            )
        )

    raise ValueError(f"no collector registered for source {source!r}")


def source_config(settings: Settings, source: str) -> Any:
    return getattr(settings.run.collection, source)


def collect_source(
    source: str,
    settings: Settings,
    *,
    session_factory,
    run_date: str,
    force: bool,
    max_records: int | None,
) -> SourceResult:
    """Run one collector to completion, writing JSONL and a manifest."""
    if has_manifest(settings.raw_dir, source, run_date) and not force:
        logger.info("[%s] manifest exists for %s; skipping (use --force to re-collect)", source, run_date)
        return SourceResult(source, "skipped", detail="manifest exists; no network calls made")

    try:
        collector = build_collector(source, settings, session_factory=session_factory, force=force)
    except Exception as exc:
        logger.error("[%s] could not be constructed: %s", source, exc)
        return SourceResult(source, "error", detail=f"{type(exc).__name__}: {exc}")

    if collector is None:
        return SourceResult(source, "skipped", detail="credentials absent")

    writer = RawWriter(
        raw_dir=settings.raw_dir,
        source=source,
        run_date=run_date,
        config_hash=settings.config_hash,
        collector_version=collector.collector_version,
        max_records=max_records,
    )
    cfg = source_config(settings, source)
    filters = settings.run.filters
    status = "complete"
    detail = ""
    stages: dict[str, int] = {}
    eligible_stages: dict[str, int] = {}

    try:
        for record in collector.fetch(cfg):
            if writer.write(record):
                stage = purchase_stage(record.source, record.meta).value
                stages[stage] = stages.get(stage, 0) + 1
                # Scored here, while the text is in hand, so the run can report a
                # floor in the unit that survives instead of one measured a stage
                # before the filters that do the cutting.
                if survives_hard_exclusions(record.text, filters):
                    eligible_stages[stage] = eligible_stages.get(stage, 0) + 1
            if writer.full:
                writer.note(f"--max-records cap of {max_records} reached")
                break

        # Only meaningful on an uncapped run: a cap below the floor would fail a
        # deliberately small smoke test.
        if max_records is None:
            collector.check_yield(writer.written)

    except EmptyImportError as exc:
        # Before ZeroYieldError and CollectionError, both of which would swallow it:
        # an unfilled import directory is an undone manual step, not a broken
        # collector, and the two need different responses from whoever is reading.
        status = "empty_import"
        detail = str(exc)
        writer.note(detail)
        logger.warning("[%s] %s", source, detail)
    except QuotaExhausted as exc:
        status = "quota_exhausted"
        detail = str(exc)
        writer.note(detail)
        logger.warning("[%s] %s", source, detail)
    except AjioBlockedError as exc:
        status = "blocked"
        detail = str(exc)
        writer.note(detail)
        logger.error("[%s] %s", source, detail)
    except ZeroYieldError as exc:
        status = "zero_yield"
        detail = str(exc)
        writer.note(detail)
        logger.error("[%s] %s", source, detail)
    except RequestBudgetExhausted as exc:
        status = "budget_exhausted"
        detail = str(exc)
        writer.note(detail)
        logger.warning("[%s] %s", source, detail)
    except (CollectionError, ScrapingError) as exc:
        status = "error"
        detail = f"{type(exc).__name__}: {exc}"
        writer.note(detail)
        logger.error("[%s] %s", source, detail)
    except Exception as exc:  # unexpected, but must not lose the other sources
        status = "error"
        detail = f"{type(exc).__name__}: {exc}"
        writer.note(detail)
        logger.exception("[%s] unexpected failure", source)
    finally:
        extra: dict[str, Any] = {
            "rejected_records": collector.rejected,
            "rejection_reasons": collector.rejection_reasons,
            "purchase_stage_counts": stages,
            "eligible_documents": sum(eligible_stages.values()),
            "eligible_stage_counts": eligible_stages,
        }
        for attribute in ("pages_fetched", "searches_made", "videos_skipped", "robots_blocked",
                          "blocked_urls", "extraction_paths", "caps_hit", "products_seen",
                          "disabled_templates", "files_read", "files_skipped",
                          "parse_warnings"):
            value = getattr(collector, attribute, None)
            if value:
                extra[attribute] = len(value) if isinstance(value, list) else value
        writer.close(status=status, extra=extra)

    session = getattr(collector, "session", None)
    return SourceResult(
        source=source,
        status=status,
        written=writer.written,
        rejected=collector.rejected,
        duplicates=writer.duplicates,
        requests=getattr(session, "requests_made", 0) if session else 0,
        stages=stages,
        eligible=sum(eligible_stages.values()),
        eligible_stages=eligible_stages,
        detail=detail,
    )


#: Sources that can contribute pre-purchase records at all. Everything else is
#: post-purchase by construction, so naming it in a pre-purchase shortfall would
#: send whoever is reading to fix a source that was never going to help.
PRE_PURCHASE_CAPABLE = tuple(
    name
    for name in SOURCE_ORDER
    if SOURCE_STAGE[name] is not PurchaseStage.POST_PURCHASE
)

#: Why a pre-purchase route is switched off in ``config.yaml``, for the shortfall
#: block. The reason has to travel with the source name: a route that is off on
#: purpose otherwise reads as one more collection task, which is how ``ajio_manual``
#: spent a phase looking like an unfilled directory rather than an absent source.
DISABLED_REASONS: dict[str, str] = {
    "ajio_manual": "AJIO has no on-site free text",
    "reddit": "collector retained; needs REDDIT_* credentials",
}


def _pre_purchase_shortfall(
    results: list[SourceResult], *, enabled: list[str] | None = None
) -> list[str]:
    """Lines explaining where the missing pre-purchase documents were meant to come from.

    Both units per source, because the two answer different questions: a source
    with records but no eligible documents needs its *content* looked at (a large
    share of YouTube comments are one-word reactions), while a source with neither
    needs its *collection* looked at.

    ``enabled`` is the settings' own enabled-source list, and it separates a source
    that is off in ``config.yaml`` from one that genuinely did not run. Without the
    distinction a deliberately disabled route is printed as "not run" and reads as
    an outstanding task — sending whoever is reading to collect AJIO Q&A prose that
    the site does not publish anywhere. When it is omitted every absent source is
    reported as not run, as before.
    """
    lines: list[str] = []
    by_source = {r.source: r for r in results}
    for name in PRE_PURCHASE_CAPABLE:
        result = by_source.get(name)
        if result is None:
            if enabled is not None and name not in enabled:
                reason = DISABLED_REASONS.get(name)
                note = "disabled in config" + (f" ({reason})" if reason else "")
            else:
                note = "not run"
            lines.append(f"    {name:<24} {note}")
            continue
        produced = result.stages.get("pre_purchase", 0)
        eligible = result.eligible_stages.get("pre_purchase", 0)
        note = f"{eligible:>6} documents  (from {produced:>6} records)"
        if result.status != "complete":
            note += f"  [{result.status}]"
        lines.append(f"    {name:<24} {note}")
    return lines


def print_summary(
    results: list[SourceResult],
    *,
    raw_dir: Path,
    floors: Any = None,
    enabled: list[str] | None = None,
) -> None:
    width = max((len(r.source) for r in results), default=10)
    print()
    print(
        f"  {'SOURCE'.ljust(width)}  STATUS            RECORDS  ELIGIBLE  REJECTED  DUPES  REQUESTS"
    )
    print(
        f"  {'-' * width}  ----------------  -------  --------  --------  -----  --------"
    )
    for result in results:
        print(
            f"  {result.source.ljust(width)}  {result.status.ljust(16)}  "
            f"{result.written:>7}  {result.eligible:>8}  {result.rejected:>8}  "
            f"{result.duplicates:>5}  {result.requests:>8}"
        )

    totals: dict[str, int] = {}
    eligible_totals: dict[str, int] = {}
    for result in results:
        for stage, count in result.stages.items():
            totals[stage] = totals.get(stage, 0) + count
        for stage, count in result.eligible_stages.items():
            eligible_totals[stage] = eligible_totals.get(stage, 0) + count
    total = sum(totals.values())
    eligible_total = sum(eligible_totals.values())

    print()
    print(f"  Raw records written: {total} into {raw_dir}")
    print(
        f"  Projected documents: {eligible_total}"
        + (f"  ({eligible_total / total:.1%} survive the hard exclusions)" if total else "")
    )
    print(
        "  ELIGIBLE applies Phase 3's min_words / emoji / language rules to the text as it\n"
        "  was written, so it is a projection of the corpus, not of relevance: triage still\n"
        "  runs on top of it. RECORDS is the leading indicator; ELIGIBLE is the gate."
    )
    if total:
        print()
        print("  Purchase-stage split          records            documents")
        for stage in ("pre_purchase", "mixed", "post_purchase"):
            count = totals.get(stage, 0)
            surviving = eligible_totals.get(stage, 0)
            share = f"({count / total:>5.1%})"
            rate = f"({surviving / count:>5.1%} survive)" if count else ""
            print(f"    {stage:<14} {count:>12}  {share}  {surviving:>10}  {rate}")

    # A partial run is the normal case once collection has been done once, and its
    # totals are not the corpus. Saying so matters more than it looks: without it,
    # re-collecting one source prints a full-corpus floor warning computed from one
    # source, which reads as a catastrophe and trains the reader to skip the block.
    absent = [name for name in (enabled or []) if name not in {r.source for r in results}]
    skipped = [r.source for r in results if r.status == "skipped"]
    partial = sorted(set(absent) | set(skipped))
    attempted = len(results) - len(skipped)
    if partial:
        print()
        print(
            f"  NOTE: this run covered {attempted} of {len(results) + len(absent)} enabled "
            f"source(s), so the totals and the floors\n"
            f"  below describe the run and not the corpus on disk. Not counted here: "
            f"{', '.join(partial)}.\n"
            "  For the corpus-wide score the floors are really about:\n"
            "      .venv\\Scripts\\python.exe scripts\\audit_collection.py"
        )

    if not attempted and results:
        # Nothing was attempted, so there is no shortfall to report — every floor
        # would read zero and say nothing true. A run that *tried* and collected
        # nothing is the opposite case and still warns below.
        print()
        return

    # Floors, in two units. The document floors are the gate; the record floors are
    # kept because they are the earliest available signal and cost nothing, but the
    # first version of this block had only them and it passed a phase whose signal
    # the next stage deleted (plan §3.3).
    pre_doc_floor = getattr(floors, "pre_purchase_documents", 2000)
    total_doc_floor = getattr(floors, "total_documents", 1500)
    pre_record_floor = getattr(floors, "pre_purchase_records", 2000)
    total_record_floor = getattr(floors, "total_records", 15000)

    # Reported even when nothing was written: a run that collected zero is
    # exactly when the pre-purchase shortfall most needs saying out loud.
    pre_documents = eligible_totals.get("pre_purchase", 0)
    if pre_documents < pre_doc_floor:
        print()
        print(
            f"  WARNING: {pre_documents:,} pre-purchase documents, below the floor of "
            f"{pre_doc_floor:,} (short by {pre_doc_floor - pre_documents:,}).\n"
            "  The metric is about pre-purchase hesitation, so fix collection rather than\n"
            "  proceeding: no downstream cleverness recovers a signal never collected.\n"
            "  Contributions from the sources that can supply it, in documents and in the\n"
            "  records they came from:"
        )
        for line in _pre_purchase_shortfall(results, enabled=enabled):
            print(line)
        print(
            "  Neither AJIO route can close this gap: ajio_onsite is refused by an Akamai\n"
            "  edge, and ajio_manual is off because the site carries no free text to hand-\n"
            "  collect — only rating, fit and quality bars, sitewide. The routes that remain\n"
            "  are saved Quora threads, widening the YouTube query terms, and re-enabling\n"
            "  Reddit with REDDIT_* credentials. Hand-collection no longer has to overshoot\n"
            "  as heavily as it did: the length gate is 3 words, not 8, so a question as\n"
            "  short as \"does this run small?\" now survives to become a document."
        )
    if eligible_total < total_doc_floor:
        print()
        print(
            f"  WARNING: {eligible_total} projected documents, below the floor of "
            f"{total_doc_floor:,}.\n"
            "  Phase 3 needs this many to state a prevalence with a usable interval, and it\n"
            "  can only shrink from here: dedupe and triage both subtract."
        )

    # Reported last and labelled, so the leading indicator is never mistaken for
    # the verdict — which is the mistake this whole block exists to undo.
    leading: list[str] = []
    if total < total_record_floor:
        leading.append(f"{total:,} raw records against a target of {total_record_floor:,}")
    pre_records = totals.get("pre_purchase", 0)
    if pre_records < pre_record_floor:
        leading.append(f"{pre_records:,} pre-purchase records against {pre_record_floor:,}")
    if leading:
        print()
        print("  Leading indicators, for context only: " + "; ".join(leading) + ".")
    print()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect raw records from every enabled source into data/raw/."
    )
    parser.add_argument(
        "--sources",
        help="comma-separated subset to collect; defaults to every enabled source",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-collect even when a manifest exists for today, and re-run YouTube searches",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        help="stop each source after this many records; useful for a smoke test",
    )
    parser.add_argument(
        "--run-date",
        default=run_date_utc(),
        help="UTC date partition to write into (default: today)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    settings.ensure_dirs()

    run_id = new_run_id("collect")
    setup_logging(run_id, settings.logs_dir)

    enabled = settings.run.collection.enabled_sources()
    if args.sources:
        requested = [name.strip() for name in args.sources.split(",") if name.strip()]
        unknown = [name for name in requested if name not in SOURCE_STAGE]
        if unknown:
            print(f"\n  Unknown source(s): {', '.join(unknown)}")
            print(f"  Known: {', '.join(sorted(SOURCE_STAGE))}\n")
            return 2
        disabled = [name for name in requested if name not in enabled]
        for name in disabled:
            logger.warning("%s is disabled in config.yaml; skipping", name)
        selected = [name for name in requested if name in enabled]
    else:
        selected = list(enabled)

    ordered = [name for name in SOURCE_ORDER if name in selected]
    if not ordered:
        print("\n  Nothing to collect: no requested source is enabled in config.yaml.\n")
        return 1

    logger.info("run_id=%s config_hash=%s", run_id, settings.config_hash[:12])
    logger.info("collecting: %s", ", ".join(ordered))

    collection = settings.run.collection
    budget = RequestBudget(collection.max_requests_per_run)
    compliance_dir = settings.raw_dir / "_compliance"

    def session_factory(*, extra_headers: dict[str, str] | None = None) -> PoliteSession:
        """A polite session per collector, sharing one robots cache and one budget."""
        import requests

        transport = requests.Session()
        gate = RobotsGate(
            transport,
            user_agent=collection.scraper_user_agent,
            compliance_dir=compliance_dir,
            enabled=collection.respect_robots_txt,
        )
        return PoliteSession(
            transport,
            user_agent=(extra_headers or {}).get("User-Agent", collection.scraper_user_agent),
            robots=gate,
            budget=budget,
            delay_seconds=collection.per_domain_delay_seconds,
            extra_headers=extra_headers,
        )

    conn = init_db(settings.interim_db)
    results: list[SourceResult] = []
    try:
        for source in ordered:
            with run_log(conn, run_id, f"collect:{source}", settings.config_hash) as entry:
                result = collect_source(
                    source,
                    settings,
                    session_factory=session_factory,
                    run_date=args.run_date,
                    force=args.force,
                    max_records=args.max_records,
                )
                entry.records_in = result.written + result.rejected
                entry.records_out = result.written
                entry.note(f"status={result.status}")
                if result.detail:
                    entry.note(result.detail[:500])
                results.append(result)
    finally:
        conn.close()

    print_summary(
        results, raw_dir=settings.raw_dir, floors=collection.floors, enabled=list(enabled)
    )
    logger.info("requests spent: %s of %s", budget.spent, budget.limit)

    # An unfilled manual import is a real unmet exit criterion, but it is unmet by
    # a person rather than by the code, and failing every run until someone hand-
    # collects would train whoever runs this to ignore the exit code. It is named
    # here instead, and scripts/audit_collection.py is what gates on it.
    empty = [r for r in results if r.status == "empty_import"]
    if empty:
        print(
            f"  {len(empty)} manual source(s) have nothing to import: "
            f"{', '.join(r.source for r in empty)}.\n"
            "  These are pre-purchase routes, so the corpus is more post-purchase than the\n"
            "  split above suggests until they are filled by hand.\n"
        )

    # A blocked or broken source is worth a non-zero exit so a wrapper notices,
    # but a quota stop is a normal free-tier outcome and exits 0 (edge-case 1.1.1).
    failed = [r for r in results if r.status in {"error", "zero_yield", "blocked"}]
    if failed:
        print(f"  {len(failed)} source(s) need attention: {', '.join(r.source for r in failed)}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
