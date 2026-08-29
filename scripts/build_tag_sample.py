"""Draw the proportional-stratified sample that Phase 4 will actually tag.

    .venv\\Scripts\\python.exe -m scripts.build_tag_sample --target 800 --seed 42
    .venv\\Scripts\\python.exe scripts\\build_tag_sample.py --target 800 --force

The corpus holds 7,127 relevant documents, which is 23 days of free-tier tagging
(plan §0.2). The decision gate in ``run_tagging --dry-run`` offers two ways out —
pay, or "sample proportionally across sources and record the choice in run_log" —
and this script is that second option, made reproducible.

**The sample is a side table, never an edit to the corpus.** Nothing here touches
``is_relevant``, ``is_duplicate_of``, or any ``documents`` row. It writes doc_ids
into ``tag_sample``, and ``run_tagging._relevant_documents`` intersects with that
table when it holds at least one row. Three properties follow, and each is the
reason for the design rather than a side effect:

* **Reversible.** ``DROP TABLE tag_sample`` restores the full 7,127-document job.
  Had the sample been applied by flipping ``is_relevant`` to 0, the triage decision
  and the budget decision would be stored in the same column, and no later reader
  could tell "the triage judged this irrelevant" from "we could not afford it".
* **Auditable.** The corpus still reports what triage decided, so the funnel and
  the sample are two separate numbers the report can quote side by side.
* **Backward compatible.** Absent or empty table means tag everything, which is
  what every run before this script did.

**Two strata, for two different reasons.**

``CENSUS_SOURCES`` are taken whole. They are small — a few hundred documents
between them — so sampling them would buy almost no tokens while adding variance
to exactly the sources that are already thin. Quora in particular is the only
hand-collected pre-purchase route in the corpus (182 records, 107 relevant), and a
proportional draw would cut it to a dozen documents, which is too few to say
anything about at all. Census keeps the small-source voice intact for the price of
a rounding error.

Everything else is drawn **proportionally to its taggable size**, so the sample's
source mix matches the corpus's source mix and the report's prevalence figures do
not need a re-weighting step to be honest. The draw is
``random.Random(seed)`` over each source's doc_ids in sorted order, so the same
seed and target reproduce the same sample byte for byte — a report that says "we
tagged 800 of 7,127" has to be able to say *which* 800.

What this deliberately does **not** fix is the YouTube concentration: proportional
sampling reproduces it faithfully, because a sample that quietly rebalanced the
mix would understate the monoculture Phase 6 is required to disclose.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.common.config import get_settings  # noqa: E402
from src.common.db import init_db, run_log  # noqa: E402
from src.common.logging import new_run_id, setup_logging  # noqa: E402
from src.tag.run_tagging import RELEVANT_PREDICATE  # noqa: E402

#: Sources included in full rather than sampled. Small enough that sampling saves
#: almost nothing, and thin enough that sampling would cost the report their voice.
CENSUS_SOURCES: frozenset[str] = frozenset(
    {"consumer_complaints_in", "quora_manual", "complaints_board"}
)

DEFAULT_TARGET = 800
DEFAULT_SEED = 42

#: Deliberately not in ``db.SCHEMA_SQL``. The tagger treats an absent table as "tag
#: everything", so the table has to be creatable by this script alone; adding it to
#: the shared schema would create it in every database that ever calls ``init_db``
#: and quietly make "absent" a state that no longer occurs.
CREATE_TAG_SAMPLE_SQL = """
CREATE TABLE IF NOT EXISTS tag_sample (
    doc_id  TEXT PRIMARY KEY,
    source  TEXT,
    drawn   TEXT
)
"""


class SampleExistsError(RuntimeError):
    """A sample is already on record and ``--force`` was not passed."""


@dataclass
class Sample:
    """One draw: what was available, what was chosen, and the spec to reproduce it."""

    seed: int
    target: int
    available: dict[str, int] = field(default_factory=dict)
    selected: dict[str, list[str]] = field(default_factory=dict)

    @property
    def total_available(self) -> int:
        return sum(self.available.values())

    @property
    def total_selected(self) -> int:
        return sum(len(ids) for ids in self.selected.values())

    def doc_ids(self) -> list[str]:
        return [doc_id for source in sorted(self.selected) for doc_id in self.selected[source]]

    def spec(self) -> dict:
        """The disclosure record: enough to redraw this sample from scratch."""
        return {
            "seed": self.seed,
            "target": self.target,
            "census_sources": sorted(CENSUS_SOURCES & set(self.available)),
            "taggable_total": self.total_available,
            "sampled_total": self.total_selected,
            "taggable_by_source": dict(sorted(self.available.items())),
            "sampled_by_source": {
                source: len(ids) for source, ids in sorted(self.selected.items())
            },
        }


def taggable_by_source(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Every taggable doc_id, grouped by source and sorted within it.

    Sorted because ``random.sample`` draws from a sequence: leaving the order to
    SQLite would make the sample depend on physical row order, and a "seeded"
    draw that changes after a VACUUM is not reproducible in any useful sense.
    """
    rows = conn.execute(
        f"SELECT source, doc_id FROM documents WHERE {RELEVANT_PREDICATE}"
    ).fetchall()
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(row[1])
    return {source: sorted(ids) for source, ids in sorted(grouped.items())}


