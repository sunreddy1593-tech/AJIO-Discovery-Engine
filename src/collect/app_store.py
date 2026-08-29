"""App Store reviews via the public iTunes customer-reviews RSS feed.

The feed is a published JSON endpoint rather than a scraped page, but it is still
routed through the robots gate and the polite session. Treating it as an exception
would mean one source with its own rules, and the point of ``scraping.py`` is that
there is exactly one fetch path to audit.

**The ~500-review ceiling is structural, not a bug.** The feed serves 10 pages of
roughly 50 entries and then stops (`edge-case.md` §1.1.3). Retrying past it wastes
requests, so the cap is recorded in the manifest instead, where the report's
limitations section can cite it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

from src.collect.base import Collector
from src.collect.scraping import HttpError, ParsedItem, PoliteSession, RobotsDisallowed
from src.common.logging import get_logger
from src.common.schemas import RawRecord

logger = get_logger("collect.app_store")

FEED_TEMPLATE = (
    "https://itunes.apple.com/{country}/rss/customerreviews/"
    "page={page}/id={app_id}/sortby=mostrecent/json"
)

#: The feed's own limit; page 11 returns an error or repeats page 10.
MAX_FEED_PAGES = 10


def parse_feed_page(payload: Any, *, app_id: str, country: str) -> list[ParsedItem]:
    """Entries from one RSS-as-JSON page.

    The first entry on page 1 describes the app rather than a review, and is
    identified by the absence of a rating rather than by position — Apple has
    changed that ordering before.
    """
    if not isinstance(payload, dict):
        return []

    feed = payload.get("feed") or {}
    entries = feed.get("entry") or []
    if isinstance(entries, dict):  # a single review arrives unwrapped
        entries = [entries]

    items: list[ParsedItem] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rating = _label(entry.get("im:rating"))
        content = _label(entry.get("content"))
        entry_id = _label(entry.get("id"))
        if rating is None or not content or not entry_id:
            continue  # app metadata entry, not a review

        title = _label(entry.get("title"))
        author = None
        author_node = entry.get("author")
        if isinstance(author_node, dict):
            author = _label(author_node.get("name"))

        text = f"{title}\n\n{content}" if title else content
        items.append(
            ParsedItem(
                native_id=str(entry_id),
                text=text,
                url=_link(entry) or f"https://apps.apple.com/{country}/app/id{app_id}",
                author=author,
                created_raw=_label(entry.get("updated")),
                meta={
                    "app_id": app_id,
                    "country": country,
                    "rating": _as_float(rating),
                    "review_title": title,
                    "app_version": _label(entry.get("im:version")),
                },
            )
        )
    return items


def _label(node: Any) -> str | None:
    if isinstance(node, dict):
        value = node.get("label")
        return str(value) if value is not None else None
    if isinstance(node, str):
        return node
    return None


def _as_float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _link(entry: dict[str, Any]) -> str | None:
    link = entry.get("link")
    if isinstance(link, dict):
        return (link.get("attributes") or {}).get("href")
    if isinstance(link, list):
        for candidate in link:
            if isinstance(candidate, dict):
                href = (candidate.get("attributes") or {}).get("href")
                if href:
                    return href
    return None


class AppStoreCollector(Collector):
    source: ClassVar[str] = "app_store"
    min_expected_records: ClassVar[int] = 50

    def __init__(self, session: PoliteSession):
        super().__init__()
        self.session = session
        self.pages_fetched = 0
        self.caps_hit: list[str] = []

    def fetch(self, cfg: Any) -> Iterator[RawRecord]:
        pages = min(int(cfg.max_pages), MAX_FEED_PAGES)
        if int(cfg.max_pages) > MAX_FEED_PAGES:
            self.log.info(
                "max_pages=%s exceeds the feed's own %s-page ceiling; using %s",
                cfg.max_pages,
                MAX_FEED_PAGES,
                MAX_FEED_PAGES,
            )

        for app_id in cfg.app_ids:
            for country in cfg.countries:
                yield from self._app_reviews(app_id, country, pages)

    def _app_reviews(self, app_id: str, country: str, pages: int) -> Iterator[RawRecord]:
        seen: set[str] = set()
        for page in range(1, pages + 1):
            url = FEED_TEMPLATE.format(country=country, page=page, app_id=app_id)
            try:
                payload = self.session.get_json(url)
            except RobotsDisallowed as exc:
                self.log.warning("skipping the App Store feed: %s", exc)
                return
            except HttpError as exc:
                # Past the real end of the feed Apple returns an error status.
                self.log.info("app store feed ended at page %s (%s)", page, exc)
                self.caps_hit.append(f"{app_id}:{country}:page_{page}")
                return

            self.pages_fetched += 1
            items = parse_feed_page(payload, app_id=app_id, country=country)
            fresh = [item for item in items if item.native_id not in seen]
            if not fresh:
                self.log.info(
                    "app store feed for %s exhausted at page %s (~%s reviews, the RSS ceiling)",
                    app_id,
                    page,
                    len(seen),
                )
                self.caps_hit.append(f"{app_id}:{country}:rss_ceiling_{len(seen)}")
                return

            for item in fresh:
                seen.add(item.native_id)
                record = self.build(
                    source_native_id=item.native_id,
                    text=item.text,
                    url=item.url,
                    author_raw=item.author,
                    created_utc=item.created_utc(),
                    meta=item.meta,
                )
                if record is not None:
                    yield record


__all__ = ["FEED_TEMPLATE", "MAX_FEED_PAGES", "AppStoreCollector", "parse_feed_page"]
