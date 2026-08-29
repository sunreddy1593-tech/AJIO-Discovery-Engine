"""Load frozen pipeline outputs for the Streamlit explorer.

Paths come from ``config.yaml`` via :func:`src.common.config.load_run_config`.
Nothing here calls :func:`src.common.config.get_settings`, so the explorer
starts without Groq, YouTube, or ``HASH_SALT``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.common.config import PROJECT_ROOT, load_run_config
from src.synthesize.ajio_aggregates import (
    AGGREGATE_PROVENANCE,
    Theme,
    cross_reference,
)
from src.synthesize.evidence import redact_pii, select_quotes, truncate
from src.store.aggregates import load_ajio_aggregates, summarize

SCORES_NAME = "opportunity_scores.csv"
SEGMENTS_NAME = "segment_matrix.csv"
APPENDIX_NAME = "evidence_appendix.md"

BOOL_COLUMNS = (
    "low_confidence",
    "reportable",
    "source_specific",
    "high_prevalence",
    "post_purchase_only",
)
_TRUE = {"true", "1", "yes"}

_QUOTE_LINE = re.compile(
    r'^- `([^`]+)` `([^`]+)`(?: \(\[source\]\(([^)]+)\)\))?: "(.*)"\s*$'
)


@dataclass(frozen=True)
class ExplorerPaths:
    """Resolved locations. Missing files are allowed; loaders say so."""

    root: Path
    processed_dir: Path
    outputs_dir: Path
    interim_db: Path
    aggregates_dir: Path

    def first_existing(self, name: str) -> Path | None:
        for folder in (self.processed_dir, self.outputs_dir):
            candidate = folder / name
            if candidate.is_file():
                return candidate
        return None


@dataclass(frozen=True)
class AppendixQuote:
    theme: str
    source: str
    doc_id: str
    url: str | None
    text: str


def default_paths() -> ExplorerPaths:
    """Project-root paths from ``config.yaml``. Does not load credentials."""
    run, _raw = load_run_config()
    return ExplorerPaths(
        root=PROJECT_ROOT,
        processed_dir=(PROJECT_ROOT / run.paths.processed_dir).resolve(),
        outputs_dir=(PROJECT_ROOT / run.paths.outputs_dir).resolve(),
        interim_db=(PROJECT_ROOT / run.paths.interim_db).resolve(),
        aggregates_dir=(PROJECT_ROOT / run.paths.aggregates_dir).resolve(),
    )


def load_opportunity_scores(path: Path) -> pd.DataFrame:
    """Ranked themes. Empty (header-only) file → empty frame, not an error."""
    if not path.is_file():
        raise FileNotFoundError(path)
    frame = pd.read_csv(path, encoding="utf-8-sig")
    frame = _normalise_scores(frame)
    return frame


def load_segment_matrix(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(path)
    frame = pd.read_csv(path, encoding="utf-8-sig")
    for column in ("n_docs", "lift"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def parse_evidence_appendix(text: str) -> list[AppendixQuote]:
    """Group the frozen appendix by ``## theme``. Stop at the pipeline run log."""
    quotes: list[AppendixQuote] = []
    theme = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("# Pipeline run log"):
            break
        if line.startswith("## "):
            theme = line[3:].strip()
            continue
        if not theme or not line.startswith("- "):
            continue
        match = _QUOTE_LINE.match(line)
        if match:
            quotes.append(
                AppendixQuote(
                    theme=theme,
                    source=match.group(1),
                    doc_id=match.group(2),
                    url=match.group(3),
                    text=match.group(4),
                )
            )
            continue
        quotes.append(
            AppendixQuote(theme=theme, source="", doc_id="", url=None, text=line[2:].strip())
        )
    return quotes


def load_evidence_appendix(path: Path) -> list[AppendixQuote]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return parse_evidence_appendix(path.read_text(encoding="utf-8"))


def quotes_by_theme(quotes: list[AppendixQuote]) -> dict[str, list[AppendixQuote]]:
    grouped: dict[str, list[AppendixQuote]] = {}
    for quote in quotes:
        grouped.setdefault(quote.theme, []).append(quote)
    return grouped


