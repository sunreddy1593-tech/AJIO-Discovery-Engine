"""Phase 6 entrypoint: assemble and render the discovery-engine report.

    python -m src.synthesize.run_synthesis            # write outputs/
    python -m src.synthesize.run_synthesis --dry-run  # assemble and print, write nothing
    python -m src.synthesize.run_synthesis --force    # overwrite existing outputs

This stage only *reads*. It never inserts into ``documents`` or ``doc_tags``, never
tags or quantifies an aggregate as a document, and never adds ``ajio_aggregate``
to a source registry. Aggregates reach the report through
``src.store.aggregates`` and leave through this file; they do not pass through
the corpus.

Ranked opportunity areas are read from ``processed_dir/opportunity_scores.csv``
(the file ``src.quantify.run_quantify`` writes). When that file does not exist,
the Opportunity areas section is marked pending and nothing is invented to fill
it. The other sections still render, because they do not depend on it.

Writes (unless ``--dry-run``): ``outputs/opportunity_report.md``,
``outputs/evidence_appendix.md``, and a copy of ``opportunity_scores.csv``.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from src.common.config import get_settings
from src.common.db import connect, run_log
from src.common.logging import get_logger, new_run_id, setup_logging
from src.store.aggregates import AjioAggregate, by_product_id, load_ajio_aggregates, summarize
from src.synthesize.evidence import render_evidence_appendix
from src.synthesize.report import (
    APPENDIX_NAME,
    Opportunity,
    SCORES_NAME,
    assemble_markdown,
    assert_scores_fresh,
    load_opportunity_scores,
)
from src.synthesize.run_log_appendix import render_pipeline_appendix, render_tagger_validation

log = get_logger("synthesize.run")

REPORT_NAME = "opportunity_report.md"
VALIDATION_NAME = "tagger_validation.md"
PROCESSED_COPIES = (
    SCORES_NAME,
    "segment_matrix.csv",
    "tag_prevalence.csv",
    "cooccurrence_lift.csv",
)

__all__ = [
    "Opportunity",
    "load_opportunity_scores",
    "run",
    "assemble_report",
]


def load_aggregates(aggregates_dir: str | Path) -> list[AjioAggregate]:
    """AJIO's own numbers, via the reader — never by opening the JSON here."""
    return load_ajio_aggregates(Path(aggregates_dir) / "ajio")


def assemble_report(
    conn,
    *,
    processed_dir: str | Path,
    aggregates_dir: str | Path,
) -> tuple[str, dict[str, Any], dict]:
    """The seven sections, in architecture.md §9 order, plus a write-side summary."""
    assert_scores_fresh(conn, processed_dir)
    aggregates = load_aggregates(aggregates_dir)
    products = by_product_id(aggregates)
    aggregate_summary = summarize(aggregates)
    markdown, quotes, meta = assemble_markdown(
        conn, processed_dir=processed_dir, aggregates=aggregates
    )
    counts = meta["counts"]
    summary = {
        "documents": counts["documents"],
        "analyzable": counts["analyzable"],
        "sources": {item["source"]: item["documents"] for item in counts["by_source"]},
        "quantify_status": meta["quantify_status"],
        "themes": meta["themes"],
        "aggregates": len(products),
        "ratings_reported": aggregate_summary.ratings_reported,
        "ratings_derived": aggregate_summary.ratings_derived,
        "sections": meta["sections"],
    }
    return markdown, summary, quotes


def report_path(settings) -> Path:
    return Path(settings.outputs_dir) / REPORT_NAME


def appendix_path(settings) -> Path:
    return Path(settings.outputs_dir) / APPENDIX_NAME


def scores_output_path(settings) -> Path:
    return Path(settings.outputs_dir) / SCORES_NAME


def validation_path(settings) -> Path:
    return Path(settings.outputs_dir) / VALIDATION_NAME


def _copy_processed(settings) -> None:
    """Stage 4 CSVs beside the report, so a reader does not hunt under data/processed."""
    dest = Path(settings.outputs_dir)
    src_dir = Path(settings.processed_dir)
    for name in PROCESSED_COPIES:
        src = src_dir / name
        if src.is_file():
            shutil.copy2(src, dest / name)


