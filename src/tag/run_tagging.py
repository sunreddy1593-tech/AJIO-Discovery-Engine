"""Phase 4 entrypoint: tag the relevant corpus with Groq (plan §4).

    python -m src.tag.run_tagging --dry-run   # offline: count, tokens, cost, ETA
    python -m src.tag.run_tagging --resume     # tag, checkpointing after every batch

The design targets a free-tier budget that legitimately spans days, so:

* **Cache-first.** Every relevant document is looked up in ``llm_cache`` before it
  is ever batched; only misses reach the API. A second run over an unchanged corpus
  therefore issues zero Groq calls (a Phase 4 exit criterion).
* **Checkpoint after every batch.** ``--resume`` continues exactly where a 429 or a
  daily-limit stop left off, because progress is the cache plus the ``doc_tags``
  table, not in-memory state.
* **``--dry-run`` needs no key.** It reads the corpus and the measured per-document
  cost and prints documents, estimated tokens, cost on the paid tier, and wall-clock
  under the current free-tier ceilings — the budget decision gate in plan §4.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from src.common.config import get_settings
from src.common.db import connect, init_db, run_log, upsert_tags
from src.common.logging import get_logger, new_run_id, setup_logging
from src.tag import cache
from src.tag.taxonomy import TAXONOMY_VERSION

log = get_logger("tag.run")

PROMPT_VERSION = "v1"
# Measured 2026-08-21 by scripts/measure_token_overhead.py against the production
# prompt (tagging_v1.md) and the full taxonomy schema, on real corpus documents
# stratified by length (30% are 3-5 words after the gate moved from 8 to 3).
# Weighted result: 644.5 tokens/document at docs_per_request=6. The previous 540
# was a projection from a one-dimension stub schema, not a measurement of what
# the tagger actually sends.
TOKENS_PER_DOC = 645
# Prompt share of those 645, measured on the same run (~2,800 prompt tokens per
# batch of 6). The 60/40 split it replaces overstated cost because the fixed
# schema+prompt is input, which is the cheap side of gpt-oss-120b pricing.
INPUT_FRACTION = 0.72
# Paid Developer-tier pricing for gpt-oss-120b (plan §0.1): $/1M tokens.
PRICE_IN_PER_M = 0.15
PRICE_OUT_PER_M = 0.60


def _prompt_text(settings) -> str:
    path = settings.project_root / "src" / "tag" / "prompts" / f"tagging_{PROMPT_VERSION}.md"
    return Path(path).read_text(encoding="utf-8")


#: What "taggable" means, in one place. ``scripts/build_tag_sample.py`` imports this
#: rather than restating it, so the sample is drawn from exactly the population this
#: module would otherwise tag — a second copy would drift the day either changed.
RELEVANT_PREDICATE = "is_relevant = 1 AND is_duplicate_of IS NULL"
RELEVANT_SQL = f"SELECT doc_id, text FROM documents WHERE {RELEVANT_PREDICATE}"


def _sample_is_active(conn) -> bool:
    """True when a non-empty ``tag_sample`` table narrows this run.

    Absent or empty means tag everything relevant, which is what every run before
    the table existed did. Sampling is therefore additive: it is expressed as rows
    in a side table rather than by editing ``is_relevant``, so the corpus keeps
    saying what the triage decided and dropping the table restores the full job.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tag_sample'"
    ).fetchone()
    if exists is None:
        return False
    return conn.execute("SELECT COUNT(*) FROM tag_sample").fetchone()[0] > 0


def _relevant_documents(conn):
    sql = RELEVANT_SQL
    if _sample_is_active(conn):
        sql += " AND doc_id IN (SELECT doc_id FROM tag_sample)"
    rows = conn.execute(sql).fetchall()
    return [{"doc_id": r[0], "text": r[1]} for r in rows]


def dry_run(settings) -> dict:
    """Estimate cost with no API call (the plan's budget decision gate)."""
    conn = connect(settings.interim_db)
    docs = _relevant_documents(conn)
    # Only cache misses would actually be billed.
    misses = [
        d for d in docs
        if cache.get(
            conn,
            cache.cache_key(
                doc_id=d["doc_id"], text=d["text"], model=settings.run.model.name,
                taxonomy_version=TAXONOMY_VERSION, prompt_version=PROMPT_VERSION,
            ),
        )
        is None
    ]
    conn.close()

    n = len(misses)
    per_call = settings.run.model.docs_per_request
    calls = math.ceil(n / per_call) if per_call else 0
    total_tokens = n * TOKENS_PER_DOC
    est_cost = (total_tokens * INPUT_FRACTION / 1_000_000 * PRICE_IN_PER_M) + (
        total_tokens * (1.0 - INPUT_FRACTION) / 1_000_000 * PRICE_OUT_PER_M
    )
    tpd = settings.run.rate_limits.tagging.tpd
    free_days = math.ceil(total_tokens / tpd) if tpd else 0
    rpm = settings.run.rate_limits.tagging.rpm
    minutes_by_rpm = calls / rpm if rpm else 0

    summary = {
        "relevant_documents": len(docs),
        "already_cached": len(docs) - n,
        "to_tag": n,
        "batches": calls,
        "estimated_tokens": total_tokens,
        "estimated_cost_usd_paid_tier": round(est_cost, 2),
        "estimated_free_tier_days": free_days,
        "estimated_minutes_by_rpm": round(minutes_by_rpm, 1),
    }
    _print_dry_run(summary)
    return summary


