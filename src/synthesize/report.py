"""Assemble the seven-section discovery report (architecture.md §9).

This module only *reads*: the corpus, Stage 4 CSVs, AJIO aggregates, and
``run_log``. It never writes ``documents`` or ``doc_tags``, never retags, and
never treats an aggregate row as a document. Section strings are stitched by
the Jinja2 template so the order in ``architecture.md`` cannot drift from the
file a reader opens.
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from src.common.schemas import PurchaseStage, purchase_stage
from src.quantify.scoring import WEIGHTING_NOTE
from src.store.aggregates import AjioAggregate
from src.synthesize.ajio_aggregates import Theme
from src.synthesize.ajio_aggregates import render_section as render_aggregates
from src.synthesize.evidence import Quote, format_quote, select_quotes
from src.synthesize.limitations import render_section as render_limitations

SCORES_NAME = "opportunity_scores.csv"
PREVALENCE_NAME = "tag_prevalence.csv"
SEGMENT_NAME = "segment_matrix.csv"
APPENDIX_NAME = "evidence_appendix.md"

THEME_COLUMNS = (
    "theme",
    "name",
    "label",
    "opportunity",
    "cluster",
    "tag",
    "blocker_type",
    "area",
)
SCORE_COLUMNS = ("score", "opportunity_score", "total", "total_score")
GENUINE_SCORE_COLUMNS = (
    "opportunity_score_genuine",
    "genuine_score",
    "score_genuine",
)
PREVALENCE_COLUMNS = ("prevalence",)
DOC_COLUMNS = ("documents", "n", "n_docs", "doc_count", "supporting_docs")
GENUINE_DOC_COLUMNS = ("n_docs_genuine", "genuine_documents", "genuine_n")
COOCCURRENCE_COLUMNS = (
    "cooccurrence",
    "co_occurrence",
    "co_occurs_with",
    "cooccurrence_lift",
    "lift",
    "defining_cooccurrence",
)

PRICE_LABELS = (
    "price_absolute",
    "price_expectation",
    "price_watch",
    "budget_timing",
)
MATERIAL_LIFT = 2.0
EVIDENCE_PER_THEME = 4
MAX_RANK_MOVERS = 6
RANK_HEAD = 10

DISCOVERY_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("Q1", "Why do users add fashion products to their wishlist?"),
    ("Q2", "What prevents wishlisted products from eventually being purchased?"),
    (
        "Q3",
        "What uncertainties remain after users have identified a product they like?",
    ),
    ("Q4", "What causes users to postpone a purchase?"),
    ("Q5", "How do users compare multiple shortlisted products?"),
    (
        "Q6",
        "What information do users seek outside Myntra/AJIO before purchasing?",
    ),
    (
        "Q7",
        "What role do fit, size, styling, price, reviews, occasion, and social validation play?",
    ),
    (
        "Q8",
        "When do users use the wishlist as genuine purchase intent versus simply as a bookmarking mechanism?",
    ),
    ("Q9", "How do these behaviors differ across user segments?"),
    ("Q10", "What unmet needs emerge consistently across user conversations?"),
)


class StaleScoresError(RuntimeError):
    """Quantify output is older than the newest ``doc_tags.tagged_at`` (edge-case §6.8)."""


@dataclass(frozen=True)
class Opportunity:
    """One ranked area as Stage 4 wrote it — optional fields stay None on older CSVs."""

    name: str
    score: float | None = None
    prevalence: float | None = None
    documents: int | None = None
    cooccurrence: str | None = None
    genuine_score: float | None = None
    genuine_documents: int | None = None
    dimension: str | None = None
    prevalence_lo: float | None = None
    prevalence_hi: float | None = None
    prevalence_norm: float | None = None
    severity_norm: float | None = None
    evidence_confidence: float | None = None
    mean_severity: float | None = None
    mean_actionability: float | None = None
    mean_confidence: float | None = None
    flagged_evidence_share: float | None = None
    low_confidence: bool | None = None
    reportable: bool | None = None
    post_purchase_only: bool | None = None
    cluster: str | None = None
    supporting_doc_ids: str | None = None
    n_authors: int | None = None
    n_pre_purchase: int | None = None
    n_post_purchase: int | None = None
    n_mixed: int | None = None
    source_spread: float | None = None


@dataclass
class TagIndex:
    """Counts and citations from tagged analyzable documents, for the ten questions."""

    n_tagged: int = 0
    by_label: dict[str, list[tuple[str, str]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    by_intent: Counter[str] = field(default_factory=Counter)
    docs: list[tuple[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------


def load_opportunity_scores(processed_dir: str | Path) -> list[Opportunity] | None:
    """Ranked areas from Stage 4, or ``None`` when that stage has not run.

    Missing file → pending (caller must not invent themes). Header-only file →
    empty list (edge-case §6.1: quantify ran, nothing cleared the bar).
    """
    path = Path(processed_dir) / SCORES_NAME
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return None
        fields = {name.strip().casefold(): name for name in reader.fieldnames if name}
        theme_key = _first_field(fields, THEME_COLUMNS)
        if theme_key is None:
            return None
        rows: list[Opportunity] = []
        for raw in reader:
            name = (raw.get(theme_key) or "").strip()
            if not name:
                continue
            rows.append(
                Opportunity(
                    name=name,
                    score=_as_float(_field(raw, fields, SCORE_COLUMNS)),
                    prevalence=_as_prevalence(_field(raw, fields, PREVALENCE_COLUMNS)),
                    documents=_as_int(_field(raw, fields, DOC_COLUMNS)),
                    cooccurrence=_as_str(_field(raw, fields, COOCCURRENCE_COLUMNS)),
                    genuine_score=_as_float(_field(raw, fields, GENUINE_SCORE_COLUMNS)),
                    genuine_documents=_as_int(_field(raw, fields, GENUINE_DOC_COLUMNS)),
                    dimension=_as_str(_field(raw, fields, ("dimension",))),
                    prevalence_lo=_as_prevalence(_field(raw, fields, ("prevalence_lo",))),
                    prevalence_hi=_as_prevalence(_field(raw, fields, ("prevalence_hi",))),
                    prevalence_norm=_as_float(_field(raw, fields, ("prevalence_norm",))),
                    severity_norm=_as_float(_field(raw, fields, ("severity_norm",))),
                    evidence_confidence=_as_float(
                        _field(raw, fields, ("evidence_confidence",))
                    ),
                    mean_severity=_as_float(_field(raw, fields, ("mean_severity",))),
                    mean_actionability=_as_float(
                        _field(raw, fields, ("mean_actionability",))
                    ),
                    mean_confidence=_as_float(_field(raw, fields, ("mean_confidence",))),
                    flagged_evidence_share=_as_float(
                        _field(raw, fields, ("flagged_evidence_share",))
                    ),
                    low_confidence=_as_bool(_field(raw, fields, ("low_confidence",))),
                    reportable=_as_bool(_field(raw, fields, ("reportable",))),
                    post_purchase_only=_as_bool(
                        _field(raw, fields, ("post_purchase_only",))
                    ),
                    cluster=_as_str(_field(raw, fields, ("cluster",))),
                    supporting_doc_ids=_as_str(
                        _field(raw, fields, ("supporting_doc_ids",))
                    ),
                    n_authors=_as_int(_field(raw, fields, ("n_authors",))),
                    n_pre_purchase=_as_int(_field(raw, fields, ("n_pre_purchase",))),
                    n_post_purchase=_as_int(_field(raw, fields, ("n_post_purchase",))),
                    n_mixed=_as_int(_field(raw, fields, ("n_mixed",))),
                    source_spread=_as_float(_field(raw, fields, ("source_spread",))),
                )
            )
    rows.sort(key=lambda row: (row.score is None, -(row.score or 0.0)))
    return rows


def load_csv_rows(processed_dir: str | Path, name: str) -> list[dict[str, str]]:
    path = Path(processed_dir) / name
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return []
        return [dict(row) for row in reader]


def _first_field(fields: dict[str, str], candidates: Sequence[str]) -> str | None:
    for candidate in candidates:
        if candidate in fields:
            return fields[candidate]
    return None


def _field(row: dict[str, str], fields: dict[str, str], candidates: Sequence[str]) -> str | None:
    key = _first_field(fields, candidates)
    if key is None:
        return None
    value = row.get(key)
    return value if value not in (None, "") else None


def _as_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _as_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _as_int(value: str | None) -> int | None:
    number = _as_float(value)
    if number is None:
        return None
    return int(number)


def _as_prevalence(value: str | None) -> float | None:
    """Stage 4 may write a 0–1 share or a 0–100 percent; Theme renders ``.1%``."""
    number = _as_float(value)
    if number is None:
        return None
    if number > 1.0:
        return number / 100.0
    return number


def _as_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().casefold()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    return None


# --------------------------------------------------------------------------
# Staleness (edge-case.md §6.8)
# --------------------------------------------------------------------------


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


def scores_mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def latest_tagged_at(conn: sqlite3.Connection) -> datetime | None:
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "doc_tags" not in tables:
        return None
    raw = conn.execute("SELECT MAX(tagged_at) FROM doc_tags").fetchone()[0]
    return _parse_iso(raw)


def assert_scores_fresh(conn: sqlite3.Connection, processed_dir: str | Path) -> None:
    """Refuse a report whose ranking predates a newer tagging run."""
    path = Path(processed_dir) / SCORES_NAME
    if not path.is_file():
        return
    tagged = latest_tagged_at(conn)
    if tagged is None:
        return
    written = scores_mtime(path)
    if written < tagged:
        raise StaleScoresError(
            f"{path} (mtime {written.isoformat()}) is older than the newest "
            f"doc_tags.tagged_at ({tagged.isoformat()}); re-run "
            "python -m src.quantify.run_quantification before synthesizing"
        )


# --------------------------------------------------------------------------
# Corpus profile
# --------------------------------------------------------------------------


def _meta(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _stage_of(source: str, meta_json: str | None) -> PurchaseStage:
    try:
        return purchase_stage(source, _meta(meta_json))
    except ValueError:
        return PurchaseStage.MIXED


def _tag_sample_active(conn: sqlite3.Connection) -> bool:
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "tag_sample" not in tables:
        return False
    return conn.execute("SELECT COUNT(*) FROM tag_sample").fetchone()[0] > 0


def _tag_sample_spec(conn: sqlite3.Connection) -> dict[str, Any] | None:
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "run_log" not in tables:
        return None
    row = conn.execute(
        """
        SELECT notes FROM run_log
        WHERE stage = 'tag_sample'
        ORDER BY finished_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None or not row[0]:
        return None
    notes = row[0]
    for chunk in reversed(notes.split("; ")):
        chunk = chunk.strip()
        if chunk.startswith("{"):
            try:
                parsed = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _tag_sample_by_source(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _tag_sample_active(conn):
        return []
    rows = conn.execute(
        """
        SELECT COALESCE(s.source, d.source) AS source, COUNT(*) AS n
        FROM tag_sample s
        LEFT JOIN documents d ON d.doc_id = s.doc_id
        GROUP BY 1
        ORDER BY n DESC, source
        """
    ).fetchall()
    return [{"source": row["source"], "n": int(row["n"])} for row in rows]


def corpus_profile(conn: sqlite3.Connection) -> dict[str, Any]:
    """Everything Part 1 of the report has to disclose, from the documents table."""
    source_rows = conn.execute(
        """
        SELECT source,
               COUNT(*) AS n,
               SUM(CASE WHEN is_relevant = 1 AND is_duplicate_of IS NULL THEN 1 ELSE 0 END)
                   AS analyzable
        FROM documents
        GROUP BY source
        ORDER BY n DESC, source
        """
    ).fetchall()
    by_source = [
        {
            "source": row["source"],
            "documents": int(row["n"]),
            "analyzable": int(row["analyzable"] or 0),
        }
        for row in source_rows
    ]

    stage_rows = conn.execute(
        """
        SELECT source, meta_json, is_relevant, is_duplicate_of
        FROM documents
        """
    ).fetchall()
    stage_all: Counter[str] = Counter()
    stage_analyzable: Counter[str] = Counter()
    youtube_pre_analyzable = 0
    pre_analyzable = 0
    non_yt_pre = 0
    non_yt_post = 0
    non_yt_mixed = 0
    for row in stage_rows:
        stage = _stage_of(row["source"], row["meta_json"])
        stage_all[stage.value] += 1
        analyzable = row["is_relevant"] == 1 and row["is_duplicate_of"] is None
        if not analyzable:
            continue
        stage_analyzable[stage.value] += 1
        if stage is PurchaseStage.PRE_PURCHASE:
            pre_analyzable += 1
            if row["source"] == "youtube":
                youtube_pre_analyzable += 1
        if row["source"] != "youtube":
            if stage is PurchaseStage.PRE_PURCHASE:
                non_yt_pre += 1
            elif stage is PurchaseStage.POST_PURCHASE:
                non_yt_post += 1
            else:
                non_yt_mixed += 1

    exclusion_rows = conn.execute(
        """
        SELECT exclusion_reason AS reason, COUNT(*) AS n
        FROM documents
        WHERE exclusion_reason IS NOT NULL
        GROUP BY 1
        ORDER BY n DESC, reason
        """
    ).fetchall()
    exclusions = [{"reason": row["reason"], "n": int(row["n"])} for row in exclusion_rows]
    n_duplicate = int(
        conn.execute(
            "SELECT COUNT(*) FROM documents WHERE is_duplicate_of IS NOT NULL"
        ).fetchone()[0]
    )
    n_triage = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM documents
            WHERE is_relevant = 0
              AND exclusion_reason IS NULL
              AND is_duplicate_of IS NULL
            """
        ).fetchone()[0]
    )

    dates = conn.execute(
        """
        SELECT MIN(created_utc), MAX(created_utc)
        FROM documents
        WHERE created_utc IS NOT NULL AND created_utc != ''
        """
    ).fetchone()
    date_start = dates[0] if dates else None
    date_end = dates[1] if dates else None

    n_tagged = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM documents d
            JOIN doc_tags t ON t.doc_id = d.doc_id
            WHERE d.is_relevant = 1 AND d.is_duplicate_of IS NULL
            """
        ).fetchone()[0]
    )
    n_relevant = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM documents
            WHERE is_relevant = 1 AND is_duplicate_of IS NULL
            """
        ).fetchone()[0]
    )

    sample_active = _tag_sample_active(conn)
    sample_n = int(conn.execute("SELECT COUNT(*) FROM tag_sample").fetchone()[0]) if sample_active else 0

    return {
        "documents": sum(item["documents"] for item in by_source),
        "analyzable": sum(item["analyzable"] for item in by_source),
        "by_source": by_source,
        "stage_all": dict(stage_all),
        "stage_analyzable": dict(stage_analyzable),
        "pre_analyzable": pre_analyzable,
        "youtube_pre_analyzable": youtube_pre_analyzable,
        "non_yt_pre": non_yt_pre,
        "non_yt_post": non_yt_post,
        "non_yt_mixed": non_yt_mixed,
        "exclusions": exclusions,
        "n_duplicate": n_duplicate,
        "n_triage": n_triage,
        "date_start": date_start,
        "date_end": date_end,
        "n_tagged": n_tagged,
        "n_relevant": n_relevant,
        "sample_active": sample_active,
        "sample_n": sample_n,
        "sample_spec": _tag_sample_spec(conn) if sample_active else None,
        "sample_by_source": _tag_sample_by_source(conn) if sample_active else [],
        "sources_present": {item["source"] for item in by_source},
    }


def build_tag_index(conn: sqlite3.Connection) -> TagIndex:
    rows = conn.execute(
        """
        SELECT d.doc_id, d.source, t.tags_json
        FROM documents d
        JOIN doc_tags t ON t.doc_id = d.doc_id
        WHERE d.is_relevant = 1 AND d.is_duplicate_of IS NULL
        """
    ).fetchall()
    index = TagIndex(n_tagged=len(rows))
    for row in rows:
        index.docs.append((row["source"], row["doc_id"]))
        try:
            payload = json.loads(row["tags_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        intent = payload.get("intent_class")
        if intent:
            index.by_intent[str(intent)] += 1
        for key in (
            "wishlist_motivation",
            "blocker_type",
            "uncertainty_type",
            "info_sought_elsewhere",
            "segment_cue",
        ):
            for label in payload.get(key) or []:
                index.by_label[str(label)].append((row["source"], row["doc_id"]))
    return index


def _cite(index: TagIndex, label: str | None = None) -> str:
    if label:
        hits = index.by_label.get(label) or []
        if hits:
            source, doc_id = hits[0]
            return f"`{source}` `{doc_id}`"
    if index.docs:
        source, doc_id = index.docs[0]
        return f"`{source}` `{doc_id}`"
    return "`(no tagged document)`"


def _n(index: TagIndex, *labels: str) -> int:
    seen: set[str] = set()
    for label in labels:
        for _source, doc_id in index.by_label.get(label) or []:
            seen.add(doc_id)
    return len(seen)


# --------------------------------------------------------------------------
# Section 1 — Corpus summary
# --------------------------------------------------------------------------


def render_corpus_summary(counts: dict[str, Any]) -> str:
    lines = [
        "## Corpus summary",
        "",
        f"**{counts['documents']} documents** in the corpus, "
        f"**{counts['analyzable']} analyzable** (relevant, not a duplicate).",
        "",
    ]
    if not counts["by_source"]:
        lines.append("The documents table is empty.")
        return "\n".join(lines) + "\n"

    lines.extend(["| source | documents | analyzable |", "| --- | ---: | ---: |"])
    for item in counts["by_source"]:
        lines.append(
            f"| `{item['source']}` | {item['documents']} | {item['analyzable']} |"
        )
    lines.append("")

    analyzable = [item for item in counts["by_source"] if item["analyzable"]]
    if not analyzable:
        lines.append("No analyzable documents yet, so there is no source mix to report.")
    elif len(analyzable) == 1:
        lines.append(
            f"The analyzable corpus is still **{analyzable[0]['source']}-only**. "
            "Pre-purchase claims inherit that platform's bias until another source survives."
        )
    else:
        shares = ", ".join(
            f"`{item['source']}` {item['analyzable'] / counts['analyzable']:.0%}"
            for item in analyzable
        )
        lines.append(f"Analyzable source mix: {shares}. The corpus is no longer YouTube-only.")
    lines.append("")

    pre = counts.get("pre_analyzable") or 0
    post = (counts.get("stage_analyzable") or {}).get(PurchaseStage.POST_PURCHASE.value, 0)
    mixed = (counts.get("stage_analyzable") or {}).get(PurchaseStage.MIXED.value, 0)
    lines.append(
        f"**Purchase-stage split** (analyzable): **{pre} pre-purchase**, "
        f"**{post} post-purchase**, **{mixed} mixed**."
    )
    yt_pre = counts.get("youtube_pre_analyzable") or 0
    if pre:
        lines.append(
            f"YouTube accounts for **{yt_pre} of {pre}** analyzable pre-purchase "
            f"documents ({yt_pre / pre:.0%}). That concentration — haul and "
            "influencer framing — is the mix Part 1 of the brief has to name, "
            "not bury in a total. Quora is the only other live pre-purchase route."
        )
    lines.append("")

    start, end = counts.get("date_start"), counts.get("date_end")
    if start and end:
        start_d, end_d = str(start)[:10], str(end)[:10]
        if start_d == end_d:
            lines.append(f"**Date range** of `created_utc`: {start_d}.")
        else:
            lines.append(f"**Date range** of `created_utc`: {start_d} to {end_d}.")
        lines.append("")

    lines.append("**Funnel by exclusion reason** (rows retained, not deleted):")
    lines.append("")
    lines.append("| reason | documents |")
    lines.append("| --- | ---: |")
    for item in counts.get("exclusions") or []:
        lines.append(f"| `{item['reason']}` | {item['n']} |")
    lines.append(f"| `duplicate` | {counts.get('n_duplicate') or 0} |")
    lines.append(
        f"| `triage_irrelevant` (no hard-exclusion code) | {counts.get('n_triage') or 0} |"
    )
    lines.append("")

    lines.append(
        "**Tagger quality:** macro-F1 on `blocker_type` and **evidence precision** "
        "against a gold set have **not been measured** — no labelled gold set is in "
        "the repository. Do not read the quotes below as human-validated spans."
    )
    lines.append("")

    n_tagged = counts.get("n_tagged") or 0
    n_relevant = counts.get("n_relevant") or counts.get("analyzable") or 0
    if counts.get("sample_active"):
        spec = counts.get("sample_spec") or {}
        seed = spec.get("seed", "unrecorded")
        target = spec.get("target", counts.get("sample_n"))
        lines.append(
            f"**Tagging denominators:** **{n_relevant} relevant**, **{n_tagged} tagged** "
            f"(sample seed `{seed}`, target {target}). Every prevalence figure below "
            "is computed over the tagged set, not the relevant corpus."
        )
        draw = counts.get("sample_by_source") or []
        if draw:
            parts = ", ".join(f"`{item['source']}` {item['n']}" for item in draw)
            lines.append(f"Per-source draw: {parts}.")
        census = spec.get("census_sources") or []
        if census:
            lines.append(
                "Censused in full (not drawn): "
                + ", ".join(f"`{name}`" for name in census)
                + "; remaining sources proportional to taggable size."
            )
    else:
        lines.append(
            f"**Tagged set:** **{n_tagged} of {n_relevant}** analyzable documents "
            "carry tags (no `tag_sample` in force). Prevalence below is over the "
            "tagged analyzable set."
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Section 2 — Opportunity areas
# --------------------------------------------------------------------------


def _supporting_ids(opportunity: Opportunity) -> list[str] | None:
    raw = opportunity.supporting_doc_ids
    if not raw:
        return None
    ids = [part for part in raw.split(";") if part.strip()]
    return ids or None


def _cluster_members(opportunity: Opportunity) -> list[str]:
    labels = [opportunity.name]
    if opportunity.cluster:
        for part in opportunity.cluster.split(";"):
            part = part.strip()
            if "=" in part:
                labels.append(part.split("=", 1)[1].strip())
            elif part:
                labels.append(part)
    return list(dict.fromkeys(label for label in labels if label))


def _segment_hits(
    opportunity: Opportunity,
    segment_rows: Sequence[dict[str, str]],
) -> list[str]:
    members = {name.casefold() for name in _cluster_members(opportunity)}
    hits: list[tuple[float, int, str]] = []
    for row in segment_rows:
        blocker = (row.get("blocker_type") or "").strip()
        if blocker.casefold() not in members:
            continue
        try:
            lift = float(row.get("lift") or 0)
            n_docs = int(float(row.get("n_docs") or 0))
        except ValueError:
            continue
        segment = (row.get("segment") or "").strip()
        if not segment:
            continue
        hits.append((lift, n_docs, segment))
    hits.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [
        f"`{segment}` (n={n}, lift={lift:.2f})"
        for lift, n, segment in hits
    ]


def render_opportunity_areas(
    opportunities: list[Opportunity] | None,
    *,
    conn: sqlite3.Connection,
    segment_rows: Sequence[dict[str, str]] | None = None,
) -> tuple[str, dict[str, list[Quote]]]:
    lines = ["## Opportunity areas", ""]
    quotes_by_theme: dict[str, list[Quote]] = {}
    if opportunities is None:
        lines.append("pending — run Stage 4 (quantify) first")
        lines.append("")
        lines.append(
            f"Stage 4 has not written `{SCORES_NAME}`, so this section lists nothing "
            "rather than inventing ranked themes."
        )
        return "\n".join(lines) + "\n", quotes_by_theme

    if not opportunities:
        lines.append(
            "No opportunity cleared the reporting bar. The funnel and corpus "
            "summary above show whether that is corpus size or a genuine absence "
            "of signal (edge-case.md §6.1)."
        )
        return "\n".join(lines) + "\n", quotes_by_theme

    lines.append(
        "Ranked themes from Stage 4. Evidence is attributed by source and `doc_id`; "
        "quotes are unflagged spans nearest the cluster (tag overlap) plus highest "
        "severity, never hand-picked. Source URLs may rot; the quote and `doc_id` "
        "remain the evidence of record."
    )
    lines.append("")
    lines.append(WEIGHTING_NOTE)
    lines.append("")
    comparison = _render_genuine_intent_comparison(opportunities, conn)
    if comparison:
        lines.append(comparison)
        lines.append("")

    used: set[str] = set()
    segments = list(segment_rows or [])
    for index, opportunity in enumerate(opportunities, start=1):
        lines.append(f"### {index}. {opportunity.name}")
        lines.append("")
        if opportunity.score is not None:
            lines.append(f"- opportunity score: {opportunity.score}")
        components = _score_components_line(opportunity)
        if components:
            lines.append(components)
        if opportunity.prevalence is not None:
            line = f"- prevalence: {opportunity.prevalence:.1%}"
            if opportunity.prevalence_lo is not None and opportunity.prevalence_hi is not None:
                line += (
                    f" (Wilson 95% CI {opportunity.prevalence_lo:.1%}–"
                    f"{opportunity.prevalence_hi:.1%})"
                )
            lines.append(line)
        if opportunity.documents is not None:
            lines.append(f"- supporting documents (Stage 4): {opportunity.documents}")
        if opportunity.n_authors is not None:
            lines.append(f"- distinct authors: {opportunity.n_authors}")
        if opportunity.genuine_documents is not None:
            lines.append(f"- genuine-intent documents: {opportunity.genuine_documents}")
        if opportunity.genuine_score is not None:
            lines.append(f"- genuine-intent score: {opportunity.genuine_score}")
        if opportunity.cooccurrence:
            lines.append(f"- co-occurrence: {opportunity.cooccurrence}")
        affected = _segment_hits(opportunity, segments)
        if affected:
            lines.append("- affected segments: " + "; ".join(affected))
        if opportunity.post_purchase_only:
            lines.append("- post-purchase only: yes (ranked below pre-purchase-supported areas)")
        if opportunity.low_confidence:
            lines.append("- low confidence: supporting n_docs below the Stage 4 threshold")
        quotes = select_quotes(
            conn,
            opportunity.name,
            cluster=opportunity.cluster,
            dimension=opportunity.dimension,
            supporting_ids=_supporting_ids(opportunity),
            limit=EVIDENCE_PER_THEME,
            used=used,
        )
        if not quotes and _supporting_ids(opportunity):
            quotes = select_quotes(
                conn,
                opportunity.name,
                cluster=opportunity.cluster,
                dimension=opportunity.dimension,
                supporting_ids=None,
                limit=EVIDENCE_PER_THEME,
                used=used,
            )
        quotes_by_theme[opportunity.name] = quotes
        lines.append("")
        lines.append("**Evidence**")
        lines.append("")
        if not quotes:
            lines.append("No unflagged quote available for this theme.")
        else:
            lines.extend(format_quote(q) for q in quotes)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", quotes_by_theme


def _score_components_line(opportunity: Opportunity) -> str | None:
    """The four factors in ``opportunity_score`` (architecture.md §8.3)."""
    if (
        opportunity.prevalence_norm is None
        and opportunity.severity_norm is None
        and opportunity.mean_actionability is None
        and opportunity.evidence_confidence is None
    ):
        return None
    parts: list[str] = []
    if opportunity.prevalence_norm is not None:
        parts.append(f"sqrt(prevalence_norm)={math.sqrt(max(opportunity.prevalence_norm, 0.0)):.2f}")
    if opportunity.severity_norm is not None:
        parts.append(f"severity_norm={opportunity.severity_norm:.2f}")
    elif opportunity.mean_severity is not None:
        parts.append(f"severity_norm={opportunity.mean_severity / 5:.2f}")
    if opportunity.mean_actionability is not None:
        parts.append(f"actionability={opportunity.mean_actionability:.2f}")
    if opportunity.evidence_confidence is not None:
        parts.append(f"evidence_confidence={opportunity.evidence_confidence:.2f}")
    if not parts:
        return None
    return "- score components: " + "; ".join(parts)


def _render_genuine_intent_comparison(
    opportunities: list[Opportunity],
    conn: sqlite3.Connection,
) -> str:
    movers = _rank_movers(opportunities)
    if not movers:
        return ""
    genuine_n = _count_genuine_intent(conn)
    size = f"the **{genuine_n}-document** subset" if genuine_n else "the subset of tagged documents"
    lines = [
        "### Full corpus vs genuine intent",
        "",
        f"`genuine_intent` is {size} showing real purchase intent rather than "
        "bookmarking or a complaint after the fact — the population a "
        "wishlist-to-purchase study actually has to move.",
        "",
        "| theme | full score | genuine-intent score | rank movement |",
        "| --- | ---: | ---: | --- |",
    ]
    for _abs_delta, delta, opportunity, full_rank, genuine_rank in movers:
        if delta > 0:
            movement = f"up {delta} ({full_rank} → {genuine_rank})"
        elif delta < 0:
            movement = f"down {abs(delta)} ({full_rank} → {genuine_rank})"
        else:
            movement = f"holds ({full_rank})"
        full_s = "" if opportunity.score is None else opportunity.score
        gen_s = "" if opportunity.genuine_score is None else opportunity.genuine_score
        lines.append(
            f"| `{opportunity.name}` | {full_s} | {gen_s} | {movement} |"
        )
    return "\n".join(lines)


def _rank_movers(
    opportunities: list[Opportunity],
) -> list[tuple[int, int, Opportunity, int, int]]:
    ranked = [
        row
        for row in opportunities
        if row.score is not None and row.genuine_score is not None
    ]
    if len(ranked) < 2:
        return []
    by_full = sorted(ranked, key=lambda row: (-(row.score or 0.0), row.name))
    by_genuine = sorted(ranked, key=lambda row: (-(row.genuine_score or 0.0), row.name))
    full_rank = {row.name: index for index, row in enumerate(by_full, start=1)}
    genuine_rank = {row.name: index for index, row in enumerate(by_genuine, start=1)}
    rows: list[tuple[int, int, Opportunity, int, int]] = []
    for row in ranked:
        delta = full_rank[row.name] - genuine_rank[row.name]
        rows.append((abs(delta), delta, row, full_rank[row.name], genuine_rank[row.name]))

    def _in_head(item: tuple[int, int, Opportunity, int, int]) -> bool:
        return item[3] <= RANK_HEAD or item[4] <= RANK_HEAD

    head = [
        item
        for item in rows
        if _in_head(item) and (item[2].genuine_score or 0.0) > 0.0
    ]
    pool = head or rows
    pool.sort(key=lambda item: (-item[0], item[3], item[2].name))
    shifted = [item for item in pool if item[1] != 0]
    holds = sorted((item for item in pool if item[1] == 0), key=lambda item: item[3])
    hold_slots = min(2, len(holds), MAX_RANK_MOVERS)
    selected = shifted[: MAX_RANK_MOVERS - hold_slots]
    selected.extend(holds[:hold_slots])
    if len(selected) < MAX_RANK_MOVERS:
        seen = {item[2].name for item in selected}
        selected.extend(
            item for item in shifted + holds if item[2].name not in seen
        )
    return selected[:MAX_RANK_MOVERS]


def _count_genuine_intent(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT t.tags_json
        FROM documents d
        JOIN doc_tags t ON t.doc_id = d.doc_id
        WHERE d.is_relevant = 1 AND d.is_duplicate_of IS NULL
        """
    ).fetchall()
    n = 0
    for row in rows:
        try:
            payload = json.loads(row["tags_json"] if "tags_json" in row.keys() else row[0])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("intent_class") == "genuine_intent":
            n += 1
    return n


