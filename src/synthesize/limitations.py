"""Phase 6's limitations section — what the evidence base cannot support.

The report's credibility rests as much on this section as on the ranked findings,
so it is rendered rather than hand-written: a limitation that has to be
remembered at write-up time is a limitation that gets dropped.

Every other limitation in `architecture.md` §9 is a property of the corpus and
belongs to whatever computes it. The one paragraph that lives here is the one
that is a property of *how two inputs were collected* rather than of what they
say, which means nothing downstream can derive it — the Quora import and the
AJIO aggregate side-channel (`src/store/aggregates.py`) both came out of a
browser session a person drove, and no stage of the pipeline can see that.

Two figures in it are read from the records instead of typed, because both go
stale silently and this document has been wrong about exactly that before: the
product count and the snapshot date range. `extracted_at` is stamped on every
aggregate record by the grabber in `scripts/manual_extract/`, so the range is a
measurement rather than a note someone has to update after re-grabbing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from src.store.aggregates import AjioAggregate, load_ajio_aggregates, summarize

HEADING = "## Limitations"

#: ``extracted_at`` is ISO 8601, so the date part is the leading ten characters.
#: Matched rather than sliced: a stamp in some other shape should fall through to
#: being quoted whole instead of being silently truncated to ten wrong characters.
_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

#: Used only when no record carries a readable ``extracted_at`` — a clone without
#: the side-channel, or a grabber that stopped stamping. It is the range measured
#: across the 51 files present when this section was written, so the sentence
#: states a historical fact rather than rendering "None".
MEASURED_SNAPSHOT = ("2026-08-23", "2026-08-23")

HAND_COLLECTED = (
    "**Hand-collected data (Quora threads and AJIO aggregates).** Two inputs were "
    "gathered manually rather than through the automated pipeline: Quora answers "
    "(`quora_manual`) and AJIO's on-site rating/fit/quality aggregates "
    "(`data/aggregates/`). Both were collected in a logged-in browser session using "
    "tools committed to `scripts/manual_extract/`, and both are point-in-time "
    "snapshots of a live site, collected {snapshot} (per each "
    "record's `extracted_at`). Coverage for the AJIO aggregates is purposive — "
    "N={products} products chosen to match the themes surfaced by the text corpus — "
    "not a random or exhaustive sample, so the aggregate figures characterise those "
    "products rather than AJIO's catalogue. The aggregates also reflect customers "
    "who purchased and rated (buyers), not the wishlist-abandoners this study "
    "targets, and are used only to corroborate themes established in the text "
    "corpus, never as primary evidence. The rendered section notes, per figure, "
    "whether an average was read directly from AJIO or derived from its rating "
    "distribution. Because these two sources are collected manually from a live "
    "site, they are method-reproducible (tool and procedure committed) but not "
    "command-reproducible; re-collection yields a fresh snapshot."
)


def _date_part(stamp: str) -> str:
    match = _ISO_DATE.match(stamp)
    return match.group(1) if match else stamp


def snapshot_range(aggregates: Sequence[AjioAggregate]) -> tuple[str, str]:
    """Earliest and latest ``extracted_at`` across the records, as dates.

    Falls back to :data:`MEASURED_SNAPSHOT` when nothing is stamped, so the
    paragraph cannot render an empty range.
    """
    stamps = sorted(_date_part(a.extracted_at) for a in aggregates if a.extracted_at)
    if not stamps:
        return MEASURED_SNAPSHOT
    return stamps[0], stamps[-1]


def snapshot_phrase(aggregates: Sequence[AjioAggregate]) -> str:
    """The snapshot clause, collapsed to one date when the range spans a day.

    Every grab so far happened in a single afternoon, and "between 2026-08-23 and
    2026-08-23" reads as though someone forgot to fill in a template. The range
    form returns as soon as a second day's records exist.
    """
    start, end = snapshot_range(aggregates)
    return f"on {start}" if start == end else f"between {start} and {end}"


def hand_collected_paragraph(aggregates: Sequence[AjioAggregate]) -> str:
    """The hand-collection caveat, with its count and date range filled in."""
    return HAND_COLLECTED.format(
        snapshot=snapshot_phrase(aggregates), products=summarize(aggregates).products
    )


def render_section(
    entries: Iterable[str] = (),
    *,
    aggregates: Sequence[AjioAggregate] | None = None,
    aggregates_dir: str | Path | None = None,
) -> str:
    """The "Limitations" section, as markdown.

    ``entries`` are the corpus-derived limitations, rendered in the order given;
    the hand-collection paragraph is always appended last, since it qualifies the
    evidence base rather than any single finding.
    """
    if aggregates is None:
        aggregates = (
            load_ajio_aggregates(Path(aggregates_dir) / "ajio")
            if aggregates_dir is not None
            else []
        )

    paragraphs = [entry.strip() for entry in entries if entry and entry.strip()]
    paragraphs.append(hand_collected_paragraph(aggregates))

    lines = [HEADING, ""]
    for paragraph in paragraphs:
        lines.extend([paragraph, ""])
    return "\n".join(lines).rstrip() + "\n"
