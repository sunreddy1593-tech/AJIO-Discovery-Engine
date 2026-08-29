"""Lift, Jaccard, and cluster merge (architecture.md §8.2, edge-case.md §5.11)."""

from src.quantify.cooccurrence import cluster_tags, jaccard, lift, lift_rows, segment_matrix_rows


def test_lift_is_none_when_a_marginal_is_zero():
    assert lift(0, 0, 4, 10) is None
    assert lift(0, 4, 0, 10) is None
    assert lift(2, 4, 4, 0) is None


def test_jaccard_is_intersection_over_union():
    assert jaccard(["a", "b"], ["b", "c"]) == 1 / 3
    assert jaccard(["a"], ["a"]) == 1.0
    assert jaccard([], []) == 0.0


def test_cluster_tags_merges_above_threshold_and_prefers_the_blocker():
    membership = {
        ("blocker_type", "fit_size_uncertainty"): ["d1", "d2"],
        ("uncertainty_type", "will_it_fit"): ["d1", "d2"],
        ("blocker_type", "quality_doubt"): ["d3"],
    }
    clusters = cluster_tags(membership, jaccard_min=0.5)
    by_label = {c.label: c for c in clusters}
    assert "will_it_fit" not in by_label
    assert by_label["fit_size_uncertainty"].members == (
        ("blocker_type", "fit_size_uncertainty"),
        ("uncertainty_type", "will_it_fit"),
    )
    assert by_label["quality_doubt"].doc_ids == frozenset(["d3"])


def test_cluster_tags_disabled_when_threshold_is_zero():
    membership = {
        ("blocker_type", "fit_size_uncertainty"): ["d1", "d2"],
        ("uncertainty_type", "will_it_fit"): ["d1", "d2"],
    }
    clusters = cluster_tags(membership, jaccard_min=0)
    assert {c.label for c in clusters} == {"fit_size_uncertainty", "will_it_fit"}


def test_empty_segment_is_omitted_from_the_matrix():
    membership = {("blocker_type", "return_friction"): ["d1"]}
    assert segment_matrix_rows(membership, 1) == []
    assert lift_rows(membership, 1) == []