# --------------------------------------------------------------------------
# Section 3 — Ten discovery questions
# --------------------------------------------------------------------------


def render_discovery_questions(
    index: TagIndex,
    opportunities: list[Opportunity] | None,
    *,
    segment_rows: Sequence[dict[str, str]] | None = None,
) -> str:
    answers = [
        _answer_q1(index),
        _answer_q2(index),
        _answer_q3(index),
        _answer_q4(index),
        _answer_q5(index),
        _answer_q6(index),
        _answer_q7(index),
        _answer_q8(index),
        _answer_q9(index, segment_rows or []),
        _answer_q10(index, opportunities),
    ]
    lines = [
        "## Discovery questions",
        "",
        "Each question from `problemStatement.md` is answered with at least one "
        "number from the tagged corpus (or Stage 4 ranking) and a `doc_id` citation. "
        "AJIO aggregate figures are not used here.",
        "",
    ]
    for (qid, title), body in zip(DISCOVERY_QUESTIONS, answers):
        lines.append(f"### {qid}. {title}")
        lines.append("")
        lines.append(body)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _share_line(index: TagIndex, labels: Sequence[str], *, cite_label: str | None = None) -> str:
    n = _n(index, *labels)
    cited = _cite(index, cite_label or (labels[0] if labels else None))
    names = ", ".join(f"`{label}`" for label in labels)
    return f"**{n} of {index.n_tagged}** tagged documents carry {names} ({cited})."


