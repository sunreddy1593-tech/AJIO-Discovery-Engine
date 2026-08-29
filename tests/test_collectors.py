"""Per-source parsers, exercised against saved fixtures rather than the network.

Parsing is deliberately separated from fetching in every collector, which is what
makes this file possible: each test is a pure function of a string. The fixtures
are small hand-written approximations of each site's shape, so they verify the
parser's contract — ids, text, meta fields, stage separation — rather than
pinning today's exact class names.

The AJIO and Quora tests carry the most weight. AJIO because ``meta.content_type``
is what keeps pre- and post-purchase evidence apart, and Quora because its
compliance guarantee is asserted in code here rather than trusted to a docstring.
"""

from __future__ import annotations

import ast
from datetime import timezone
from pathlib import Path

import pytest

from src.collect import ajio_manual, ajio_onsite, app_store, complaints_board
from src.collect import consumer_complaints_in
from src.collect import mouthshut, play_store, quora_manual, trustpilot, youtube
from src.collect.base import EmptyImportError
from src.collect.scraping import RobotsDisallowed
from src.common.schemas import AjioContentType, PurchaseStage, purchase_stage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --- MouthShut ------------------------------------------------------------

MOUTHSHUT_HTML = """
<html><body>
<div class="review-article" id="div_review_1001">
  <a class="reviewdata-title" href="/websites/ajiocom-reviews-925915881-1234567">Sizes are a lottery</a>
  <div class="profile"><a href="/user/asha">asha_r</a></div>
  <div class="review-date">12 May 2026</div>
  <div class="rating"><span>2 of 5</span></div>
  <div class="reviewdata">I kept three kurtas in my wishlist for weeks because the size chart
  never matches what arrives. Finally ordered a medium and it was tight everywhere.</div>
</div>
<div class="review-article">
  <a class="reviewdata-title" href="/no-numeric-id">Return process</a>
  <div class="reviewdata">The return pickup was delayed by a week and nobody answered the helpline
  even after repeated attempts to reach them about it.</div>
</div>
</body></html>
"""


def test_mouthshut_parses_id_text_rating_and_date():
    items = mouthshut.parse_reviews(MOUTHSHUT_HTML, "https://www.mouthshut.com/websites/ajiocom")
    assert len(items) == 2

    first = items[0]
    assert first.native_id == "1234567"
    assert "size chart" in first.text
    assert first.author == "asha_r"
    assert first.meta["rating"] == 2.0
    assert first.meta["content_type"] == "review"
    assert first.created_utc().year == 2026


def test_mouthshut_falls_back_to_a_content_hash_when_no_id_exists():
    """A site without stable ids must still not produce duplicates across runs."""
    items = mouthshut.parse_reviews(MOUTHSHUT_HTML, "https://www.mouthshut.com/x")
    again = mouthshut.parse_reviews(MOUTHSHUT_HTML, "https://www.mouthshut.com/x")
    assert items[1].native_id == again[1].native_id
    assert len(items[1].native_id) == 16


def test_mouthshut_page_url_appends_the_page_suffix():
    collector = mouthshut.MouthShutCollector(session=None)  # type: ignore[arg-type]
    listing = "https://www.mouthshut.com/websites/ajiocom-reviews-925915881"
    assert collector.page_url(listing, 1) == listing
    assert collector.page_url(listing, 3).endswith("-page-3")


def test_unparseable_html_yields_nothing_which_the_walk_turns_into_a_raise():
    assert mouthshut.parse_reviews("<html><body><p>redesigned</p></body></html>", "u") == []


# --- ComplaintsBoard and ConsumerComplaints.in ---------------------------

COMPLAINTS_HTML = """
<html><body>
<div class="complaint-item">
  <a class="complaint__title" href="/ajio-wrong-size-delivered-c8891234">Wrong size delivered</a>
  <span class="complaint__author">ravi</span>
  <time datetime="2026-04-02T10:00:00Z">2 April 2026</time>
  <span class="complaint__status">Resolved</span>
  <div class="complaint__text">Ordered a large after checking the chart twice and received a small.
  My order id 4051234567 and phone 9876543210 are in every email I sent.</div>
</div>
</body></html>
"""


def test_complaints_board_parses_status_title_and_iso_date():
    items = complaints_board.parse_complaints(
        COMPLAINTS_HTML, "https://www.complaintsboard.com/ajio", company_path="/ajio"
    )
    assert len(items) == 1
    item = items[0]
    assert item.native_id == "8891234"
    assert item.meta["status"] == "Resolved"
    assert item.meta["company_path"] == "/ajio"
    assert item.created_utc().astimezone(timezone.utc).month == 4


def test_complaint_pii_is_redacted_when_the_record_is_built():
    """The parser keeps raw text; build() is the single redaction chokepoint."""
    collector = complaints_board.ComplaintsBoardCollector(session=None)  # type: ignore[arg-type]
    item = complaints_board.parse_complaints(COMPLAINTS_HTML, "https://x/ajio")[0]
    record = collector.build(
        source_native_id=item.native_id, text=item.text, meta=item.meta
    )
    assert record is not None
    assert "4051234567" not in record.text
    assert "9876543210" not in record.text


