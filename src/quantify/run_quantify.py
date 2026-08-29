"""Phase 5 entrypoint: tagged corpus -> processed CSVs (architecture.md §8).

    python -m src.quantify.run_quantify              # write all four CSVs
    python -m src.quantify.run_quantify --dry-run    # count, print, write nothing
    python -m src.quantify.run_quantification        # the name the plan uses

This stage only *reads* ``documents`` and ``doc_tags``. It never retags, never
touches the taxonomy, and never treats ``ajio_aggregate`` as a document.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any

from src.common.config import get_settings
from src.common.db import connect, run_log
from src.common.logging import get_logger, new_run_id, setup_logging
from src.quantify.metrics import (
    BASE_COLUMNS,
    LIFT_COLUMNS,
    PREVALENCE_COLUMNS,
    SCORE_EXTRA_COLUMNS,
    SEGMENT_COLUMNS,
    knobs_from_settings,
    load_analyzable,
    quantify,
)
from src.quantify.scoring import WEIGHTING_NOTE

log = get_logger("quantify.run")

SCORES_NAME = "opportunity_scores.csv"
PREVALENCE_NAME = "tag_prevalence.csv"
LIFT_NAME = "cooccurrence_lift.csv"
SEGMENT_NAME = "segment_matrix.csv"

_RATE_PLACES = 6
_MEAN_PLACES = 4
_SCORE_PLACES = 2
_BOOL_FIELDS = frozenset(
    {
        "low_confidence",
        "post_purchase_only",
        "reportable",
        "source_specific",
        "high_prevalence",
    }
)
_INT_FIELDS = frozenset(
    {
        "n_docs",
        "n_authors",
        "n_pre_purchase",
        "n_post_purchase",
        "n_mixed",
        "n_docs_genuine",
        "n_both",
        "n_a",
        "n_b",
    }
)
_SCORE_FIELDS = frozenset({"opportunity_score", "opportunity_score_genuine"})
_MEAN_FIELDS = frozenset(
    {
        "mean_severity",
        "mean_actionability",
        "mean_confidence",
        "mean_severity_genuine",
        "mean_actionability_genuine",
        "mean_confidence_genuine",
    }
)


def _require_corpus(conn) -> None:
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "documents" not in tables or "doc_tags" not in tables:
        raise FileNotFoundError(
            "corpus is not built (documents/doc_tags missing); "
            "run python -m src.store.build_corpus and python -m src.tag.run_tagging first"
        )


def _cell(key: str, value: Any) -> Any:
    if value is None:
        return ""
    if key in _BOOL_FIELDS:
        return "true" if value else "false"
    if key in _INT_FIELDS:
        return int(value)
    if key in _SCORE_FIELDS:
        return f"{float(value):.{_SCORE_PLACES}f}"
    if key in _MEAN_FIELDS:
        return f"{float(value):.{_MEAN_PLACES}f}"
    if isinstance(value, float):
        return f"{value:.{_RATE_PLACES}f}"
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: _cell(col, row.get(col)) for col in columns})


def fieldnames_for(sources: list[str]) -> list[str]:
    return list(BASE_COLUMNS) + list(SCORE_EXTRA_COLUMNS) + [f"prevalence_{s}" for s in sources]


def write_scores(path: Path, rows: list[dict[str, Any]], sources: list[str]) -> None:
    _write_csv(path, rows, fieldnames_for(sources))


def _print_summary(summary: dict[str, Any]) -> None:
    print("\n" + "=" * 54)
    print(" QUANTIFY  (plan §5)")
    print("=" * 54)
    print(f"  analyzable documents (tagged) {summary['analyzable_docs']:>6}")
    print(f"  genuine_intent subset         {summary['n_genuine']:>6}")
    print(f"  opportunity rows              {summary['rows']:>6}")
    print(f"  lift pairs                    {summary['lift_rows']:>6}")
    print(f"  segment-matrix cells          {summary['segment_rows']:>6}")
    if summary.get("path"):
        print(f"  wrote                         {summary['path']}")
    print("=" * 54)
    print(f"  {WEIGHTING_NOTE}")
    print()


def dry_run(settings) -> dict[str, Any]:
    conn = connect(settings.interim_db)
    try:
        _require_corpus(conn)
        docs = load_analyzable(conn)
    finally:
        conn.close()
    result = quantify(docs, knobs=knobs_from_settings(settings))
    path = Path(settings.processed_dir) / SCORES_NAME
    summary = {
        "analyzable_docs": result.n_docs,
        "n_genuine": result.n_genuine,
        "rows": len(result.opportunities),
        "lift_rows": len(result.cooccurrence_lift),
        "segment_rows": len(result.segment_matrix),
        "n_sources": len(result.sources),
        "path": str(path),
        "dry_run": True,
    }
    _print_summary(summary)
    log.info(
        "dry-run: %d analyzable docs (%d genuine_intent) -> %d opportunity rows; "
        "would write %s and sibling CSVs",
        result.n_docs,
        result.n_genuine,
        len(result.opportunities),
        path,
    )
    return summary


def run(settings) -> dict[str, Any]:
    processed = Path(settings.processed_dir)
    conn = connect(settings.interim_db)
    try:
        _require_corpus(conn)
        docs = load_analyzable(conn)
        result = quantify(docs, knobs=knobs_from_settings(settings))
        scores_path = processed / SCORES_NAME
        write_scores(scores_path, result.opportunities, result.sources)
        _write_csv(processed / PREVALENCE_NAME, result.prevalence, list(PREVALENCE_COLUMNS))
        _write_csv(processed / LIFT_NAME, result.cooccurrence_lift, list(LIFT_COLUMNS))
        _write_csv(processed / SEGMENT_NAME, result.segment_matrix, list(SEGMENT_COLUMNS))
        summary = {
            "analyzable_docs": result.n_docs,
            "n_genuine": result.n_genuine,
            "rows": len(result.opportunities),
            "lift_rows": len(result.cooccurrence_lift),
            "segment_rows": len(result.segment_matrix),
            "n_sources": len(result.sources),
            "path": str(scores_path),
            "dry_run": False,
        }
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "run_log" in tables:
            with run_log(
                conn,
                run_id=settings.config_hash[:12],
                stage="quantify",
                config_hash=settings.config_hash,
            ) as entry:
                entry.records_in = result.n_docs
                entry.records_out = len(result.opportunities)
                entry.note(WEIGHTING_NOTE)
                entry.note(
                    f"wrote {SCORES_NAME}, {PREVALENCE_NAME}, {LIFT_NAME}, {SEGMENT_NAME}"
                )
    finally:
        conn.close()

    log.info(
        "wrote %d opportunity rows from %d analyzable docs (%d genuine_intent) to %s",
        summary["rows"],
        summary["analyzable_docs"],
        summary["n_genuine"],
        summary["path"],
    )
    _print_summary(summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Quantify tagged documents (Phase 5).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count analyzable docs and opportunity rows; write nothing",
    )
    args = parser.parse_args()
    settings = get_settings()
    setup_logging(new_run_id("quantify"), settings.logs_dir)
    if args.dry_run:
        dry_run(settings)
    else:
        run(settings)


if __name__ == "__main__":
    main()
