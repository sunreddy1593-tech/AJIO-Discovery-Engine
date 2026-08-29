"""AJIO on-site reviews and Q&A — the corpus's best pre-purchase source.

On-site Q&A is the single richest source on the roster: *"does this run small?"*
asked on a product page **is** the blocker this project exists to find, stated by
someone who has not yet bought. Nothing else on the roster is that direct.

Three things shape this module:

**AJIO returns 403 to non-browser clients** (`edge-case.md` §1.1.13). A probe on
2026-08-19 sharpened this: with browser-grade headers ``robots.txt`` itself now
answers 200, while the home page, category pages and even the gzipped sitemaps
are refused by an Akamai edge. That distinction matters, because it rules out the
cheap explanations. The refusal is bot management rather than policy — robots.txt
permits ``/p/`` and ``/c/`` for every user agent — and it is not a missing header,
since the headers below are already a full Chrome set. The remaining escalation is
a browser-driven fetch. If that is unavailable, this collector raises
:class:`AjioBlockedError` rather than yielding nothing quietly — losing this source
silently is how the corpus becomes all post-purchase and the engine confidently
reports refund complaints as wishlist blockers.

**Q&A and reviews must never be conflated** (§1.1.14). They share a source but sit
on opposite sides of the purchase, so every record carries
``meta.content_type`` of ``qa`` or ``review``, and ``schemas.purchase_stage``
refuses to resolve an AJIO record that lacks it.

**Extraction has to survive an unverified layout.** AJIO is a single-page app whose
review and Q&A payloads are fetched by the client, so this module tries the JSON
endpoints first, then the JSON embedded in the product page, then visible HTML.
The endpoint templates live in ``config.yaml`` precisely so that correcting them
after a live probe is a config edit rather than a code change. They now ship
empty, because AJIO's robots.txt disallows ``/api/*`` outright: the JSON path is
closed by the compliance stance, not merely unverified. A template that points
into ``/api/`` is refused before the first byte, so this module disables it after
the first refusal instead of re-requesting a forbidden URL once per product.

**A question is a document; its answers are metadata.** Answers usually come from
people who already bought the item, so promoting them to their own pre-purchase
documents would quietly mislabel post-purchase voice as deliberation. They are
kept on the question record instead, where they add context without being counted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

from src.collect.base import Collector, CollectionError
from src.collect.scraping import (
    ChallengeDetected,
    HttpError,
    ParsedItem,
    PoliteSession,
    RobotsDisallowed,
    ScrapingError,
    make_soup,
)
from src.common.hashing import content_id
from src.common.schemas import AjioContentType, RawRecord

BASE_URL = "https://www.ajio.com"

#: Product ids appear in every product link as ``/p/<digits>``; scanning for that
#: is far more durable than any category-page selector, and it is what makes
#: resolving ids at run time practical rather than maintaining a URL list (§1.1.15).
_PRODUCT_ID_RE = re.compile(r"/p/(\d{6,})")

_REVIEW_BLOCK_SELECTORS = (
    "div.review-item",
    "div[class*='review-list'] div[class*='item']",
    "div.user-review",
    "li.review",
)
_QA_BLOCK_SELECTORS = (
    "div.qa-item",
    "div[class*='question'] div[class*='item']",
    "li.qa-list-item",
)
_EMBEDDED_STATE_RE = re.compile(
    r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.DOTALL
)


class AjioBlockedError(CollectionError):
    """AJIO refused automated access and no fallback produced records.

    Deliberately fatal. The alternative — proceeding with a corpus that has lost
    its only rich pre-purchase source — produces a report that looks complete and
    answers the wrong question.
    """


def browser_headers(user_agent: str) -> dict[str, str]:
    """Headers a real Chrome session sends.

    A ``User-Agent`` alone is not enough: the missing ``Sec-Fetch-*`` and
    ``Accept`` headers are themselves the signal that a client is automated.
    """
    return {
        "User-Agent": user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }


# --------------------------------------------------------------------------
# Pure parsing
# --------------------------------------------------------------------------


def extract_product_ids(html: str) -> list[str]:
    """Every product id linked from a category or listing page, in page order."""
    seen: dict[str, None] = {}
    for match in _PRODUCT_ID_RE.finditer(html or ""):
        seen.setdefault(match.group(1), None)
    return list(seen)


def parse_ld_json_reviews(html: str, product_id: str) -> list[ParsedItem]:
    """Reviews from schema.org ``Product`` markup.

    Worth trying before any selector: structured markup exists for SEO, is stable
    across redesigns, and carries the rating and date as data rather than as text
    needing a locale-specific parse.
    """
    soup = make_soup(html)
    items: list[ParsedItem] = []

    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.get_text())
        except (json.JSONDecodeError, AttributeError):
            continue
        for block in payload if isinstance(payload, list) else [payload]:
            if not isinstance(block, dict):
                continue
            reviews = block.get("review") or []
            if isinstance(reviews, dict):
                reviews = [reviews]
            brand = block.get("brand")
            brand_name = brand.get("name") if isinstance(brand, dict) else brand
            for review in reviews:
                if not isinstance(review, dict):
                    continue
                body = review.get("reviewBody") or review.get("description")
                if not body:
                    continue
                author = review.get("author")
                author_name = author.get("name") if isinstance(author, dict) else author
                rating_node = review.get("reviewRating") or {}
                items.append(
                    ParsedItem(
                        native_id=f"review-{product_id}-{content_id(body)[:12]}",
                        text=body,
                        url=f"{BASE_URL}/p/{product_id}",
                        author=author_name if isinstance(author_name, str) else None,
                        created_raw=review.get("datePublished"),
                        meta={
                            "product_id": product_id,
                            "content_type": AjioContentType.REVIEW.value,
                            "rating": _as_float(rating_node.get("ratingValue"))
                            if isinstance(rating_node, dict)
                            else None,
                            "brand": brand_name,
                            "review_title": review.get("name"),
                        },
                    )
                )
    return items


def parse_review_api(payload: Any, product_id: str) -> list[ParsedItem]:
    """Reviews from AJIO's JSON review endpoint.

    The exact key names differ between AJIO's endpoint versions, so each field is
    read from a list of candidates. A rename costs a missing optional field rather
    than a dead source.
    """
    items: list[ParsedItem] = []
    for entry in _iter_entries(payload, ("reviews", "reviewList", "results", "data", "content")):
        body = _first_key(entry, ("reviewText", "comment", "description", "text", "reviewBody"))
        if not body:
            continue
        native = _first_key(entry, ("reviewId", "id", "reviewID"))
        items.append(
            ParsedItem(
                native_id=f"review-{product_id}-{native or content_id(str(body))[:12]}",
                text=str(body),
                url=f"{BASE_URL}/p/{product_id}",
                author=_first_key(entry, ("userName", "nickName", "author", "displayName")),
                created_raw=_first_key(entry, ("submissionTime", "createdAt", "date", "reviewDate")),
                meta={
                    "product_id": product_id,
                    "content_type": AjioContentType.REVIEW.value,
                    "rating": _as_float(_first_key(entry, ("rating", "ratingValue", "score"))),
                    "review_title": _first_key(entry, ("title", "headline", "reviewTitle")),
                    "size_bought": _first_key(entry, ("sizeBought", "size", "purchasedSize")),
                    "fit_feedback": _first_key(entry, ("fitFeedback", "fit", "fitRating")),
                },
            )
        )
    return items


def parse_qa_api(payload: Any, product_id: str) -> list[ParsedItem]:
    """Questions from AJIO's JSON Q&A endpoint, answers folded into meta."""
    items: list[ParsedItem] = []
    for entry in _iter_entries(payload, ("questions", "questionList", "results", "data", "content")):
        question = _first_key(entry, ("questionText", "question", "title", "text"))
        if not question:
            continue

        answers: list[str] = []
        raw_answers = entry.get("answers") or entry.get("answerList") or []
        if isinstance(raw_answers, dict):
            raw_answers = [raw_answers]
        for answer in raw_answers if isinstance(raw_answers, list) else []:
            if isinstance(answer, dict):
                text = _first_key(answer, ("answerText", "answer", "text", "body"))
                if text:
                    answers.append(str(text))
            elif isinstance(answer, str):
                answers.append(answer)

        native = _first_key(entry, ("questionId", "id", "questionID"))
        items.append(
            ParsedItem(
                native_id=f"qa-{product_id}-{native or content_id(str(question))[:12]}",
                text=str(question),
                url=f"{BASE_URL}/p/{product_id}",
                author=_first_key(entry, ("userName", "nickName", "author", "displayName")),
                created_raw=_first_key(entry, ("submissionTime", "createdAt", "date")),
                meta={
                    "product_id": product_id,
                    "content_type": AjioContentType.QA.value,
                    "rating": None,
                    "answers": answers,
                    "answer_count": len(answers),
                },
            )
        )
    return items