def test_consumer_complaints_pagination_and_parse():
    collector = consumer_complaints_in.ConsumerComplaintsInCollector(session=None)  # type: ignore[arg-type]
    listing = "https://www.consumercomplaints.in/ajio-b115930"
    assert collector.page_url(listing, 1) == listing
    assert collector.page_url(listing, 4) == f"{listing}/page/4"

    html = COMPLAINTS_HTML.replace("complaint-item", "complaint").replace(
        "complaint__text", "complaint-detail"
    ).replace("complaint__title", "complaint-title").replace(
        "complaint__author", "author"
    ).replace("complaint__status", "status")
    items = consumer_complaints_in.parse_complaints(html, "https://www.consumercomplaints.in/x")
    assert items and "Ordered a large" in items[0].text


# The two fixtures below are trimmed from the markup actually served on
# 2026-08-19, with the personal details replaced. They exist because the
# hand-written approximations above passed for months against selector sets that
# matched nothing on the real pages: an invented fixture can only ever prove the
# parser is self-consistent. Names and ids here are synthetic; the *structure* is
# the part being pinned.

COMPLAINTS_BOARD_LIVE_HTML = """
<html><body>
<div class="complaint">
 <div class="complaint__wrapper">
  <div class="author-header">
   <div class="author-header__content">
    <div class="author-header__row" itemprop="author" itemscope itemtype="https://schema.org/Person">
     <span class="author-header__user">
      <span class="author-header__name" itemprop="name">a shopper</span>
      <span class="author-header__address">of Bengaluru, IN</span>
     </span>
    </div>
    <div class="author-header__row author-header__row_flex">
     <span class="author-header__date" itemprop="datePublished">Jan 20, 2024</span>
     <span class="author-header__time">12:06 am UTC</span>
     <div class="author-header__user--verified">
      <span>Verified customer</span>
      <span class="author-header__tooltip">Confirmed via Google.
       <a href="/faq#verified_user">Learn more</a></span>
     </div>
    </div>
   </div>
  </div>
  <div class="complaint-main">
   <div class="complaint-main__content">
    <h3 class="complaint-main__header">
     <span itemprop="itemReviewed" itemscope itemtype="https://schema.org/Organization"
           style="display:none"><span itemprop="name">Ajio</span></span>
     <span class="complaint-main__header-name" itemprop="about">Seller has not refunded money</span>
    </h3>
    <div class="complaint-main__accordion">
     <div class="complaint-main__accordion-panel js-cl-short-text">
      <p class="complaint-main__text" itemprop="reviewBody">I returned one of four items and the
       refund never arrived. Order number FN4058405688 was picked up weeks ago.</p>
     </div>
    </div>
    <a href="/images/complaint/full/1931240/1705727190483648752.png"></a>
    <a href="#create-comment-1931240">Add a comment</a>
   </div>
  </div>
 </div>
</div>
</body></html>
"""


def test_complaints_board_reads_the_live_microdata_layout():
    """The real listing has no per-complaint permalink and no class named 'title'."""
    url = "https://www.complaintsboard.com/ajio-b144612"
    items = complaints_board.parse_complaints(
        COMPLAINTS_BOARD_LIVE_HTML, url, company_path="/ajio-b144612"
    )
    assert len(items) == 1
    item = items[0]
    assert item.author == "a shopper"
    assert item.created_raw == "Jan 20, 2024"
    assert item.meta["complaint_title"] == "Seller has not refunded money"
    assert "refund never arrived" in item.text
    # The id leaks only through the comment anchor and the attachment path.
    assert item.native_id == "1931240"
    # And crucially not the /faq link that is the first anchor in the block.
    assert item.url == f"{url}#c1931240"


CONSUMER_COMPLAINTS_LIVE_HTML = """
<html><body>
<div class="complaint-box" id="c3541211">
 <div class="complaint-box__box">
  <div class="complaint-box__header">
   <div class="complaint-box__ins">
    <div class="author-box">
     <div class="author-box__column">
      <div class="author-box__row-profile">
       <div class="author-box__user"><b class="author-box__user_bold">a_shopper</b></div>
      </div>
      <div class="author-box__row-info">
       <div class="author-box__date">Apr 28, 2026</div>
      </div>
     </div>
    </div>
   </div>
   <h4 class="complaint-box__title">
    <a class="complaint-box__link" href="/ajio-delivery-delay-and-finally-order-cancel-by-ajio-c3541211"
       id="cmcc3541211">Delivery delay and finally order cancel by ajio</a>
   </h4>
  </div>
  <div class="complaint-box__text" id="cctxt3541211">
   <div class="complaint-box__more-txt">
    <div>I waited twenty five days for two orders and then both were cancelled by the
     seller without any explanation at all....</div>
   </div>
  </div>
 </div>
</div>
</body></html>
"""


