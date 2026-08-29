"""Shared robots.txt gate, polite fetching, and the paginated-listing template.

Every HTML source goes through this module, so compliance is implemented once and
cannot be forgotten by one collector. Three decisions here are policy rather than
mechanics, and each is the conservative reading:

**Fail closed on an unreachable robots.txt.** A 404 means no restrictions exist
and crawling may proceed. A 403, a timeout, or a 5xx means *treat as disallowed*
(`edge-case.md` §1.1.6). A site that actively blocks its own policy file is not
inviting automated access, and AJIO already returned 403 on exactly that file.

**A challenge page is not content.** Cloudflare interstitials arrive with HTTP
200, so status alone cannot be trusted. Body markers are checked on every
response, because ingesting "Checking your browser" as a user review pollutes the
corpus with boilerplate that the tagger would then code as real user voice
(§1.1.8).

**Zero parsed items on a first page is fatal.** ``HtmlListingCollector`` raises
rather than continuing, since a redesigned layout is indistinguishable from a
quiet source once the run has finished (§1.1.7). Zero items on a *later* page is
just the end of pagination.

The HTTP transport is injected, so every test in ``tests/test_scraping.py`` runs
against a stub and the suite makes no network calls.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from abc import abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.robotparser import RobotFileParser

from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from src.collect.base import (
    Collector,
    RateLimiter,
    RequestBudget,
    ZeroYieldError,
    parse_date,
)
from src.common.logging import get_logger
from src.common.schemas import RawRecord

logger = get_logger("collect.scraping")

#: Markers of a bot-check page served with HTTP 200 (§1.1.8).
CHALLENGE_MARKERS = (
    "just a moment...",
    "checking your browser",
    "cf-browser-verification",
    "cf_chl_opt",
    "__cf_chl_",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "please verify you are a human",
    "ddos protection by cloudflare",
    "captcha-delivery.com",
    "px-captcha",
)

#: Retried; anything else is a decision, not a hiccup.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 408})

MAX_RETRY_AFTER_SECONDS = 120.0


class ScrapingError(RuntimeError):
    """Base class for fetch-time failures."""


class RobotsDisallowed(ScrapingError):
    """The URL is disallowed by robots.txt, or robots.txt was unreachable."""


class ChallengeDetected(ScrapingError):
    """A bot-check page was returned instead of content."""


class RetryableHttpError(ScrapingError):
    """A transient status that tenacity should retry."""


class HttpError(ScrapingError):
    """A non-retryable HTTP status."""


class Response(Protocol):
    """The slice of ``requests.Response`` this module actually uses."""

    status_code: int
    text: str
    headers: Any


class Transport(Protocol):
    """Injectable HTTP client. ``requests.Session`` satisfies this."""

    def get(self, url: str, **kwargs: Any) -> Response: ...


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RobotsDecision:
    """Why a URL was allowed or refused, recorded for the compliance log."""

    allowed: bool
    domain: str
    reason: str
    robots_status: int | None = None
    crawl_delay: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "allowed": self.allowed,
            "reason": self.reason,
            "robots_status": self.robots_status,
            "crawl_delay": self.crawl_delay,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }


def domain_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower()


class RobotsGate:
    """Fetches, caches, and applies robots.txt per domain.

    The fetched policy file is written to ``compliance_dir`` alongside a JSONL of
    every decision taken, which is what satisfies the Phase 2 exit criterion that
    "a robots.txt compliance log exists for every scraped domain". Keeping the
    verbatim file matters more than the decision log: it is the evidence of what
    the site said at collection time, which is unrecoverable later.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        user_agent: str,
        compliance_dir: Path,
        timeout: float = 15.0,
        enabled: bool = True,
    ):
        self.transport = transport
        self.user_agent = user_agent
        self.compliance_dir = Path(compliance_dir)
        self.timeout = timeout
        self.enabled = enabled
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._decisions: dict[str, RobotsDecision] = {}

    def _load(self, domain: str) -> RobotsDecision:
        """Fetch and parse robots.txt once per domain."""
        url = f"https://{domain}/robots.txt"
        try:
            response = self.transport.get(
                url, headers={"User-Agent": self.user_agent}, timeout=self.timeout
            )
            status = int(response.status_code)
            body = response.text or ""
        except Exception as exc:  # network error, TLS failure, timeout
            self._parsers[domain] = None
            logger.warning("robots.txt for %s unreachable (%s); treating as disallowed", domain, exc)
            return RobotsDecision(
                False, domain, f"robots.txt unreachable ({type(exc).__name__}); failing closed"
            )

        if status in (404, 410):
            parser = RobotFileParser()
            parser.parse([])  # no rules: everything is allowed
            self._parsers[domain] = parser
            return RobotsDecision(True, domain, "robots.txt absent (404); no restrictions", status)

        if status != 200:
            self._parsers[domain] = None
            logger.warning(
                "robots.txt for %s returned %s; treating as disallowed (edge-case 1.1.6)",
                domain,
                status,
            )
            return RobotsDecision(
                False, domain, f"robots.txt returned {status}; failing closed", status
            )

        self._write_policy_file(domain, body)
        parser = RobotFileParser()
        parser.parse(body.splitlines())
        self._parsers[domain] = parser
        delay = None
        try:
            delay = parser.crawl_delay(self.user_agent)
        except Exception:  # pragma: no cover - defensive; parser internals vary
            delay = None
        return RobotsDecision(True, domain, "robots.txt fetched", status, delay)

    def _write_policy_file(self, domain: str, body: str) -> None:
        self.compliance_dir.mkdir(parents=True, exist_ok=True)
        safe = domain.replace(":", "_")
        (self.compliance_dir / f"robots_{safe}.txt").write_text(body, encoding="utf-8")

    def _record(self, decision: RobotsDecision, url: str) -> None:
        self.compliance_dir.mkdir(parents=True, exist_ok=True)
        line = decision.as_dict() | {"url": url, "user_agent": self.user_agent}
        with (self.compliance_dir / "robots_decisions.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")

    def allows(self, url: str) -> RobotsDecision:
        """Whether ``url`` may be fetched, consulting a cached policy per domain."""
        domain = domain_of(url)
        if not self.enabled:
            return RobotsDecision(True, domain, "robots checking disabled in config")

        if domain not in self._parsers:
            decision = self._load(domain)
            self._decisions[domain] = decision
            self._record(decision, url)
            if not decision.allowed:
                return decision

        parser = self._parsers.get(domain)
        base = self._decisions.get(domain)
        if parser is None:
            return base or RobotsDecision(False, domain, "robots.txt unavailable; failing closed")

        if parser.can_fetch(self.user_agent, url):
            return RobotsDecision(
                True, domain, "allowed by robots.txt", base.robots_status if base else None,
                base.crawl_delay if base else None,
            )

        decision = RobotsDecision(
            False,
            domain,
            "path disallowed by robots.txt",
            base.robots_status if base else None,
        )
        self._record(decision, url)
        return decision


