"""Read tagged documents and emit prevalence, lift, and opportunity rows.

The analyzable set is relevant, not a duplicate, and a tag row exists.
Labels are enumerated from the data. ``ajio_aggregate`` is not a document
source and is filtered out. Nothing here writes to ``documents`` or ``doc_tags``.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from pydantic import ValidationError

from src.common.hashing import anonymous_author_hash
from src.common.logging import get_logger
from src.common.schemas import DocumentTags, PurchaseStage, purchase_stage
from src.quantify.cooccurrence import (
    TagCluster,
    cluster_tags,
    lift_rows,
    segment_matrix_rows,
    top_cooccurring,
)
from src.quantify.scoring import (
    CLUSTER_JACCARD_MIN,
    HALF_LIFE_DAYS,
    HIGH_PREVALENCE,
    LOW_CONFIDENCE_N,
    SEVERITY_MAX,
    evidence_confidence,
    min_max_normalize,
    opportunity_score,
    recency_weight,
    sample_size_factor,
    source_spread_factor,
    wilson_interval,
)
from src.quantify.screen import document_is_flagged
from src.tag.taxonomy import MULTI_LABEL_DIMENSIONS, IntentClass

log = get_logger("quantify.metrics")

THEME_DIMENSIONS: tuple[str, ...] = tuple(name for name, _ in MULTI_LABEL_DIMENSIONS)
#: segment_cue is a breakdown, never an opportunity area (taxonomy.py).
OPPORTUNITY_DIMENSIONS: tuple[str, ...] = tuple(
    name for name in THEME_DIMENSIONS if name != "segment_cue"
)

BASE_COLUMNS: tuple[str, ...] = (
    "dimension",
    "label",
    "n_docs",
    "prevalence",
    "mean_severity",
    "mean_actionability",
    "mean_confidence",
    "opportunity_score",
    "co_occurs_with",
)

SCORE_EXTRA_COLUMNS: tuple[str, ...] = (
    "prevalence_lo",
    "prevalence_hi",
    "prevalence_norm",
    "severity_norm",
    "evidence_confidence",
    "source_spread",
    "attribution_factor",
    "flagged_evidence_share",
    "low_confidence",
    "reportable",
    "source_specific",
    "high_prevalence",
    "post_purchase_only",
    "n_pre_purchase",
    "n_post_purchase",
    "n_mixed",
    "n_authors",
    "author_prevalence",
    "n_docs_genuine",
    "prevalence_genuine",
    "prevalence_lo_genuine",
    "prevalence_hi_genuine",
    "mean_severity_genuine",
    "mean_actionability_genuine",
    "mean_confidence_genuine",
    "evidence_confidence_genuine",
    "opportunity_score_genuine",
    "cluster",
    "supporting_doc_ids",
)

PREVALENCE_COLUMNS: tuple[str, ...] = (
    "dimension",
    "label",
    "n_docs",
    "n_authors",
    "prevalence",
    "prevalence_lo",
    "prevalence_hi",
    "author_prevalence",
    "mean_severity",
    "mean_actionability",
    "mean_confidence",
    "source_spread",
    "flagged_evidence_share",
    "low_confidence",
    "reportable",
    "source_specific",
    "high_prevalence",
    "post_purchase_only",
    "n_pre_purchase",
    "n_post_purchase",
    "n_mixed",
    "n_docs_genuine",
    "prevalence_genuine",
    "prevalence_lo_genuine",
    "opportunity_score_genuine",
)

LIFT_COLUMNS: tuple[str, ...] = (
    "dimension_a",
    "label_a",
    "dimension_b",
    "label_b",
    "n_both",
    "n_a",
    "n_b",
    "lift",
)

SEGMENT_COLUMNS: tuple[str, ...] = ("segment", "blocker_type", "n_docs", "lift")

ANALYZABLE_SQL = """
SELECT d.doc_id, d.source, d.author_hash, d.created_utc, d.ingested_at,
       d.text, d.meta_json, t.tags_json,
       t.taxonomy_version, t.prompt_version, t.model
FROM documents d
INNER JOIN doc_tags t ON t.doc_id = d.doc_id
WHERE d.is_relevant = 1
  AND d.is_duplicate_of IS NULL
  AND d.source != 'ajio_aggregate'