def _print_dry_run(s: dict) -> None:
    print("\n" + "=" * 54)
    print(" TAGGING DRY-RUN  (plan §4 budget gate)")
    print("=" * 54)
    print(f"  relevant documents        {s['relevant_documents']:>8}")
    print(f"  already cached (free)      {s['already_cached']:>8}")
    print(f"  to tag                     {s['to_tag']:>8}")
    print(f"  batches                    {s['batches']:>8}")
    print(f"  estimated tokens           {s['estimated_tokens']:>8}")
    print(f"  cost @ paid tier (USD)     {s['estimated_cost_usd_paid_tier']:>8}")
    print(f"  free-tier days (200k TPD)  {s['estimated_free_tier_days']:>8}")
    print("=" * 54)
    if s["estimated_free_tier_days"] > 2:
        print(
            "  Decision gate: estimate exceeds ~2 free-tier days. Either upgrade to\n"
            f"  the Developer tier (${s['estimated_cost_usd_paid_tier']:.2f} for the whole "
            "corpus) or sample\n"
            "  proportionally across sources — and record the choice in run_log.\n"
        )


def run(settings, *, resume: bool) -> dict:
    prompt = _prompt_text(settings)
    conn = init_db(settings.interim_db)
    tagged = 0
    api_calls = 0
    totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cached_documents": 0,
    }
    try:
        with run_log(
            conn,
            run_id=settings.config_hash[:12],
            stage="tag",
            config_hash=settings.config_hash,
        ) as entry:
            docs = _relevant_documents(conn)

            from src.tag.llm_client import (
                DailyLimitReached,
                TaggingClient,
                TaggingFailedError,
                input_token_budget,
                overhead_tokens,
                pack_batches,
            )

            client = TaggingClient(settings=settings)
            per_call = settings.run.model.docs_per_request
            max_doc_tokens = settings.run.model.max_doc_tokens
            budget = input_token_budget(settings)
            overhead = overhead_tokens(prompt, client.schema)

            pending = []
            by_id = {}
            for d in docs:
                key = cache.cache_key(
                    doc_id=d["doc_id"],
                    text=d["text"],
                    model=settings.run.model.name,
                    taxonomy_version=TAXONOMY_VERSION,
                    prompt_version=PROMPT_VERSION,
                )
                cached = cache.get(conn, key)
                if cached is not None:
                    _persist(conn, d["doc_id"], cached, settings)
                    continue
                pending.append(d)
                by_id[d["doc_id"]] = key

            # Pack on a copy of the text. The cache key above used the original, so a
            # truncated tagging call still resumes against the same row (plan §1.2.4).
            for group in pack_batches(
                pending,
                max_count=per_call,
                max_doc_tokens=max_doc_tokens,
                input_budget=budget,
                overhead=overhead,
            ):
                try:
                    results, usage = client.tag_batch(prompt, group)
                    api_calls += 1
                except DailyLimitReached:
                    log.warning(
                        "daily limit reached; checkpointing. Re-run with --resume tomorrow."
                    )
                    break
                except TaggingFailedError as exc:
                    log.warning("%s", exc)
                    continue
                by_result = {r.doc_id: r for r in results}
                per_doc = max(1, usage.get("total_tokens", 0) // max(1, len(group)))
                for d in group:
                    result = by_result.get(d["doc_id"])
                    if result is None:
                        log.warning(
                            "skipping document %s: no valid tags in this batch; "
                            "will retry on --resume",
                            d["doc_id"],
                        )
                        continue
                    key = by_id[d["doc_id"]]
                    cache.put(conn, key, result, prompt_tokens=per_doc)
                    _persist(conn, d["doc_id"], result, settings)
                    tagged += 1
                conn.commit()  # checkpoint after every batch

            totals = cache.token_totals(conn)
            entry.records_in = len(docs)
            entry.records_out = tagged
            entry.note(
                json.dumps(
                    {
                        "tagged_this_run": tagged,
                        "api_calls_this_run": api_calls,
                        "resume": bool(resume),
                        **totals,
                    }
                )
            )
    finally:
        conn.close()
    log.info(
        "tagged %d new documents this run (%d API calls)", tagged, api_calls
    )
    return {"tagged_this_run": tagged, "api_calls_this_run": api_calls, **totals}


def _persist(conn, doc_id, tags, settings) -> None:
    upsert_tags(
        conn, doc_id, tags,
        taxonomy_version=TAXONOMY_VERSION,
        prompt_version=PROMPT_VERSION,
        model=settings.run.model.name,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Tag the corpus (Phase 4).")
    parser.add_argument("--dry-run", action="store_true", help="estimate cost, no API call")
    parser.add_argument("--resume", action="store_true", help="tag, resuming from the cache")
    args = parser.parse_args()
    settings = get_settings()
    # This stage runs for days against the free tier, so it is the one that most
    # needs a log file to resume from — and the one where an untagged review's emoji
    # reaching a cp1252 console would otherwise end the run.
    setup_logging(new_run_id("tag"), settings.logs_dir)
    if args.dry_run:
        dry_run(settings)
    else:
        run(settings, resume=args.resume)


if __name__ == "__main__":
    main()