def _answer_q1(index: TagIndex) -> str:
    return (
        "Wishlist motivations in the tagged set: "
        + _share_line(
            index,
            [
                "price_watch",
                "decide_later",
                "compare_options",
                "awaiting_occasion",
                "budget_timing",
                "inspiration_bookmark",
                "size_unavailable",
                "seeking_opinion",
                "cart_proxy",
            ],
            cite_label="price_watch",
        )
    )


def _answer_q2(index: TagIndex) -> str:
    return "Purchase blockers: " + _share_line(
        index,
        [
            "fit_size_uncertainty",
            "quality_doubt",
            "color_fabric_accuracy",
            "return_friction",
            "delivery_uncertainty",
            "trust_authenticity",
            "choice_overload",
            "styling_uncertainty",
            "social_validation_needed",
            "checkout_friction",
            "price_absolute",
            "price_expectation",
        ],
        cite_label="fit_size_uncertainty",
    )


def _answer_q3(index: TagIndex) -> str:
    return "Open uncertainties: " + _share_line(
        index,
        [
            "will_it_fit",
            "how_does_it_look_on_me",
            "is_quality_worth_it",
            "true_color",
            "occasion_appropriate",
            "can_i_return",
            "better_alternative_exists",
        ],
        cite_label="will_it_fit",
    )


