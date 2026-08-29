"""Phase 7 live-corpus checks. Does not wipe data, retag, or re-collect.

    .venv\\Scripts\\python.exe scripts\\verify_hardening.py

Exit 0 means: tagging cache is warm (a second run would cost zero tokens) and
two in-memory quantify passes produce identical opportunity_scores.csv.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.common.config import get_settings  # noqa: E402
from src.common.db import connect  # noqa: E402
from src.common.encoding import harden_stdio  # noqa: E402
from src.quantify.metrics import knobs_from_settings, load_analyzable, quantify  # noqa: E402
from src.quantify.run_quantify import SCORES_NAME, write_scores  # noqa: E402
from src.tag import run_tagging  # noqa: E402


def _require_db(settings) -> None:
    path = Path(settings.interim_db)
    if not path.is_file():
        raise SystemExit(
            f"no corpus at {path}; this script checks an existing tagged DB, "
            "it does not rebuild one. See README.md."
        )


def check_cache_gate(settings) -> dict:
    summary = run_tagging.dry_run(settings)
    if summary["to_tag"] != 0:
        raise SystemExit(
            f"cache gate failed: dry-run would tag {summary['to_tag']} documents "
            f"({summary['already_cached']} already cached). A second tagging run "
            "is not free until every sample member is in llm_cache."
        )
    return summary


def check_reproducibility(settings, tmp: Path) -> None:
    conn = connect(settings.interim_db)
    try:
        docs = load_analyzable(conn)
    finally:
        conn.close()
    if not docs:
        raise SystemExit("no tagged analyzable documents; quantify has nothing to freeze")
    knobs = knobs_from_settings(settings)
    first = quantify(docs, knobs=knobs)
    second = quantify(docs, knobs=knobs)
    a = tmp / "a" / SCORES_NAME
    b = tmp / "b" / SCORES_NAME
    write_scores(a, first.opportunities, first.sources)
    write_scores(b, second.opportunities, second.sources)
    if a.read_bytes() != b.read_bytes():
        raise SystemExit("reproducibility gate failed: two quantify passes differ")


def main() -> None:
    harden_stdio()
    settings = get_settings()
    _require_db(settings)
    print("Phase 7 hardening (read-only against data/interim; nothing is deleted)\n")
    cache_summary = check_cache_gate(settings)
    print(
        f"  cache gate                 PASS  "
        f"{cache_summary['already_cached']} cached, {cache_summary['to_tag']} to tag"
    )
    scratch = Path(settings.logs_dir) / "_hardening_scratch"
    try:
        check_reproducibility(settings, scratch)
        print("  reproducibility gate       PASS  identical opportunity_scores.csv")
    finally:
        for path in scratch.rglob("*"):
            if path.is_file():
                path.unlink()
        for path in sorted(scratch.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
        if scratch.exists():
            scratch.rmdir()
    print("\n  pytest tests/              run separately: .venv\\Scripts\\python.exe -m pytest tests\\ -q")
    print()


if __name__ == "__main__":
    main()
