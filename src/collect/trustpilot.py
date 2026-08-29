"""Trustpilot — attempted last, expected to yield nothing compliant.

Trustpilot's ``robots.txt`` disallows ``/review/`` and ``/api/*``, which is where
all of the review content lives. The collector is written and enabled anyway, for
one reason: an empty result that is *recorded* is evidence for the report's
limitations section, whereas an absent collector is an unexplained gap a reader
cannot evaluate.

So the expected outcome of running this is a logged robots refusal and zero
records (`edge-case.md` §1.1.10). That is a pass, not a failure, and
``HtmlListingCollector`` already turns a ``RobotsDisallowed`` into a logged
non-event rather than a crash. If Trustpilot ever relaxes the policy, the parser
below is ready and no other change is needed.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, ClassVar

from src.collect.scraping import ParsedItem, HtmlListingCollector, make_soup
from src.common.hashing import content_id

BASE_URL = "https://www.trustpilot.com/review/{domain}"


def parse_reviews(html: str, url: str, *, domain: str = "") -> list[ParsedItem]:
    """Parse reviews from the Next.js payload Trustpilot embeds in the page.

    Selector-based scraping of this site is futile — the class names are hashed
    per build — so the embedded ``__NEXT_DATA__`` JSON is the only stable surface.
    Kept behind the robots gate, so this code is expected to be unreachable in
    practice.
    """
    soup = make_soup(html)
    node = soup.select_one("script#__NEXT_DATA__")
    if node is None:
        return []

    try:
        payload = json.loads(node.get_text())
    except json.JSONDecodeError:
        return []

    reviews = (
        payload.get("props", {})
        .get("pageProps", {})
        .get("reviews", [])
    )
    items: list[ParsedItem] = []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        text = review.get("text") or ""
        if not text:
            continue
        consumer = review.get("consumer") or {}
        rating = review.get("rating")
        review_id = review.get("id") or content_id(text)
        items.append(
            ParsedItem(
                native_id=str(review_id),
                text=f"{review.get('title')}\n\n{text}" if review.get("title") else text,
                url=f"https://www.trustpilot.com/reviews/{review_id}",
                author=consumer.get("displayName"),
                created_raw=(review.get("dates") or {}).get("publishedDate"),
                meta={
                    "rating": rating,
                    "domain": domain,
                    "review_title": review.get("title"),
                },
            )
        )
    return items


class TrustpilotCollector(HtmlListingCollector):
    source: ClassVar[str] = "trustpilot"
    #: No floor, and no raise on an empty page: zero records is the *expected*
    #: compliant outcome here, so either check would fail the run for doing the
    #: right thing.
    min_expected_records: ClassVar[int] = 0
    raise_on_empty_first_page: ClassVar[bool] = False

    def listings(self, cfg: Any) -> Sequence[str]:
        return [BASE_URL.format(domain=domain) for domain in cfg.domains]

    def page_url(self, listing: str, page: int) -> str:
        return listing if page == 1 else f"{listing}?page={page}"

    def parse_page(self, html: str, url: str) -> list[ParsedItem]:
        domain = url.split("/review/")[-1].split("?")[0]
        return parse_reviews(html, url, domain=domain)


__all__ = ["BASE_URL", "TrustpilotCollector", "parse_reviews"]