def _answer_q4(index: TagIndex) -> str:
    return "Postpone / wait signals: " + _share_line(
        index,
        ["decide_later", "budget_timing", "price_watch", "awaiting_occasion"],
        cite_label="decide_later",
    )


def _answer_q5(index: TagIndex) -> str:
    return "Comparison behaviour: " + _share_line(
        index,
        ["compare_options", "choice_overload"],
        cite_label="compare_options",
    )


def _answer_q6(index: TagIndex) -> str:
    return "Information sought off-site: " + _share_line(
        index,
        [
            "youtube_haul",
            "friend_family_opinion",
            "other_marketplace_reviews",
            "brand_site_size_chart",
            "instagram_styling",
            "offline_store_tryon",
        ],
        cite_label="other_marketplace_reviews",
    )


def _answer_q7(index: TagIndex) -> str:
    roles = [
        ("fit/size", ("fit_size_uncertainty", "will_it_fit")),
        ("styling", ("styling_uncertainty", "how_does_it_look_on_me")),
        ("price", ("price_absolute", "price_expectation", "price_watch")),
        ("reviews", ("other_marketplace_reviews",)),
        ("occasion", ("awaiting_occasion", "occasion_appropriate", "occasion_shopper")),
        ("social validation", ("social_validation_needed", "friend_family_opinion")),
    ]
    bits = []
    cite = _cite(index, "fit_size_uncertainty")
    for name, labels in roles:
        bits.append(f"{name} **{_n(index, *labels)}**")
    return (
        f"Of {index.n_tagged} tagged documents, tag volumes are "
        + "; ".join(bits)
        + f" ({cite})."
    )