"""

MIN_DISTINCT_AUTHORS = 3
MAX_DOCS_PER_AUTHOR_PER_TAG = 5


class MixedTaggingError(ValueError):
    """doc_tags mixes (taxonomy_version, prompt_version, model) triples."""


@dataclass(frozen=True)
class QuantifyKnobs:
    half_life_days: float = HALF_LIFE_DAYS
    low_confidence_min_docs: int = LOW_CONFIDENCE_N
    min_distinct_authors: int = MIN_DISTINCT_AUTHORS
    max_docs_per_author_per_tag: int = MAX_DOCS_PER_AUTHOR_PER_TAG
    cluster_jaccard_min: float = CLUSTER_JACCARD_MIN
    author_salt: str | None = None


def knobs_from_settings(settings: Any | None) -> QuantifyKnobs:
    q = getattr(getattr(settings, "run", None), "quantification", None)
    salt = _hash_salt(settings)
    if q is None:
        return QuantifyKnobs(author_salt=salt)
    return QuantifyKnobs(
        half_life_days=float(q.recency_half_life_days),
        low_confidence_min_docs=int(q.low_confidence_min_docs),
        min_distinct_authors=int(q.min_distinct_authors),
        max_docs_per_author_per_tag=int(q.max_docs_per_author_per_tag),
        cluster_jaccard_min=float(getattr(q, "cluster_jaccard_min", CLUSTER_JACCARD_MIN)),
        author_salt=salt,
    )


def _hash_salt(settings: Any | None) -> str | None:
    creds = getattr(settings, "credentials", None) if settings is not None else None
    secret = getattr(creds, "hash_salt", None) if creds is not None else None
    if secret is None:
        return None
    if hasattr(secret, "get_secret_value"):
        return secret.get_secret_value()
    text = str(secret).strip()
    return text or None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class AnalyzableDoc:
    doc_id: str
    source: str
    author_hash: str | None
    created_utc: datetime | None
    ingested_at: datetime | None
    text: str
    meta: dict[str, Any]
    tags: DocumentTags

    @property
    def author_key(self) -> str:
        """Named authors stay one voice. The anonymous sentinel is not one person."""
        return self.voice_key(None)

    def voice_key(self, salt: str | None) -> str:
        """Per-author identity used for caps and distinct-author counts.

        Author-less records share ``anonymous_author_hash(source, salt)``. Treating
        that sentinel as one prolific poster would cap a whole source at
        ``max_docs_per_author_per_tag`` and fail the ≥3-author gate. Each such
        document is therefore its own unknown voice (plan §2.4, hashing.py).
        """
        if not self.author_hash:
            return f"anon:{self.doc_id}"
        if salt and self.author_hash == anonymous_author_hash(self.source, salt):
            return f"anon:{self.doc_id}"
        return self.author_hash

    @property
    def stage(self) -> PurchaseStage:
        return purchase_stage(self.source, self.meta)

    @property
    def genuine(self) -> bool:
        return self.tags.intent_class == IntentClass.GENUINE_INTENT

    def theme_labels(self) -> list[tuple[str, str]]:
        seen: set[tuple[str, str]] = set()
        pairs: list[tuple[str, str]] = []
        for dimension in THEME_DIMENSIONS:
            for value in getattr(self.tags, dimension):
                label = value.value if hasattr(value, "value") else str(value)
                key = (dimension, label)
                if key in seen:
                    continue
                seen.add(key)
                pairs.append(key)
        return pairs

    def recency(self, reference: datetime, half_life_days: float) -> float:
        stamp = self.created_utc or self.ingested_at
        if stamp is None:
            return 1.0
        age_days = (reference - stamp).total_seconds() / 86400.0
        return recency_weight(age_days, half_life_days=half_life_days)


@dataclass
class QuantifyResult:
    opportunities: list[dict[str, Any]]
    prevalence: list[dict[str, Any]]
    cooccurrence_lift: list[dict[str, Any]]
    segment_matrix: list[dict[str, Any]]
    sources: list[str]
    n_docs: int
    n_genuine: int
    knobs: QuantifyKnobs = field(default_factory=QuantifyKnobs)


def parse_tags_json(raw: str) -> DocumentTags:
    """Parse a ``doc_tags.tags_json`` blob, ignoring a leftover ``doc_id``."""
    payload = json.loads(raw)
    if isinstance(payload, dict):
        payload.pop("doc_id", None)
    return DocumentTags.model_validate(payload)


def load_analyzable(conn: sqlite3.Connection) -> list[AnalyzableDoc]:
    """Tagged, relevant, non-duplicate documents. Unparseable tag rows are skipped.

    Refuses to mix ``(taxonomy_version, prompt_version, model)`` triples
    (edge-case.md §4.4.2): a mid-run prompt edit must not silently blend corpora.
    """
    raw_rows = list(conn.execute(ANALYZABLE_SQL))
    triples = {
        (row["taxonomy_version"], row["prompt_version"], row["model"])
        for row in raw_rows
    }
    if len(triples) > 1:
        pretty = ", ".join(
            f"(taxonomy={t[0]!r}, prompt={t[1]!r}, model={t[2]!r})"
            for t in sorted(triples)
        )
        raise MixedTaggingError(
            "quantification refuses to mix tagging triples; found " + pretty
        )
    docs: list[AnalyzableDoc] = []
    seen: set[str] = set()
    for row in raw_rows:
        doc_id = row["doc_id"]
        if doc_id in seen:
            continue
        seen.add(doc_id)
        try:
            tags = parse_tags_json(row["tags_json"])
            meta = json.loads(row["meta_json"] or "{}")
            if not isinstance(meta, dict):
                meta = {}
        except (ValueError, TypeError, ValidationError) as exc:
            log.warning("skipping document %s: unparseable tags_json (%s)", doc_id, exc)
            continue
        docs.append(
            AnalyzableDoc(
                doc_id=doc_id,
                source=row["source"],
                author_hash=row["author_hash"],
                created_utc=_parse_dt(row["created_utc"]),
                ingested_at=_parse_dt(row["ingested_at"]),
                text=row["text"] or "",
                meta=meta,
                tags=tags,
            )
        )
    return docs


def _reference_time(docs: list[AnalyzableDoc]) -> datetime:
    stamps = [d.created_utc or d.ingested_at for d in docs if d.created_utc or d.ingested_at]
    return max(stamps) if stamps else datetime.now(timezone.utc)


def _author_recency_share(
    population: list[AnalyzableDoc],
    tagged: set[str],
    recency: dict[str, float],
    cap: int,
    salt: str | None,
) -> float:
    """Each author is one vote; within an author, recency-weight up to ``cap`` docs."""
    by_author: dict[str, list[AnalyzableDoc]] = defaultdict(list)
    for doc in population:
        by_author[doc.voice_key(salt)].append(doc)
    shares: list[float] = []
    for author_docs in by_author.values():
        ordered = sorted(author_docs, key=lambda d: recency[d.doc_id], reverse=True)
        capped = ordered[: cap] if cap > 0 else ordered
        total_w = sum(recency[d.doc_id] for d in capped)
        if total_w <= 0:
            continue
        tagged_w = sum(recency[d.doc_id] for d in capped if d.doc_id in tagged)
        shares.append(tagged_w / total_w)
    return mean(shares) if shares else 0.0


def _stage_counts(group: list[AnalyzableDoc]) -> tuple[int, int, int]:
    pre = sum(1 for d in group if d.stage == PurchaseStage.PRE_PURCHASE)
    post = sum(1 for d in group if d.stage == PurchaseStage.POST_PURCHASE)
    mixed = sum(1 for d in group if d.stage == PurchaseStage.MIXED)
    return pre, post, mixed


def _cluster_flagged(doc: AnalyzableDoc, cluster: TagCluster) -> bool:
    present = set(doc.theme_labels())
    for dimension, label in cluster.members:
        if (dimension, label) not in present:
            continue
        if document_is_flagged(
            dimension=dimension,
            label=label,
            text=doc.text,
            evidence=doc.tags.evidence,
        ):
            return True
    return False


def _rows_for_population(
    population: list[AnalyzableDoc],
    clusters: list[TagCluster],
    knobs: QuantifyKnobs,
    recency: dict[str, float],
    membership_all: dict[tuple[str, str], list[str]],
) -> list[dict[str, Any]]:
    """One candidate per cluster in this population."""
    total = len(population)
    if total == 0:
        return []
    salt = knobs.author_salt
    n_authors_total = len({d.voice_key(salt) for d in population})
    n_sources_total = len({d.source for d in population})
    by_id = {d.doc_id: d for d in population}

    holders: list[tuple[TagCluster, list[AnalyzableDoc]]] = []
    for cluster in clusters:
        group = [by_id[doc_id] for doc_id in cluster.doc_ids if doc_id in by_id]
        if not group:
            continue
        holders.append((cluster, group))
    if not holders:
        return []

    weighted = [
        _author_recency_share(
            population,
            {d.doc_id for d in group},
            recency,
            knobs.max_docs_per_author_per_tag,
            salt,
        )
        for _cluster, group in holders
    ]
    norms = min_max_normalize(weighted)

    rows: list[dict[str, Any]] = []
    for (cluster, group), norm in zip(holders, norms):
        n_docs = len(group)
        prevalence = n_docs / total
        _, lo, hi = wilson_interval(n_docs, total)
        mean_severity = mean(d.tags.severity for d in group)
        severity_norm = mean_severity / SEVERITY_MAX
        mean_actionability = mean(d.tags.actionability_non_monetary for d in group)
        mean_confidence = mean(d.tags.confidence_pct for d in group)
        authors = {d.voice_key(salt) for d in group}
        sources = {d.source for d in group}
        spread = source_spread_factor(len(sources), n_sources_total)
        size_factor = sample_size_factor(prevalence, lo)
        flagged = sum(1 for d in group if _cluster_flagged(d, cluster))
        flagged_share = flagged / n_docs if n_docs else 0.0
        attrib = 1.0 - flagged_share
        ev_conf = evidence_confidence(mean_confidence, spread, size_factor, attrib)
        pre, post, mixed = _stage_counts(group)
        post_only = pre == 0
        n_authors = len(authors)
        low = n_docs < knobs.low_confidence_min_docs or n_authors < knobs.min_distinct_authors
        reportable = n_authors >= knobs.min_distinct_authors
        co = top_cooccurring(
            cluster.primary,
            membership_all,
            exclude=cluster.members,
            doc_ids=list(cluster.doc_ids),
        )
        score = opportunity_score(norm, mean_severity, mean_actionability, ev_conf)
        rows.append(
            {
                "dimension": cluster.dimension,
                "label": cluster.label,
                "n_docs": n_docs,
                "prevalence": prevalence,
                "prevalence_lo": lo,
                "prevalence_hi": hi,
                "prevalence_norm": norm,
                "severity_norm": severity_norm,
                "mean_severity": mean_severity,
                "mean_actionability": mean_actionability,
                "mean_confidence": mean_confidence,
                "evidence_confidence": ev_conf,
                "source_spread": spread,
                "attribution_factor": attrib,
                "flagged_evidence_share": flagged_share,
                "opportunity_score": score,
                "co_occurs_with": co,
                "cluster": cluster.member_csv(),
                "low_confidence": low,
                "reportable": reportable,
                "source_specific": len(sources) == 1,
                "high_prevalence": prevalence > HIGH_PREVALENCE,
                "post_purchase_only": post_only,
                "n_pre_purchase": pre,
                "n_post_purchase": post,
                "n_mixed": mixed,
                "n_authors": n_authors,
                "author_prevalence": n_authors / n_authors_total if n_authors_total else 0.0,
                "supporting_doc_ids": ";".join(sorted(d.doc_id for d in group)),
            }
        )
    return rows


def _attach_genuine(
    row: dict[str, Any],
    gen: dict[str, Any] | None,
) -> dict[str, Any]:
    out = dict(row)
    if gen is None:
        out.update(
            {
                "n_docs_genuine": 0,
                "prevalence_genuine": 0.0,
                "prevalence_lo_genuine": 0.0,
                "prevalence_hi_genuine": 0.0,
                "mean_severity_genuine": 0.0,
                "mean_actionability_genuine": 0.0,
                "mean_confidence_genuine": 0.0,
                "evidence_confidence_genuine": 0.0,
                "opportunity_score_genuine": 0.0,
            }
        )
        return out
    out["n_docs_genuine"] = gen["n_docs"]
    out["prevalence_genuine"] = gen["prevalence"]
    out["prevalence_lo_genuine"] = gen["prevalence_lo"]
    out["prevalence_hi_genuine"] = gen["prevalence_hi"]
    out["mean_severity_genuine"] = gen["mean_severity"]
    out["mean_actionability_genuine"] = gen["mean_actionability"]
    out["mean_confidence_genuine"] = gen["mean_confidence"]
    out["evidence_confidence_genuine"] = gen["evidence_confidence"]
    out["opportunity_score_genuine"] = gen["opportunity_score"]
    return out


def _rank_key(row: dict[str, Any]) -> tuple:
    """Reportable, pre-purchase-supported clusters occupy the top tier."""
    return (
        not row["reportable"],
        row["post_purchase_only"],
        -row["opportunity_score"],
        -row["n_docs"],
        row["dimension"],
        row["label"],
    )


def quantify(
    docs: list[AnalyzableDoc],
    *,
    knobs: QuantifyKnobs | None = None,
) -> QuantifyResult:
    """Full Phase 5 pass: opportunities, prevalence, lift, segment matrix."""
    knobs = knobs or QuantifyKnobs()
    total = len(docs)
    genuine_docs = [d for d in docs if d.genuine]
    membership: dict[tuple[str, str], list[str]] = defaultdict(list)
    source_totals: dict[str, int] = defaultdict(int)
    for doc in docs:
        source_totals[doc.source] += 1
        for key in doc.theme_labels():
            membership[key].append(doc.doc_id)
    sources = sorted(source_totals, key=lambda name: (-source_totals[name], name))
    lifts = lift_rows(membership, total) if total else []
    segments = segment_matrix_rows(membership, total) if total else []
    reference = _reference_time(docs)
    recency = {d.doc_id: d.recency(reference, knobs.half_life_days) for d in docs}
    if docs and all(d.created_utc is None for d in docs):
        log.info("no created_utc on analyzable docs; recency decay is uniform")

    opportunity_clusters = cluster_tags(
        membership,
        dimensions=OPPORTUNITY_DIMENSIONS,
        jaccard_min=knobs.cluster_jaccard_min,
    )
    prevalence_clusters = cluster_tags(
        membership,
        dimensions=THEME_DIMENSIONS,
        jaccard_min=0,
    )

    full_opp = _rows_for_population(docs, opportunity_clusters, knobs, recency, membership)
    gen_opp = _rows_for_population(
        genuine_docs, opportunity_clusters, knobs, recency, membership
    )
    genuine_opp = {(r["dimension"], r["label"]): r for r in gen_opp}

    full_prev = _rows_for_population(docs, prevalence_clusters, knobs, recency, membership)
    gen_prev = _rows_for_population(
        genuine_docs, prevalence_clusters, knobs, recency, membership
    )
    genuine_prev = {(r["dimension"], r["label"]): r for r in gen_prev}

    source_of = {d.doc_id: d.source for d in docs}
    opportunities: list[dict[str, Any]] = []
    for row in full_opp:
        key = (row["dimension"], row["label"])
        row = _attach_genuine(row, genuine_opp.get(key))
        supporting = row["supporting_doc_ids"].split(";") if row["supporting_doc_ids"] else []
        for source in sources:
            sourced = sum(1 for doc_id in supporting if source_of.get(doc_id) == source)
            denom = source_totals[source]
            row[f"prevalence_{source}"] = sourced / denom if denom else 0.0
        opportunities.append(row)
    opportunities.sort(key=_rank_key)

    prevalence_full = []
    for row in full_prev:
        key = (row["dimension"], row["label"])
        prevalence_full.append(_attach_genuine(row, genuine_prev.get(key)))
    prevalence_full.sort(key=_rank_key)
    prevalence = [{col: row[col] for col in PREVALENCE_COLUMNS} for row in prevalence_full]

    return QuantifyResult(
        opportunities=opportunities,
        prevalence=prevalence,
        cooccurrence_lift=lifts,
        segment_matrix=segments,
        sources=sources,
        n_docs=total,
        n_genuine=len(genuine_docs),
        knobs=knobs,
    )


def score_opportunities(
    docs: list[AnalyzableDoc],
    *,
    knobs: QuantifyKnobs | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Back-compat wrapper: opportunity rows and the source column order."""
    result = quantify(docs, knobs=knobs)
    return result.opportunities, result.sources