def allocate(counts: dict[str, int], budget: int) -> dict[str, int]:
    """Split ``budget`` across sources in proportion to ``counts``.

    Largest-remainder rather than plain rounding, so the parts sum to the budget
    instead of landing a few either side of it, and capped per source so a small
    source is never asked for more documents than it has.
    """
    allocation = {source: 0 for source in counts}
    total = sum(counts.values())
    if total <= 0 or budget <= 0:
        return allocation
    if budget >= total:
        return dict(counts)

    exact = {source: budget * count / total for source, count in counts.items()}
    for source, value in exact.items():
        allocation[source] = min(int(math.floor(value)), counts[source])

    # Hand out what rounding down left over, largest fractional part first. Ties
    # break on the bigger source, then on name, so the result is order-independent.
    remainder = budget - sum(allocation.values())
    order = sorted(
        counts,
        key=lambda source: (-(exact[source] % 1), -counts[source], source),
    )
    while remainder > 0:
        room = [source for source in order if allocation[source] < counts[source]]
        if not room:
            break
        for source in room:
            if remainder == 0:
                break
            allocation[source] += 1
            remainder -= 1
    return allocation


def draw_sample(
    universe: dict[str, list[str]], *, target: int = DEFAULT_TARGET, seed: int = DEFAULT_SEED
) -> Sample:
    """Census the small sources, then spend what is left proportionally."""
    available = {source: len(ids) for source, ids in universe.items()}
    sample = Sample(seed=seed, target=target, available=available)

    census = {s: ids for s, ids in universe.items() if s in CENSUS_SOURCES}
    sampled = {s: ids for s, ids in universe.items() if s not in CENSUS_SOURCES}
    for source, ids in census.items():
        sample.selected[source] = sorted(ids)

    budget = max(0, target - sum(len(ids) for ids in census.values()))
    allocation = allocate({s: len(ids) for s, ids in sampled.items()}, budget)

    rng = random.Random(seed)
    for source in sorted(sampled):
        take = allocation.get(source, 0)
        if take <= 0:
            continue
        # rng.sample over a sorted pool: same seed, same target, same doc_ids.
        sample.selected[source] = sorted(rng.sample(sampled[source], take))
    return sample


