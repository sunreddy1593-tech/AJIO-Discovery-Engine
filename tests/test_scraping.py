"""Compliance and fetch policy. Every test runs against a stub transport.

The three properties worth defending here are the ones where being wrong is
either a legal problem or an invisible data problem: failing closed when
robots.txt cannot be read, refusing to ingest a bot-check page that arrived with
HTTP 200, and raising when a first page parses to zero items.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.collect.base import RequestBudget, RequestBudgetExhausted, ZeroYieldError
from src.collect.scraping import (
    ChallengeDetected,
    HtmlListingCollector,
    HttpError,
    ParsedItem,
    PoliteSession,
    RobotsDisallowed,
    RobotsGate,
    domain_of,
    extract_rating,
    is_challenge_page,
    make_soup,
)

ALLOW_ALL = "User-agent: *\nAllow: /\n"
DISALLOW_REVIEWS = "User-agent: *\nDisallow: /reviews/\nDisallow: /api/\n"


class StubResponse:
    def __init__(self, status_code: int = 200, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class StubTransport:
    """Minimal ``requests.Session`` stand-in: three attributes, no mock framework."""

    def __init__(self, routes: dict[str, Any] | None = None, default: Any = None):
        self.routes = routes or {}
        self.default = default or StubResponse(200, "<html></html>")
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append(url)
        handler = self.routes.get(url)
        if handler is None:
            for pattern, candidate in self.routes.items():
                if pattern in url:
                    handler = candidate
                    break
        if handler is None:
            handler = self.default
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(url)
        return handler


def gate(transport: StubTransport, tmp_path, **kwargs) -> RobotsGate:
    return RobotsGate(
        transport, user_agent="test-agent", compliance_dir=tmp_path / "_compliance", **kwargs
    )


# --- robots.txt -----------------------------------------------------------


def test_allow_all_permits_any_path(tmp_path):
    transport = StubTransport({"robots.txt": StubResponse(200, ALLOW_ALL)})
    decision = gate(transport, tmp_path).allows("https://example.com/reviews/page/2")
    assert decision.allowed is True


def test_disallowed_path_is_refused_while_others_are_allowed(tmp_path):
    transport = StubTransport({"robots.txt": StubResponse(200, DISALLOW_REVIEWS)})
    robots = gate(transport, tmp_path)
    assert robots.allows("https://example.com/reviews/ajio.com").allowed is False
    assert robots.allows("https://example.com/about").allowed is True


def test_missing_robots_txt_allows_everything(tmp_path):
    """404 means no restrictions exist and crawling may proceed (edge-case 1.1.6)."""
    transport = StubTransport({"robots.txt": StubResponse(404, "Not Found")})
    decision = gate(transport, tmp_path).allows("https://example.com/anything")
    assert decision.allowed is True
    assert decision.robots_status == 404


def test_robots_403_is_treated_as_disallowed(tmp_path):
    """Fail closed: AJIO returned exactly this on its own policy file."""
    transport = StubTransport({"robots.txt": StubResponse(403, "Forbidden")})
    decision = gate(transport, tmp_path).allows("https://www.ajio.com/p/123")
    assert decision.allowed is False
    assert "failing closed" in decision.reason


def test_robots_timeout_is_treated_as_disallowed(tmp_path):
    transport = StubTransport({"robots.txt": TimeoutError("timed out")})
    decision = gate(transport, tmp_path).allows("https://example.com/x")
    assert decision.allowed is False
    assert "unreachable" in decision.reason


def test_robots_500_is_treated_as_disallowed(tmp_path):
    transport = StubTransport({"robots.txt": StubResponse(503, "unavailable")})
    assert gate(transport, tmp_path).allows("https://example.com/x").allowed is False


def test_robots_txt_is_fetched_once_per_domain(tmp_path):
    transport = StubTransport({"robots.txt": StubResponse(200, ALLOW_ALL)})
    robots = gate(transport, tmp_path)
    for page in range(5):
        robots.allows(f"https://example.com/page/{page}")
    assert transport.calls.count("https://example.com/robots.txt") == 1


def test_compliance_log_exists_for_every_scraped_domain(tmp_path):
    """A Phase 2 exit criterion, and the only durable record of what a site said."""
    transport = StubTransport({"robots.txt": StubResponse(200, DISALLOW_REVIEWS)})
    robots = gate(transport, tmp_path)
    robots.allows("https://example.com/about")
    robots.allows("https://example.com/reviews/x")

    compliance = tmp_path / "_compliance"
    assert (compliance / "robots_example.com.txt").read_text(encoding="utf-8") == DISALLOW_REVIEWS

    decisions = [
        json.loads(line)
        for line in (compliance / "robots_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(entry["allowed"] is False for entry in decisions)
    assert all(entry["domain"] == "example.com" for entry in decisions)


def test_robots_can_be_disabled_only_by_explicit_config(tmp_path):
    transport = StubTransport({"robots.txt": StubResponse(403, "")})
    decision = gate(transport, tmp_path, enabled=False).allows("https://example.com/x")
    assert decision.allowed is True
    assert "disabled" in decision.reason


# --- challenge detection --------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "<html><title>Just a moment...</title></html>",
        "<html>Checking your browser before accessing</html>",
        "<div id='cf-browser-verification'>x</div>",
        "<html>Attention Required! | Cloudflare</html>",
        "<p>Please verify you are a human</p>",
    ],
)
def test_challenge_pages_are_recognised(body):
    assert is_challenge_page(body) is True


def test_ordinary_review_html_is_not_a_challenge():
    assert is_challenge_page("<div class='review'>the medium runs small</div>") is False
    assert is_challenge_page("") is False


def test_challenge_page_with_status_200_is_not_ingested(tmp_path):
    """Status alone cannot be trusted; the body is what gives it away (1.1.8)."""
    transport = StubTransport(
        {
            "robots.txt": StubResponse(200, ALLOW_ALL),
            "/reviews": StubResponse(200, "<html><title>Just a moment...</title></html>"),
        }
    )
    session = PoliteSession(
        transport,
        user_agent="test-agent",
        robots=gate(transport, tmp_path),
        delay_seconds=0,
        sleep=lambda _: None,
    )
    with pytest.raises(ChallengeDetected):
        session.get_text("https://example.com/reviews")


# --- session behaviour ----------------------------------------------------


def session_for(transport, tmp_path, **kwargs) -> PoliteSession:
    return PoliteSession(
        transport,
        user_agent="test-agent",
        robots=gate(transport, tmp_path),
        delay_seconds=0,
        sleep=lambda _: None,
        **kwargs,
    )


def test_disallowed_url_raises_before_any_request(tmp_path):
    transport = StubTransport({"robots.txt": StubResponse(200, DISALLOW_REVIEWS)})
    session = session_for(transport, tmp_path)
    with pytest.raises(RobotsDisallowed):
        session.get_text("https://example.com/reviews/ajio")
    assert transport.calls == ["https://example.com/robots.txt"]


def test_transient_status_is_retried_then_succeeds(tmp_path):
    attempts = {"n": 0}

    def flaky(url):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return StubResponse(503, "later")
        return StubResponse(200, "<html>ok</html>")

    transport = StubTransport({"robots.txt": StubResponse(200, ALLOW_ALL), "/page": flaky})
    session = session_for(transport, tmp_path)
    assert "ok" in session.get_text("https://example.com/page")
    assert attempts["n"] == 3


def test_403_is_not_retried_because_it_is_a_decision(tmp_path):
    calls = {"n": 0}

    def forbidden(url):
        calls["n"] += 1
        return StubResponse(403, "Forbidden")

    transport = StubTransport({"robots.txt": StubResponse(200, ALLOW_ALL), "/page": forbidden})
    session = session_for(transport, tmp_path)
    with pytest.raises(HttpError, match="403"):
        session.get_text("https://example.com/page")
    assert calls["n"] == 1


def test_429_retry_after_is_honoured(tmp_path):
    slept: list[float] = []
    attempts = {"n": 0}

    def limited(url):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return StubResponse(429, "slow down", headers={"Retry-After": "7"})
        return StubResponse(200, "<html>ok</html>")

    transport = StubTransport({"robots.txt": StubResponse(200, ALLOW_ALL), "/page": limited})
    session = PoliteSession(
        transport,
        user_agent="test-agent",
        robots=gate(transport, tmp_path),
        delay_seconds=0,
        sleep=slept.append,
    )
    assert "ok" in session.get_text("https://example.com/page")
    assert 7.0 in slept


def test_request_budget_is_enforced_across_urls(tmp_path):
    transport = StubTransport({"robots.txt": StubResponse(200, ALLOW_ALL)})
    session = session_for(transport, tmp_path, budget=RequestBudget(2))
    session.get_text("https://example.com/a")
    session.get_text("https://example.com/b")
    with pytest.raises(RequestBudgetExhausted):
        session.get_text("https://example.com/c")


def test_get_json_parses_and_reports_non_json(tmp_path):
    transport = StubTransport(
        {
            "robots.txt": StubResponse(200, ALLOW_ALL),
            "/good": StubResponse(200, '{"a": 1}'),
            "/bad": StubResponse(200, "<html>not json</html>"),
        }
    )
    session = session_for(transport, tmp_path)
    assert session.get_json("https://example.com/good") == {"a": 1}
    with pytest.raises(Exception, match="did not return JSON"):
        session.get_json("https://example.com/bad")


# --- helpers --------------------------------------------------------------


def test_domain_of_lowercases_the_host():
    assert domain_of("https://WWW.Example.COM/path") == "www.example.com"


@pytest.mark.parametrize(
    "text,expected",
    [("4 of 5", 4.0), ("Rated 3.5/5", 3.5), ("2 out of 5 stars", 2.0), ("5", 5.0), (None, None)],
)
def test_extract_rating_reads_the_common_shapes(text, expected):
    assert extract_rating(text) == expected


# --- the listing walk -----------------------------------------------------


class FakeListingCollector(HtmlListingCollector):
    source = "mouthshut"

    def __init__(self, session, pages):
        super().__init__(session)
        self.pages = pages

    def listings(self, cfg):
        return ["https://example.com/reviews-ajio"]

    def page_url(self, listing, page):
        return f"{listing}?page={page}"

    def parse_page(self, html, url):
        return self.pages.get(html, [])

    def max_pages(self, cfg):
        return 5

    def record_cap(self, cfg):
        return None


def item(native_id: str) -> ParsedItem:
    return ParsedItem(
        native_id=native_id,
        text="the medium size runs small so it is still sitting in my wishlist",
        meta={"rating": 3},
    )


def walk(page_bodies, pages, tmp_path):
    transport = StubTransport({"robots.txt": StubResponse(200, ALLOW_ALL)}, default=None)
    transport.routes.update(page_bodies)
    session = session_for(transport, tmp_path)
    collector = FakeListingCollector(session, pages)
    return list(collector.fetch(cfg=None))


def test_empty_first_page_raises_because_it_means_a_redesign(tmp_path):
    records = None
    with pytest.raises(ZeroYieldError, match="parsed to zero items"):
        records = walk({"page=1": StubResponse(200, "EMPTY")}, {"EMPTY": []}, tmp_path)
    assert records is None


def test_empty_later_page_just_ends_pagination(tmp_path):
    bodies = {
        "page=1": StubResponse(200, "P1"),
        "page=2": StubResponse(200, "EMPTY"),
    }
    pages = {"P1": [item("a"), item("b")], "EMPTY": []}
    records = walk(bodies, pages, tmp_path)
    assert [r.source_native_id for r in records] == ["a", "b"]


def test_repeated_ids_stop_a_looping_paginator(tmp_path):
    """Edge case 1.1.9: complaint boards repeat page 1 forever at the end."""
    bodies = {
        "page=1": StubResponse(200, "P1"),
        "page=2": StubResponse(200, "P1"),
        "page=3": StubResponse(200, "P1"),
    }
    records = walk(bodies, {"P1": [item("a")]}, tmp_path)
    assert len(records) == 1


def test_robots_refusal_mid_walk_is_a_logged_non_event(tmp_path):
    """Trustpilot is expected to refuse; that must not fail the run."""
    transport = StubTransport({"robots.txt": StubResponse(200, DISALLOW_REVIEWS)})
    session = session_for(transport, tmp_path)
    collector = FakeListingCollector(session, {})
    collector.listings = lambda cfg: ["https://example.com/reviews/ajio"]  # type: ignore[assignment]
    assert list(collector.fetch(cfg=None)) == []
    assert collector.robots_blocked


def test_listing_url_is_recorded_in_meta(tmp_path):
    records = walk({"page=1": StubResponse(200, "P1")}, {"P1": [item("a")]}, tmp_path)
    assert records[0].meta["listing_url"] == "https://example.com/reviews-ajio"
    assert records[0].meta["rating"] == 3


def test_make_soup_parses_html():
    soup = make_soup("<div class='review'>runs small</div>")
    assert soup.select_one("div.review").get_text() == "runs small"
