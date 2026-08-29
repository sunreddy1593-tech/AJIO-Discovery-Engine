"""AJIO's own aggregate numbers, kept deliberately outside the text corpus.

AJIO publishes no free-text reviews or Q&A anywhere on site (`edge-case.md`
§1.1.13f), which is why ``ajio_manual`` is disabled. What it does publish is a
star-rating distribution and fit/quality percentage breakdowns, and a browser
grabber saves those as one JSON per product under ``aggregates_dir/ajio``. This
module is the only reader of that directory.

**These records are not documents and this module is not a collector.**
``ajio_aggregate`` is absent from ``SOURCE_STAGE``, ``KNOWN_SOURCES``,
``STAGE_BY_CONTENT_TYPE``, the collector registry, ``run_collection`` and the
audit's source counts, and a test asserts it stays absent. Nothing here imports
collector code, and nothing here produces a ``RawRecord`` or a ``Document``.

The reason for the wall is arithmetic rather than tidiness. A document is one
person saying one thing, and every metric downstream counts people: prevalence,
distinct authors, per-author caps. One row here summarises hundreds of raters, so
admitting it to the corpus would weight a crowd as an individual and inflate
whatever it agreed with, with nothing in the funnel to show it happened. Read
alongside the corpus instead, the same row is a real cross-check: AJIO's own
buyers on fit, beside what the text says about fit.

Phase 6 is the only consumer (``src/synthesize/ajio_aggregates.py``). Numbers are
quoted as numbers, attributed to AJIO, and never rendered as a review-like
sentence.

**A bad file costs itself and nothing else.** Two failure shapes are already on
disk — a 0-byte grab, and two JSON objects concatenated by a grab that ran twice
into the same file — so :func:`load_ajio_aggregates` skips and warns per file
rather than failing the batch. Fifty good products must not be lost to one bad
one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Which figure ``average_rating`` came from. ``None`` means it is still unknown:
#: neither reported by AJIO nor derivable, because the distribution was empty too.
#: A report citing an average has to disclose this — a derived average is a weaker
#: claim than a published one, and the two must not read alike.
AverageSource = Literal["reported", "distribution"]

#: ``opinions`` is a list of free-form prompts, so the fit and quality questions
#: are found by matching rather than by position: AJIO has changed the wording
#: ("How was the Product fit?" / "How was the fit?") and a positional read would
#: silently attribute quality percentages to fit.
FIT_QUESTION_HINTS = ("fit",)
QUALITY_QUESTION_HINTS = ("quality",)

#: Fit labels that mean the garment did not fit, in either direction. "Perfect" is
#: the only label that is not a complaint, but the two directions are kept apart
#: because they have opposite remedies: "Tight" argues for sizing up in the size
#: chart, "Loose" for the opposite.
FIT_LOOSE_LABELS = ("loose", "too loose")
FIT_TIGHT_LABELS = ("tight", "too tight")

#: Quality labels counted as a negative verdict. "Average" is deliberately not
#: here: it is the middle of AJIO's own five-point scale, and folding it into "bad"
#: would manufacture a quality problem out of indifference.
BAD_QUALITY_LABELS = ("bad", "very bad")


class Opinion(BaseModel):
    """One of AJIO's own prompts and the percentage split of the answers."""

    model_config = ConfigDict(extra="ignore")

    question: str | None = None
    options: dict[str, int] = Field(default_factory=dict)

    def share(self, labels: Sequence[str]) -> int:
        """Summed percentage for ``labels``, matched case-insensitively."""
        wanted = {label.casefold() for label in labels}
        return sum(pct for label, pct in self.options.items() if label.casefold() in wanted)

    def top_option(self) -> str | None:
        """The highest-percentage label, or None when there are no options."""
        if not self.options:
            return None
        return max(self.options.items(), key=lambda item: item[1])[0]

    def matches(self, hints: Sequence[str]) -> bool:
        question = (self.question or "").casefold()
        return any(hint in question for hint in hints)