def test_consumer_complaints_reads_the_live_complaint_box_layout():
    url = "https://www.consumercomplaints.in/ajio-b115930"
    items = consumer_complaints_in.parse_complaints(
        CONSUMER_COMPLAINTS_LIVE_HTML, url, company_path="/ajio-b115930"
    )
    assert len(items) == 1
    item = items[0]
    # The block's own id attribute is preferred over re-deriving it from the href.
    assert item.native_id == "3541211"
    assert item.author == "a_shopper"
    assert item.created_raw == "Apr 28, 2026"
    assert item.url.endswith("-c3541211")
    assert "waited twenty five days" in item.text
    assert item.meta["complaint_title"].startswith("Delivery delay")


# --- App Store ------------------------------------------------------------

APP_STORE_FEED = {
    "feed": {
        "entry": [
            {  # the app metadata entry: no rating, must be skipped
                "id": {"label": "https://apps.apple.com/in/app/id1039873143"},
                "im:name": {"label": "AJIO"},
            },
            {
                "id": {"label": "9988776655"},
                "title": {"label": "Sizes inconsistent"},
                "content": {"label": "Every brand fits differently so I never trust the chart."},
                "im:rating": {"label": "2"},
                "im:version": {"label": "9.4.1"},
                "author": {"name": {"label": "shopper_in"}},
                "updated": {"label": "2026-06-01T09:00:00-07:00"},
                "link": {"attributes": {"href": "https://apps.apple.com/review/9988776655"}},
            },
        ]
    }
}


def test_app_store_skips_the_metadata_entry_and_keeps_reviews():
    items = app_store.parse_feed_page(APP_STORE_FEED, app_id="1039873143", country="in")
    assert len(items) == 1
    item = items[0]
    assert item.native_id == "9988776655"
    assert item.text.startswith("Sizes inconsistent")
    assert item.meta["rating"] == 2.0
    assert item.meta["app_version"] == "9.4.1"
    assert item.created_utc().year == 2026


def test_app_store_handles_a_single_unwrapped_entry_and_junk():
    single = {"feed": {"entry": APP_STORE_FEED["feed"]["entry"][1]}}
    assert len(app_store.parse_feed_page(single, app_id="1", country="in")) == 1
    assert app_store.parse_feed_page({}, app_id="1", country="in") == []
    assert app_store.parse_feed_page("not json", app_id="1", country="in") == []


def test_app_store_feed_ceiling_is_respected():
    """~500 reviews is the feed's own limit, not something to retry past (1.1.3)."""
    assert app_store.MAX_FEED_PAGES == 10


# --- Play Store -----------------------------------------------------------


def test_play_store_maps_a_library_row():
    row = {
        "reviewId": "abc-123",
        "content": "App keeps losing my wishlist between sessions which is infuriating.",
        "userName": "Nisha",
        "score": 1,
        "thumbsUpCount": 12,
        "reviewCreatedVersion": "9.1.0",
        "replyContent": "We are sorry to hear this.",
    }
    item = play_store.map_review(row, app_id="com.ril.ajio", lang="en", country="in")
    assert item is not None
    assert item.native_id == "abc-123"
    assert item.meta["rating"] == 1
    assert item.meta["thumbs_up"] == 12
    assert item.meta["reply"] is True
    assert item.meta["brand"] == "ajio"


def test_play_store_skips_rows_with_no_text_and_marks_comparison_apps():
    assert play_store.map_review({"reviewId": "x"}, app_id="a", lang="en", country="in") is None
    item = play_store.map_review(
        {"reviewId": "y", "content": "fine app"}, app_id="com.myntra.android", lang="en", country="in"
    )
    assert item is not None and item.meta["brand"] == "comparison"


def test_play_store_stops_when_the_token_loops():
    """Edge case 1.1.4: a repeating token would otherwise page forever."""
    rows = [{"reviewId": "same", "content": "the size chart is wrong for every brand here"}]

    calls = {"n": 0}

    def fake_reviews(app_id, *, lang, country, count, token):
        calls["n"] += 1
        return rows, "always-the-same-token"

    collector = play_store.PlayStoreCollector(reviews_fn=fake_reviews)

    class Cfg:
        app_ids = ["com.ril.ajio"]
        languages = ["en"]
        countries = ["in"]
        max_reviews = 500

    records = list(collector.fetch(Cfg()))
    assert len(records) == 1
    assert calls["n"] == 2  # second page returned only seen ids, then stopped


# --- YouTube --------------------------------------------------------------

SEARCH_RESPONSE = {
    "items": [
        {
            "id": {"videoId": "vid1"},
            "snippet": {
                "title": "AJIO try on haul",
                "channelTitle": "StyleWithAsha",
                "publishedAt": "2026-03-01T00:00:00Z",
            },
        },
        {"id": {"kind": "channel"}, "snippet": {"title": "not a video"}},
    ]
}