def parse_html_blocks(
    html: str, product_id: str, *, content_type: str
) -> list[ParsedItem]:
    """Last-resort parse of visible review or Q&A blocks."""
    soup = make_soup(html)
    selectors = (
        _REVIEW_BLOCK_SELECTORS
        if content_type == AjioContentType.REVIEW.value
        else _QA_BLOCK_SELECTORS
    )

    blocks: list[Any] = []
    for selector in selectors:
        blocks = soup.select(selector)
        if blocks:
            break

    items: list[ParsedItem] = []
    for block in blocks:
        text = block.get_text(" ", strip=True)
        if not text:
            continue
        items.append(
            ParsedItem(
                native_id=f"{content_type}-{product_id}-{content_id(text)[:12]}",
                text=text,
                url=f"{BASE_URL}/p/{product_id}",
                meta={
                    "product_id": product_id,
                    "content_type": content_type,
                    "rating": None,
                    "extraction": "html_fallback",
                },
            )
        )
    return items


def _iter_entries(payload: Any, keys: Sequence[str]) -> Iterator[dict[str, Any]]:
    """Yield record dicts from a JSON payload of unknown shape."""
    if isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, dict):
                yield entry
        return
    if not isinstance(payload, dict):
        return
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    yield entry
            return
        if isinstance(value, dict):
            yield from _iter_entries(value, keys)
            return


