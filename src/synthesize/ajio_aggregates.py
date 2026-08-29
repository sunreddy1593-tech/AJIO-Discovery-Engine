"""Phase 6 evidence section: AJIO's own aggregate numbers beside the text themes.

The corpus says what people write; this says what AJIO's buyers answered when the
site asked them directly. Putting the two next to each other is the whole value:
"fit uncertainty is the top blocker in the text" is a stronger claim when AJIO's
own fit breakdown skews the same way, and a claim that needs rewriting when it
does not.

**These are not documents and they are not tags.** They enter the report here and
nowhere else. Nothing in this module touches tagging or document quantification,
and no number is ever rendered as a review-like sentence — a percentage is quoted
as a percentage, attributed to AJIO, with the product count it rests on. The
reader (`src/store/aggregates.py`) explains why the wall is arithmetic rather than
stylistic: one row summarises hundreds of raters, so counting it as a voice would
weight a crowd as an individual.

Two disclosures are mandatory in the rendered output rather than optional, because
without them the section reads as if it were corpus evidence:

- **whose numbers these are** — AJIO-reported, from buyers who answered AJIO's own
  prompts, self-selected among people who already purchased. That makes them
  post-purchase evidence about a pre-purchase question, which is exactly why they
  corroborate rather than prove.
- **where any average came from** — reported by AJIO, or derived here from the star
  distribution. A derived average is a weaker claim and must not read like a
  published one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.store.aggregates import (
    AggregateSummary,
    AjioAggregate,
    load_ajio_aggregates,
    summarize,
)
from src.tag.taxonomy import BlockerType, UncertaintyType

#: Theme names this section can corroborate, by the aggregate view that speaks to
#: them. Matching is on substrings of the theme name so both a taxonomy value
#: (``fit_size_uncertainty``) and a human label ("sizing and fit") resolve; the
#: canonical values are listed so a taxonomy rename shows up here as a test
#: failure rather than as a section that quietly stops cross-referencing.
FIT_THEME_HINTS = (
    BlockerType.FIT_SIZE_UNCERTAINTY.value,
    UncertaintyType.WILL_IT_FIT.value,
    "fit",
    "size",
    "sizing",
)
QUALITY_THEME_HINTS = (
    BlockerType.QUALITY_DOUBT.value,
    UncertaintyType.IS_QUALITY_WORTH_IT.value,
    "quality",
)

AGGREGATE_PROVENANCE = (
    "AJIO-reported aggregates, not corpus documents: percentages AJIO computed "
    "from buyers who answered its own on-site prompts. They are post-purchase and "
    "self-selected, so they corroborate a text theme rather than establishing one, "
    "and no document, tag or prevalence figure anywhere in this report is derived "
    "from them."
)


@dataclass(frozen=True)
class Theme:
    """One ranked theme from the text corpus, as the report will pass it in.

    Only the fields this section needs. ``name`` is a taxonomy value or the label
    the report prints; ``documents`` and ``prevalence`` are carried so the section
    can state the corpus side of the comparison without recomputing it, since the
    ranked list is Phase 6's own output.
    """

    name: str
    documents: int = 0
    prevalence: float | None = None


@dataclass(frozen=True)
class CrossReference:
    """What the aggregates say about one text theme, or why they say nothing."""

    theme: Theme
    kind: str  # "fit", "quality", or "none"
    products: int = 0
    detail: str = ""

    @property
    def corroborates(self) -> bool:
        return self.kind != "none" and self.products > 0


def _matches(theme: Theme, hints: Sequence[str]) -> bool:
    name = theme.name.casefold()
    return any(hint.casefold() in name for hint in hints)


def cross_reference(theme: Theme, aggregates: Sequence[AjioAggregate]) -> CrossReference:
    """Pair one theme with the aggregate view that speaks to it, if any.

    A theme with no aggregate view returns ``kind="none"`` and says so. That is
    reported rather than omitted: a reader comparing sections needs to know a theme
    went uncorroborated because AJIO asks no question about it, not because the
    number was unflattering.
    """
    summary = summarize(aggregates)

    if _matches(theme, FIT_THEME_HINTS):
        if not summary.products_with_fit:
            return CrossReference(theme, "fit", 0, "AJIO answered no fit prompt on any product")
        share = summary.misfit_share_of_products
        parts = [
            f"{summary.products_with_fit} product(s) carry AJIO's fit prompt",
            f"mean misfit response {summary.mean_misfit_pct}%"
            if summary.mean_misfit_pct is not None
            else "",
            f"{summary.top_fit_is_misfit} of {summary.products_with_fit} "
            f"({share:.0%}) have a misfit option as their most-answered"
            if share is not None
            else "",
            f"skew: {summary.top_fit_is_loose} loose, {summary.top_fit_is_tight} tight",
        ]
        return CrossReference(
            theme, "fit", summary.products_with_fit, "; ".join(p for p in parts if p)
        )

    if _matches(theme, QUALITY_THEME_HINTS):
        if not summary.products_with_quality:
            return CrossReference(
                theme, "quality", 0, "AJIO answered no quality prompt on any product"
            )
        return CrossReference(
            theme,
            "quality",
            summary.products_with_quality,
            f"{summary.products_with_quality} product(s) carry AJIO's quality prompt; "
            f"mean Bad + Very Bad {summary.mean_bad_quality_pct}% "
            f"(AJIO's own middle option, Average, is not counted as bad)",
        )

    return CrossReference(
        theme, "none", 0, "AJIO publishes no aggregate prompt covering this theme"
    )


def _average_line(summary: AggregateSummary) -> str:
    """The mean average rating, with its provenance spelled out.

    Never a bare number. An average derived from the star distribution is a weaker
    claim than one AJIO published, and the counts below are what let a reader tell
    which they are looking at.
    """
    if summary.mean_average_rating is None:
        return (
            "- **Average rating:** not stated. No product reported an average and "
            "none had a star distribution to derive one from."
        )
    provenance: list[str] = []
    if summary.ratings_reported:
        provenance.append(f"{summary.ratings_reported} reported by AJIO")
    if summary.ratings_derived:
        provenance.append(
            f"{summary.ratings_derived} derived here as the weighted mean of the "
            "star distribution"
        )
    if summary.ratings_unknown:
        provenance.append(f"{summary.ratings_unknown} unknown and excluded")
    return (
        f"- **Average rating:** {summary.mean_average_rating} across "
        f"{summary.ratings_reported + summary.ratings_derived} product(s) "
        f"({', '.join(provenance)}). AJIO's star buckets are individually rounded "
        "and sum to 96–100%, so a derived mean divides by their actual sum rather "
        "than by 100; dividing by 100 would count the rounding shortfall as "
        "zero-star ratings and understate every average."
    )


def render_section(
    themes: Iterable[Theme] = (),
    *,
    aggregates: Sequence[AjioAggregate] | None = None,
    aggregates_dir: str | Path | None = None,
) -> str:
    """The "AJIO on-site aggregates" section, as markdown.

    Pass ``aggregates`` directly, or ``aggregates_dir`` to load them. With neither,
    the section renders as explicitly empty rather than being silently dropped —
    a missing side-channel is a fact about the evidence base, and the report says
    so instead of leaving a reader to assume it was never collected.
    """
    if aggregates is None:
        aggregates = (
            load_ajio_aggregates(Path(aggregates_dir) / "ajio")
            if aggregates_dir is not None
            else []
        )
    summary = summarize(aggregates)

    lines = ["## AJIO on-site aggregates", "", AGGREGATE_PROVENANCE, ""]

    if not summary.products:
        lines.append(
            "No aggregate records were available, so no AJIO-reported figure is cited "
            "anywhere in this report. The text corpus is the sole evidence base for "
            "every claim."
        )
        return "\n".join(lines) + "\n"

    lines.append(f"Coverage: **{summary.products} product(s)**.")
    lines.append("")
    lines.append(_average_line(summary))
    if summary.products_with_fit:
        lines.append(
            f"- **Fit:** {summary.products_with_fit} product(s) carry the fit prompt; "
            f"mean misfit response {summary.mean_misfit_pct}%, with "
            f"{summary.top_fit_is_loose} product(s) skewing loose and "
            f"{summary.top_fit_is_tight} skewing tight."
        )
    if summary.products_with_quality:
        lines.append(
            f"- **Quality:** {summary.products_with_quality} product(s) carry the "
            f"quality prompt; mean Bad + Very Bad {summary.mean_bad_quality_pct}%."
        )

    themes = list(themes)
    if themes:
        lines.extend(["", "### Cross-reference against the text themes", ""])
        for theme in themes:
            reference = cross_reference(theme, aggregates)
            corpus = f"{theme.documents} document(s)" if theme.documents else "corpus count n/a"
            if theme.prevalence is not None:
                corpus += f", prevalence {theme.prevalence:.1%}"
            verdict = "corroborated" if reference.corroborates else "not corroborated"
            lines.append(f"- **{theme.name}** ({corpus}) — {verdict}: {reference.detail}.")

    return "\n".join(lines) + "\n"