# --------------------------------------------------------------------------
# Polite fetching
# --------------------------------------------------------------------------


class PoliteSession:
    """Rate-limited, retrying, robots-respecting HTTP GET.

    Retries cover transient statuses only. A 403 is not retried: it is a decision
    by the server, and hammering it is both rude and pointless — AJIO's 403 needs
    different headers, not another attempt (§1.1.13).
    """

    def __init__(
        self,
        transport: Transport,
        *,
        user_agent: str,
        robots: RobotsGate | None = None,
        budget: RequestBudget | None = None,
        delay_seconds: float = 3.0,
        timeout: float = 30.0,
        max_attempts: int = 4,
        extra_headers: dict[str, str] | None = None,
        sleep=time.sleep,
    ):
        self.transport = transport
        self.user_agent = user_agent
        self.robots = robots
        self.budget = budget
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.extra_headers = extra_headers or {}
        self.limiter = RateLimiter(delay_seconds, sleep=sleep)
        self._sleep = sleep
        self.requests_made = 0

    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept-Language": "en-IN,en;q=0.9",
            **self.extra_headers,
        }

    def _request_once(self, url: str, params: dict[str, Any] | None) -> Response:
        self.limiter.wait(domain_of(url))
        if self.budget is not None:
            self.budget.spend()
        self.requests_made += 1
        response = self.transport.get(
            url, headers=self.headers(), params=params, timeout=self.timeout
        )

        status = int(response.status_code)
        if status in RETRYABLE_STATUS:
            retry_after = self._retry_after(response)
            if retry_after:
                self._sleep(retry_after)
            raise RetryableHttpError(f"{url} returned {status}")
        if status >= 400:
            raise HttpError(f"{url} returned {status}")

        body = response.text or ""
        if is_challenge_page(body):
            raise ChallengeDetected(
                f"{url} returned a bot-check page with status {status}; "
                "not ingesting it as content (edge-case 1.1.8)"
            )
        return response

    @staticmethod
    def _retry_after(response: Response) -> float | None:
        headers = getattr(response, "headers", None) or {}
        try:
            value = headers.get("Retry-After") or headers.get("retry-after")
        except AttributeError:
            return None
        if not value:
            return None
        try:
            return min(float(value), MAX_RETRY_AFTER_SECONDS)
        except (TypeError, ValueError):
            return None

    def get(self, url: str, *, params: dict[str, Any] | None = None) -> Response:
        """Fetch ``url`` or raise. Robots is consulted before the first byte."""
        if self.robots is not None:
            decision = self.robots.allows(url)
            if not decision.allowed:
                raise RobotsDisallowed(f"{url}: {decision.reason}")

        for attempt in Retrying(
            retry=retry_if_exception_type(RetryableHttpError),
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential_jitter(initial=1.0, max=30.0),
            reraise=True,
        ):
            with attempt:
                return self._request_once(url, params)
        raise ScrapingError(f"unreachable: retry loop exited without result for {url}")

    def get_text(self, url: str, *, params: dict[str, Any] | None = None) -> str:
        return self.get(url, params=params).text or ""

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        """Parse a JSON response body.

        ``json.loads`` on the text rather than ``response.json()`` keeps the
        transport protocol to three attributes, which is what makes the test stub
        a dozen lines instead of a mock framework.
        """
        body = self.get_text(url, params=params)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ScrapingError(f"{url} did not return JSON: {exc}") from exc

    def get_soup(self, url: str, *, params: dict[str, Any] | None = None):
        return make_soup(self.get_text(url, params=params))