def existing_rows(conn: sqlite3.Connection) -> int:
    """How many doc_ids a previous draw left behind. 0 when the table is absent."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tag_sample'"
    ).fetchone()
    if table is None:
        return 0
    return conn.execute("SELECT COUNT(*) FROM tag_sample").fetchone()[0]


def write_sample(conn: sqlite3.Connection, sample: Sample, *, force: bool = False) -> int:
    """Persist the draw. Refuses to silently replace an existing one."""
    if force:
        conn.execute("DROP TABLE IF EXISTS tag_sample")
    conn.execute(CREATE_TAG_SAMPLE_SQL)
    if existing_rows(conn):
        raise SampleExistsError(
            "tag_sample already holds rows; re-run with --force to redraw it, or "
            "drop the table to tag the whole relevant corpus again"
        )

    drawn = datetime.now(timezone.utc).isoformat()
    rows = [
        (doc_id, source, drawn)
        for source in sorted(sample.selected)
        for doc_id in sample.selected[source]
    ]
    conn.executemany(
        "INSERT INTO tag_sample (doc_id, source, drawn) VALUES (?, ?, ?)", rows
    )
    return len(rows)


def build(settings, *, target: int, seed: int, force: bool) -> Sample:
    conn = init_db(settings.interim_db)
    try:
        if existing_rows(conn) and not force:
            raise SampleExistsError(
                f"tag_sample already holds {existing_rows(conn)} doc_ids. Pass --force to "
                "redraw it, or drop the table to restore the full tagging job."
            )
        universe = taggable_by_source(conn)
        sample = draw_sample(universe, target=target, seed=seed)
        with run_log(
            conn,
            run_id=settings.config_hash[:12],
            stage="tag_sample",
            config_hash=settings.config_hash,
        ) as entry:
            entry.records_in = sample.total_available
            entry.records_out = write_sample(conn, sample, force=force)
            entry.note(json.dumps(sample.spec(), sort_keys=True))
    finally:
        conn.close()
    _print_sample(sample)
    return sample


def _print_sample(sample: Sample) -> None:
    print("\n" + "=" * 66)
    print(" TAG SAMPLE  (plan §4 budget gate: sample rather than pay)")
    print("=" * 66)
    print(f"  seed {sample.seed}   target {sample.target}")
    print()
    rule = f"  {'-' * 22} {'-' * 8} {'-' * 8} {'-' * 7} {'-' * 12}"
    print(f"  {'SOURCE':<22} {'TAGGABLE':>8} {'SAMPLED':>8} {'SHARE':>7} BASIS")
    print(rule)
    for source in sorted(sample.available):
        taggable = sample.available[source]
        chosen = len(sample.selected.get(source, []))
        share = chosen / taggable if taggable else 0.0
        basis = "census" if source in CENSUS_SOURCES else "proportional"
        print(f"  {source:<22} {taggable:>8,} {chosen:>8,} {share:>6.1%} {basis}")
    print(rule)
    overall = sample.total_selected / sample.total_available if sample.total_available else 0.0
    print(
        f"  {'TOTAL':<22} {sample.total_available:>8,} {sample.total_selected:>8,} "
        f"{overall:>6.1%}"
    )
    print("=" * 66)
    if sample.total_selected > sample.target:
        print(
            f"  Note: the census sources alone exceed the target, so the sample is "
            f"{sample.total_selected} rather than {sample.target}.\n"
        )
    print(
        "  The corpus is untouched: is_relevant and is_duplicate_of are unchanged and\n"
        "  this draw lives only in tag_sample. Drop that table to tag everything again.\n"
        "  Next: python -m src.tag.run_tagging --dry-run\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Draw the Phase 4 tagging sample.")
    parser.add_argument(
        "--target", type=int, default=DEFAULT_TARGET, help="documents to tag (default 800)"
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help="RNG seed; the sample is reproducible from it"
    )
    parser.add_argument("--force", action="store_true", help="drop and redraw an existing sample")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(new_run_id("tag_sample"), settings.logs_dir)
    try:
        build(settings, target=args.target, seed=args.seed, force=args.force)
    except SampleExistsError as exc:
        print(f"\n  {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
