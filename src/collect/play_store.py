"""Play Store reviews for AJIO and Myntra via ``google-play-scraper``.

Myntra is collected as a comparison set, not as padding: a blocker that shows up
on both apps is a category-level problem, while one that shows up only on AJIO is
a product problem, and the distinction changes what the report recommends.

**Stage is `mixed`, and mostly not what we want.** Store reviews skew heavily
toward app bugs, crashes, and one-line praise. High volume makes them worth
collecting — Phase 3's hard exclusions will cut most of the noise — but they are
not evidence about wishlist deliberation on their own.

Pagination is the one real hazard: continuation tokens can expire or loop
(`edge-case.md` §1.1.4), so ids seen in this run are tracked and a page that
yields nothing new ends the walk rather than trusting the token.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, ClassVar

from src.collect.base import Collector
from src.collect.scraping import ParsedItem
from src.common.logging import get_logger
from src.common.schemas import RawRecord

logger = get_logger("collect.play_store")

BATCH_SIZE = 200

#: Above this the token is looping rather than paginating.
MAX_PAGES_PER_APP = 200

ReviewsFn = Callable[..., tuple[list[dict[str, Any]], Any]]


def default_reviews_fn() -> ReviewsFn:
    from google_play_scraper import Sort, reviews

    def fetch(app_id: str, *, lang: str, country: str, count: int, token: Any) -> Any:
        return reviews(
            app_id,
            lang=lang,
            country=country,
            sort=Sort.NEWEST,
            count=count,
            continuation_token=token,
        )

    return fetch


def map_review(row: dict[str, Any], *, app_id: str, lang: str, country: str) -> ParsedItem | None:
    """One library row to a ParsedItem, or None if it carries no text.

    ``replyContent`` is kept in ``meta`` rather than as its own document: it is the
    brand speaking, not a user, and counting it as user voice would inflate
    prevalence with AJIO's own words.
    """
    review_id = row.get("reviewId")
    content = row.get("content")
    if not review_id or not content:
        return None

    return ParsedItem(
        native_id=str(review_id),
        text=str(content),
        url=f"https://play.google.com/store/apps/details?id={app_id}&reviewId={review_id}",
        author=row.get("userName"),
        created_raw=None,
        meta={
            "app_id": app_id,
            "brand": "ajio" if "ajio" in app_id else "comparison",
            "rating": row.get("score"),
            "thumbs_up": row.get("thumbsUpCount"),
            "app_version": row.get("reviewCreatedVersion"),
            "reply": bool(row.get("replyContent")),
            "lang": lang,
            "country": country,
        },
    )


class PlayStoreCollector(Collector):
    """Newest-first review pages per configured app id.

    The scraping library owns its own HTTP, so this collector is not routed
    through ``PoliteSession``; it inherits the library's politeness and this run's
    record caps instead. That is a deliberate exception to "every source goes
    through the robots gate", and it is limited to library-mediated store APIs.
    """

    source: ClassVar[str] = "play_store"
    min_expected_records: ClassVar[int] = 200

    def __init__(self, reviews_fn: ReviewsFn | None = None):
        super().__init__()
        self._reviews_fn = reviews_fn or default_reviews_fn()
        self.pages_fetched = 0

    def fetch(self, cfg: Any) -> Iterator[RawRecord]:
        per_app = max(1, cfg.max_reviews // max(1, len(cfg.app_ids)))
        for app_id in cfg.app_ids:
            for lang in cfg.languages:
                for country in cfg.countries:
                    yield from self._app_reviews(app_id, lang, country, per_app)

    def _app_reviews(
        self, app_id: str, lang: str, country: str, cap: int
    ) -> Iterator[RawRecord]:
        token: Any = None
        seen: set[str] = set()
        produced = 0

        for page in range(MAX_PAGES_PER_APP):
            remaining = cap - produced
            if remaining <= 0:
                return

            try:
                rows, token = self._reviews_fn(
                    app_id,
                    lang=lang,
                    country=country,
                    count=min(BATCH_SIZE, remaining),
                    token=token,
                )
            except Exception as exc:
                # A store-side failure part way through is a data problem: keep
                # what we have rather than losing the whole app's reviews.
                self.log.warning("play store fetch failed for %s page %s: %s", app_id, page, exc)
                return

            self.pages_fetched += 1
            if not rows:
                return

            fresh = 0
            for row in rows:
                item = map_review(row, app_id=app_id, lang=lang, country=country)
                if item is None or item.native_id in seen:
                    continue
                seen.add(item.native_id)
                fresh += 1

                record = self.build(
                    source_native_id=item.native_id,
                    text=item.text,
                    url=item.url,
                    author_raw=item.author,
                    created_utc=row.get("at"),
                    meta=item.meta,
                )
                if record is not None:
                    yield record
                    produced += 1

            if fresh == 0:
                self.log.info("%s page %s returned only seen ids; token is looping", app_id, page)
                return
            if token is None:
                return


__all__ = ["BATCH_SIZE", "PlayStoreCollector", "map_review"]
