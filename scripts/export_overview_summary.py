"""Write data/processed/overview_summary.json from the local tagged corpus.

    .venv\\Scripts\\python.exe -m scripts.export_overview_summary

Reads ``data/interim/discovery.db`` (or ``--db``). Does not tag, collect, or
modify the database. The JSON is what the deployed Streamlit Overview reads
when the DB is absent.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.data import OVERVIEW_SUMMARY_NAME, build_overview_summary, default_paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite corpus (default: paths.interim_db from config.yaml)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"Output JSON (default: processed_dir/{OVERVIEW_SUMMARY_NAME})",
    )
    args = parser.parse_args()

    paths = default_paths()
    db_path = (args.db or paths.interim_db).resolve()
    out_path = (args.out or (paths.processed_dir / OVERVIEW_SUMMARY_NAME)).resolve()

    summary = build_overview_summary(db_path)
    try:
        summary["generated_from"] = db_path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        summary["generated_from"] = db_path.as_posix()
    summary["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {out_path.relative_to(PROJECT_ROOT)} "
        f"documents={summary['documents']} "
        f"analyzable={summary['analyzable']} "
        f"tagged={summary['tagged']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
