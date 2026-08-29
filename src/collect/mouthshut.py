"""MouthShut reviews — long-form Indian reviews, post-purchase.

The friendliest robots policy of the review sites: `Allow: /` with a content
signal of ``ai-train=no, use=reference``. This pipeline classifies and quotes,
which is reference use, and never fine-tunes or persists embeddings from this
source. That boundary is a compliance commitment, not a performance choice — do
not add a training or embedding step for MouthShut without revisiting the terms
(`edge-case.md` §1.1.11).

Reviews here run long enough to survive Phase 3's length gate almost entirely,
which makes this the most token-efficient post-purchase source per collected
record even though its volume is modest.

**Disabled in config.yaml as of 2026-08-19, and the reason is not a selector.** A
live fetch of the AJIO listing returns 200 with the aggregate rating, the "about"
blurb and two teaser links — and no review bodies. The list is rendered
client-side, so no selector set can recover it and the parsers below are
consequently unverified against live markup. The listing URL itself is now
correct, and page 2 titles itself "Reviews - 21 to 40", so the ~2,600 reviews
exist; reaching them needs a browser-rendering fetch, at which point this module
should be re-checked with ``scripts/verify_sources.py`` before being trusted.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, ClassVar

from src.collect.scraping import (
    HtmlListingCollector,
    ParsedItem,
    absolute_url,
    extract_rating,
    first_attr,
    first_text,
    make_soup,
)
from src.common.hashing import content_id

#: MouthShut appends the page number to the listing slug.
_PAGE_SUFFIX = "-page-{page}"

#: Several candidates per field because class names change without notice; a
#: missing optional field degrades gracefully, while a missing review body is
#: skipped and counted.
_ITEM_SELECTORS = (
    "div.review-article",
    "div.reviewarticle",
    "div[id^='div_review']",
    "li.review",
    "div.review-block",
)
_BODY_SELECTORS = (
    "div.more.reviewdata",
    "div.reviewdata",
    "div.review-body",
    "p.review-text",
    "div.more",
)
_TITLE_SELECTORS = ("a.reviewdata-title", "h2.review-title", "strong.reviewtitle", "h3 a")
_AUTHOR_SELECTORS = ("div.profile a", "span.user-name", "a.usernm", "div.reviewer-name")
_DATE_SELECTORS = ("div.review-date", "span.datetime", "small.review-date", "span.date")
_RATING_SELECTORS = ("div.rating", "span.rating", "div.ratingstar", "span.star-rating")

_REVIEW_ID_RE = re.compile(r"-(\d{6,})(?:$|[/?#])")


def parse_reviews(html: str, url: str) -> list[ParsedItem]:
    """Pure parse of one MouthShut listing page.

    Ids come from the review permalink where one exists and from a content hash
    otherwise, so a site without stable ids still cannot produce duplicate
    documents across runs (`edge-case.md` §1.2.8 applies the same idea to files).
    """
    soup = make_soup(html)
    items: list[ParsedItem] = []

    blocks: list[Any] = []
    for selector in _ITEM_SELECTORS:
        blocks = soup.select(selector)
        if blocks:
            break

    for block in blocks:
        body = first_text(block, _BODY_SELECTORS)
        title = first_text(block, _TITLE_SELECTORS)
        if not body:
            continue

        href = first_attr(block, _TITLE_SELECTORS + ("a[href]",), "href")
        permalink = absolute_url(url, href)
        native_id = None
        if permalink:
            match = _REVIEW_ID_RE.search(permalink)
            if match:
                native_id = match.group(1)
        if not native_id:
            block_id = block.get("id")
            native_id = str(block_id) if block_id else content_id(body)

        text = f"{title}\n\n{body}" if title and title not in body else body
        items.append(
            ParsedItem(
                native_id=str(native_id),
                text=text,
                url=permalink or url,
                author=first_text(block, _AUTHOR_SELECTORS),
                created_raw=first_text(block, _DATE_SELECTORS),
                meta={
                    "review_title": title,
                    "rating": extract_rating(first_text(block, _RATING_SELECTORS)),
                    "content_type": "review",
                },
            )
        )
    return items


class MouthShutCollector(HtmlListingCollector):
    source: ClassVar[str] = "mouthshut"
    min_expected_records: ClassVar[int] = 40

    def listings(self, cfg: Any) -> Sequence[str]:
        return cfg.listing_urls

    def page_url(self, listing: str, page: int) -> str:
        if page == 1:
            return listing
        return listing.rstrip("/") + _PAGE_SUFFIX.format(page=page)

    def parse_page(self, html: str, url: str) -> list[ParsedItem]:
        return parse_reviews(html, url)


__all__ = ["MouthShutCollector", "parse_reviews"]