def _answer_q8(index: TagIndex) -> str:
    genuine = index.by_intent.get("genuine_intent", 0)
    bookmark = index.by_intent.get("bookmark_only", 0)
    ambiguous = index.by_intent.get("ambiguous", 0)
    return (
        f"**{genuine}** tagged documents are `genuine_intent`, **{bookmark}** "
        f"`bookmark_only`, **{ambiguous}** `ambiguous`, out of {index.n_tagged} "
        f"({_cite(index)})."
    )


def _answer_q9(index: TagIndex, segment_rows: Sequence[dict[str, str]]) -> str:
    n_cued = _n(
        index,
        "first_time_online_buyer",
        "frequent_shopper",
        "budget_conscious",
        "premium_seeker",
        "occasion_shopper",
        "plus_or_petite_size",
        "menswear",
        "womenswear",
        "tier2_3_city",
    )
    material = [
        row
        for row in segment_rows
        if _as_float(row.get("lift")) is not None
        and float(row["lift"]) >= MATERIAL_LIFT
    ]
    extra = (
        f" Stage 4 `segment_matrix.csv` has **{len(material)}** cell(s) with lift "
        f"≥ {MATERIAL_LIFT:g}."
        if segment_rows
        else " Stage 4 has not written `segment_matrix.csv`."
    )
    return (
        f"**{n_cued} of {index.n_tagged}** tagged documents carry a `segment_cue` "
        f"({_cite(index, 'budget_conscious')})."
        + extra
    )


