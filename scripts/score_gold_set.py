"""Score independent gold labels against the tagger's stored predictions.

    .venv\\Scripts\\python.exe -m scripts.score_gold_set

Reads ``tests/gold/gold_set.jsonl`` (your labels) and ``doc_tags`` for those
same ``doc_id``s. Writes ``outputs/tagger_validation.md``. Does not insert into
``documents`` or ``doc_tags`` and does not call the tagger.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.common.config import get_settings  # noqa: E402
from src.common.db import connect  # noqa: E402
from src.quantify.screen import quote_in_document  # noqa: E402
from src.tag.taxonomy import (  # noqa: E402
    IntentClass,
    MULTI_LABEL_DIMENSIONS,
    TAXONOMY_VERSION,
)

GOLD_PATH = PROJECT_ROOT / "tests" / "gold" / "gold_set.jsonl"
WORKSHEET_PATH = PROJECT_ROOT / "tests" / "gold" / "gold_worksheet.jsonl"
REPORT_PATH = PROJECT_ROOT / "outputs" / "tagger_validation.md"

BLOCKER_F1_GATE = 0.65
EVIDENCE_PRECISION_GATE = 0.80

MULTI_LABEL_FIELDS = tuple(name for name, _ in MULTI_LABEL_DIMENSIONS)


def read_jsonl(path: Path) -> tuple[dict | None, list[dict]]:
    meta = None
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if "_meta" in payload:
                meta = payload
                continue
            rows.append(payload)
    return meta, rows


def _as_label_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value} if value else set()
    return {str(item) for item in value if item}


def binary_prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return precision, recall, f1


def per_label_scores(
    gold_sets: list[set[str]], pred_sets: list[set[str]], labels: list[str]
) -> dict[str, dict[str, float | int]]:
    """One binary P/R/F1 per taxonomy label, then the caller macro-averages."""
    out: dict[str, dict[str, float | int]] = {}
    for label in labels:
        tp = fp = fn = 0
        for gold, pred in zip(gold_sets, pred_sets):
            in_gold = label in gold
            in_pred = label in pred
            if in_gold and in_pred:
                tp += 1
            elif in_pred and not in_gold:
                fp += 1
            elif in_gold and not in_pred:
                fn += 1
        precision, recall, f1 = binary_prf(tp, fp, fn)
        out[label] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "support": tp + fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    return out


def macro_f1(per_label: dict[str, dict[str, float | int]]) -> float | None:
    """Mean of per-label F1 over labels that appear in gold or predictions.

    Labels that nobody used in this sample are excluded so unused taxonomy
    values cannot drag a 40-document score toward zero.
    """
    active = [
        stats
        for stats in per_label.values()
        if stats["support"] or stats["fp"]
    ]
    if not active:
        return None
    return sum(float(stats["f1"]) for stats in active) / len(active)


def evidence_precision(predictions: list[dict], texts: dict[str, str]) -> dict[str, float | int]:
    """Share of the tagger's evidence quotes that are a substring of the document."""
    total = 0
    hits = 0
    for pred in predictions:
        text = texts.get(pred["doc_id"], "")
        for span in pred.get("evidence") or []:
            quote = span.get("quote") if isinstance(span, dict) else getattr(span, "quote", "")
            if not quote:
                continue
            total += 1
            if quote_in_document(str(quote), text):
                hits += 1
    return {
        "hits": hits,
        "total": total,
        "precision": (hits / total) if total else 0.0,
    }


def load_predictions(conn: sqlite3.Connection, doc_ids: list[str]) -> dict[str, dict]:
    """Latest ``TAXONOMY_VERSION`` tag row per doc_id. Read-only."""
    if not doc_ids:
        return {}
    placeholders = ",".join("?" for _ in doc_ids)
    rows = conn.execute(
        f"""
        SELECT doc_id, tags_json
        FROM doc_tags
        WHERE taxonomy_version = ? AND doc_id IN ({placeholders})
        """,
        (TAXONOMY_VERSION, *doc_ids),
    ).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        payload = json.loads(row["tags_json"])
        payload["doc_id"] = row["doc_id"]
        out[row["doc_id"]] = payload
    return out