THREADS_RESPONSE = {
    "items": [
        {
            "id": "thread1",
            "snippet": {
                "topLevelComment": {
                    "id": "c1",
                    "snippet": {
                        "textOriginal": "Does the medium run small? It has been in my wishlist for weeks.",
                        "authorDisplayName": "curious_shopper",
                        "publishedAt": "2026-03-05T10:00:00Z",
                        "likeCount": 4,
                    },
                }
            },
            "replies": {
                "comments": [
                    {
                        "id": "r1",
                        "snippet": {
                            "textOriginal": "I sized up and it fit perfectly for reference.",
                            "authorDisplayName": "helpful_person",
                            "publishedAt": "2026-03-06T10:00:00Z",
                            "likeCount": 1,
                        },
                    }
                ]
            },
        }
    ],
    "nextPageToken": None,
}


def test_youtube_search_parsing_ignores_non_video_results():
    videos = youtube.parse_search_results(SEARCH_RESPONSE)
    assert len(videos) == 1
    assert videos[0]["video_id"] == "vid1"
    assert videos[0]["channel"] == "StyleWithAsha"


def test_youtube_comment_and_reply_become_separate_documents():
    """A reply is often where the fit answer lives, and it has its own author."""
    video = {"video_id": "vid1", "video_title": "AJIO try on haul", "channel": "StyleWithAsha"}
    items = youtube.parse_comment_threads(THREADS_RESPONSE, video)
    assert [i.native_id for i in items] == ["c1", "r1"]
    assert items[0].meta["is_reply"] is False
    assert items[1].meta["is_reply"] is True
    assert items[1].meta["parent_id"] == "c1"
    assert items[0].meta["like_count"] == 4


def test_youtube_replies_can_be_excluded():
    video = {"video_id": "vid1"}
    items = youtube.parse_comment_threads(THREADS_RESPONSE, video, include_replies=False)
    assert len(items) == 1


def test_quota_and_skip_reasons_are_distinct_sets():
    """Conflating them either loses a day of collection or aborts on one locked video."""
    assert "quotaExceeded" in youtube.QUOTA_REASONS
    assert "commentsDisabled" in youtube.SKIP_VIDEO_REASONS
    assert not (youtube.QUOTA_REASONS & youtube.SKIP_VIDEO_REASONS)


def test_error_reason_is_read_from_the_api_body():
    class FakeHttpError(Exception):
        content = b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}'

    assert youtube.error_reason(FakeHttpError()) == "quotaExceeded"
    assert youtube.error_reason(ValueError("nothing structured")) == ""


def test_video_ids_are_cached_so_search_quota_is_spent_once(tmp_path):
    """search.list costs 100 units; commentThreads costs 1. The cache is the design."""

    class FakeSearch:
        def __init__(self, counter):
            self.counter = counter

        def list(self, **kwargs):
            self.counter["n"] += 1
            return self

        def execute(self):
            return SEARCH_RESPONSE

    class FakeClient:
        def __init__(self):
            self.counter = {"n": 0}

        def search(self):
            return FakeSearch(self.counter)

        def commentThreads(self):  # pragma: no cover - not used here
            raise AssertionError

    class Cfg:
        query_terms = ["ajio haul"]
        max_videos_per_term = 5

    client = FakeClient()
    first = youtube.YouTubeCollector(client=client, cache_dir=tmp_path)
    assert len(first.resolve_videos(Cfg())) == 1
    assert client.counter["n"] == 1
    assert (tmp_path / "youtube" / youtube.VIDEO_ID_CACHE_NAME).is_file()

    second = youtube.YouTubeCollector(client=client, cache_dir=tmp_path)
    second.resolve_videos(Cfg())
    assert client.counter["n"] == 1  # no second search

    forced = youtube.YouTubeCollector(client=client, cache_dir=tmp_path, force_search=True)
    forced.resolve_videos(Cfg())
    assert client.counter["n"] == 2


# --- AJIO ----------------------------------------------------------------

CATEGORY_HTML = """
<html><body>
<a href="/puma-men-round-neck-tshirt/p/469558637_black">one</a>
<a href="/women-kurta/p/441029001_blue">two</a>
<a href="/women-kurta/p/441029001_blue">dup</a>
<a href="/some/other/page">not a product</a>
</body></html>
"""

LD_JSON_HTML = """
<html><head>
<script type="application/ld+json">
{"@type": "Product", "name": "Kurta", "brand": {"name": "AJIO Own"},
 "review": [{"@type": "Review", "reviewBody": "Fabric is thinner than it looks in photos.",
             "author": {"name": "meera"}, "datePublished": "2026-02-11",
             "reviewRating": {"ratingValue": "3"}, "name": "Thin fabric"}]}
</script>
</head><body></body></html>
"""

QA_PAYLOAD = {
    "questions": [
        {
            "questionId": "q77",
            "questionText": "Does this kurta run small? I usually take a medium.",
            "userName": "asha",
            "submissionTime": "2026-05-01",
            "answers": [{"answerText": "Yes, size up."}, {"answerText": "Fits me fine."}],
        }
    ]
}