def _answer_q10(index: TagIndex, opportunities: list[Opportunity] | None) -> str:
    if opportunities is None:
        return (
            f"**0** ranked opportunity areas (Stage 4 has not written `{SCORES_NAME}`). "
            f"The tagged set is still {index.n_tagged} documents ({_cite(index)})."
        )
    if not opportunities:
        return (
            f"**0** opportunity areas cleared the reporting bar, from {index.n_tagged} "
            f"tagged documents ({_cite(index)})."
        )
    top = opportunities[0]
    n = top.documents if top.documents is not None else 0
    score = "" if top.score is None else f", score {top.score}"
    cited_label = top.name if index.by_label.get(top.name) else None
    return (
        f"The leading unmet-need cluster is `{top.name}` "
        f"({n} supporting documents{score}; {_cite(index, cited_label)})."
    )


# --------------------------------------------------------------------------
# Section 5 — Segment differences
# --------------------------------------------------------------------------


def render_segment_differences(rows: Sequence[dict[str, str]]) -> str:
    lines = ["## Segment differences", ""]
    if not rows:
        lines.append(
            "pending — run Stage 4 (quantify) first. `segment_matrix.csv` is not "
            "on disk, so no segment lift is invented."
        )
        return "\n".join(lines) + "\n"
    material = []
    for row in rows:
        lift = _as_float(row.get("lift"))
        if lift is None or lift < MATERIAL_LIFT:
            continue
        material.append(row)
    lines.append(
        f"`segment_cue` × `blocker_type` cells whose lift is at least "
        f"**{MATERIAL_LIFT:g}** relative to the tagged-corpus baseline "
        f"({len(material)} of {len(rows)} cells)."
    )
    lines.append("")
    if not material:
        lines.append("No segment–blocker cell reaches that margin.")
        return "\n".join(lines) + "\n"
    lines.extend(["| segment | blocker | n_docs | lift |", "| --- | --- | ---: | ---: |"])
    material.sort(key=lambda r: (-float(r.get("lift") or 0), r.get("segment") or ""))
    for row in material:
        lines.append(
            f"| `{row.get('segment', '')}` | `{row.get('blocker_type', '')}` | "
            f"{row.get('n_docs', '')} | {row.get('lift', '')} |"
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Section 6 — Excluded-by-constraint (price)
# --------------------------------------------------------------------------


def render_excluded_by_constraint(
    index: TagIndex,
    *,
    prevalence_rows: Sequence[dict[str, str]] | None = None,
) -> str:
    lines = [
        "## Excluded by constraint",
        "",
        "Price-driven tags are scored by Stage 4 but the no-incentives rule keeps "
        "them out of the action the report would recommend. Volumes are shown so "
        "a reader can see what that constraint removed rather than wondering "
        "whether it was missed.",
        "",
        "| tag | tagged documents |",
        "| --- | ---: |",
    ]
    by_csv: dict[str, int] = {}
    for row in prevalence_rows or []:
        label = (row.get("label") or "").strip()
        if label not in PRICE_LABELS:
            continue
        n = _as_int(row.get("n_docs"))
        if n is not None:
            by_csv[label] = n
    for label in PRICE_LABELS:
        n = by_csv.get(label, _n(index, label))
        lines.append(f"| `{label}` | {n} |")
    total = _n(index, *PRICE_LABELS)
    cite = _cite(index, next((lab for lab in PRICE_LABELS if _n(index, lab)), None))
    lines.append("")
    lines.append(
        f"**{total} of {index.n_tagged}** tagged documents carry at least one "
        f"price-driven tag ({cite})."
    )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Limitations extras
# --------------------------------------------------------------------------


def limitation_entries(
    counts: dict[str, Any],
    opportunities: list[Opportunity] | None,
) -> list[str]:
    entries: list[str] = []
    analyzable = [item for item in counts["by_source"] if item["analyzable"]]
    if len(analyzable) == 1:
        entries.append(
            f"The analyzable corpus is {analyzable[0]['source']}-only, so ranked "
            "findings inherit that platform's self-selection until another source is tagged."
        )
    pre = counts.get("pre_analyzable") or 0
    yt_pre = counts.get("youtube_pre_analyzable") or 0
    if pre:
        entries.append(
            f"YouTube is {yt_pre} of {pre} analyzable pre-purchase documents "
            f"({yt_pre / pre:.0%}). Haul-video audiences, comment-section "
            "self-selection, and influencer framing therefore dominate any "
            "pre-purchase claim."
        )
    non_yt = (
        (counts.get("non_yt_pre") or 0)
        + (counts.get("non_yt_post") or 0)
        + (counts.get("non_yt_mixed") or 0)
    )
    if non_yt:
        entries.append(
            f"Among non-YouTube analyzable documents, {counts.get('non_yt_post') or 0} "
            f"are post-purchase, {counts.get('non_yt_pre') or 0} pre-purchase, and "
            f"{counts.get('non_yt_mixed') or 0} mixed — a post-purchase tilt once "
            "haul comments are set aside."
        )
    entries.append(
        "Public conversation over-represents strong opinions; reviews and complaint "
        "boards in particular skew to extremity. The corpus is English/Hinglish "
        "because `hindi_language` is a hard exclusion, so Hindi-only hesitation is "
        "out of scope."
    )
    if "trustpilot" not in (counts.get("sources_present") or set()):
        entries.append(
            "`trustpilot` yielded nothing (robots-restricted / expected zero-yield) "
            "and is absent from the documents table."
        )
    else:
        tp = next(
            (item for item in counts["by_source"] if item["source"] == "trustpilot"),
            None,
        )
        if tp and tp["analyzable"] == 0:
            entries.append(
                "`trustpilot` is in the raw corpus but contributed 0 analyzable "
                "documents (robots-restricted zero-yield)."
            )
    entries.append(
        "Quora (`quora_manual`) is a manual-only sample of threads a person saved; "
        "a share of answers arrived truncated, and authors and timestamps are often "
        "missing. AJIO on-site prose is absent: Akamai blocks automated collection "
        "and the site publishes no free-text Q&A to import."
    )
    if counts.get("sample_active"):
        spec = counts.get("sample_spec") or {}
        seed = spec.get("seed", "unrecorded")
        target = spec.get("target", counts.get("sample_n"))
        census = spec.get("census_sources") or []
        census_txt = ", ".join(f"`{name}`" for name in census) or "none recorded"
        entries.append(
            f"Phase 4 tagged a sample, not the corpus: **{counts.get('n_tagged')} of "
            f"{counts.get('n_relevant')}** relevant documents (seed `{seed}`, "
            f"target {target}; censused: {census_txt}; the rest drawn proportionally). "
            "Read from `run_log` stage `tag_sample`. Prevalence figures are over the "
            "tagged set."
        )
    flagged = [
        row.flagged_evidence_share
        for row in (opportunities or [])
        if row.flagged_evidence_share is not None
    ]
    if flagged:
        mean_flagged = sum(flagged) / len(flagged)
        entries.append(
            f"Tags are machine-assigned. Mean `flagged_evidence_share` across "
            f"ranked areas is {mean_flagged:.0%} — that share of supporting "
            "documents rest on a quote a human would not have chosen as evidence. "
            "The report shows only unflagged quotes."
        )
    else:
        entries.append(
            "Tags are machine-assigned. Gold-set macro-F1 and evidence precision "
            "have not been measured (no labelled gold set), so attribution error "
            "is undisclosed rather than assumed to be zero."
        )
    if opportunities is None:
        entries.append(
            "Stage 4 (quantify) has not produced ranked opportunity areas, so this "
            "report has no theme ranking and invents none."
        )
    return entries


# --------------------------------------------------------------------------
# Stitch
# --------------------------------------------------------------------------


def _template_env() -> Environment:
    root = Path(__file__).resolve().parent / "templates"
    return Environment(
        loader=FileSystemLoader(str(root)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def stitch_report(
    *,
    corpus_summary: str,
    opportunity_areas: str,
    discovery_questions: str,
    ajio_aggregates: str,
    segment_differences: str,
    excluded_by_constraint: str,
    limitations: str,
) -> str:
    template = _template_env().get_template("report.md.j2")
    return template.render(
        corpus_summary=corpus_summary.rstrip(),
        opportunity_areas=opportunity_areas.rstrip(),
        discovery_questions=discovery_questions.rstrip(),
        ajio_aggregates=ajio_aggregates.rstrip(),
        segment_differences=segment_differences.rstrip(),
        excluded_by_constraint=excluded_by_constraint.rstrip(),
        limitations=limitations.rstrip(),
    )


def assemble_markdown(
    conn: sqlite3.Connection,
    *,
    processed_dir: str | Path,
    aggregates: Sequence[AjioAggregate],
) -> tuple[str, dict[str, list[Quote]], dict[str, Any]]:
    """Render every section and stitch them. Returns markdown, appendix quotes, summary extras."""
    counts = corpus_profile(conn)
    opportunities = load_opportunity_scores(processed_dir)
    index = build_tag_index(conn)
    segment_rows = load_csv_rows(processed_dir, SEGMENT_NAME)
    prevalence_rows = load_csv_rows(processed_dir, PREVALENCE_NAME)

    if opportunities is None:
        themes: list[Theme] = []
    else:
        themes = [
            Theme(
                name=item.name,
                documents=item.documents or 0,
                prevalence=item.prevalence,
            )
            for item in opportunities
        ]

    opportunity_md, quotes = render_opportunity_areas(
        opportunities, conn=conn, segment_rows=segment_rows
    )
    extras = limitation_entries(counts, opportunities)
    markdown = stitch_report(
        corpus_summary=render_corpus_summary(counts),
        opportunity_areas=opportunity_md,
        discovery_questions=render_discovery_questions(
            index, opportunities, segment_rows=segment_rows
        ),
        ajio_aggregates=render_aggregates(themes, aggregates=aggregates),
        segment_differences=render_segment_differences(segment_rows),
        excluded_by_constraint=render_excluded_by_constraint(
            index, prevalence_rows=prevalence_rows
        ),
        limitations=render_limitations(extras, aggregates=aggregates),
    )
    meta = {
        "counts": counts,
        "quantify_status": "pending" if opportunities is None else "present",
        "themes": 0 if opportunities is None else len(opportunities),
        "sections": [
            "corpus_summary",
            "opportunity_areas",
            "discovery_questions",
            "ajio_aggregates",
            "segment_differences",
            "excluded_by_constraint",
            "limitations",
        ],
    }
    return markdown, quotes, meta
