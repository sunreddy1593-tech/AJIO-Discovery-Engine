"""Phase 3's fourth exit criterion: audit what the filters threw away.

    .venv\\Scripts\\python.exe -m scripts.audit_rejected_pool            # draw the worksheet
    .venv\\Scripts\\python.exe -m scripts.audit_rejected_pool --score    # score it once labelled

Three rules and a keyword list currently discard **19,591 of 26,718** eligible
documents, and their combined false-rejection rate has never been measured. That
is the criterion this script serves, and the reason it matters more than it looks:
an over-aggressive filter does not produce a visibly broken corpus, it produces a
plausible corpus with the finding removed, and nothing downstream can recover it.

**This script cannot score the audit by itself, and does not pretend to.** Whether
a rejection was *wrong* is a judgement about meaning — that is what "50-document
manual audit" in plan §3 means, and a machine verdict here would be the tagger
grading its own filter. So the work is split:

1. ``--sample`` draws a seeded, stratified worksheet to ``outputs/rejected_pool_audit.jsonl``
   with ``false_rejection: null`` on every row.
2. A person reads each document and sets that field to ``true`` or ``false``.
3. ``--score`` computes the per-stratum rates and writes ``outputs/rejected_pool_audit.md``,
   passing or failing the < 10% gate.

**Strata, and why these.** The plan asks for the three hard-exclusion codes and
triage rejections scored *separately*, because they fail differently and the
remedies are unrelated — a bad emoji rule is one config flag, a narrow vocabulary
is an edit to a keyword file. Tier-1's zero-hit drop is split further, on the
distinction ``min_content_words`` exists to draw: a document with almost no content
words is "about nothing" and its rejection is uninformative, while a contentful
document with zero keyword hits is "about something else" — and that stratum is
the only direct evidence that the vocabulary is too narrow.

**A note on resolution that the report repeats, because it bounds the conclusion.**
Ten documents per stratum measures a rate in steps of 10%, so a single false
rejection already sits at the gate. That is enough to detect a *broken* rule and
not enough to certify a good one at 9%; raise ``--per-stratum`` when a stratum
lands near the line rather than reading the first number as a verdict.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.common.config import get_settings  # noqa: E402
from src.common.db import connect  # noqa: E402
from src.common.logging import new_run_id, setup_logging  # noqa: E402
from src.store.relevance import content_word_count  # noqa: E402

WORKSHEET_NAME = "rejected_pool_audit.jsonl"
REPORT_NAME = "rejected_pool_audit.md"

DEFAULT_PER_STRATUM = 10
DEFAULT_SEED = 42

#: The gate from plan §3's fourth exit criterion.
MAX_FALSE_REJECTION_RATE = 0.10

#: Stratum -> what a labeller is being asked, and what a failure there would mean.
STRATUM_QUESTION: dict[str, str] = {
    "too_short": (
        "Does this text, despite its length, describe deliberating over, saving, "
        "comparing, postponing or abandoning a fashion purchase? "
        '("does this run small?" is four words and is a real blocker.)'
    ),
    "contains_emoji": (
        "Strip the emoji. Is what remains a substantive comment that belongs in the "
        "corpus? A yes here is the strongest argument for narrowing the emoji rule, "
        "which is one config flag."
    ),
    "hindi_language": (
        "Is this actually Hindi? Romanized Hinglish is meant to be KEPT, so a "
        "Hinglish document here is a false rejection."
    ),
    "tier1_zero_hits_contentful": (
        "Is this about wishlist/purchase deliberation despite matching no keyword? "
        "A yes names a term the vocabulary is missing — this is the stratum that "
        "measures whether the vocabulary is too narrow."
    ),
    "tier1_zero_hits_contentless": (
        "Does this say anything at all about a purchase decision? Most of these are "
        "expected to be genuinely contentless; a yes is still worth recording."
    ),
    "tier2_rejected": (
        "The triage model judged this NOT about pre-purchase deliberation. Do you "
        "agree? A no is a false rejection by the LLM rather than by a rule."
    ),
}

STRATUM_ORDER = list(STRATUM_QUESTION)


@dataclass
class Candidate:
    doc_id: str
    source: str
    text: str
    stratum: str
    word_count: int
    content_words: int
    relevance_score: float | None


def rejected_pool(conn: sqlite3.Connection, *, min_content_words: int) -> dict[str, list[Candidate]]:
    """Every rejected document, grouped into the strata above.

    ``is_relevant = 0`` is the whole rejected pool, but it does not say *which*
    stage did the rejecting, so the stratum is reconstructed from the columns that
    do. ``exclusion_reason`` names the three hard rules directly. Below them, a
    zero ``relevance_score`` means tier 1 found no keyword at all, while a non-zero
    score with ``is_relevant = 0`` can only have come from tier 2 — the document
    matched the vocabulary and was then judged irrelevant by the model.
    """
    rows = conn.execute(
        """
        SELECT doc_id, source, text, word_count, relevance_score, exclusion_reason
        FROM documents
        WHERE is_relevant = 0 AND is_duplicate_of IS NULL
        """
    ).fetchall()

    pool: dict[str, list[Candidate]] = {name: [] for name in STRATUM_ORDER}
    for row in rows:
        reason = row["exclusion_reason"]
        score = row["relevance_score"]
        content_words = content_word_count(row["text"])
        if reason in ("too_short", "contains_emoji", "hindi_language"):
            stratum = reason
        elif score is None or score == 0:
            stratum = (
                "tier1_zero_hits_contentless"
                if min_content_words and content_words < min_content_words
                else "tier1_zero_hits_contentful"
            )
        else:
            stratum = "tier2_rejected"
        pool[stratum].append(
            Candidate(
                doc_id=row["doc_id"],
                source=row["source"],
                text=row["text"],
                stratum=stratum,
                word_count=row["word_count"] or len(row["text"].split()),
                content_words=content_words,
                relevance_score=score,
            )
        )
    return pool


def draw(
    pool: dict[str, list[Candidate]], *, per_stratum: int, seed: int
) -> list[Candidate]:
    """An equal-sized draw per stratum, capped by what each one holds.

    Equal rather than proportional on purpose: the criterion asks for a rate *per
    stratum*, and a proportional draw would spend 46 of 50 slots on `too_short`
    and leave the emoji rule — the one with a config flag waiting on the answer —
    measured by two documents.
    """
    rng = random.Random(seed)
    sample: list[Candidate] = []
    for stratum in STRATUM_ORDER:
        members = sorted(pool.get(stratum, []), key=lambda c: c.doc_id)
        if not members:
            continue
        take = min(per_stratum, len(members))
        sample.extend(rng.sample(members, take))
    return sample


def write_worksheet(path: Path, sample: list[Candidate], *, seed: int, per_stratum: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "_meta": "labelling worksheet — set false_rejection to true or false on every row below",
                    "seed": seed,
                    "per_stratum": per_stratum,
                    "gate": f"< {MAX_FALSE_REJECTION_RATE:.0%} false rejections per stratum",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        for candidate in sample:
            handle.write(
                json.dumps(
                    {
                        "doc_id": candidate.doc_id,
                        "stratum": candidate.stratum,
                        "source": candidate.source,
                        "machine_reason": candidate.stratum,
                        "question": STRATUM_QUESTION[candidate.stratum],
                        "word_count": candidate.word_count,
                        "content_words": candidate.content_words,
                        "text": candidate.text,
                        "false_rejection": None,
                        "note": "",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return len(sample)


def read_worksheet(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if "_meta" in payload:
                continue
            rows.append(payload)
    return rows


def score(rows: list[dict]) -> dict:
    """Per-stratum false-rejection rates, and the gate's verdict on each."""
    labelled = [r for r in rows if isinstance(r.get("false_rejection"), bool)]
    unlabelled = len(rows) - len(labelled)

    by_stratum: dict[str, dict] = {}
    for stratum in STRATUM_ORDER:
        members = [r for r in labelled if r.get("stratum") == stratum]
        if not members:
            continue
        wrong = sum(1 for r in members if r["false_rejection"])
        rate = wrong / len(members)
        by_stratum[stratum] = {
            "audited": len(members),
            "false_rejections": wrong,
            "rate": rate,
            "passes": rate < MAX_FALSE_REJECTION_RATE,
        }

    total_wrong = sum(s["false_rejections"] for s in by_stratum.values())
    total_audited = sum(s["audited"] for s in by_stratum.values())
    overall = total_wrong / total_audited if total_audited else 0.0
    return {
        "unlabelled": unlabelled,
        "by_stratum": by_stratum,
        "audited": total_audited,
        "false_rejections": total_wrong,
        "rate": overall,
        "passes": bool(by_stratum) and all(s["passes"] for s in by_stratum.values()),
    }


