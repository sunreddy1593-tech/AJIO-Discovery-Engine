"""ComplaintsBoard listings — severity signal, heavily delivery and refund.

Worth collecting for the intensity of the language, worth reading with suspicion
for the same reason. Complaint boards are self-selected post-purchase grievance,
so a blocker that appears *only* here is probably a service failure rather than a
wishlist blocker — which is why Phase 5 requires presence in at least one
pre-purchase source before a finding can rank in the top tier
(`edge-case.md` §5.7b).

This source carries the most PII of any on the roster: order ids, AWB numbers and
phone numbers are pasted into complaint bodies as a matter of course. Redaction
happens in ``Collector.build`` for every source, which is what keeps that promise
here without this module having to remember it (§1.2.10).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, ClassVar

from src.collect.scraping import (
    HtmlListingCollector,
    ParsedItem,
    absolute_url,
    first_attr,
    first_text,
    make_soup,
)
from src.common.hashing import content_id

BASE_URL = "https://www.complaintsboard.com"

#: Verified against a live fetch of /ajio-b144612 on 2026-08-19. Every complaint
#: block carries schema.org microdata, so ``itemprop`` is tried before any class
#: name: the microdata exists for search engines and survives redesigns that
#: rename CSS, which is exactly the failure these tuples are defending against.
_ITEM_SELECTORS = (
    "div.complaint",
    "[itemtype*='schema.org/Review']",
    "div.complaint-item",
    "article.complaint",
)
_BODY_SELECTORS = (
    "[itemprop='reviewBody']",
    "p.complaint-main__text",
    "div.complaint-main__accordion-panel",
    "div.complaint__text",
)
_TITLE_SELECTORS = (
    "[itemprop='about']",
    "span.complaint-main__header-name",
    "h3.complaint-main__header",
    "a.complaint__title",
)
_AUTHOR_SELECTORS = (
    "[itemprop='author'] [itemprop='name']",
    "span.author-header__name",
    "span.complaint__author",
)
_DATE_SELECTORS = (
    "[itemprop='datePublished']",
    "span.author-header__date",
    "time",
    "span.complaint__date",
)
_STATUS_SELECTORS = ("span.complaint__status", "span.status", "div.resolved")

#: A complaint permalink, when the layout offers one.
_PERMALINK_RE = re.compile(r"-c(\d{5,})(?:$|[/?#])", re.IGNORECASE)

#: On the live listing the blocks carry no permalink at all. The numeric id leaks
#: through the comment anchor and the attachment paths instead, and both were
#: observed on the page. Anything else falls back to a content hash.
_ID_HINT_RES = (
    re.compile(r"#create-comment-(\d{5,})"),
    re.compile(r"/images/(?:complaint|post)/full/(\d{5,})/"),
)


def _complaint_identity(block: Any, listing_url: str) -> tuple[str | None, str | None]:
    """The site's id for one complaint and the best URL for it.

    Deliberately not "the first anchor in the block": on the live listing that is
    a "Learn more" link to /faq, and using it gave every record the same URL and
    no identity of its own.
    """
    for attribute in ("id", "data-id"):
        value = block.get(attribute)
        if value and str(value).strip("c").isdigit():
            identifier = str(value).strip("c")
            return identifier, f"{listing_url.split('#')[0]}#c{identifier}"

    # A real permalink is best: it identifies and locates in one go.
    for anchor in block.select("a[href]"):
        href = anchor.get("href") or ""
        match = _PERMALINK_RE.search(href)
        if match:
            return match.group(1), absolute_url(listing_url, href)

    # Otherwise the complaint is addressable only as a fragment on the listing.
    for anchor in block.select("a[href]"):
        href = anchor.get("href") or ""
        for pattern in _ID_HINT_RES:
            match = pattern.search(href)
            if match:
                identifier = match.group(1)
                return identifier, f"{listing_url.split('#')[0]}#c{identifier}"
    return None, None


def parse_complaints(html: str, url: str, *, company_path: str = "") -> list[ParsedItem]:
    """Pure parse of one ComplaintsBoard listing page."""
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

        native_id, permalink = _complaint_identity(block, url)
        native_id = native_id or content_id(body)

        text = f"{title}\n\n{body}" if title and title not in body else body
        items.append(
            ParsedItem(
                native_id=str(native_id),
                text=text,
                url=permalink or url,
                author=first_text(block, _AUTHOR_SELECTORS),
                created_raw=first_attr(block, ("time",), "datetime")
                or first_text(block, _DATE_SELECTORS),
                meta={
                    "complaint_title": title,
                    "status": first_text(block, _STATUS_SELECTORS),
                    "company_path": company_path,
                },
            )
        )
    return items


class ComplaintsBoardCollector(HtmlListingCollector):
    source: ClassVar[str] = "complaints_board"
    #: The AJIO board is genuinely small: a live fetch on 2026-08-19 found five
    #: complaints on a single page, the newest from Jan 2024, and /page/2 returns
    #: 404. The previous floor of 20 could therefore never be met, which would
    #: have failed every run for a source that was working correctly. Three is
    #: low enough to tolerate a deletion and high enough that a broken selector
    #: set — which yields zero — still trips the empty-first-page tripwire.
    min_expected_records: ClassVar[int] = 3

    def listings(self, cfg: Any) -> Sequence[str]:
        return [f"{BASE_URL}{path}" for path in cfg.company_paths]

    def page_url(self, listing: str, page: int) -> str:
        if page == 1:
            return listing
        return f"{listing.rstrip('/')}/page/{page}"

    def parse_page(self, html: str, url: str) -> list[ParsedItem]:
        company_path = url.replace(BASE_URL, "").split("/page/")[0]
        return parse_complaints(html, url, company_path=company_path)


__all__ = ["BASE_URL", "ComplaintsBoardCollector", "parse_complaints"]
