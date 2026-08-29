"""Pipeline spend and wall-clock, read from ``run_log`` and ``llm_cache``.

Phase 7's appendix obligation: token spend and elapsed time per stage are
recorded as the stages run, then *surfaced* here rather than typed into the
report. This module only reads. AJIO aggregate figures do not belong in it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.tag import cache as llm_cache

#: Keep in lockstep with ``src.tag.run_tagging`` (paid-tier gpt-oss-120b).
PRICE_IN_PER_M = 0.15
PRICE_OUT_PER_M = 0.60
INPUT_FRACTION = 0.72

HEADING = "# Pipeline run log"


def _parse_iso(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    text = stamp.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _elapsed_seconds(started: str | None, finished: str | None) -> float | None:
    start = _parse_iso(started)
    end = _parse_iso(finished)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def _fmt_seconds(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 1:
        return f"{value * 1000:.0f} ms"
    if value < 60:
        return f"{value:.1f} s"
    minutes, seconds = divmod(int(round(value)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min {seconds} s"


def _paid_usd(prompt: int, completion: int, reasoning: int) -> float:
    """Same paid-tier arithmetic ``run_tagging --dry-run`` uses."""
    output = completion + reasoning
    return (prompt / 1_000_000 * PRICE_IN_PER_M) + (output / 1_000_000 * PRICE_OUT_PER_M)


def load_stage_rows(conn: sqlite3.Connection) -> list[dict]:
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "run_log" not in tables:
        return []
    rows = conn.execute(
        """
        SELECT run_id, stage, config_hash, started_at, finished_at,
               records_in, records_out, notes
        FROM run_log
        ORDER BY started_at, stage
        """
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        mapping = dict(row)
        mapping["elapsed_s"] = _elapsed_seconds(
            mapping.get("started_at"), mapping.get("finished_at")
        )
        out.append(mapping)
    return out


def render_pipeline_appendix(conn: sqlite3.Connection) -> str:
    """Markdown for token spend and wall-clock, appended to the evidence file."""
    rows = load_stage_rows(conn)
    totals = llm_cache.token_totals(conn)
    prompt = int(totals.get("prompt_tokens") or 0)
    completion = int(totals.get("completion_tokens") or 0)
    reasoning = int(totals.get("reasoning_tokens") or 0)
    cached = int(totals.get("cached_documents") or 0)
    billed = prompt + completion + reasoning
    cost = _paid_usd(prompt, completion, reasoning)

    lines = [
        HEADING,
        "",
        "Wall-clock is ``finished_at - started_at`` on each ``run_log`` row. "
        "Token counts are the sums stored on ``llm_cache`` when a tagging batch "
        "landed — a second tagging run over an unchanged corpus adds nothing, "
        "which is the cache gate. Cost uses the paid-tier rates from "
        f"`run_tagging` (${PRICE_IN_PER_M:.2f}/${PRICE_OUT_PER_M:.2f} per 1M). "
        "Stored prompt/completion/reasoning columns are the ground truth "
        f"(dry-run still amortizes with input fraction {INPUT_FRACTION:.0%}).",
        "",
        f"**Cached tagging spend:** {billed:,} tokens "
        f"({prompt:,} prompt, {completion:,} completion, {reasoning:,} reasoning) "
        f"across {cached:,} cached document(s). "
        f"Implied paid-tier tagging cost: **${cost:.4f}**.",
        "",
    ]
    if not rows:
        lines.append("No `run_log` rows are on disk yet.")
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "| stage | started (UTC) | elapsed | in | out |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    for row in rows:
        started = (row.get("started_at") or "")[:19].replace("T", " ")
        lines.append(
            f"| `{row.get('stage')}` | {started} | {_fmt_seconds(row.get('elapsed_s'))} | "
            f"{row.get('records_in') if row.get('records_in') is not None else '—'} | "
            f"{row.get('records_out') if row.get('records_out') is not None else '—'} |"
        )
    lines.append("")
    lines.append(
        "`data/aggregates/` is exempt from the byte-identical reproducibility gate: "
        "it is method-reproducible, not command-reproducible (see Limitations)."
    )
    return "\n".join(lines) + "\n"


def render_tagger_validation() -> str:
    """Placeholder until a gold set is scored; never invents F1/precision.

    If ``tests/gold/gold_set.jsonl`` exists and ``outputs/tagger_validation.md``
    already holds a measured report, reuse it so a later ``run_synthesis`` cannot
    overwrite real numbers with this stand-in.
    """
    from src.common.config import PROJECT_ROOT

    gold = PROJECT_ROOT / "tests" / "gold" / "gold_set.jsonl"
    measured = PROJECT_ROOT / "outputs" / "tagger_validation.md"
    if gold.is_file() and measured.is_file():
        text = measured.read_text(encoding="utf-8")
        if "No gold set is in this repository" not in text:
            return text
    if gold.is_file():
        return (
            "# Tagger validation\n"
            "\n"
            "`tests/gold/gold_set.jsonl` is on disk but has not been scored (or "
            "the measured page is still the placeholder). Run "
            "`.venv\\Scripts\\python.exe -m scripts.score_gold_set` — do not invent "
            "an F1 or a precision here.\n"
        )
    return (
        "# Tagger validation\n"
        "\n"
        "Macro-F1 on `blocker_type` (≥ 0.65) and evidence precision (≥ 0.80) are "
        "gated against a labelled gold set (`tests/gold/gold_set.jsonl` in the "
        "architecture). **No gold set is in this repository**, so neither figure "
        "has been measured. This file is written so that absence is explicit "
        "rather than looking like the gate was skipped.\n"
        "\n"
        "Do not invent an F1 or a precision. Label `tests/gold/gold_worksheet.jsonl` "
        "blind, save as `gold_set.jsonl`, then run `scripts.score_gold_set`.\n"
    )