def render_report(result: dict, *, seed: int, sources: Counter | None = None) -> str:
    lines = [
        "# Rejected-pool audit — Phase 3 exit criterion 4",
        "",
        f"Sample drawn with seed {seed}. The gate is a false-rejection rate below "
        f"{MAX_FALSE_REJECTION_RATE:.0%}, scored per stratum rather than as one number, "
        "because the three hard rules and the two triage tiers fail differently and "
        "are fixed differently.",
        "",
        "| Stratum | Audited | False rejections | Rate | Gate |",
        "| --- | --- | --- | --- | --- |",
    ]
    for stratum, stats in result["by_stratum"].items():
        verdict = "PASS" if stats["passes"] else "**FAIL**"
        lines.append(
            f"| `{stratum}` | {stats['audited']} | {stats['false_rejections']} | "
            f"{stats['rate']:.0%} | {verdict} |"
        )
    lines.append(
        f"| **All strata** | {result['audited']} | {result['false_rejections']} | "
        f"{result['rate']:.0%} | {'PASS' if result['passes'] else '**FAIL**'} |"
    )
    lines += [
        "",
        f"**Verdict: {'PASS' if result['passes'] else 'FAIL'}** — the criterion requires "
        "every stratum below the gate, not just the average, since a rule that is wrong "
        "half the time can hide behind four that are never wrong.",
        "",
        "## How far this measurement goes",
        "",
        f"At {min((s['audited'] for s in result['by_stratum'].values()), default=0)}–"
        f"{max((s['audited'] for s in result['by_stratum'].values()), default=0)} documents "
        "per stratum, a rate is resolvable only in steps of roughly 10%, so one false "
        "rejection puts a stratum at the gate. That is enough to detect a broken rule and "
        "not enough to certify a working one — a stratum landing near the line should be "
        "re-drawn at a larger `--per-stratum` before any rule is called sound.",
    ]
    if sources:
        lines += [
            "",
            "## Sources represented in the sample",
            "",
            "| Source | Documents |",
            "| --- | --- |",
        ]
        for source, count in sources.most_common():
            lines.append(f"| {source} | {count} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the rejected pool (Phase 3).")
    parser.add_argument("--score", action="store_true", help="score the labelled worksheet")
    parser.add_argument(
        "--per-stratum", type=int, default=DEFAULT_PER_STRATUM,
        help=f"documents per stratum (default {DEFAULT_PER_STRATUM}, so ~50 across five)",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="RNG seed")
    parser.add_argument("--force", action="store_true", help="overwrite an existing worksheet")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(new_run_id("rejected_audit"), settings.logs_dir)
    worksheet = Path(settings.outputs_dir) / WORKSHEET_NAME

    if args.score:
        if not worksheet.exists():
            print(f"\n  No worksheet at {worksheet}. Draw one first (no --score).\n")
            return 1
        rows = read_worksheet(worksheet)
        result = score(rows)
        if result["unlabelled"]:
            print(
                f"\n  {result['unlabelled']} of {len(rows)} rows still have "
                "false_rejection: null.\n  Label every row before scoring — a rate over "
                "a partial sample is not the\n  measurement the criterion asks for.\n"
            )
            return 1
        sources = Counter(r.get("source", "?") for r in rows)
        report = Path(settings.outputs_dir) / REPORT_NAME
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_report(result, seed=args.seed, sources=sources), encoding="utf-8")
        _print_score(result, report)
        return 0 if result["passes"] else 1

    if worksheet.exists() and not args.force:
        print(
            f"\n  {worksheet} already exists. Pass --force to redraw it — but note that\n"
            "  redrawing discards any labelling already done in it.\n"
        )
        return 1

    conn = connect(settings.interim_db)
    pool = rejected_pool(conn, min_content_words=settings.run.filters.min_content_words)
    conn.close()

    total = sum(len(v) for v in pool.values())
    if not total:
        print("\n  The rejected pool is empty. Build the corpus first.\n")
        return 1

    sample = draw(pool, per_stratum=args.per_stratum, seed=args.seed)
    written = write_worksheet(worksheet, sample, seed=args.seed, per_stratum=args.per_stratum)
    _print_draw(pool, sample, written, worksheet)
    return 0


