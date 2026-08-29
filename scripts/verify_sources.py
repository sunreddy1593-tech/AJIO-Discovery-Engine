"""Check every configured scrape URL and selector set against the live sites.

The URLs and selectors in ``config.yaml`` were originally written from guesswork,
and a guess that is wrong fails in one of three ways that look nothing alike:

* a **404**, which is obvious;
* a **403 or a bot check**, which looks like a broken selector;
* a **200 serving the wrong page**, which looks like success right up until the
  parse yields nothing — and which actually happened here, when the configured
  MouthShut listing returned a restaurants category page with HTTP 200;
* a **200 serving a well-formed but empty envelope**, which is the worst of the
  four because there is no wrong content to notice. The configured App Store id
  did not exist, and iTunes answers a dead id with a valid feed carrying no
  entries — identical on the wire to an app nobody has reviewed. That source
  recorded ``zero_yield`` for a full run before anyone asked why, which is the
  case that motivated covering API sources here and not only scraped HTML.

Telling those apart during a collection run is expensive and late. This script
does it in one page per listing, before the run:

    .venv\\Scripts\\python.exe scripts\\verify_sources.py
    .venv\\Scripts\\python.exe scripts\\verify_sources.py --source ajio_onsite

It fetches through the same :class:`PoliteSession` the collectors use, so the
robots decision, the delay and the headers are the ones that will apply in
anger — a URL that passes here is a URL the real run can reach. It then hands
the HTML to that source's own ``parse_page``, so the number it reports is items
actually parsed, not selectors hypothetically matched.

Exit code 0 means every enabled source resolved and parsed. A source that is
*expected* to yield nothing (Trustpilot, whose robots.txt disallows the review
paths) is reported as EXPECTED rather than failed, because failing a run for
honouring robots.txt would teach exactly the wrong lesson.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import requests  # noqa: E402

from src.collect.ajio_onsite import (  # noqa: E402
    BASE_URL as AJIO_BASE,
)
from src.collect.ajio_onsite import (  # noqa: E402
    browser_headers,
    extract_product_ids,
)
from src.collect.app_store import FEED_TEMPLATE, parse_feed_page  # noqa: E402
from src.collect.complaints_board import ComplaintsBoardCollector  # noqa: E402
from src.collect.consumer_complaints_in import ConsumerComplaintsInCollector  # noqa: E402
from src.collect.mouthshut import MouthShutCollector  # noqa: E402
from src.collect.scraping import (  # noqa: E402
    ChallengeDetected,
    HttpError,
    PoliteSession,
    RobotsDisallowed,
    RobotsGate,
    ScrapingError,
)
from src.collect.trustpilot import TrustpilotCollector  # noqa: E402
from src.common.config import ConfigFileError, MissingConfigError, get_settings  # noqa: E402
from src.common.encoding import harden_stdio  # noqa: E402


#: iTunes' public metadata endpoints. Not in ``config.yaml`` because they are the
#: API's own shape rather than a collection choice.
LOOKUP_URL = "https://itunes.apple.com/lookup"
SEARCH_URL = "https://itunes.apple.com/search"


class Check:
    """One configured URL and what came back from it."""

    def __init__(self, source: str, url: str, ok: bool, detail: str, expected: bool = False):
        self.source = source
        self.url = url
        self.ok = ok
        self.detail = detail
        self.expected = expected

    @property
    def label(self) -> str:
        if self.ok:
            return "PASS"
        return "EXPECTED" if self.expected else "FAIL"


def _fetch(session: PoliteSession, url: str) -> tuple[str | None, str]:
    """Return ``(html, detail)``; html is None when the fetch did not succeed."""
    try:
        return session.get_text(url), "fetched"
    except RobotsDisallowed as exc:
        return None, f"robots.txt refuses this path: {exc}"
    except ChallengeDetected:
        return None, "bot-check page returned instead of content"
    except HttpError as exc:
        return None, str(exc)
    except ScrapingError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _listing_check(
    source: str, url: str, collector, session: PoliteSession, *, expected_empty: bool = False
) -> Check:
    """Fetch one listing page and parse it with the collector that owns it."""
    html, detail = _fetch(session, url)
    if html is None:
        return Check(source, url, False, detail, expected=expected_empty)

    items = collector.parse_page(html, url)
    if not items:
        # The wrong-page-at-200 case is worth naming explicitly, because the
        # title is usually the fastest way for a human to see what happened.
        title = ""
        try:
            from src.collect.scraping import make_soup

            node = make_soup(html).title
            title = node.get_text(strip=True) if node else ""
        except Exception:
            pass
        return Check(
            source,
            url,
            False,
            f"fetched {len(html):,} bytes but parsed 0 items"
            + (f"; page title is {title!r}" if title else ""),
            expected=expected_empty,
        )

    authors = sum(1 for i in items if i.author)
    dates = sum(1 for i in items if i.created_raw)
    unique = len({i.native_id for i in items})
    return Check(
        source,
        url,
        True,
        f"{len(items)} items, {unique} unique ids, author {authors}/{len(items)}, "
        f"date {dates}/{len(items)}",
    )


def check_ajio(cfg, session: PoliteSession) -> list[Check]:
    """AJIO is checked for product-id yield, which is what the collector needs."""
    checks: list[Check] = []
    for template_name in ("review_api_template", "qa_api_template"):
        template = getattr(cfg, template_name)
        if template and "/api/" in template:
            checks.append(
                Check(
                    "ajio_onsite",
                    template,
                    False,
                    "points into /api/, which AJIO's robots.txt disallows; this can "
                    "never be fetched while respect_robots_txt is true. Set it to \"\".",
                )
            )

    for url in cfg.category_urls:
        html, detail = _fetch(session, url)
        if html is None:
            # A 403 or a bot check here is the settled outcome, not news: browser-
            # grade headers are already being sent and every content path is refused
            # by an Akamai edge, which is the site's access decision and out of
            # scope to defeat (edge-case 1.1.13d). Reported EXPECTED so this script's
            # exit code keeps meaning "something changed" — if the AJIO block failed
            # the run forever, nobody would read the exit code by the third time.
            blocked = "403" in detail or "bot-check" in detail
            checks.append(
                Check(
                    "ajio_onsite",
                    url,
                    False,
                    detail
                    + (
                        "; and hand-collection is not a route around it either — AJIO "
                        "publishes no review or Q&A prose on site, so ajio_manual is "
                        "disabled rather than pending"
                        if blocked
                        else ""
                    ),
                    expected=blocked,
                )
            )
            continue
        ids = extract_product_ids(html)
        if not ids:
            checks.append(
                Check(
                    "ajio_onsite",
                    url,
                    False,
                    f"fetched {len(html):,} bytes but found no /p/<digits> links; "
                    "either this is not a category page or the markup changed",
                )
            )
        else:
            checks.append(
                Check("ajio_onsite", url, True, f"{len(ids)} product ids, e.g. {ids[0]}")
            )
    return checks


def check_app_store(cfg, session: PoliteSession) -> list[Check]:
    """Confirm each configured app id exists before believing its review feed.

    The order matters. Asking the feed first cannot distinguish "no reviews" from
    "no such app", so every id is resolved through the lookup API — which answers
    ``resultCount: 0`` for a dead id — and only then is the feed parsed.
    """
    checks: list[Check] = []
    for app_id in cfg.app_ids:
        for country in cfg.countries:
            lookup_url = f"{LOOKUP_URL}?id={app_id}&country={country}"
            try:
                payload = session.get_json(lookup_url)
            except (HttpError, ScrapingError, RobotsDisallowed, ChallengeDetected) as exc:
                checks.append(Check("app_store", lookup_url, False, f"{type(exc).__name__}: {exc}"))
                continue

            results = payload.get("results") or [] if isinstance(payload, dict) else []
            if not results:
                checks.append(
                    Check(
                        "app_store",
                        lookup_url,
                        False,
                        f"no app with id {app_id} on the {country!r} storefront. Its review "
                        f"feed will still answer 200 with an empty envelope, so this is the "
                        f"failure that looks like an app with no reviews. Find the right id "
                        f"with {SEARCH_URL}?term=<name>&country={country}&entity=software",
                    )
                )
                continue

            name = results[0].get("trackName")
            feed_url = FEED_TEMPLATE.format(country=country, page=1, app_id=app_id)
            try:
                items = parse_feed_page(
                    session.get_json(feed_url), app_id=str(app_id), country=country
                )
            except (HttpError, ScrapingError, RobotsDisallowed, ChallengeDetected) as exc:
                checks.append(Check("app_store", feed_url, False, f"{type(exc).__name__}: {exc}"))
                continue

            if not items:
                checks.append(
                    Check(
                        "app_store",
                        feed_url,
                        False,
                        f"{name!r} exists but its feed page 1 parsed 0 reviews",
                    )
                )
            else:
                rated = sum(1 for i in items if i.meta.get("rating") is not None)
                checks.append(
                    Check(
                        "app_store",
                        feed_url,
                        True,
                        f"{name!r}: {len(items)} reviews on page 1, rating {rated}/{len(items)}",
                    )
                )
    return checks


def build_session(settings, *, extra_headers=None) -> PoliteSession:
    collection = settings.run.collection
    transport = requests.Session()
    gate = RobotsGate(
        transport,
        user_agent=collection.scraper_user_agent,
        compliance_dir=settings.raw_dir / "_compliance",
        enabled=collection.respect_robots_txt,
    )
    return PoliteSession(
        transport,
        user_agent=(extra_headers or {}).get("User-Agent", collection.scraper_user_agent),
        robots=gate,
        delay_seconds=collection.per_domain_delay_seconds,
        extra_headers=extra_headers,
    )


def run_checks(settings, only: str | None) -> list[Check]:
    collection = settings.run.collection
    checks: list[Check] = []

    def wanted(name: str) -> bool:
        return only is None or only == name

    if wanted("ajio_onsite") and collection.ajio_onsite.enabled:
        cfg = collection.ajio_onsite
        session = build_session(
            settings, extra_headers=browser_headers(cfg.browser_user_agent)
        )
        checks.extend(check_ajio(cfg, session))

    if wanted("app_store") and collection.app_store.enabled:
        checks.extend(check_app_store(collection.app_store, build_session(settings)))

    # Every listing source resolves its own URLs from config, so asking the
    # collector rather than rebuilding the join here is what keeps this script
    # checking the URLs the run will actually request.
    listing_sources = (
        ("mouthshut", MouthShutCollector, False),
        ("complaints_board", ComplaintsBoardCollector, False),
        ("consumer_complaints_in", ConsumerComplaintsInCollector, False),
        ("trustpilot", TrustpilotCollector, True),
    )
    for name, collector_cls, expected_empty in listing_sources:
        source_cfg = getattr(collection, name)
        if not wanted(name) or not source_cfg.enabled:
            continue
        session = build_session(settings)
        collector = collector_cls(session)
        for listing in collector.listings(source_cfg):
            url = collector.page_url(listing, 1)
            checks.append(
                _listing_check(name, url, collector, session, expected_empty=expected_empty)
            )

    return checks


def print_table(checks: list[Check]) -> None:
    if not checks:
        print("\n  Nothing to check: no matching source is enabled in config.yaml.\n")
        return
    width = max(len(c.source) for c in checks)
    print()
    print(f"  {'SOURCE'.ljust(width)}  STATUS      DETAIL")
    print(f"  {'-' * width}  ----------  {'-' * 52}")
    for c in checks:
        print(f"  {c.source.ljust(width)}  [{c.label:<8}]  {c.url}")
        print(f"  {' ' * width}              {c.detail}")
    print()


def main(argv: list[str] | None = None) -> int:
    harden_stdio()

    parser = argparse.ArgumentParser(
        description="Verify configured scrape URLs and selector sets against the live sites."
    )
    parser.add_argument("--source", help="check only this source")
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
    except (MissingConfigError, ConfigFileError) as exc:
        print(f"\n  Configuration error:\n\n    {exc}\n")
        return 1

    print(f"\n  config_hash={settings.config_hash[:12]}")
    print("  One page per configured listing, fetched through the collectors' own session.")

    checks = run_checks(settings, args.source)
    print_table(checks)

    failures = [c for c in checks if not c.ok and not c.expected]
    if failures:
        print(
            f"  {len(failures)} configured URL(s) did not resolve or did not parse. Fix these\n"
            "  in config.yaml before collecting: a wrong URL and a blocked client are\n"
            "  indistinguishable once a run is underway.\n"
        )
        return 1

    expected = sorted({c.source for c in checks if c.expected})
    if expected:
        print(
            "  Every enabled source resolved and parsed, except those whose failure is\n"
            f"  the documented outcome: {', '.join(expected)}. Their reasons are in the\n"
            "  DETAIL column above and in the limitations the report has to state.\n"
        )
    else:
        print("  Every enabled source resolved and parsed.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
