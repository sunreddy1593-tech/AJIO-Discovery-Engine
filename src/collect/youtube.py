"""YouTube comments via Data API v3 — a pre-purchase source (`architecture.md` §5).

Haul and try-on review videos attract exactly the "should I buy this" audience the
North Star metric is about, which makes their comment threads one of only three
pre-purchase sources on the roster.

**Quota is the whole design constraint.** ``search.list`` costs 100 units against
a 10,000/day default, so ~90 searches would exhaust the day and leave no budget
for the comments themselves. ``commentThreads.list`` costs 1 unit. So video ids
are resolved once, cached in ``data/raw/youtube/_video_ids.json``, and reused on
every later run; quota then goes almost entirely on comment pagination, which is
what actually produces documents. Pass ``--force`` to re-run the searches.

Two API failures are told apart deliberately (`edge-case.md` §1.1.1–1.1.2):
``quotaExceeded`` stops the source and asks the caller to resume tomorrow, while
``commentsDisabled`` skips one video and continues. Conflating them would either
throw away a day of collection or abort a run over a single locked comment
section.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Protocol

from src.collect.base import Collector, QuotaExhausted
from src.collect.scraping import ParsedItem
from src.common.logging import get_logger
from src.common.schemas import RawRecord

logger = get_logger("collect.youtube")

VIDEO_ID_CACHE_NAME = "_video_ids.json"

#: API error reasons that mean "stop this source", not "skip this item".
QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"})

#: Per-video reasons that must not abort the source.
SKIP_VIDEO_REASONS = frozenset(
    {"commentsDisabled", "videoNotFound", "forbidden", "processingFailure"}
)


class YouTubeClient(Protocol):
    """The googleapiclient resource surface used here, narrowed for testing."""

    def search(self) -> Any: ...
    def commentThreads(self) -> Any: ...  # noqa: N802 - mirrors the Google client


def build_client(api_key: str) -> Any:
    from googleapiclient.discovery import build

    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


def error_reason(exc: Exception) -> str:
    """Extract the API's machine-readable reason from an ``HttpError``.

    The reason string is the only thing that separates "quota gone for the day"
    from "this one video has comments off", and it lives in the JSON body rather
    than the status code — both arrive as 403.
    """
    details = getattr(exc, "error_details", None)
    if details:
        for detail in details:
            if isinstance(detail, dict) and detail.get("reason"):
                return str(detail["reason"])

    content = getattr(exc, "content", None)
    if content:
        try:
            payload = json.loads(content.decode() if isinstance(content, bytes) else content)
            errors = payload.get("error", {}).get("errors", [])
            if errors and errors[0].get("reason"):
                return str(errors[0]["reason"])
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    return ""


# --------------------------------------------------------------------------
# Pure parsing
# --------------------------------------------------------------------------


def parse_search_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Video descriptors from a ``search.list`` response."""
    videos: list[dict[str, Any]] = []
    for item in payload.get("items", []):
        video_id = (item.get("id") or {}).get("videoId")
        if not video_id:
            continue
        snippet = item.get("snippet") or {}
        videos.append(
            {
                "video_id": video_id,
                "video_title": snippet.get("title"),
                "channel": snippet.get("channelTitle"),
                "published_at": snippet.get("publishedAt"),
            }
        )
    return videos


def parse_comment_threads(
    payload: dict[str, Any], video: dict[str, Any], *, include_replies: bool = True
) -> list[ParsedItem]:
    """Flatten a ``commentThreads.list`` response into items.

    Replies are included because a reply is often where the answer to "does it run
    small?" actually lives, and it carries its own author for the distinct-author
    counts that gate reportability.
    """
    items: list[ParsedItem] = []
    video_id = video.get("video_id")

    for thread in payload.get("items", []):
        snippet = thread.get("snippet") or {}
        top = (snippet.get("topLevelComment") or {}).get("snippet") or {}
        top_id = (snippet.get("topLevelComment") or {}).get("id") or thread.get("id")
        if top_id and top.get("textOriginal") or top.get("textDisplay"):
            items.append(
                ParsedItem(
                    native_id=str(top_id),
                    text=top.get("textOriginal") or top.get("textDisplay") or "",
                    url=f"https://www.youtube.com/watch?v={video_id}&lc={top_id}",
                    author=top.get("authorDisplayName"),
                    created_raw=top.get("publishedAt"),
                    meta={
                        "video_id": video_id,
                        "video_title": video.get("video_title"),
                        "channel": video.get("channel"),
                        "like_count": top.get("likeCount"),
                        "is_reply": False,
                        "query_term": video.get("query_term"),
                    },
                )
            )

        if not include_replies:
            continue
        for reply in (thread.get("replies") or {}).get("comments", []):
            reply_snippet = reply.get("snippet") or {}
            reply_id = reply.get("id")
            if not reply_id:
                continue
            items.append(
                ParsedItem(
                    native_id=str(reply_id),
                    text=reply_snippet.get("textOriginal")
                    or reply_snippet.get("textDisplay")
                    or "",
                    url=f"https://www.youtube.com/watch?v={video_id}&lc={reply_id}",
                    author=reply_snippet.get("authorDisplayName"),
                    created_raw=reply_snippet.get("publishedAt"),
                    meta={
                        "video_id": video_id,
                        "video_title": video.get("video_title"),
                        "channel": video.get("channel"),
                        "like_count": reply_snippet.get("likeCount"),
                        "is_reply": True,
                        "parent_id": str(top_id) if top_id else None,
                        "query_term": video.get("query_term"),
                    },
                )
            )
    return items