REVIEW_PAYLOAD = {
    "reviews": [
        {
            "reviewId": "rv12",
            "reviewText": "Ordered a medium and it was tight across the shoulders.",
            "userName": "vikram",
            "rating": 2,
            "submissionTime": "2026-05-04",
            "sizeBought": "M",
            "fitFeedback": "tight",
        }
    ]
}


def test_product_ids_are_resolved_from_any_product_link():
    """Regex over /p/<digits> survives redesigns that break every selector (1.1.15)."""
    assert ajio_onsite.extract_product_ids(CATEGORY_HTML) == ["469558637", "441029001"]


def test_ld_json_reviews_are_parsed_with_rating_and_brand():
    items = ajio_onsite.parse_ld_json_reviews(LD_JSON_HTML, "441029")
    assert len(items) == 1
    item = items[0]
    assert "thinner" in item.text
    assert item.meta["content_type"] == AjioContentType.REVIEW.value
    assert item.meta["rating"] == 3.0
    assert item.meta["brand"] == "AJIO Own"
    assert item.native_id.startswith("review-441029-")


def test_qa_payload_yields_the_question_with_answers_as_metadata():
    """Answers come from buyers, so promoting them would mislabel post-purchase voice."""
    items = ajio_onsite.parse_qa_api(QA_PAYLOAD, "441029")
    assert len(items) == 1
    item = items[0]
    assert item.text.startswith("Does this kurta run small?")
    assert item.meta["content_type"] == AjioContentType.QA.value
    assert item.meta["answer_count"] == 2
    assert item.meta["answers"] == ["Yes, size up.", "Fits me fine."]


def test_review_payload_captures_size_and_fit_fields():
    items = ajio_onsite.parse_review_api(REVIEW_PAYLOAD, "441029")
    assert items[0].meta["size_bought"] == "M"
    assert items[0].meta["fit_feedback"] == "tight"
    assert items[0].native_id == "review-441029-rv12"


def test_ajio_content_type_decides_the_purchase_stage():
    """Edge case 1.1.14: conflating Q&A with reviews destroys the whole analysis."""
    qa = ajio_onsite.parse_qa_api(QA_PAYLOAD, "441029")[0]
    review = ajio_onsite.parse_review_api(REVIEW_PAYLOAD, "441029")[0]
    assert purchase_stage("ajio_onsite", qa.meta) is PurchaseStage.PRE_PURCHASE
    assert purchase_stage("ajio_onsite", review.meta) is PurchaseStage.POST_PURCHASE


def test_every_ajio_record_carries_a_content_type():
    """Without it, RawRecord validation refuses the record outright."""
    collector = ajio_onsite.AjioOnsiteCollector(session=None)  # type: ignore[arg-type]
    item = ajio_onsite.parse_qa_api(QA_PAYLOAD, "441029")[0]
    record = collector.build(
        source_native_id=item.native_id, text=item.text, meta=item.meta
    )
    assert record is not None
    assert record.purchase_stage is PurchaseStage.PRE_PURCHASE

    orphan = collector.build(source_native_id="qa-x", text="a question with no content type at all")
    assert orphan is None  # rejected, and counted
    assert collector.rejected == 1


def test_browser_headers_carry_more_than_a_user_agent():
    """A bare UA is itself the tell; Sec-Fetch-* absence is what flags a bot (1.1.13)."""
    headers = ajio_onsite.browser_headers("Mozilla/5.0 Chrome/140")
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert "Sec-Fetch-Mode" in headers
    assert "Accept-Language" in headers


def test_no_resolvable_products_raises_rather_than_yielding_nothing():
    """Losing the only rich pre-purchase source silently is the failure to avoid."""

    class Cfg:
        product_urls: list[str] = []
        category_urls: list[str] = []
        max_products = 10
        max_reviews_per_product = 5
        max_qa_per_product = 5

    collector = ajio_onsite.AjioOnsiteCollector(session=None)  # type: ignore[arg-type]
    with pytest.raises(ajio_onsite.AjioBlockedError, match="post-purchase"):
        list(collector.fetch(Cfg()))


def test_a_robots_disallowed_api_template_is_disabled_after_one_refusal():
    """AJIO's robots.txt disallows /api/*, and the verdict is the same every time.

    Left to the generic handler this was swallowed at debug level and re-requested
    once per product, which read in the logs as "the endpoint returned nothing"
    rather than "this endpoint is forbidden and always will be".
    """

    class RefusingSession:
        def __init__(self):
            self.calls = 0

        def get_json(self, url, **kwargs):
            self.calls += 1
            raise RobotsDisallowed(f"{url}: path disallowed by robots.txt")

        def get_text(self, url, **kwargs):
            return "<html><body></body></html>"

    session = RefusingSession()
    collector = ajio_onsite.AjioOnsiteCollector(
        session,  # type: ignore[arg-type]
        qa_api_template="https://www.ajio.com/api/p/{product_id}/questions?pageNo={page}",
    )

    class Cfg:
        max_qa_per_product = 5

    collector._collect_qa("441029", Cfg())
    collector._collect_qa("441030", Cfg())

    assert session.calls == 1
    assert collector.qa_api_template == ""
    assert collector.disabled_templates == ["qa"]


