"""ConsumerComplaints.in listings — Indian post-purchase grievance.

Same evidential caveat as ComplaintsBoard: high severity, narrow subject matter,
and a strong tilt toward delivery and refund problems that say nothing about why a
wishlisted item was never bought. Collected for severity signal and for the
occasional fit or quality complaint that reveals which pre-purchase uncertainty
turned out to be justified.
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

BASE_URL = "https://www.consumercomplaints.in"

#: Verified against a live fetch of /ajio-b115930 on 2026-08-19: 20 complaint
#: blocks per page, each a ``div.complaint-box`` carrying ``id="c<digits>"``.
#: The first entry of each tuple is the observed markup; the rest are the earlier
#: guesses, kept as fallbacks so a rename degrades to a missing optional field.
_ITEM_SELECTORS = (
    "div.complaint-box",
    "div.complaint",
    "div.review-text",
    "div.complaint-card",
)
#: ``complaint-box__text`` wraps the whole visible body including the "more"
#: block. Long complaints are served as an excerpt that can begin mid-sentence —
#: that is the site's own truncation on the listing, not a parse bug, and the
#: full text would cost one request per complaint.
_BODY_SELECTORS = ("div.complaint-box__text", "div.complaint-detail", "div.complaint-body")
_TITLE_SELECTORS = ("h4.complaint-box__title", "a.complaint-box__link", "h2 a", "h3 a")
_LINK_SELECTORS = ("a.complaint-box__link",)
_AUTHOR_SELECTORS = ("div.author-box__user", "span.author", "div.user-name")
_DATE_SELECTORS = ("div.author-box__date", "time", "span.date", "span.complaint-date")
_STATUS_SELECTORS = ("div.complaint-box__status", "span.status", "div.complaint-status")

_ID_RE = re.compile(r"-c(\d{5,})(?:$|[/?#])", re.IGNORECASE)
#: The block's own ``id`` attribute, e.g. ``c3541211``.
_BLOCK_ID_RE = re.compile(r"^c(\d{5,})$", re.IGNORECASE)


def parse_complaints(html: str, url: str, *, company_path: str = "") -> list[ParsedItem]:
    """Pure parse of one ConsumerComplaints.in listing page."""
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

        href = first_attr(block, _LINK_SELECTORS + ("a[href]",), "href")
        permalink = absolute_url(url, href)
        # Preference order is deliberate: the block's own id attribute is the
        # site's identifier, the permalink is the same number in another place,
        # and the content hash only exists so an unidentified complaint still
        # cannot be written twice.
        native_id = None
        block_id = block.get("id")
        if block_id:
            match = _BLOCK_ID_RE.match(str(block_id))
            if match:
                native_id = match.group(1)
        if not native_id and permalink:
            match = _ID_RE.search(permalink)
            if match:
                native_id = match.group(1)
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


class ConsumerComplaintsInCollector(HtmlListingCollector):
    source: ClassVar[str] = "consumer_complaints_in"
    min_expected_records: ClassVar[int] = 20

    def listings(self, cfg: Any) -> Sequence[str]:
        return [f"{BASE_URL}{path}" for path in cfg.company_paths]

    def page_url(self, listing: str, page: int) -> str:
        if page == 1:
            return listing
        return f"{listing.rstrip('/')}/page/{page}"

    def parse_page(self, html: str, url: str) -> list[ParsedItem]:
        company_path = url.replace(BASE_URL, "").split("/page/")[0]
        return parse_complaints(html, url, company_path=company_path)


__all__ = ["BASE_URL", "ConsumerComplaintsInCollector", "parse_complaints"]
