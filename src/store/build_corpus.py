"""Phase 3 entrypoint: raw JSONL -> deduplicated, triaged ``documents`` table.

    python -m src.store.build_corpus              # full build, Tier-2 if key present
    python -m src.store.build_corpus --no-tier2   # Tier-1 only (fully offline)
    python -m src.store.build_corpus --force      # rebuild the documents table
    python -m src.store.build_corpus --limit 2000 # cap raw records (quick pass)

Stages run in the order the plan fixes (plan §3.2), each recording why it acted so
the funnel is auditable end to end:

    raw -> normalize -> exclusions -> dedupe -> tier-1 keyword -> tier-2 LLM

Excluded and duplicate rows are kept, not deleted. The funnel report — printed and
written to ``run_log`` — breaks every drop out by reason code and reports the
pre/post-purchase split, so a lopsided corpus is visible here rather than at
synthesis.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from src.common.config import get_settings
from src.common.db import init_db, run_log, upsert_documents
from src.common.logging import get_logger, new_run_id, setup_logging
from src.common.schemas import Document, PurchaseStage, RawRecord, purchase_stage
from src.store.dedupe import mark_duplicates
from src.store.exclusions import classify_exclusion
from src.store.normalize import normalize_record
from src.store.relevance import run_tier2, score_tier1

log = get_logger("build_corpus")


def replace_document_rows(conn, documents: list) -> int:
    """``--force`` rebuild: swap ``documents`` without cascading ``doc_tags``.

    ``doc_tags.doc_id`` and ``documents.is_duplicate_of`` are foreign keys, so a
    plain ``DELETE FROM documents`` after tagging fails with IntegrityError —
    which is what the 2026-08-28 persist hit after 40 minutes of exclusions.
    Tags outlive the wipe the same way ``triage_cache`` does: ``doc_id`` is
    derived from ``(source, source_native_id)`` and comes back on insert.
    Foreign keys are suspended only for the swap.
    """
    ordered = sorted(documents, key=lambda d: d.is_duplicate_of is not None)
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("DELETE FROM documents")
        return upsert_documents(conn, ordered)
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _iter_raw_files(raw_dir: Path):
    """Yield every collected part file under data/raw/<source>/<run_date>/."""
    yield from sorted(raw_dir.glob("*/*/part-*.jsonl"))


def load_raw_records(raw_dir: Path, *, limit: int | None = None):
    """Read and validate raw records; skip the compliance log and bad lines.

    Collapses repeats on ``(source, source_native_id)`` — the pair ``doc_id`` is
    derived from — keeping the most recently collected copy. This is not the
    near-duplicate pass; it is record *identity*. Collecting a source again on a
    later date is the design's normal path (raw data is append-only, and the
    manifest only guards a single run date), so the same comment legitimately
    appears in two part files. The ``documents`` table already tolerates that via
    ``UNIQUE (source, source_native_id)``, but the funnel is computed from this
    list, so without collapsing here every count above the DB — records loaded,
    each exclusion reason, and the exact-duplicate tally — silently doubles for
    the re-collected source. Files are read in date order, so the last write wins
    and an edited comment body supersedes the copy first seen.
    """
    unique: dict[tuple[str, str], RawRecord] = {}
    malformed = 0
    superseded = 0
    for path in _iter_raw_files(raw_dir):
        if "_compliance" in path.parts:
            continue
        # Iterating the handle splits on newlines and nothing else. ``splitlines()``
        # additionally breaks on U+2028, U+2029, U+0085 and the C0 separators, none
        # of which a JSON serializer escapes — so a record whose text contains one
        # arrives here as several fragments and is counted as malformed. That is not
        # hypothetical: one YouTube comment laid a numbered list out with U+2028 and
        # lost itself six times over. Collection now folds those into ``\n``, and
        # reading this way recovers the records written before it did.
        with path.open("r", encoding="utf-8") as handle:
            lines = list(handle)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = RawRecord.model_validate_json(line)
            except ValidationError:
                malformed += 1
                continue
            key = (record.source, record.source_native_id)
            if key in unique:
                superseded += 1
            unique[key] = record
            if limit and len(unique) >= limit:
                return list(unique.values()), malformed, superseded
    return list(unique.values()), malformed, superseded


def build(*, force: bool, no_tier2: bool, limit: int | None) -> dict:
    settings = get_settings()
    settings.ensure_dirs()
    salt = settings.credentials.hash_salt.get_secret_value()
    filters = settings.run.filters

    raw_records, malformed, superseded = load_raw_records(settings.raw_dir, limit=limit)
    log.info(
        "loaded %d raw records (%d malformed skipped, %d re-collected copies superseded)",
        len(raw_records),
        malformed,
        superseded,
    )

    # 1. Normalize (de-identify + derive structural fields).
    documents: list[Document] = [normalize_record(r, salt=salt) for r in raw_records]
    log.info("normalized %d documents", len(documents))

    # 2. Hard exclusions — first matching reason wins.
    for i, doc in enumerate(documents, start=1):
        doc.exclusion_reason = classify_exclusion(
            doc.text,
            min_words=filters.min_words,
            exclude_emoji=filters.exclude_emoji,
            excluded_languages=filters.excluded_languages,
            language_confidence=filters.language_confidence,
            language_min_words=filters.language_min_words,
        )
        if doc.exclusion_reason is not None:
            doc.is_relevant = False
        if i % 10000 == 0:
            log.info("hard exclusions %d/%d", i, len(documents))
    log.info("hard exclusions done")

    # 3. Dedupe (exact + near) among the survivors.
    log.info("deduping")
    dedupe_counts = mark_duplicates(
        documents,
        near_duplicate_hamming=filters.near_duplicate_hamming,
        near_duplicate_min_words=filters.near_duplicate_min_words,
    )
    log.info("dedupe %s", dedupe_counts)

    # 4. Tier-1 keyword triage.
    log.info("tier-1 keyword triage")
    keywords_path = (settings.project_root / filters.relevance_keywords_path).resolve()
    tier1_counts = score_tier1(
        documents,
        keywords_path=keywords_path,
        min_content_words=filters.min_content_words,
    )
    log.info("tier-1 %s", tier1_counts)

    # 5. Tier-2 LLM triage (optional / gated).
    # The connection is opened before triage rather than after it, because triage
    # checkpoints each batch's verdicts into triage_cache as they arrive. Opening
    # it afterwards is what made the 2026-08-24 run lose ~1,960 classifications to
    # a rate limit it could not survive (plan §3.3).
    conn = init_db(settings.interim_db)
    tier2_counts = run_tier2(documents, settings=settings, enable=not no_tier2, conn=conn)

    # --- persist ---
    # is_duplicate_of is a self-FK, so a duplicate row cannot be inserted
    # before the canonical it points at. Canonicals (is_duplicate_of is None)
    # go in first; the rest follow.
    ordered = sorted(documents, key=lambda d: d.is_duplicate_of is not None)
    if force:
        log.info("replacing documents table (%d rows); tags are kept", len(ordered))
        added = replace_document_rows(conn, ordered)
    else:
        added = upsert_documents(conn, ordered)
    with run_log(conn, run_id=settings.config_hash[:12], stage="build_corpus", config_hash=settings.config_hash) as entry:
        entry.records_in = len(raw_records)
        entry.records_out = added
        funnel = _funnel(documents)
        entry.note(json.dumps(funnel))
    conn.close()

    summary = {
        "raw_loaded": len(raw_records),
        "malformed_skipped": malformed,
        "superseded_recollected": superseded,
        **dedupe_counts,
        **tier1_counts,
        **tier2_counts,
        **_funnel(documents),
    }
    _print_funnel(
        summary,
        documents,
        floors=settings.run.collection.floors,
        min_words=filters.min_words,
    )
    return summary


def _funnel(documents: list[Document]) -> dict:
    total = len(documents)
    excluded = Counter(d.exclusion_reason for d in documents if d.exclusion_reason)
    duplicates = sum(1 for d in documents if d.is_duplicate_of is not None)
    relevant = [d for d in documents if d.is_relevant]
    return {
        "normalized": total,
        "excluded_too_short": excluded.get("too_short", 0),
        "excluded_contains_emoji": excluded.get("contains_emoji", 0),
        "excluded_hindi_language": excluded.get("hindi_language", 0),
        "duplicates_marked": duplicates,
        "relevant": len(relevant),
    }


def _print_funnel(
    summary: dict, documents: list[Document], *, floors=None, min_words: int = 3
) -> None:
    relevant = [d for d in documents if d.is_relevant]
    stage_counts: Counter = Counter()
    for d in relevant:
        try:
            stage_counts[purchase_stage(d.source, d.meta)] += 1
        except ValueError:
            stage_counts[PurchaseStage.MIXED] += 1
    pre = stage_counts.get(PurchaseStage.PRE_PURCHASE, 0)
    post = stage_counts.get(PurchaseStage.POST_PURCHASE, 0)
    mixed = stage_counts.get(PurchaseStage.MIXED, 0)

    print("\n" + "=" * 58)
    print(" CORPUS FUNNEL  (plan §3 exit criteria)")
    print("=" * 58)
    print(f"  raw records loaded        {summary['raw_loaded']:>8}")
    print(f"  malformed skipped         {summary['malformed_skipped']:>8}")
    print(f"  re-collected superseded   {summary.get('superseded_recollected', 0):>8}")
    print(f"  normalized                {summary['normalized']:>8}")
    print("  -- hard exclusions --")
    print(f"    too_short               {summary['excluded_too_short']:>8}")
    print(f"    contains_emoji          {summary['excluded_contains_emoji']:>8}")
    print(f"    hindi_language          {summary['excluded_hindi_language']:>8}")
    print(f"  duplicates marked         {summary['duplicates_marked']:>8}")
    print(f"     of which exact         {summary.get('exact_duplicates', 0):>8}")
    print(f"     of which near          {summary.get('near_duplicates', 0):>8}")
    print("  -- relevance triage --")
    print(f"    tier-1 dropped (0 hits) {summary.get('tier1_dropped_zero_hits', 0):>8}")
    print(f"      of those, contentless {summary.get('tier1_dropped_low_content', 0):>8}")
    print(f"    tier-2 status           {str(summary.get('tier2_status', 'n/a')):>8}")
    if summary.get("tier2_status") in {"ran", "partial"}:
        print(f"      classified this run   {summary.get('tier2_classified', 0):>8}")
        print(f"      reused from cache     {summary.get('tier2_from_cache', 0):>8}")
        print(f"      dropped by tier-2     {summary.get('tier2_irrelevant', 0):>8}")
        # The number that decides whether this corpus is a finished triage or a
        # partial one, so it is printed rather than left to the run_log.
        print(f"      NOT judged (kept)     {summary.get('tier2_unclassified', 0):>8}")
        print(f"      tokens spent          {summary.get('tier2_tokens', 0):>8}")
    print(f"  RELEVANT (corpus)         {summary['relevant']:>8}")
    print("  -- pre/post split of relevant --")
    print(f"    pre_purchase            {pre:>8}")
    print(f"    post_purchase           {post:>8}")
    print(f"    mixed                   {mixed:>8}")
    print("=" * 58)
    # From config rather than a literal, so this threshold and the one the
    # collection summary reports are the same number rather than two numbers that
    # agree today (plan §3.3).
    pre_floor = getattr(floors, "pre_purchase_documents", 2000)
    total_floor = getattr(floors, "total_documents", 1500)
    if pre < pre_floor:
        # ASCII marker on purpose. UTF-8 output is now forced, so a glyph here no
        # longer crashes — but a cp1252 console decoding those bytes renders it as
        # mojibake, and the one line an operator must not misread is the warning.
        print(
            f"  WARNING  pre-purchase floor NOT met: {pre} < {pre_floor}. Live pre-purchase\n"
            f"     routes are YouTube + manual Quora + manual AJIO import. Hand-collect\n"
            f"     AJIO Q&A into data/manual/ajio/ (ajio_manual) to raise it — the\n"
            f"     on-site route is blocked by Akamai and out of scope (edge-case §1.1.13d)."
        )
    if summary.get("tier2_status") == "partial":
        reason = summary.get("tier2_stop_reason") or "unknown"
        print(
            f"  NOTE  tier-2 stopped early ({reason}) after judging "
            f"{summary.get('tier2_classified', 0)} document(s); "
            f"{summary.get('tier2_unclassified', 0)} were kept on their tier-1 verdict.\n"
            f"     Every verdict it did reach is in triage_cache, so re-running this\n"
            f"     build tomorrow resumes rather than restarting. The relevant count\n"
            f"     above is a partly-triaged corpus and should be labelled as such."
        )
    relevant_count = summary["relevant"]
    if relevant_count < total_floor:
        print(
            f"  WARNING  corpus floor NOT met: {relevant_count} relevant < {total_floor}.\n"
            f"     Of the drops above, contains_emoji is the recoverable one: because\n"
            f"     exclusions are first-match-wins, those documents had already cleared\n"
            f"     the {min_words}-word gate, so narrowing filters.exclude_emoji is a one-flag\n"
            f"     change. Audit the rejected pool before deciding — an over-aggressive\n"
            f"     filter deletes the finding."
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the corpus (Phase 3).")
    parser.add_argument("--force", action="store_true", help="rebuild the documents table from scratch")
    parser.add_argument("--no-tier2", action="store_true", help="Tier-1 keyword triage only; skip the LLM triage")
    parser.add_argument("--limit", type=int, default=None, help="cap raw records for a quick pass")
    args = parser.parse_args()

    # Before anything is printed: this forces stdout to UTF-8 as well as opening the
    # run log. Without it stdout keeps the console codepage and the funnel report
    # raises UnicodeEncodeError *after* every document is written and committed.
    settings = get_settings()
    setup_logging(new_run_id("corpus"), settings.logs_dir)

    build(force=args.force, no_tier2=args.no_tier2, limit=args.limit)


if __name__ == "__main__":
    main()