# --- Trustpilot ----------------------------------------------------------


def test_trustpilot_reads_the_embedded_next_data_payload():
    html = """
    <html><body><script id="__NEXT_DATA__" type="application/json">
    {"props": {"pageProps": {"reviews": [
      {"id": "tp1", "title": "Never again", "text": "Returns took a month to process.",
       "rating": 1, "consumer": {"displayName": "Sam"},
       "dates": {"publishedDate": "2026-01-05T00:00:00Z"}}]}}}
    </script></body></html>
    """
    items = trustpilot.parse_reviews(html, "u", domain="ajio.com")
    assert len(items) == 1
    assert items[0].native_id == "tp1"
    assert items[0].meta["rating"] == 1
    assert items[0].meta["domain"] == "ajio.com"


def test_trustpilot_expects_zero_yield_without_failing():
    """robots.txt disallows /reviews/, so zero is the compliant outcome (1.1.10)."""
    assert trustpilot.parse_reviews("<html></html>", "u") == []
    assert trustpilot.TrustpilotCollector.raise_on_empty_first_page is False
    assert trustpilot.TrustpilotCollector.min_expected_records == 0


# --- AJIO manual import --------------------------------------------------

AJIO_MANUAL_FILE = """product: https://www.ajio.com/p/469558637
title: Puma Men Round Neck T-shirt

## Q&A

Q: Does this run small? I am usually a medium and between sizes on most brands.
A: Yes, order one size up.
A: True to size for me.

Q: Is the white one see-through in daylight?

## Reviews

[2] Kept this in my wishlist for a month because the size chart contradicts
the brand's own chart. Ordered anyway and the shoulders were tight.
- by meera, 12 May 2026
"""


def test_manual_sections_decide_content_type_and_never_the_prose():
    """Edge case 1.1.14, restated for hand-typed input where mixing is easiest."""
    payloads, warnings = ajio_manual.parse_file(_written(AJIO_MANUAL_FILE))
    assert warnings == []

    kinds = [p["meta"]["content_type"] for p in payloads]
    assert kinds == [
        AjioContentType.QA.value,
        AjioContentType.QA.value,
        AjioContentType.REVIEW.value,
    ]
    assert all(p["meta"]["product_id"] == "469558637" for p in payloads)
    assert all(p["url"] == "https://www.ajio.com/p/469558637" for p in payloads)
    assert all(p["meta"]["extraction"] == "manual_import" for p in payloads)


def test_manual_answers_stay_metadata_on_the_question():
    """Answers come from buyers, so promoting them mislabels post-purchase voice."""
    payloads, _ = ajio_manual.parse_file(_written(AJIO_MANUAL_FILE))
    question = payloads[0]
    assert question["text"].startswith("Does this run small?")
    assert question["meta"]["answers"] == ["Yes, order one size up.", "True to size for me."]
    assert question["meta"]["answer_count"] == 2
    assert not any("order one size up" == p["text"] for p in payloads)


def test_manual_review_captures_rating_author_and_date():
    payloads, _ = ajio_manual.parse_file(_written(AJIO_MANUAL_FILE))
    review = payloads[-1]
    assert review["meta"]["rating"] == 2.0
    assert review["author"] == "meera"
    assert review["created_raw"] == "12 May 2026"
    assert review["text"].startswith("Kept this in my wishlist")


def test_manual_block_without_a_header_is_skipped_not_guessed():
    """Guessing the content type is the one failure this source cannot absorb."""
    payloads, warnings = ajio_manual.parse_file(
        _written("product: 469558637\n\nRuns quite small across the board, be warned.\n")
    )
    assert payloads == []
    assert any("content type is never guessed" in w for w in warnings)


def test_manual_q_prefix_may_stand_in_for_a_header():
    payloads, warnings = ajio_manual.parse_file(
        _written("product: 469558637\n\nQ: Will this shrink in a machine wash?\n")
    )
    assert warnings == []
    assert len(payloads) == 1
    assert payloads[0]["meta"]["content_type"] == AjioContentType.QA.value


def test_manual_block_without_a_product_id_is_skipped():
    payloads, warnings = ajio_manual.parse_file(
        _written("## Q&A\n\nQ: Does this run small?\n")
    )
    assert payloads == []
    assert any("no product id" in w for w in warnings)


def test_manual_product_directive_switches_products_mid_file():
    """One file can hold a morning's browsing across several product pages."""
    payloads, _ = ajio_manual.parse_file(
        _written(
            "product: 469558637\n\n## Q&A\n\nQ: Does this run small on the shoulders?\n\n"
            "product: 441029001\n\nQ: Is the fabric lined or single layer?\n"
        )
    )
    assert [p["meta"]["product_id"] for p in payloads] == ["469558637", "441029001"]