def run(settings, *, force: bool = False, dry_run: bool = False) -> dict:
    """Assemble the report. Write it unless ``dry_run`` or an unforced existing file."""
    path = report_path(settings)
    if path.exists() and not force and not dry_run:
        log.warning("report exists at %s; pass --force to overwrite", path)
        summary = {"status": "exists", "written": False, "report": str(path)}
        _record_run(settings, summary)
        return summary

    conn = connect(settings.interim_db)
    quotes_md = ""
    try:
        _require_corpus(conn)
        with run_log(
            conn,
            run_id=settings.config_hash[:12],
            stage="synthesize",
            config_hash=settings.config_hash,
        ) as entry:
            markdown, summary, quotes = assemble_report(
                conn,
                processed_dir=settings.processed_dir,
                aggregates_dir=settings.aggregates_dir,
            )
            quotes_md = render_evidence_appendix(quotes)
            summary["report"] = str(path)
            summary["written"] = False
            if dry_run:
                summary["status"] = "dry_run"
                entry.records_in = summary["documents"]
                entry.note(json.dumps(summary))
            else:
                out = Path(settings.outputs_dir)
                out.mkdir(parents=True, exist_ok=True)
                path.write_text(markdown, encoding="utf-8")
                _copy_processed(settings)
                validation_path(settings).write_text(
                    render_tagger_validation(), encoding="utf-8"
                )
                summary["status"] = "written"
                summary["written"] = True
                summary["appendix"] = str(appendix_path(settings))
                entry.records_in = summary["documents"]
                entry.records_out = 1
                entry.note(json.dumps(summary))
        if not dry_run and summary.get("written"):
            appendix_path(settings).write_text(
                quotes_md.rstrip() + "\n\n" + render_pipeline_appendix(conn),
                encoding="utf-8",
            )
    finally:
        conn.close()

    if dry_run:
        print(markdown)
        log.info(
            "dry-run: would write %s (%s, %d documents, %d themes, %d aggregates)",
            path,
            summary["quantify_status"],
            summary["documents"],
            summary["themes"],
            summary["aggregates"],
        )
    else:
        log.info("wrote %s", path)
        _print_summary(summary)
    return summary


def _require_corpus(conn) -> None:
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "documents" not in tables:
        raise FileNotFoundError(
            "corpus is not built (no documents table); "
            "run python -m src.store.build_corpus first"
        )


def _record_run(settings, summary: dict[str, Any]) -> None:
    """A skip still leaves a run_log row, so 'did not overwrite' is auditable."""
    conn = connect(settings.interim_db)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "run_log" not in tables:
            return
        with run_log(
            conn,
            run_id=settings.config_hash[:12],
            stage="synthesize",
            config_hash=settings.config_hash,
        ) as entry:
            entry.note(json.dumps(summary))
    finally:
        conn.close()


def _print_summary(summary: dict[str, Any]) -> None:
    print("\n" + "=" * 54)
    print(" SYNTHESIS  (plan §6)")
    print("=" * 54)
    print(f"  report                    {summary['report']}")
    print(f"  documents                 {summary['documents']:>8}")
    print(f"  analyzable                {summary['analyzable']:>8}")
    print(f"  sources                   {len(summary['sources']):>8}")
    if summary["quantify_status"] == "pending":
        print("  opportunity areas         pending — run Stage 4 (quantify) first")
    else:
        print(f"  opportunity areas         {summary['themes']:>8}")
    print(f"  AJIO aggregates           {summary['aggregates']:>8}")
    print("=" * 54)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the discovery-engine report (Phase 6).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="assemble and print what would be rendered; write nothing",
    )
    parser.add_argument("--force", action="store_true", help="overwrite an existing report")
    args = parser.parse_args()
    settings = get_settings()
    setup_logging(new_run_id("synth"), settings.logs_dir)
    run(settings, force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