class AjioAggregate(BaseModel):
    """One product's aggregate numbers as AJIO reports them.

    Tolerant by design on everything the grabber can legitimately fail to find:
    ``average_rating`` is null in every file collected so far, ``rating_count`` can
    be absent, and both ``rating_distribution`` and ``opinions`` can be empty. What
    is *not* tolerated is a missing ``product_id`` — without it the numbers cannot
    be attributed to anything, and a report citing them would carry a dead URL.
    """

    model_config = ConfigDict(extra="ignore")

    source: Literal["ajio_aggregate"] = "ajio_aggregate"
    product_id: str = Field(min_length=1)
    product_title: str | None = None
    url: str | None = None
    extracted_at: str | None = None
    average_rating: float | None = None
    rating_count: int | None = None
    rating_distribution: dict[str, int] = Field(default_factory=dict)
    opinions: list[Opinion] = Field(default_factory=list)
    #: Set by the validator below, never by the file on disk.
    average_rating_source: AverageSource | None = None

    @model_validator(mode="after")
    def _fill_average_from_distribution(self) -> AjioAggregate:
        """Derive the average from the star buckets, but only when none was captured.

        The mean divides by the actual sum of the buckets, not by 100. AJIO rounds
        each bucket independently, so they sum to 96–100 (median 97 across the
        collected files) and the shortfall is rounding loss rather than missing
        raters. Dividing by 100 would treat those few percent as ratings of zero
        stars — impossible on a 1–5 scale — and pull every average down by about
        0.1 in the same direction, which is exactly the kind of quiet, one-sided
        bias a report cannot disclose because nothing records it.
        """
        if self.average_rating is not None:
            self.average_rating_source = "reported"
            return self
        total = sum(self.rating_distribution.values())
        if not self.rating_distribution or total <= 0:
            self.average_rating_source = None
            return self
        weighted = sum(int(star) * pct for star, pct in self.rating_distribution.items())
        self.average_rating = round(weighted / total, 1)
        self.average_rating_source = "distribution"
        return self

    # -- the two questions the report actually asks -------------------------

    def fit_opinion(self) -> Opinion | None:
        return next((op for op in self.opinions if op.matches(FIT_QUESTION_HINTS)), None)

    def quality_opinion(self) -> Opinion | None:
        return next((op for op in self.opinions if op.matches(QUALITY_QUESTION_HINTS)), None)

    def misfit_share(self) -> int | None:
        """Percentage answering anything other than a good fit, both directions."""
        fit = self.fit_opinion()
        if fit is None or not fit.options:
            return None
        return fit.share(FIT_LOOSE_LABELS) + fit.share(FIT_TIGHT_LABELS)

    def bad_quality_share(self) -> int | None:
        """Percentage answering Bad or Very Bad. "Average" is not counted."""
        quality = self.quality_opinion()
        if quality is None or not quality.options:
            return None
        return quality.share(BAD_QUALITY_LABELS)

    def top_fit_option(self) -> str | None:
        fit = self.fit_opinion()
        return fit.top_option() if fit is not None else None


class AggregateLoad(BaseModel):
    """What one directory scan produced, including what it refused."""

    model_config = ConfigDict(extra="ignore")

    aggregates: list[AjioAggregate] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    files_skipped: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    #: Files dropped because a newer grab of the same product superseded them.
    superseded: list[str] = Field(default_factory=list)


def _sort_key(value: str | None) -> str:
    """``extracted_at`` as a sortable string; a missing stamp sorts oldest.

    ISO 8601 sorts correctly as text when the stamps share a shape, which they do
    here because one grabber writes all of them. Parsing to ``datetime`` would buy
    nothing and would add a failure mode for a field that is only ever compared.
    """
    return value or ""


def load_ajio_aggregates(directory: str | Path) -> list[AjioAggregate]:
    """Every valid aggregate in ``directory``, newest grab per product.

    Skips and warns per file, so one bad grab cannot cost the batch. Use
    :func:`scan_ajio_aggregates` when the warnings matter to the caller.
    """
    return scan_ajio_aggregates(directory).aggregates