def _print_draw(pool, sample, written, worksheet) -> None:
    drawn = Counter(c.stratum for c in sample)
    print("\n" + "=" * 66)
    print(" REJECTED-POOL AUDIT  (plan §3 exit criterion 4)")
    print("=" * 66)
    print(f"  {'STRATUM':<30} {'REJECTED':>10} {'SAMPLED':>8}")
    print(f"  {'-' * 30} {'-' * 10} {'-' * 8}")
    for stratum in STRATUM_ORDER:
        available = len(pool.get(stratum, []))
        if not available:
            continue
        print(f"  {stratum:<30} {available:>10,} {drawn.get(stratum, 0):>8}")
    print(f"  {'-' * 30} {'-' * 10} {'-' * 8}")
    print(f"  {'TOTAL':<30} {sum(len(v) for v in pool.values()):>10,} {written:>8}")
    print("=" * 66)
    print(
        f"  Worksheet: {worksheet}\n\n"
        "  Set false_rejection to true or false on every row, then run with --score.\n"
        "  Each row carries the question its stratum is asking. Nothing here labels\n"
        "  itself: whether a rejection was wrong is a judgement about meaning, and a\n"
        "  machine verdict would be the filter grading its own homework.\n"
    )


def _print_score(result, report) -> None:
    print("\n" + "=" * 66)
    print(" REJECTED-POOL AUDIT — SCORED")
    print("=" * 66)
    print(f"  {'STRATUM':<30} {'AUDITED':>8} {'WRONG':>7} {'RATE':>7}  GATE")
    print(f"  {'-' * 30} {'-' * 8} {'-' * 7} {'-' * 7}  {'-' * 4}")
    for stratum, stats in result["by_stratum"].items():
        verdict = "PASS" if stats["passes"] else "FAIL"
        print(
            f"  {stratum:<30} {stats['audited']:>8} {stats['false_rejections']:>7} "
            f"{stats['rate']:>6.0%}  {verdict}"
        )
    print(f"  {'-' * 30} {'-' * 8} {'-' * 7} {'-' * 7}  {'-' * 4}")
    print(
        f"  {'ALL':<30} {result['audited']:>8} {result['false_rejections']:>7} "
        f"{result['rate']:>6.0%}  {'PASS' if result['passes'] else 'FAIL'}"
    )
    print("=" * 66)
    print(f"  Report written to {report}\n")


if __name__ == "__main__":
    raise SystemExit(main())