def score(gold_rows: list[dict], predictions: dict[str, dict], texts: dict[str, str]) -> dict:
    unlabelled = [
        row["doc_id"]
        for row in gold_rows
        if not str(row.get("intent_class") or "").strip()
    ]
    missing_pred = [row["doc_id"] for row in gold_rows if row["doc_id"] not in predictions]
    if unlabelled:
        return {"error": "unlabelled", "doc_ids": unlabelled}
    if missing_pred:
        return {"error": "missing_predictions", "doc_ids": missing_pred}

    dimensions: dict[str, dict] = {}
    all_active_f1: list[float] = []
    for name, enum_cls in MULTI_LABEL_DIMENSIONS:
        labels = [member.value for member in enum_cls]
        gold_sets = [_as_label_set(row.get(name)) for row in gold_rows]
        pred_sets = [_as_label_set(predictions[row["doc_id"]].get(name)) for row in gold_rows]
        per_label = per_label_scores(gold_sets, pred_sets, labels)
        dimension_macro = macro_f1(per_label)
        dimensions[name] = {"per_label": per_label, "macro_f1": dimension_macro}
        if dimension_macro is not None:
            all_active_f1.extend(
                float(stats["f1"])
                for stats in per_label.values()
                if stats["support"] or stats["fp"]
            )

    intent_gold = [str(row.get("intent_class") or "") for row in gold_rows]
    intent_pred = [str(predictions[row["doc_id"]].get("intent_class") or "") for row in gold_rows]
    intent_correct = sum(g == p for g, p in zip(intent_gold, intent_pred))

    ev = evidence_precision(list(predictions[row["doc_id"]] for row in gold_rows), texts)
    blocker = dimensions["blocker_type"]["macro_f1"]
    overall = (sum(all_active_f1) / len(all_active_f1)) if all_active_f1 else None

    return {
        "n": len(gold_rows),
        "dimensions": dimensions,
        "overall_macro_f1": overall,
        "intent_accuracy": intent_correct / len(gold_rows) if gold_rows else 0.0,
        "intent_correct": intent_correct,
        "evidence": ev,
        "blocker_gate": BLOCKER_F1_GATE,
        "evidence_gate": EVIDENCE_PRECISION_GATE,
        "blocker_passes": blocker is not None and blocker >= BLOCKER_F1_GATE,
        "evidence_passes": ev["total"] > 0 and ev["precision"] >= EVIDENCE_PRECISION_GATE,
        "sources": dict(Counter(row.get("source", "?") for row in gold_rows)),
    }