def open_corpus_readonly(db_path: Path) -> sqlite3.Connection | None:
    """SELECT-only connection. Does not create the database if it is missing."""
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def load_tagged_documents(db_path: Path) -> pd.DataFrame:
    """One row per tagged relevant document. Body is PII-redacted and truncated."""
    conn = open_corpus_readonly(db_path)
    if conn is None:
        return pd.DataFrame()
    try:
        rows = conn.execute(
            """
            SELECT d.doc_id, d.source, d.url, d.created_utc, d.word_count, d.text,
                   t.tags_json
            FROM documents d
            JOIN doc_tags t ON t.doc_id = d.doc_id
            WHERE d.is_relevant = 1 AND d.is_duplicate_of IS NULL
            ORDER BY d.source, d.doc_id
            """
        ).fetchall()
    finally:
        conn.close()

    records: list[dict] = []
    for row in rows:
        payload = _payload(row["tags_json"])
        preview = truncate(redact_pii(row["text"] or ""), limit=280)
        records.append(
            {
                "doc_id": row["doc_id"],
                "source": row["source"],
                "url": row["url"],
                "created_utc": row["created_utc"],
                "word_count": row["word_count"],
                "intent_class": payload.get("intent_class") or "",
                "blocker_type": _join(payload.get("blocker_type")),
                "uncertainty_type": _join(payload.get("uncertainty_type")),
                "wishlist_motivation": _join(payload.get("wishlist_motivation")),
                "info_sought_elsewhere": _join(payload.get("info_sought_elsewhere")),
                "segment_cue": _join(payload.get("segment_cue")),
                "outcome_mentioned": payload.get("outcome_mentioned") or "",
                "severity": payload.get("severity"),
                "confidence_pct": payload.get("confidence_pct"),
                "preview": preview,
            }
        )
    return pd.DataFrame.from_records(records)


def corpus_facts(db_path: Path) -> dict:
    """Small counts for the overview strip. Zeroes when the DB is absent."""
    conn = open_corpus_readonly(db_path)
    if conn is None:
        return {
            "documents": 0,
            "analyzable": 0,
            "tagged": 0,
            "genuine_intent": 0,
            "available": False,
        }
    try:
        documents = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        analyzable = conn.execute(
            """
            SELECT COUNT(*) FROM documents
            WHERE is_relevant = 1 AND is_duplicate_of IS NULL
            """
        ).fetchone()[0]
        tagged = conn.execute(
            """
            SELECT COUNT(*) FROM documents d
            JOIN doc_tags t ON t.doc_id = d.doc_id
            WHERE d.is_relevant = 1 AND d.is_duplicate_of IS NULL
            """
        ).fetchone()[0]
        genuine = 0
        for (blob,) in conn.execute(
            """
            SELECT t.tags_json FROM documents d
            JOIN doc_tags t ON t.doc_id = d.doc_id
            WHERE d.is_relevant = 1 AND d.is_duplicate_of IS NULL
            """
        ):
            payload = _payload(blob)
            if payload.get("intent_class") == "genuine_intent":
                genuine += 1
    finally:
        conn.close()
    return {
        "documents": int(documents),
        "analyzable": int(analyzable),
        "tagged": int(tagged),
        "genuine_intent": int(genuine),
        "available": True,
    }