def is_challenge_page(body: str) -> bool:
    """Whether a 200 response is actually a bot check (§1.1.8)."""
    if not body:
        return False
    head = body[:4000].lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


def make_soup(html: str):
    """Parse HTML with lxml, falling back to the stdlib parser.

    The fallback exists because lxml is a compiled wheel: if it fails to install
    on a fresh machine the pipeline should degrade rather than refuse to run.
    """
    from bs4 import BeautifulSoup

    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - only when lxml is unavailable
        return BeautifulSoup(html, "html.parser")


def first_text(node, selectors: Sequence[str]) -> str | None:
    """Text of the first matching selector.

    Collectors pass several candidate selectors per field because review sites
    change class names without warning; a list of candidates degrades to a
    missing field instead of a crashed source.
    """
    for selector in selectors:
        found = node.select_one(selector)
        if found is None:
            continue
        text = found.get_text(" ", strip=True)
        if text:
            return text
    return None


def first_attr(node, selectors: Sequence[str], attribute: str) -> str | None:
    for selector in selectors:
        found = node.select_one(selector)
        if found is None:
            continue
        value = found.get(attribute)
        if value:
            return value if isinstance(value, str) else " ".join(value)
    return None


def extract_rating(text: str | None) -> float | None:
    """Pull a numeric rating out of strings like "4 of 5", "Rated 3.5", "2/5"."""
    if not text:
        return None
    import re

    match = re.search(r"(\d(?:\.\d)?)\s*(?:/|of|out of)\s*5", text, re.IGNORECASE)
    if match:
        return float(match.group(1))
    match = re.search(r"(\d(?:\.\d)?)", text)
    if match:
        value = float(match.group(1))
        return value if 0 <= value <= 5 else None
    return None


def absolute_url(base: str, href: str | None) -> str | None:
    if not href:
        return None
    return urllib.parse.urljoin(base, href)


# --------------------------------------------------------------------------
# Paginated listing template
# --------------------------------------------------------------------------


@dataclass
class ParsedItem:
    """One review or complaint parsed from a page, before validation.

    Deliberately not a ``RawRecord``: parsing is a pure function of HTML and is
    unit-tested against saved fixtures, while record construction needs the clock
    and the redaction pass. Keeping them apart is what makes the parsers testable
    without a network or a frozen time.
    """

    native_id: str
    text: str
    url: str | None = None
    author: str | None = None
    created_raw: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def created_utc(self) -> datetime | None:
        return parse_date(self.created_raw)