def render_report(result: dict, *, seed: int | None, n_requested: int | None) -> str:
    def fmt(value: float | None) -> str:
        return "—" if value is None else f"{value:.3f}"

    blocker = result["dimensions"]["blocker_type"]["macro_f1"]
    ev = result["evidence"]
    seed_s = "unknown" if seed is None else str(seed)
    n_s = str(n_requested) if n_requested is not None else str(result["n"])

    lines = [
        "# Tagger validation",
        "",
        "Scored against an independently labelled gold set. The worksheet was "
        "drawn **blind** (no tagger tags in the file). Predictions come from "
        f"`doc_tags` (`taxonomy_version={TAXONOMY_VERSION}`), not from a new tagging run.",
        "",
        f"- Sample size: **{result['n']}** labelled documents"
        + (f" (drawn with `--n {n_s}`)" if n_requested is not None else ""),
        f"- Seed: **{seed_s}**",
        f"- Sources: {', '.join(f'`{s}` {c}' for s, c in sorted(result['sources'].items()))}",
        "",
        "## Gates (architecture §11 / plan §4)",
        "",
        "| Metric | Gate | Measured | Verdict |",
        "| --- | ---: | ---: | --- |",
        f"| Macro-F1 `blocker_type` | ≥ {BLOCKER_F1_GATE:.2f} | {fmt(blocker)} | "
        f"{'PASS' if result['blocker_passes'] else 'FAIL'} |",
        f"| Evidence precision (quote ⊂ document) | ≥ {EVIDENCE_PRECISION_GATE:.2f} | "
        f"{fmt(ev['precision'])} ({ev['hits']}/{ev['total']}) | "
        f"{'PASS' if result['evidence_passes'] else 'FAIL'} |",
        "",
        "## Macro-F1 by multi-label dimension",
        "",
        "Per-label precision/recall/F1, then macro-averaged over labels that "
        "appear in gold or in the tagger's predictions on this sample.",
        "",
        "| Dimension | Macro-F1 |",
        "| --- | ---: |",
    ]
    for name, _enum in MULTI_LABEL_DIMENSIONS:
        lines.append(f"| `{name}` | {fmt(result['dimensions'][name]['macro_f1'])} |")
    lines.append(f"| **All theme labels** | {fmt(result['overall_macro_f1'])} |")
    lines += [
        "",
        f"**`intent_class` accuracy** (not a Phase 4 gate): "
        f"{result['intent_correct']}/{result['n']} = {result['intent_accuracy']:.3f}.",
        "",
        "## Per-label detail",
        "",
    ]
    for name, _enum in MULTI_LABEL_DIMENSIONS:
        per_label = result["dimensions"][name]["per_label"]
        active = [
            (label, stats)
            for label, stats in per_label.items()
            if stats["support"] or stats["fp"]
        ]
        lines.append(f"### `{name}`")
        lines.append("")
        if not active:
            lines.append("No labels in this dimension on the gold sample or the tagger.")
            lines.append("")
            continue
        lines.append("| Label | P | R | F1 | support |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for label, stats in active:
            lines.append(
                f"| `{label}` | {stats['precision']:.3f} | {stats['recall']:.3f} | "
                f"{stats['f1']:.3f} | {stats['support']} |"
            )
        lines.append("")
    lines += [
        "## Notes",
        "",
        "- Evidence precision here is **verbatim-in-document**: the share of the "
        "tagger's stored quotes that appear in the source text (the same check "
        "`quote_in_document` uses). It is not span-overlap against your quotes.",
        "- Do not treat these numbers as corpus-wide quality. They are this sample, "
        f"seed {seed_s}, n={result['n']}.",
        "",
    ]
    return "\n".join(lines)


def _seed_from_meta(meta: dict | None, worksheet_meta: dict | None) -> tuple[int | None, int | None]:
    blob = meta or worksheet_meta or {}
    seed = blob.get("seed")
    n = blob.get("n")
    return (int(seed) if seed is not None else None, int(n) if n is not None else None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Score gold_set.jsonl against doc_tags.")
    parser.add_argument("--gold", type=Path, default=GOLD_PATH)
    args = parser.parse_args()

    gold_path = args.gold if args.gold.is_absolute() else PROJECT_ROOT / args.gold
    if not gold_path.is_file():
        print(
            f"\n  No gold set at {gold_path}.\n"
            "  Label tests/gold/gold_worksheet.jsonl blind, save as gold_set.jsonl,\n"
            "  then re-run this command. Nothing was written to tagger_validation.md.\n"
        )
        return 1

    meta, gold_rows = read_jsonl(gold_path)
    worksheet_meta = None
    if WORKSHEET_PATH.is_file():
        worksheet_meta, _ = read_jsonl(WORKSHEET_PATH)
    seed, n_requested = _seed_from_meta(meta, worksheet_meta)

    if not gold_rows:
        print("\n  gold_set.jsonl has no document rows.\n")
        return 1

    settings = get_settings()
    conn = connect(settings.interim_db)
    try:
        predictions = load_predictions(conn, [row["doc_id"] for row in gold_rows])
        texts = {
            row["doc_id"]: row["text"]
            for row in conn.execute(
                "SELECT doc_id, text FROM documents WHERE doc_id IN ({})".format(
                    ",".join("?" for _ in gold_rows)
                ),
                [row["doc_id"] for row in gold_rows],
            )
        }
    finally:
        conn.close()

    result = score(gold_rows, predictions, texts)
    if result.get("error") == "unlabelled":
        print(
            f"\n  {len(result['doc_ids'])} row(s) still have empty intent_class.\n"
            "  Fill every row before scoring — a partial gold set is not the measurement.\n"
        )
        return 1
    if result.get("error") == "missing_predictions":
        print(
            f"\n  {len(result['doc_ids'])} gold doc_id(s) have no doc_tags row.\n"
            "  The scorer does not tag; those ids are not in the tagged sample.\n"
        )
        return 1

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report = render_report(result, seed=seed, n_requested=n_requested)
    REPORT_PATH.write_text(report, encoding="utf-8")
    _print_score(result, seed)
    print(f"  Report: {REPORT_PATH}\n")
    return 0 if result["blocker_passes"] and result["evidence_passes"] else 1


def _print_score(result: dict, seed: int | None) -> None:
    def fmt(value: float | None) -> str:
        return "   —" if value is None else f"{value:6.3f}"

    print("\n" + "=" * 66)
    print(" TAGGER VALIDATION  (gold set vs doc_tags)")
    print("=" * 66)
    print(f"  n={result['n']}  seed={seed if seed is not None else 'unknown'}")
    print()
    print(f"  {'DIMENSION':<28} {'MACRO-F1':>8}")
    print(f"  {'-' * 28} {'-' * 8}")
    for name, _enum in MULTI_LABEL_DIMENSIONS:
        print(f"  {name:<28} {fmt(result['dimensions'][name]['macro_f1'])}")
    print(f"  {'all theme labels':<28} {fmt(result['overall_macro_f1'])}")
    ev = result["evidence"]
    print()
    print(
        f"  blocker_type gate ≥ {BLOCKER_F1_GATE:.2f}:  "
        f"{fmt(result['dimensions']['blocker_type']['macro_f1']).strip()}  "
        f"{'PASS' if result['blocker_passes'] else 'FAIL'}"
    )
    print(
        f"  evidence precision ≥ {EVIDENCE_PRECISION_GATE:.2f}:  "
        f"{ev['precision']:.3f} ({ev['hits']}/{ev['total']})  "
        f"{'PASS' if result['evidence_passes'] else 'FAIL'}"
    )
    print(
        f"  intent_class accuracy:  {result['intent_accuracy']:.3f} "
        f"({result['intent_correct']}/{result['n']})"
    )
    print("=" * 66)


if __name__ == "__main__":
    raise SystemExit(main())