def load_document_body(db_path: Path, doc_id: str) -> str:
    """Redacted body for the document drawer. Empty if the id is unknown."""
    conn = open_corpus_readonly(db_path)
    if conn is None:
        return ""
    try:
        row = conn.execute(
            "SELECT text FROM documents WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return ""
    return truncate(redact_pii(row["text"] or ""), limit=1200)


def live_quotes_for_theme(
    db_path: Path,
    *,
    label: str,
    dimension: str | None,
    cluster: str | None,
    supporting_doc_ids: str | None,
    limit: int = 4,
) -> list[dict]:
    """Unflagged quotes from the tagged corpus (same picker the report used)."""
    conn = open_corpus_readonly(db_path)
    if conn is None:
        return []
    ids = [part for part in (supporting_doc_ids or "").split(";") if part.strip()]
    try:
        quotes = select_quotes(
            conn,
            label,
            cluster=cluster or None,
            dimension=dimension or None,
            supporting_ids=ids or None,
            limit=limit,
        )
    finally:
        conn.close()
    return [
        {
            "doc_id": quote.doc_id,
            "source": quote.source,
            "url": quote.url,
            "text": quote.text,
            "severity": quote.severity,
        }
        for quote in quotes
    ]


def load_ajio_side_channel(aggregates_dir: Path) -> dict:
    """AJIO on-site aggregates. Never mixed into corpus prevalence."""
    ajio_dir = aggregates_dir / "ajio"
    aggregates = load_ajio_aggregates(ajio_dir) if ajio_dir.is_dir() else []
    summary = summarize(aggregates)
    return {
        "provenance": AGGREGATE_PROVENANCE,
        "products": summary.products,
        "products_with_fit": summary.products_with_fit,
        "products_with_quality": summary.products_with_quality,
        "mean_misfit_pct": summary.mean_misfit_pct,
        "mean_bad_quality_pct": summary.mean_bad_quality_pct,
        "mean_average_rating": summary.mean_average_rating,
        "ratings_reported": summary.ratings_reported,
        "ratings_derived": summary.ratings_derived,
        "top_fit_is_loose": summary.top_fit_is_loose,
        "top_fit_is_tight": summary.top_fit_is_tight,
        "aggregates": aggregates,
        "summary": summary,
    }


def corroboration_rows(scores: pd.DataFrame, aggregates) -> pd.DataFrame:
    rows = []
    for record in scores.itertuples(index=False):
        theme = Theme(
            name=str(record.label),
            documents=int(record.n_docs) if pd.notna(record.n_docs) else 0,
            prevalence=float(record.prevalence) if pd.notna(record.prevalence) else None,
        )
        reference = cross_reference(theme, aggregates)
        rows.append(
            {
                "label": theme.name,
                "kind": reference.kind,
                "corroborates": reference.corroborates,
                "detail": reference.detail,
            }
        )
    return pd.DataFrame(rows)


def source_prevalence_columns(frame: pd.DataFrame) -> list[str]:
    return [column for column in frame.columns if column.startswith("prevalence_")]


def source_names(frame: pd.DataFrame) -> list[str]:
    names = []
    for column in source_prevalence_columns(frame):
        name = column.removeprefix("prevalence_")
        if name and name not in {"lo", "hi", "norm", "genuine", "lo_genuine", "hi_genuine"}:
            names.append(name)
    return names


def pretty_label(label: str) -> str:
    """``return_friction`` → ``Return Friction`` for display. Does not change CSV keys."""
    return str(label or "").replace("_", " ").strip().title()


def source_display_name(source: str) -> str:
    names = {
        "youtube": "YouTube",
        "play_store": "Play Store",
        "app_store": "App Store",
        "consumer_complaints_in": "ConsumerComplaints.in",
        "quora_manual": "Quora",
        "complaints_board": "ComplaintsBoard",
        "reddit": "Reddit",
    }
    return names.get(source, pretty_label(source))


def top_source_for_row(mapping: dict) -> str | None:
    """Source with the highest per-source prevalence on this theme, if any."""
    best_name = None
    best_val = 0.0
    for key, value in mapping.items():
        if not str(key).startswith("prevalence_"):
            continue
        name = str(key).removeprefix("prevalence_")
        if name in {"lo", "hi", "norm", "genuine", "lo_genuine", "hi_genuine"}:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if pd.isna(number):
            continue
        if number > best_val:
            best_val = number
            best_name = name
    return best_name


def filter_scores(
    frame: pd.DataFrame,
    *,
    dimensions: list[str] | None = None,
    sources: list[str] | None = None,
    reportable_only: bool = False,
    hide_post_purchase_only: bool = False,
) -> pd.DataFrame:
    out = frame
    if dimensions:
        out = out[out["dimension"].isin(dimensions)]
    if reportable_only and "reportable" in out.columns:
        out = out[out["reportable"] == True]  # noqa: E712 — pandas boolean column
    if hide_post_purchase_only and "post_purchase_only" in out.columns:
        out = out[out["post_purchase_only"] != True]  # noqa: E712
    if sources:
        keep = pd.Series(False, index=out.index)
        for source in sources:
            column = f"prevalence_{source}"
            if column in out.columns:
                keep = keep | (pd.to_numeric(out[column], errors="coerce").fillna(0) > 0)
        out = out[keep]
    return out.reset_index(drop=True)


def rank_view(frame: pd.DataFrame, *, genuine: bool) -> pd.DataFrame:
    """Sort for the selected population. Ranking column is 1-based."""
    score_col = "opportunity_score_genuine" if genuine else "opportunity_score"
    docs_col = "n_docs_genuine" if genuine else "n_docs"
    prev_col = "prevalence_genuine" if genuine else "prevalence"
    if frame.empty:
        return frame.copy()
    if "rank" in frame.columns:
        frame = frame.drop(columns=["rank"])
    out = frame.copy()
    out["_score"] = pd.to_numeric(_column_or_default(out, score_col, None), errors="coerce")
    out["_post"] = _column_or_default(out, "post_purchase_only", False).fillna(False).astype(bool)
    out["_reportable"] = _column_or_default(out, "reportable", True).fillna(True).astype(bool)
    # Match Stage 4: reportable first, then pre-purchase-supported, then score.
    out = out.sort_values(
        by=["_reportable", "_post", "_score"],
        ascending=[False, True, False],
        kind="mergesort",
    ).reset_index(drop=True)
    out.insert(0, "rank", range(1, len(out) + 1))
    out["view_score"] = out["_score"]
    out["view_n_docs"] = pd.to_numeric(_column_or_default(out, docs_col, None), errors="coerce")
    out["view_prevalence"] = pd.to_numeric(_column_or_default(out, prev_col, None), errors="coerce")
    return out.drop(columns=["_score", "_post", "_reportable"])


def rank_movement(frame: pd.DataFrame) -> pd.DataFrame:
    """Full-corpus rank vs genuine-intent rank (positive delta = rose on genuine)."""
    if frame.empty or "opportunity_score_genuine" not in frame.columns:
        return pd.DataFrame()
    full = rank_view(frame, genuine=False)[["label", "rank", "opportunity_score"]].rename(
        columns={"rank": "full_rank", "opportunity_score": "full_score"}
    )
    genuine = rank_view(frame, genuine=True)[
        ["label", "rank", "opportunity_score_genuine"]
    ].rename(columns={"rank": "genuine_rank", "opportunity_score_genuine": "genuine_score"})
    merged = full.merge(genuine, on="label")
    merged["delta"] = merged["full_rank"] - merged["genuine_rank"]
    merged["movement"] = merged["delta"].map(_movement_label)
    return merged.sort_values("delta", ascending=False).reset_index(drop=True)


def compact_score_rows(frame: pd.DataFrame) -> list[dict]:
    """Theme table for the Ask payload — never includes ``supporting_doc_ids``."""
    drop = {"supporting_doc_ids"}
    keep = [
        column
        for column in frame.columns
        if column not in drop and not column.startswith("_")
    ]
    rows = []
    for record in frame[keep].to_dict(orient="records"):
        clean = {}
        for key, value in record.items():
            if pd.isna(value):
                clean[key] = None
            elif hasattr(value, "item"):
                clean[key] = value.item()
            else:
                clean[key] = value
        rows.append(clean)
    return rows


def _column_or_default(frame: pd.DataFrame, column: str, default) -> pd.Series:
    if column in frame.columns:
        return frame[column]
    return pd.Series(default, index=frame.index)


def _movement_label(delta: int) -> str:
    if delta > 0:
        return f"up {int(delta)}"
    if delta < 0:
        return f"down {int(-delta)}"
    return "holds"


def _normalise_scores(frame: pd.DataFrame) -> pd.DataFrame:
    rename = {}
    folded = {str(column).strip().casefold(): column for column in frame.columns}
    aliases = {
        "theme": "label",
        "score": "opportunity_score",
        "documents": "n_docs",
        "genuine_score": "opportunity_score_genuine",
        "genuine_documents": "n_docs_genuine",
    }
    for alias, canonical in aliases.items():
        if canonical not in frame.columns and alias in folded:
            rename[folded[alias]] = canonical
    if rename:
        frame = frame.rename(columns=rename)
    for column in BOOL_COLUMNS:
        if column in frame.columns:
            frame[column] = (
                frame[column]
                .astype(str)
                .str.strip()
                .str.casefold()
                .isin(_TRUE)
            )
    numeric = [
        column
        for column in frame.columns
        if column not in BOOL_COLUMNS
        and column
        not in {
            "dimension",
            "label",
            "co_occurs_with",
            "cluster",
            "supporting_doc_ids",
        }
    ]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def _payload(tags_json: str) -> dict:
    try:
        payload = json.loads(tags_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _join(values) -> str:
    if not values:
        return ""
    if isinstance(values, str):
        return values
    return "; ".join(str(item) for item in values if item)
