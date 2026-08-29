"""Phase 6's aggregate section: labelled as AJIO's, cross-referenced, never a quote.

The section exists to corroborate a text theme with AJIO's own numbers. Its two
mandatory disclosures are tested here, because without them the block reads as
corpus evidence: whose numbers these are, and — when an average is cited — whether
it was reported by AJIO or derived from the star distribution.
"""

from __future__ import annotations

from src.store.aggregates import AjioAggregate
from src.synthesize.ajio_aggregates import Theme, cross_reference, render_section

FIT = {
    "question": "How was the Product fit?",
    "options": {"Perfect": 40, "Loose": 12, "Tight": 30, "Too Loose": 3, "Too Tight": 12},
}
QUALITY = {
    "question": "How was the Product Quality?",
    "options": {"Excellent": 27, "Very Good": 29, "Average": 32, "Bad": 5, "Very Bad": 5},
}


def aggregate(product_id="410334633", **overrides):
    payload = {
        "source": "ajio_aggregate",
        "product_id": product_id,
        "url": f"https://www.ajio.com/p/{product_id}",
        "extracted_at": "2026-08-23T17:56:20Z",
        "average_rating": None,
        "rating_count": 59,
        "rating_distribution": {"5": 54, "4": 16, "3": 11, "2": 3, "1": 13},
        "opinions": [FIT, QUALITY],
    }
    payload.update(overrides)
    return AjioAggregate.model_validate(payload)


def test_the_section_says_whose_numbers_these_are():
    """Unlabelled, the block reads as corpus evidence — which is the whole risk."""
    out = render_section(aggregates=[aggregate()])

    assert "## AJIO on-site aggregates" in out
    assert "not corpus documents" in out
    assert "post-purchase and self-selected" in out
    assert "1 product(s)" in out


def test_a_cited_average_discloses_where_it_came_from():
    """A derived average is a weaker claim and must not read like a published one."""
    out = render_section(aggregates=[aggregate(), aggregate("2", average_rating=4.2)])

    assert "derived here as the weighted mean" in out
    assert "1 reported by AJIO" in out
    # And the reason the denominator is the buckets' sum, so a reader can check it.
    assert "sum to 96–100%" in out


def test_a_fit_theme_is_cross_referenced_against_ajios_fit_skew():
    reference = cross_reference(Theme("fit_size_uncertainty", documents=1200), [aggregate()])

    assert reference.kind == "fit"
    assert reference.corroborates
    assert "misfit" in reference.detail
    assert "tight" in reference.detail


def test_a_quality_theme_is_cross_referenced_against_the_quality_breakdown():
    reference = cross_reference(Theme("quality_doubt", documents=300), [aggregate()])

    assert reference.kind == "quality"
    assert "Bad + Very Bad" in reference.detail
    # AJIO's own middle option is not folded into "bad".
    assert "Average, is not counted" in reference.detail


def test_a_theme_ajio_asks_nothing_about_is_reported_as_uncorroborated():
    """Stated, not omitted: silence must not look like an unflattering number hidden."""
    reference = cross_reference(Theme("return_friction", documents=800), [aggregate()])

    assert reference.kind == "none"
    assert not reference.corroborates
    assert "no aggregate prompt" in reference.detail

    out = render_section([Theme("return_friction", documents=800)], aggregates=[aggregate()])
    assert "not corroborated" in out


def test_a_human_theme_label_resolves_as_well_as_a_taxonomy_value():
    """The report may print "sizing and fit" rather than the enum value."""
    assert cross_reference(Theme("sizing and fit"), [aggregate()]).kind == "fit"
    assert cross_reference(Theme("Product quality doubts"), [aggregate()]).kind == "quality"


def test_no_aggregates_renders_an_explicit_absence():
    """A missing side-channel is a fact about the evidence base, so it is stated."""
    out = render_section([Theme("fit_size_uncertainty")], aggregates=[])

    assert "No aggregate records were available" in out
    assert "sole evidence base" in out


def test_the_section_imports_no_tagging_or_quantification_path():
    """Aggregates reach the report and nothing else — asserted on the import graph."""
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "synthesize" / "ajio_aggregates.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    forbidden = {
        "src.tag.run_tagging",
        "src.tag.llm_client",
        "src.tag.cache",
        "src.store.build_corpus",
        "src.store.dedupe",
        "src.store.relevance",
    }
    assert not (modules & forbidden), modules & forbidden
    assert not any(module.startswith("src.collect") for module in modules)
    # The taxonomy is enums only, imported so a theme rename fails a test here
    # rather than quietly ending the cross-reference.
    assert "src.tag.taxonomy" in modules
