"""Opportunity score arithmetic (architecture.md §8.3, plan §5)."""

from __future__ import annotations

import math

import pytest

from src.quantify.scoring import (
    SCORE_SCALE,
    SEVERITY_MAX,
    evidence_confidence,
    min_max_normalize,
    opportunity_score,
    recency_weight,
    sample_size_factor,
    source_spread_factor,
    wilson_interval,
)


def test_score_is_sqrt_norm_times_severity_actionability_and_confidence():
    expected = SCORE_SCALE * math.sqrt(0.5) * (4 / SEVERITY_MAX) * 1.0 * 1.0
    assert opportunity_score(0.5, 4.0, 1.0, 1.0) == pytest.approx(expected)


def test_score_is_monotonic_in_each_component():
    base = opportunity_score(0.4, 3.0, 0.5, 0.8)
    assert opportunity_score(0.5, 3.0, 0.5, 0.8) > base
    assert opportunity_score(0.4, 4.0, 0.5, 0.8) > base
    assert opportunity_score(0.4, 3.0, 1.0, 0.8) > base
    assert opportunity_score(0.4, 3.0, 0.5, 0.9) > base


def test_sqrt_dampens_volume_so_doubling_prevalence_norm_does_not_double_the_score():
    low = opportunity_score(0.2, 5.0, 1.0, 1.0)
    high = opportunity_score(0.4, 5.0, 1.0, 1.0)
    assert high / low == pytest.approx(math.sqrt(2))


def test_a_price_only_cluster_scores_near_zero():
    assert opportunity_score(1.0, 5.0, 0.0, 1.0) == 0.0


def test_recency_weight_halves_after_one_half_life():
    assert recency_weight(0) == 1.0
    assert recency_weight(365) == pytest.approx(0.5)
    assert recency_weight(730) == pytest.approx(0.25)


def test_wilson_boundaries_do_not_raise():
    assert wilson_interval(0, 0) == (0.0, 0.0, 0.0)
    p, lo, hi = wilson_interval(0, 10)
    assert p == 0.0
    assert lo == 0.0
    assert 0 < hi <= 1
    p, lo, hi = wilson_interval(10, 10)
    assert p == 1.0
    assert hi == 1.0
    assert 0 <= lo < 1


def test_wilson_interval_contains_the_point_and_widens_for_small_n():
    p, lo, hi = wilson_interval(10, 20)
    assert lo < p < hi
    assert p == pytest.approx(0.5)
    _, lo_small, hi_small = wilson_interval(1, 2)
    assert (hi_small - lo_small) > (hi - lo)


def test_sample_size_factor_is_wilson_lower_over_point():
    p, lo, _ = wilson_interval(10, 20)
    assert sample_size_factor(p, lo) == pytest.approx(lo / p)
    assert sample_size_factor(0.0, 0.0) == 0.0


def test_source_spread_factor_is_share_of_sources():
    assert source_spread_factor(1, 6) == pytest.approx(1 / 6)
    assert source_spread_factor(6, 6) == 1.0


def test_min_max_normalize_spans_unit_interval_and_does_not_zero_ties():
    assert min_max_normalize([0.1, 0.3, 0.5]) == pytest.approx([0.0, 0.5, 1.0])
    assert min_max_normalize([0.2, 0.2, 0.2]) == [1.0, 1.0, 1.0]


def test_evidence_confidence_is_the_product_of_four_unit_factors():
    assert evidence_confidence(80, 0.5, 0.5, 1.0) == pytest.approx(0.8 * 0.5 * 0.5 * 1.0)
    assert evidence_confidence(100, 1.0, 1.0, 0.0) == 0.0