def test_manual_short_question_survives_the_collector():
    """"Does this run small?" is four words, and Phase 3 — not this parser — is what
    applies the length rule. Keeping the layers separate is what made it possible to
    fix the gate by changing one config value: had this parser enforced its own
    length floor, lowering `min_words` to 3 would have silently done nothing here."""
    payloads, _ = ajio_manual.parse_file(
        _written("product: 469558637\n\n## Q&A\n\nQ: Does this run small?\n")
    )
    assert [p["text"] for p in payloads] == ["Does this run small?"]


def test_manual_content_type_decides_the_purchase_stage():
    payloads, _ = ajio_manual.parse_file(_written(AJIO_MANUAL_FILE))
    assert purchase_stage("ajio_manual", payloads[0]["meta"]) is PurchaseStage.PRE_PURCHASE
    assert purchase_stage("ajio_manual", payloads[-1]["meta"]) is PurchaseStage.POST_PURCHASE


def test_manual_renaming_a_file_does_not_create_a_duplicate(tmp_path):
    """Edge case 1.2.8: identity is the content, never the filename.

    This is also why there is no filename fallback for the product id, tempting
    though it is — it would make ``source_native_id`` depend on the file's name.
    """
    first = tmp_path / "notes1.md"
    first.write_text(AJIO_MANUAL_FILE, encoding="utf-8")
    renamed = tmp_path / "ajio-puma-tshirt.md"
    renamed.write_text(AJIO_MANUAL_FILE, encoding="utf-8")

    assert [p["native_id"] for p in ajio_manual.parse_file(first)[0]] == [
        p["native_id"] for p in ajio_manual.parse_file(renamed)[0]
    ]


def test_manual_collector_reads_a_directory_and_stays_offline(tmp_path):
    (tmp_path / "data" / "manual" / "ajio").mkdir(parents=True)
    (tmp_path / "data" / "manual" / "ajio" / "puma.md").write_text(
        AJIO_MANUAL_FILE, encoding="utf-8"
    )

    class Cfg:
        import_dir = "data/manual/ajio"

    collector = ajio_manual.AjioManualCollector(project_root=tmp_path)
    records = list(collector.fetch(Cfg()))
    assert len(records) == 3
    assert all(r.source == "ajio_manual" for r in records)
    assert [r.purchase_stage for r in records] == [
        PurchaseStage.PRE_PURCHASE,
        PurchaseStage.PRE_PURCHASE,
        PurchaseStage.POST_PURCHASE,
    ]
    assert collector.makes_network_calls is False
    assert collector.files_read == ["puma.md"]


def test_manual_missing_import_directory_says_so_rather_than_crashing(tmp_path):
    """A missing directory is announced, not crashed on and not passed over.

    ``EmptyImportError`` rather than an empty iterator, because the two are
    indistinguishable in a summary table and one of them is a problem: this source
    stands in for the blocked ``ajio_onsite``, so an unfilled directory means the
    corpus has no AJIO evidence at all. The runner catches it and records
    ``empty_import``, so one unfilled source still cannot fail the other eight.
    """
    class Cfg:
        import_dir = "data/manual/ajio"

    collector = ajio_manual.AjioManualCollector(project_root=tmp_path)
    with pytest.raises(EmptyImportError) as raised:
        list(collector.fetch(Cfg()))
    assert "data" in str(raised.value)


def test_manual_import_ignores_its_own_readme(tmp_path):
    """A directory holding only instructions must collect nothing.

    Observed live: `data/manual/quora/` contained just the README documenting the
    expected format, and because that README is written *in* the format, it parsed
    into nine pre-purchase documents. A source nobody had filled yet reported
    records, and the instructions would have reached tagging as evidence.
    """
    manual = tmp_path / "data" / "manual" / "ajio"
    manual.mkdir(parents=True)
    (manual / "README.md").write_text(AJIO_MANUAL_FILE, encoding="utf-8")
    (manual / "_scratch.md").write_text(AJIO_MANUAL_FILE, encoding="utf-8")

    class Cfg:
        import_dir = "data/manual/ajio"

    collector = ajio_manual.AjioManualCollector(project_root=tmp_path)
    with pytest.raises(EmptyImportError):
        list(collector.fetch(Cfg()))

    (manual / "puma.md").write_text(AJIO_MANUAL_FILE, encoding="utf-8")
    assert len(list(collector.fetch(Cfg()))) == 3


def test_quora_manual_import_ignores_its_own_readme(tmp_path):
    manual = tmp_path / "data" / "manual" / "quora"
    manual.mkdir(parents=True)
    (manual / "README.md").write_text(QUORA_THREAD, encoding="utf-8")

    class Cfg:
        import_dir = "data/manual/quora"

    collector = quora_manual.QuoraManualCollector(project_root=tmp_path)
    with pytest.raises(EmptyImportError):
        list(collector.fetch(Cfg()))


