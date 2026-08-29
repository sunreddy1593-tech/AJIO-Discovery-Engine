"""Draw a blind gold-set worksheet from already-tagged documents.

    .venv\\Scripts\\python.exe -m scripts.build_gold_worksheet
    .venv\\Scripts\\python.exe -m scripts.build_gold_worksheet --n 40 --seed 7

The tagger has already coded 800 documents. Phase 4's remaining gate is to score
those codes against *independent* labels, which only works if the labeller has
not seen the model's tags. This script writes ``tests/gold/gold_worksheet.jsonl``
with text and empty label fields, and prints the taxonomy legend. It never reads
``tags_json`` into the worksheet and never writes to ``documents`` or ``doc_tags``.

Label the worksheet, save it as ``tests/gold/gold_set.jsonl``, then run
``scripts.score_gold_set``.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.common.config import get_settings  # noqa: E402
from src.common.db import connect  # noqa: E402
from src.tag.taxonomy import (  # noqa: E402
    MULTI_LABEL_DIMENSIONS,
    IntentClass,
)

DEFAULT_N = 40
DEFAULT_SEED = 7
WORKSHEET = PROJECT_ROOT / "tests" / "gold" / "gold_worksheet.jsonl"

MULTI_LABEL_FIELDS = (
    "blocker_type",
    "uncertainty_type",
    "wishlist_motivation",
    "info_sought_elsewhere",
    "segment_cue",
)

EMPTY_ROW_LABELS = {
    "blocker_type": [],
    "uncertainty_type": [],
    "wishlist_motivation": [],
    "info_sought_elsewhere": [],
    "segment_cue": [],
    "intent_class": "",
    "evidence": [],
}


def tagged_by_source(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    """``source -> [(doc_id, text), ...]`` for every document that has a tag row.

    Sorted by ``doc_id`` so a seeded ``random.sample`` does not depend on SQLite
    row order. ``tags_json`` is deliberately not selected.
    """
    rows = conn.execute(
        """
        SELECT d.doc_id, d.source, d.text
        FROM documents AS d
        WHERE d.doc_id IN (SELECT DISTINCT doc_id FROM doc_tags)
        """
    ).fetchall()
    grouped: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["source"], []).append((row["doc_id"], row["text"]))
    return {
        source: sorted(members, key=lambda item: item[0])
        for source, members in sorted(grouped.items())
    }


def allocate_with_source_floor(counts: dict[str, int], n: int) -> dict[str, int]:
    """Proportional draw with a floor of one document per source that has any.

    A plain proportional 40 of 800 would give YouTube ~21 and ``complaints_board``
    zero. The gold set is small enough that a missing source is a missing voice,
    so every tagged source gets at least one row when ``n`` can afford it. The
    remainder is largest-remainder proportional to what is left after the floor.
    """
    present = {source: count for source, count in counts.items() if count > 0}
    if not present or n <= 0:
        return {source: 0 for source in counts}
    if n >= sum(present.values()):
        return dict(present)

    sources = sorted(present)
    if n < len(sources):
        ranked = sorted(present, key=lambda source: (-present[source], source))[:n]
        return {source: int(source in ranked) for source in present}

    leftover_counts = {source: present[source] - 1 for source in sources}
    extra = _largest_remainder(leftover_counts, n - len(sources))
    return {source: 1 + extra.get(source, 0) for source in sources}


def _largest_remainder(counts: dict[str, int], budget: int) -> dict[str, int]:
    allocation = {source: 0 for source in counts}
    total = sum(counts.values())
    if total <= 0 or budget <= 0:
        return allocation
    if budget >= total:
        return dict(counts)
    exact = {source: budget * count / total for source, count in counts.items()}
    for source, value in exact.items():
        allocation[source] = min(int(math.floor(value)), counts[source])
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


def draw(
    universe: dict[str, list[tuple[str, str]]], *, n: int, seed: int
) -> list[tuple[str, str, str]]:
    """Return ``(doc_id, source, text)`` rows, stratified then shuffled by seed."""
    counts = {source: len(members) for source, members in universe.items()}
    allocation = allocate_with_source_floor(counts, n)
    rng = random.Random(seed)
    picked: list[tuple[str, str, str]] = []
    for source in sorted(universe):
        take = allocation.get(source, 0)
        if take <= 0:
            continue
        chosen = rng.sample(universe[source], take)
        picked.extend((doc_id, source, text) for doc_id, text in chosen)
    rng.shuffle(picked)
    return picked


def empty_row(doc_id: str, source: str, text: str) -> dict:
    return {"doc_id": doc_id, "source": source, "text": text, **EMPTY_ROW_LABELS}


def write_worksheet(
    path: Path, rows: list[tuple[str, str, str]], *, n: int, seed: int
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "_meta": (
                        "blind gold worksheet — fill labels, save as gold_set.jsonl. "
                        "Do not consult doc_tags while labelling."
                    ),
                    "n": n,
                    "seed": seed,
                    "blind": True,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        for doc_id, source, text in rows:
            handle.write(json.dumps(empty_row(doc_id, source, text), ensure_ascii=False) + "\n")


def legend() -> str:
    lines = [
        "LABEL LEGEND  (use these strings exactly; multi-label fields are JSON arrays)",
        "",
    ]
    by_name = {name: enum_cls for name, enum_cls in MULTI_LABEL_DIMENSIONS}
    for field in MULTI_LABEL_FIELDS:
        enum_cls = by_name[field]
        lines.append(f"  {field}:")
        for member in enum_cls:
            lines.append(f"    - {member.value}")
        lines.append("")
    lines.append("  intent_class:  (one of)")
    for member in IntentClass:
        lines.append(f"    - {member.value}")
    lines.append("")
    lines.append(
        "  evidence:  list of {\"tag\": <any multi-label value above>, "
        "\"quote\": \"<verbatim span from text>\"}"
    )
    lines.append("             [] if you asserted no multi-label tags.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Draw a blind gold-set worksheet.")
    parser.add_argument("--n", type=int, default=DEFAULT_N, help="documents to sample")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="RNG seed")
    parser.add_argument("--force", action="store_true", help="overwrite an existing worksheet")
    args = parser.parse_args()

    if args.n < 1:
        print("  --n must be at least 1.")
        return 1
    if WORKSHEET.exists() and not args.force:
        print(
            f"\n  {WORKSHEET} already exists. Pass --force to redraw it — that\n"
            "  discards an unlabelled worksheet, not gold_set.jsonl.\n"
        )
        return 1

    settings = get_settings()
    conn = connect(settings.interim_db)
    try:
        universe = tagged_by_source(conn)
    finally:
        conn.close()

    total = sum(len(v) for v in universe.values())
    if not total:
        print("\n  No tagged documents. This script samples doc_tags; it does not tag.\n")
        return 1
    if args.n > total:
        print(f"\n  --n {args.n} is larger than the tagged set ({total}).\n")
        return 1

    rows = draw(universe, n=args.n, seed=args.seed)
    write_worksheet(WORKSHEET, rows, n=args.n, seed=args.seed)
    _print_draw(universe, rows, args.n, args.seed)
    print()
    print(legend())
    print()
    return 0


def _print_draw(
    universe: dict[str, list[tuple[str, str]]],
    rows: list[tuple[str, str, str]],
    n: int,
    seed: int,
) -> None:
    drawn = {}
    for _doc_id, source, _text in rows:
        drawn[source] = drawn.get(source, 0) + 1
    print("\n" + "=" * 66)
    print(" GOLD WORKSHEET  (blind — tagger output is not in this file)")
    print("=" * 66)
    print(f"  seed {seed}   n {n}   tagged universe {sum(len(v) for v in universe.values())}")
    print()
    print(f"  {'SOURCE':<24} {'TAGGED':>8} {'SAMPLED':>8}")
    print(f"  {'-' * 24} {'-' * 8} {'-' * 8}")
    for source in sorted(universe):
        print(
            f"  {source:<24} {len(universe[source]):>8} {drawn.get(source, 0):>8}"
        )
    print(f"  {'-' * 24} {'-' * 8} {'-' * 8}")
    print(f"  {'TOTAL':<24} {sum(len(v) for v in universe.values()):>8} {len(rows):>8}")
    print("=" * 66)
    print(f"  Worksheet: {WORKSHEET}")
    print("  Fill the empty arrays / intent_class, save as tests/gold/gold_set.jsonl,")
    print("  then run:  .venv\\Scripts\\python.exe -m scripts.score_gold_set")
    print("  Do not open doc_tags while labelling.")


if __name__ == "__main__":
    raise SystemExit(main())