# --------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------


class YouTubeCollector(Collector):
    """Comments from haul and review videos, resolved from cached video ids."""

    source: ClassVar[str] = "youtube"
    min_expected_records: ClassVar[int] = 200

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: YouTubeClient | None = None,
        cache_dir: Path,
        force_search: bool = False,
    ):
        super().__init__()
        if client is None and api_key is None:
            raise ValueError("YouTubeCollector needs either an api_key or a client")
        self._client = client
        self._api_key = api_key
        self.cache_path = Path(cache_dir) / self.source / VIDEO_ID_CACHE_NAME
        self.force_search = force_search
        self.searches_made = 0
        self.videos_skipped: list[str] = []

    @property
    def client(self) -> YouTubeClient:
        if self._client is None:
            assert self._api_key is not None
            self._client = build_client(self._api_key)
        return self._client

    # --- video id cache ----------------------------------------------------

    def _load_cache(self) -> dict[str, Any]:
        if not self.cache_path.is_file():
            return {"terms": {}}
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self.log.warning("video id cache at %s is corrupt; re-searching", self.cache_path)
            return {"terms": {}}
        payload.setdefault("terms", {})
        return payload

    def _save_cache(self, cache: dict[str, Any]) -> None:
        cache["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )

    def resolve_videos(self, cfg: Any) -> list[dict[str, Any]]:
        """Video descriptors for every configured query term, searching only on a miss.

        Each ``search.list`` costs 100 quota units against a 10,000/day budget, so
        this cache is the difference between spending the day on discovery and
        spending it on comments.
        """
        cache = self._load_cache()
        terms: Sequence[str] = cfg.query_terms
        videos: list[dict[str, Any]] = []
        dirty = False

        for term in terms:
            cached = cache["terms"].get(term)
            if cached and not self.force_search:
                self.log.info("using %s cached video ids for %r", len(cached), term)
            else:
                cached = self._search(term, cfg.max_videos_per_term)
                cache["terms"][term] = cached
                dirty = True
            for video in cached:
                videos.append({**video, "query_term": term})

        if dirty:
            self._save_cache(cache)

        # One video can answer several query terms; comments are fetched once.
        unique: dict[str, dict[str, Any]] = {}
        for video in videos:
            unique.setdefault(video["video_id"], video)
        return list(unique.values())

    def _search(self, term: str, max_videos: int) -> list[dict[str, Any]]:
        self.log.info("search.list for %r (100 quota units)", term)
        try:
            response = (
                self.client.search()
                .list(
                    part="snippet",
                    q=term,
                    type="video",
                    maxResults=min(50, max_videos),
                    relevanceLanguage="en",
                    regionCode="IN",
                    order="relevance",
                )
                .execute()
            )
        except Exception as exc:
            self._raise_if_quota(exc, context=f"search for {term!r}")
            raise
        self.searches_made += 1
        return parse_search_results(response)[:max_videos]

    def _raise_if_quota(self, exc: Exception, *, context: str) -> None:
        reason = error_reason(exc)
        if reason in QUOTA_REASONS:
            raise QuotaExhausted(
                f"YouTube quota exhausted during {context} (reason={reason}). "
                "Collected records are kept; re-run tomorrow after the midnight "
                "Pacific reset to continue."
            ) from exc

    # --- comments ----------------------------------------------------------

    def fetch(self, cfg: Any) -> Iterator[RawRecord]:
        videos = self.resolve_videos(cfg)
        self.log.info("collecting comments from %s videos", len(videos))

        for video in videos:
            try:
                yield from self._video_comments(video, cfg.max_comments_per_video)
            except QuotaExhausted:
                raise
            except Exception as exc:
                reason = error_reason(exc)
                if reason in SKIP_VIDEO_REASONS:
                    self.log.info("skipping video %s (%s)", video["video_id"], reason)
                    self.videos_skipped.append(f"{video['video_id']}:{reason}")
                    continue
                raise

    def _video_comments(self, video: dict[str, Any], max_comments: int) -> Iterator[RawRecord]:
        page_token: str | None = None
        produced = 0

        while produced < max_comments:
            try:
                response = (
                    self.client.commentThreads()
                    .list(
                        part="snippet,replies",
                        videoId=video["video_id"],
                        maxResults=min(100, max_comments - produced),
                        order="relevance",
                        textFormat="plainText",
                        pageToken=page_token,
                    )
                    .execute()
                )
            except Exception as exc:
                self._raise_if_quota(exc, context=f"comments for {video['video_id']}")
                raise

            items = parse_comment_threads(response, video)
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
                    produced += 1
                    if produced >= max_comments:
                        break

            page_token = response.get("nextPageToken")
            if not page_token:
                return


__all__ = [
    "QUOTA_REASONS",
    "SKIP_VIDEO_REASONS",
    "VIDEO_ID_CACHE_NAME",
    "YouTubeCollector",
    "error_reason",
    "parse_comment_threads",
    "parse_search_results",
]
