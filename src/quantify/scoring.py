"""Opportunity score and the named constants behind it (architecture.md §8.3).

    score = 100 × sqrt(prevalence_norm) × severity_norm × actionability × evidence_confidence

Every factor is 0–1, so the product stays on a 0–100 scale and a reader can
re-weight from the CSV columns without reverse-engineering a black box.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

#: Taxonomy severity is a 1–5 Likert (``DocumentTags.severity``).
SEVERITY_MAX = 5
#: ``DocumentTags.confidence_pct`` is an integer percent 0–100.
CONFIDENCE_MAX = 100
#: Architecture.md §8.3 writes the ranking on a 0–100 scale.
SCORE_SCALE = 100
#: Recency decay half-life on ``created_utc`` (architecture.md §8.1).
HALF_LIFE_DAYS = 365
#: z for a Wilson 95% interval.
Z95 = 1.96
#: Exit criterion: fewer supporting documents than this is ``low_confidence``.
LOW_CONFIDENCE_N = 20
#: Tags above this prevalence are likelier a taxonomy problem than a finding.
HIGH_PREVALENCE = 0.90
#: Merge tags whose supporting-document Jaccard is at least this (edge-case §5.11).
CLUSTER_JACCARD_MIN = 0.5

WEIGHTING_NOTE = (
    "opportunity_score = 100 × sqrt(prevalence_norm) × (mean_severity / 5) × "
    "mean_actionability × evidence_confidence; prevalence_norm is the "
    "author-weighted, recency-weighted share min-max'd across candidates "
    "(12-month half-life); evidence_confidence = (mean_confidence/100) × "
    "source_spread × (Wilson lower / prevalence) × attribution_factor; "
    "post_purchase_only clusters are ranked below pre-purchase-supported ones; "
    "ajio_aggregate is never an input"
)


def recency_weight(age_days: float, *, half_life_days: float = HALF_LIFE_DAYS) -> float:
    """Exponential decay: a document one half-life old contributes 0.5."""
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def wilson_interval(
    successes: int,
    n: int,
    *,
    z: float = Z95,
) -> tuple[float, float, float]:
    """Wilson 95% interval on a binomial proportion.

    Returns ``(point, lower, upper)``. Small-n tags get a wide interval rather
    than a falsely precise prevalence (architecture.md §8.1).
    """
    if n <= 0:
        return 0.0, 0.0, 0.0
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n) / denom
    return p, max(0.0, centre - margin), min(1.0, centre + margin)


def sample_size_factor(prevalence: float, lower: float) -> float:
    """Wilson lower bound ÷ point estimate, capped at 1.

    A tag on 3 of 800 documents has a point prevalence but a much lower bound;
    dividing pulls ``evidence_confidence`` down so small-n cannot lead the
    ranking on volume alone. A zero prevalence contributes nothing.
    """
    if prevalence <= 0:
        return 0.0
    return min(1.0, lower / prevalence)


def source_spread_factor(n_sources_with_tag: int, n_sources_total: int) -> float:
    """Share of the analyzable source mix that surfaces this tag.

    A blocker seen in one of six sources is likelier to be a platform bug than
    a category-wide need (architecture.md §8.1).
    """
    if n_sources_total <= 0:
        return 0.0
    return n_sources_with_tag / n_sources_total


def min_max_normalize(values: Sequence[float]) -> list[float]:
    """Map a list onto [0, 1] across candidates. Equal values all become 1.

    Architecture.md §8.3 min-max-normalises prevalence across candidates so
    the square root is taken of a relative share, not a raw rate. If every
    candidate has the same share we must not send them all to 0.
    """
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return [1.0] * len(values)
    span = hi - lo
    return [(v - lo) / span for v in values]


def evidence_confidence(
    mean_confidence_pct: float,
    source_spread: float,
    sample_size: float,
    attribution_factor: float,
    *,
    confidence_max: float = CONFIDENCE_MAX,
) -> float:
    """§8.3 evidence_confidence: four 0–1 factors, multiplied not averaged.

    ``mean_confidence_pct / confidence_max`` is the tagger's self-report.
    ``source_spread`` is how widely the tag appears. ``sample_size`` is the
    Wilson lower bound over the point prevalence. ``attribution_factor`` is
    the share of supporting documents whose evidence carries no screen flag
    — a component, not a filter, so a heavily flagged cluster stays visible
    and countable but cannot lead on volume alone.
    """
    if confidence_max <= 0:
        raise ValueError("confidence_max must be positive")
    tagger = mean_confidence_pct / confidence_max
    return tagger * source_spread * sample_size * attribution_factor


def opportunity_score(
    prevalence_norm: float,
    mean_severity: float,
    mean_actionability: float,
    evidence_confidence_value: float,
    *,
    severity_max: float = SEVERITY_MAX,
    scale: float = SCORE_SCALE,
) -> float:
    """A frequent, painful, cheaply-fixable, well-evidenced theme ranks highest.

        score = scale
              × sqrt(prevalence_norm)
              × (mean_severity / severity_max)
              × mean_actionability
              × evidence_confidence

    The square root dampens pure volume so a widespread-but-mild annoyance
    does not outrank a severe blocker affecting a large minority. A cluster
    driven purely by ``price_absolute`` has ``mean_actionability`` near 0 and
    collapses. ``prevalence_norm`` is already min-max'd across candidates.
    """
    if severity_max <= 0:
        raise ValueError("severity_max must be positive")
    if prevalence_norm < 0:
        raise ValueError("prevalence_norm must be >= 0")
    return (
        scale
        * math.sqrt(prevalence_norm)
        * (mean_severity / severity_max)
        * mean_actionability
        * evidence_confidence_value
    )