def test_ajio_manual_module_imports_no_network_library():
    """The fallback for a blocked source must not be able to fetch anything.

    ``ajio_onsite`` is forbidden alongside the HTTP libraries: it owns a
    ``PoliteSession`` and imports ``scraping``, so importing it would put a
    network path one attribute access away and make this guarantee hollow.
    """
    source_path = PROJECT_ROOT / "src" / "collect" / "ajio_manual.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    imported: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            modules.add(node.module)

    forbidden = {
        "requests", "urllib", "urllib3", "http", "httpx", "aiohttp",
        "socket", "praw", "selenium", "playwright", "mechanize", "ftplib",
        "curl_cffi", "tls_client",
    }
    assert not (imported & forbidden), f"ajio_manual must make no network calls: {imported & forbidden}"
    assert not modules & {"src.collect.scraping", "src.collect.ajio_onsite"}


def _written(text: str) -> Path:
    """Write ``text`` to a temp file, since parse_file takes a path."""
    import tempfile

    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8"
    )
    handle.write(text)
    handle.close()
    return Path(handle.name)


# --- Quora ---------------------------------------------------------------

QUORA_THREAD = """Why do I keep saving clothes on Ajio and never buying them?

I have about forty items saved right now. Most of them are things I liked but was
not sure would fit me, and the size charts are inconsistent between brands.

Answer 2:

For me it is entirely about waiting for a sale. I add things to the wishlist and
wait for the end of season discount, then half of them are out of stock.

short one
"""


def test_quora_thread_splits_into_question_and_answers():
    question, answers = quora_manual.split_thread(QUORA_THREAD)
    assert question is not None and question.startswith("Why do I keep saving")
    assert len(answers) == 2  # "short one" is below MIN_ANSWER_CHARS
    assert "size charts are inconsistent" in answers[0]
    assert "end of season discount" in answers[1]


def test_quora_boilerplate_is_stripped():
    thread = "Why is my wishlist so long?\n\n12 views\nUpvote\nShare\n\n" + "x" * 60
    question, answers = quora_manual.split_thread(thread)
    assert question == "Why is my wishlist so long?"
    assert answers and "Upvote" not in answers[0]


def test_each_answer_becomes_its_own_document_carrying_the_question(tmp_path):
    """A 5,000-word blob would otherwise be tagged once and counted once (1.2.9)."""
    path = tmp_path / "ajio-wishlist.txt"
    path.write_text(QUORA_THREAD, encoding="utf-8")

    payloads = quora_manual.parse_file(path)
    assert len(payloads) == 2
    assert all(p["meta"]["question"].startswith("Why do I keep saving") for p in payloads)
    assert [p["meta"]["answer_index"] for p in payloads] == [0, 1]
    assert payloads[0]["meta"]["source_file"] == "ajio-wishlist.txt"


def test_renaming_a_file_does_not_create_a_duplicate_document(tmp_path):
    """Edge case 1.2.8: identity is the content, never the filename."""
    first = tmp_path / "thread1.txt"
    first.write_text(QUORA_THREAD, encoding="utf-8")
    renamed = tmp_path / "ajio-sizing-thread.txt"
    renamed.write_text(QUORA_THREAD, encoding="utf-8")

    assert [p["native_id"] for p in quora_manual.parse_file(first)] == [
        p["native_id"] for p in quora_manual.parse_file(renamed)
    ]


def test_quora_collector_reads_a_directory_and_stays_offline(tmp_path):
    (tmp_path / "data" / "manual" / "quora").mkdir(parents=True)
    (tmp_path / "data" / "manual" / "quora" / "t1.md").write_text(QUORA_THREAD, encoding="utf-8")

    class Cfg:
        import_dir = "data/manual/quora"

    collector = quora_manual.QuoraManualCollector(project_root=tmp_path)
    records = list(collector.fetch(Cfg()))
    assert len(records) == 2
    assert all(r.source == "quora_manual" for r in records)
    assert all(r.url is None and r.author_raw is None for r in records)
    assert collector.makes_network_calls is False


def test_missing_import_directory_says_so_rather_than_crashing(tmp_path):
    class Cfg:
        import_dir = "data/manual/quora"

    collector = quora_manual.QuoraManualCollector(project_root=tmp_path)
    with pytest.raises(EmptyImportError):
        list(collector.fetch(Cfg()))


def test_quora_module_imports_no_network_library():
    """The compliance guarantee is only as strong as the code (edge-case 1.1.12).

    Quora's robots.txt prohibits bot use of its content for AI/ML systems, so this
    module must be provably incapable of fetching. Asserted by reading the import
    statements rather than by trusting the docstring above them.
    """
    source_path = PROJECT_ROOT / "src" / "collect" / "quora_manual.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {
        "requests", "urllib", "urllib3", "http", "httpx", "aiohttp",
        "socket", "praw", "selenium", "playwright", "mechanize", "ftplib",
    }
    assert not (imported & forbidden), f"quora_manual must make no network calls: {imported & forbidden}"

    # scraping.py owns the only HTTP path, so importing it would be a backdoor.
    assert "src.collect.scraping" not in {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