def scan_ajio_aggregates(directory: str | Path) -> AggregateLoad:
    """:func:`load_ajio_aggregates` with the warnings and the skip list kept.

    Only ``*.json`` is read. README and anything else in the directory is ignored
    rather than warned about, because documentation sitting beside data is normal
    and a warning per run would train the reader to skip warnings.
    """
    directory = Path(directory)
    result = AggregateLoad()
    if not directory.is_dir():
        result.warnings.append(f"{directory} does not exist; no AJIO aggregates loaded")
        return result

    newest: dict[str, tuple[str, AjioAggregate]] = {}
    for path in sorted(directory.glob("*.json")):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if not raw.strip():
            # A 0-byte file is a grab that produced nothing. Named rather than
            # ignored: the product still needs re-grabbing, and silence here is
            # indistinguishable from a product that has no ratings.
            result.files_skipped.append(path.name)
            result.warnings.append(f"{path.name}: file is empty ({len(raw)} bytes); re-grab it")
            continue
        try:
            payload: Any = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            detail = getattr(exc, "msg", str(exc))
            result.files_skipped.append(path.name)
            result.warnings.append(f"{path.name}: invalid JSON ({detail})")
            continue
        if not isinstance(payload, dict):
            result.files_skipped.append(path.name)
            result.warnings.append(
                f"{path.name}: expected one object per file, got {type(payload).__name__}"
            )
            continue
        try:
            aggregate = AjioAggregate.model_validate(payload)
        except Exception as exc:  # pydantic ValidationError, or a bad star key
            result.files_skipped.append(path.name)
            result.warnings.append(f"{path.name}: {_first_error(exc)}")
            continue

        result.files_read.append(path.name)
        stamp = _sort_key(aggregate.extracted_at)
        known = newest.get(aggregate.product_id)
        if known is None or stamp >= known[0]:
            if known is not None:
                result.superseded.append(known[1].product_id)
            newest[aggregate.product_id] = (stamp, aggregate)
        else:
            result.superseded.append(aggregate.product_id)

    result.aggregates = [
        aggregate for _, aggregate in sorted(newest.values(), key=lambda item: item[1].product_id)
    ]
    return result


def _first_error(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
            location = ".".join(str(part) for part in first.get("loc", ()))
            return f"{location or 'record'}: {first.get('msg', 'invalid')}"
        except (IndexError, TypeError):
            pass
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# Summaries for the report
# --------------------------------------------------------------------------


def by_product_id(aggregates: Iterable[AjioAggregate]) -> dict[str, AjioAggregate]:
    return {aggregate.product_id: aggregate for aggregate in aggregates}


class AggregateSummary(BaseModel):
    """Corpus-level figures for the aggregates, for citing beside text themes.

    Every share carries its own denominator. Products differ in which prompts AJIO
    answered, so a fit figure and a quality figure can rest on different product
    counts, and quoting one denominator for both would misstate whichever is
    smaller.
    """

    model_config = ConfigDict(extra="ignore")

    products: int = 0
    #: Products whose most-answered fit option is a misfit rather than "Perfect".
    products_with_fit: int = 0
    top_fit_is_loose: int = 0
    top_fit_is_tight: int = 0
    mean_misfit_pct: float | None = None
    #: Products where AJIO answered the quality prompt.
    products_with_quality: int = 0
    mean_bad_quality_pct: float | None = None
    #: Averages, split by where the figure came from.
    mean_average_rating: float | None = None
    ratings_reported: int = 0
    ratings_derived: int = 0
    ratings_unknown: int = 0

    @property
    def top_fit_is_misfit(self) -> int:
        return self.top_fit_is_loose + self.top_fit_is_tight

    @property
    def misfit_share_of_products(self) -> float | None:
        if not self.products_with_fit:
            return None
        return self.top_fit_is_misfit / self.products_with_fit


def summarize(aggregates: Sequence[AjioAggregate]) -> AggregateSummary:
    """Roll a product list up into the figures Phase 6 cites."""
    summary = AggregateSummary(products=len(aggregates))

    misfits: list[int] = []
    bad_quality: list[int] = []
    averages: list[float] = []

    for aggregate in aggregates:
        top = aggregate.top_fit_option()
        if top is not None:
            summary.products_with_fit += 1
            folded = top.casefold()
            if folded in FIT_LOOSE_LABELS:
                summary.top_fit_is_loose += 1
            elif folded in FIT_TIGHT_LABELS:
                summary.top_fit_is_tight += 1
        misfit = aggregate.misfit_share()
        if misfit is not None:
            misfits.append(misfit)

        bad = aggregate.bad_quality_share()
        if bad is not None:
            summary.products_with_quality += 1
            bad_quality.append(bad)

        if aggregate.average_rating_source == "reported":
            summary.ratings_reported += 1
        elif aggregate.average_rating_source == "distribution":
            summary.ratings_derived += 1
        else:
            summary.ratings_unknown += 1
        if aggregate.average_rating is not None:
            averages.append(aggregate.average_rating)

    if misfits:
        summary.mean_misfit_pct = round(sum(misfits) / len(misfits), 1)
    if bad_quality:
        summary.mean_bad_quality_pct = round(sum(bad_quality) / len(bad_quality), 1)
    if averages:
        summary.mean_average_rating = round(sum(averages) / len(averages), 2)
    return summary