class HtmlListingCollector(Collector):
    """Template for a paginated HTML review or complaint source.

    Subclasses supply the listing URLs, the page-URL scheme, and a pure
    ``parse_page``. This class owns the parts that are easy to get subtly wrong
    and that every source needs identically: the zero-yield distinction between
    "redesigned" and "finished", the seen-id set that stops a looping paginator
    (§1.1.9), and turning a robots refusal into a logged non-event rather than a
    crash, since Trustpilot is *expected* to refuse.
    """

    #: Selector candidates for the container of a single review on a listing page.
    item_selectors: ClassVar[tuple[str, ...]] = ()

    #: Whether an empty first page is a structural failure. True everywhere except
    #: Trustpilot, whose compliant yield is legitimately zero (§1.1.10) — raising
    #: there would fail the run for honouring robots.txt.
    raise_on_empty_first_page: ClassVar[bool] = True

    def __init__(self, session: PoliteSession):
        super().__init__()
        self.session = session
        self.pages_fetched = 0
        self.robots_blocked: list[str] = []

    # --- subclass contract -------------------------------------------------

    @abstractmethod
    def listings(self, cfg: Any) -> Sequence[str]:
        """The listing roots to walk, taken from ``config.yaml``."""

    @abstractmethod
    def page_url(self, listing: str, page: int) -> str:
        """URL of page ``page`` (1-based) of ``listing``."""

    @abstractmethod
    def parse_page(self, html: str, url: str) -> list[ParsedItem]:
        """Pure parse of one page into items. No network, no clock."""

    def max_pages(self, cfg: Any) -> int:
        return int(getattr(cfg, "max_pages_per_listing", getattr(cfg, "max_pages", 10)))

    def record_cap(self, cfg: Any) -> int | None:
        value = getattr(cfg, "max_reviews", None)
        return int(value) if value else None

    # --- the walk ----------------------------------------------------------

    def fetch(self, cfg: Any) -> Iterator[RawRecord]:
        cap = self.record_cap(cfg)
        produced = 0
        for listing in self.listings(cfg):
            for record in self._walk_listing(listing, cfg):
                yield record
                produced += 1
                if cap is not None and produced >= cap:
                    self.log.info("record cap of %s reached for %s", cap, self.source)
                    return

    def _walk_listing(self, listing: str, cfg: Any) -> Iterator[RawRecord]:
        seen: set[str] = set()
        for page in range(1, self.max_pages(cfg) + 1):
            url = self.page_url(listing, page)
            try:
                html = self.session.get_text(url)
            except RobotsDisallowed as exc:
                # Expected for Trustpilot; a compliance outcome, not a failure.
                self.log.warning("skipping %s: %s", url, exc)
                self.robots_blocked.append(url)
                return
            except (HttpError, ChallengeDetected) as exc:
                if page == 1:
                    raise
                self.log.warning("stopping pagination of %s at page %s: %s", listing, page, exc)
                return

            self.pages_fetched += 1
            items = self.parse_page(html, url)

            if not items:
                if page == 1 and self.raise_on_empty_first_page:
                    raise ZeroYieldError(
                        f"{self.source}: {url} parsed to zero items. Either the layout "
                        "changed or the client was blocked; both need a human, because a "
                        "silently empty source is indistinguishable from a quiet one."
                    )
                self.log.debug("%s page %s empty; end of listing", listing, page)
                return

            fresh = [item for item in items if item.native_id not in seen]
            if not fresh:
                self.log.debug("%s page %s repeated only seen ids; stopping", listing, page)
                return

            for item in fresh:
                seen.add(item.native_id)
                record = self.build(
                    source_native_id=item.native_id,
                    text=item.text,
                    url=item.url or url,
                    author_raw=item.author,
                    created_utc=item.created_utc(),
                    meta={**item.meta, "listing_url": listing},
                )
                if record is not None:
                    yield record


__all__ = [
    "CHALLENGE_MARKERS",
    "ChallengeDetected",
    "HtmlListingCollector",
    "HttpError",
    "ParsedItem",
    "PoliteSession",
    "RobotsDecision",
    "RobotsDisallowed",
    "RobotsGate",
    "ScrapingError",
    "absolute_url",
    "domain_of",
    "extract_rating",
    "first_attr",
    "first_text",
    "is_challenge_page",
    "make_soup",
]