def _first_key(entry: dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = entry.get(key)
        if value not in (None, "", []):
            return value
    return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------


class AjioOnsiteCollector(Collector):
    """Product reviews and Q&A from ajio.com, separated by ``meta.content_type``."""

    source: ClassVar[str] = "ajio_onsite"
    #: No floor here even though this is the most valuable source: a 403 must
    #: surface as AjioBlockedError with escalation guidance, not as a generic
    #: yield failure that reads like a quiet source.
    min_expected_records: ClassVar[int] = 0

    def __init__(
        self,
        session: PoliteSession,
        *,
        review_api_template: str = "",
        qa_api_template: str = "",
    ):
        super().__init__()
        self.session = session
        self.review_api_template = review_api_template
        self.qa_api_template = qa_api_template
        self.products_seen: list[str] = []
        self.products_missing: list[str] = []
        self.blocked_urls: list[str] = []
        self.disabled_templates: list[str] = []
        self.extraction_paths: dict[str, int] = {}

    # --- product resolution -------------------------------------------------

    def resolve_product_ids(self, cfg: Any) -> list[str]:
        """Explicit product urls if configured, otherwise ids scraped from categories.

        Resolving at run time is what keeps a hand-maintained URL list from going
        stale as products are discontinued (§1.1.15).
        """
        ids: list[str] = []
        for url in cfg.product_urls:
            found = extract_product_ids(url)
            ids.extend(found or [])

        if ids:
            self.log.info("using %s product ids from config.product_urls", len(ids))
            return ids[: cfg.max_products]

        for category_url in cfg.category_urls:
            try:
                html = self._get_text(category_url)
            except AjioBlockedError:
                raise
            except (HttpError, ScrapingError) as exc:
                self.log.warning("category page %s failed: %s", category_url, exc)
                continue
            found = extract_product_ids(html)
            self.log.info("%s product ids from %s", len(found), category_url)
            ids.extend(found)
            if len(ids) >= cfg.max_products:
                break

        unique = list(dict.fromkeys(ids))
        return unique[: cfg.max_products]

    # --- fetching -----------------------------------------------------------

    def _get_text(self, url: str) -> str:
        """Fetch with the 403 escalation path attached (§1.1.13)."""
        try:
            return self.session.get_text(url)
        except RobotsDisallowed:
            raise
        except (HttpError, ChallengeDetected) as exc:
            message = str(exc)
            if "403" in message or isinstance(exc, ChallengeDetected):
                self.blocked_urls.append(url)
                raise AjioBlockedError(
                    f"AJIO refused automated access to {url} ({message}).\n"
                    "This is bot management, not robots policy: robots.txt fetches "
                    "with a 200 and allows /p/ and /c/ for every user agent, and a "
                    "full Chrome header set was already sent. So neither 'add a "
                    "header' nor 'we are not allowed' is the explanation, and "
                    "retrying will not help.\n"
                    "The supported route from here is the ajio_manual collector: "
                    "browse the product pages yourself and save the Q&A and reviews "
                    "into data/manual/ajio as .txt or .md, in the format documented "
                    "in src/collect/ajio_manual.py. Note that the JSON endpoints are "
                    "not an option either — robots.txt disallows /api/*.\n"
                    "Defeating the bot management itself is not on this list. A 403 "
                    "is the site's access decision for this client, and robots.txt "
                    "permitting a path is not a grant that overrides it.\n"
                    "Do not proceed without this source: it is the corpus's only "
                    "rich pre-purchase evidence, and without it the run cannot "
                    "reach its pre-purchase floor (edge-case 1.1.13)."
                ) from exc
            raise

    def fetch(self, cfg: Any) -> Iterator[RawRecord]:
        product_ids = self.resolve_product_ids(cfg)
        if not product_ids:
            raise AjioBlockedError(
                "No AJIO product ids could be resolved from config.product_urls or "
                "config.category_urls. Either the category pages returned no product "
                "links (a layout change or a soft block) or the configured URLs are "
                "wrong — the category slugs in config.yaml have never been confirmed "
                "against the live site, only their /<slug>/c/<id> shape has. Run "
                "`python scripts/verify_sources.py --source ajio_onsite` to tell a "
                "wrong URL apart from a block. Fix before continuing: without this "
                "source the corpus is almost entirely post-purchase."
            )

        self.log.info("collecting reviews and Q&A for %s products", len(product_ids))
        for product_id in product_ids:
            self.products_seen.append(product_id)
            yield from self._product_records(product_id, cfg)

    def _product_records(self, product_id: str, cfg: Any) -> Iterator[RawRecord]:
        items: list[ParsedItem] = []

        qa_items = self._collect_qa(product_id, cfg)
        review_items = self._collect_reviews(product_id, cfg)
        items.extend(qa_items[: cfg.max_qa_per_product])
        items.extend(review_items[: cfg.max_reviews_per_product])

        if not items:
            self.products_missing.append(product_id)
            self.log.debug("no reviews or Q&A found for product %s", product_id)

        for item in items:
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

    def _api_json(self, template: str, product_id: str, kind: str) -> Any | None:
        """One JSON endpoint call, or None if it failed for any reason."""
        url = template.format(product_id=product_id, page=1)
        try:
            return self.session.get_json(url)
        except RobotsDisallowed as exc:
            # Configuration error, not a transient one: the same verdict applies
            # to every product, so say it once loudly and stop asking. Left at
            # debug this looked exactly like "the endpoint returned nothing".
            self.log.warning(
                "%s endpoint disabled for this run: %s. AJIO's robots.txt disallows "
                "/api/*, so this template can never be fetched while "
                "respect_robots_txt is true; clear it in config.yaml.",
                kind,
                exc,
            )
            self._disable_template(kind)
            return None
        except (HttpError, ScrapingError, KeyError, IndexError) as exc:
            self.log.debug("%s endpoint failed for %s: %s", kind, product_id, exc)
            return None

    def _disable_template(self, kind: str) -> None:
        if kind == "qa":
            self.qa_api_template = ""
        else:
            self.review_api_template = ""
        self.disabled_templates.append(kind)

    def _collect_qa(self, product_id: str, cfg: Any) -> list[ParsedItem]:
        if self.qa_api_template:
            payload = self._api_json(self.qa_api_template, product_id, "qa")
            if payload is not None:
                items = parse_qa_api(payload, product_id)
                if items:
                    self._note_path("qa_api")
                    return items

        try:
            html = self._get_text(f"{BASE_URL}/p/{product_id}")
        except (HttpError, ScrapingError) as exc:
            self.log.debug("product page failed for %s: %s", product_id, exc)
            return []

        items = parse_html_blocks(html, product_id, content_type=AjioContentType.QA.value)
        if items:
            self._note_path("qa_html")
        return items

    def _collect_reviews(self, product_id: str, cfg: Any) -> list[ParsedItem]:
        if self.review_api_template:
            payload = self._api_json(self.review_api_template, product_id, "review")
            if payload is not None:
                items = parse_review_api(payload, product_id)
                if items:
                    self._note_path("review_api")
                    return items

        try:
            html = self._get_text(f"{BASE_URL}/p/{product_id}")
        except (HttpError, ScrapingError) as exc:
            self.log.debug("product page failed for %s: %s", product_id, exc)
            return []

        items = parse_ld_json_reviews(html, product_id)
        if items:
            self._note_path("review_ld_json")
            return items

        items = parse_html_blocks(html, product_id, content_type=AjioContentType.REVIEW.value)
        if items:
            self._note_path("review_html")
        return items

    def _note_path(self, name: str) -> None:
        self.extraction_paths[name] = self.extraction_paths.get(name, 0) + 1


__all__ = [
    "BASE_URL",
    "AjioBlockedError",
    "AjioOnsiteCollector",
    "browser_headers",
    "extract_product_ids",
    "parse_html_blocks",
    "parse_ld_json_reviews",
    "parse_qa_api",
    "parse_review_api",
]
