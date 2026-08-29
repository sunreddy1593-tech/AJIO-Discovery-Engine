"""Reddit — **disabled by default**, collector retained.

Reddit is the richest source of "still sitting in my wishlist because..." language
anywhere, and dropping it is the largest single compromise in this project's
corpus: with it off, only three sources speak to pre-purchase hesitation and one
of them is manual (`implementation-plan.md` §2.1).

So the collector is kept working rather than deleted. To restore the source, set
``collection.reddit.enabled: true`` in ``config.yaml`` and add ``REDDIT_CLIENT_ID``,
``REDDIT_CLIENT_SECRET`` and ``REDDIT_USER_AGENT`` to ``.env``.
``check_credentials.py`` already fails with a message naming both the flag and the
keys if one is present without the other (`edge-case.md` §1.1.16), and it validates
the user-agent format, because a malformed one gets throttled hard rather than
rejected outright.

Three Reddit-specific data shapes are handled here (§1.1.17): ``[deleted]`` and
``[removed]`` bodies carry no content and are skipped, absent authors collapse to a
``__deleted__`` sentinel rather than being dropped, and ``replace_more(limit=0)``
prevents a single thread from expanding into thousands of extra API calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, ClassVar

from src.collect.base import Collector
from src.common.schemas import RawRecord

#: Bodies with no content left to analyse.
DELETED_BODIES = frozenset({"[deleted]", "[removed]", "[deleted by user]"})

DELETED_AUTHOR = "__deleted__"


def build_client(client_id: str, client_secret: str, user_agent: str) -> Any:
    import praw

    reddit = praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent,
        check_for_async=False,
    )
    reddit.read_only = True
    return reddit


def _created_utc(value: float | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


class RedditCollector(Collector):
    """Search-driven submission and comment collection across configured subreddits."""

    source: ClassVar[str] = "reddit"
    min_expected_records: ClassVar[int] = 0

    def __init__(self, client: Any):
        super().__init__()
        self.client = client
        self.skipped_deleted = 0

    def fetch(self, cfg: Any) -> Iterator[RawRecord]:
        per_query = max(1, cfg.max_posts // max(1, len(cfg.subreddits) * len(cfg.queries)))

        for subreddit_name in cfg.subreddits:
            subreddit = self.client.subreddit(subreddit_name)
            for query in cfg.queries:
                try:
                    submissions = subreddit.search(query, limit=per_query, sort="relevance")
                    for submission in submissions:
                        yield from self._submission_records(
                            submission, subreddit_name, query, cfg.include_comments
                        )
                except Exception as exc:
                    # One failed query must not lose the other subreddits.
                    self.log.warning("search %r in r/%s failed: %s", query, subreddit_name, exc)
                    continue

    def _submission_records(
        self, submission: Any, subreddit_name: str, query: str, include_comments: bool
    ) -> Iterator[RawRecord]:
        title = getattr(submission, "title", "") or ""
        body = getattr(submission, "selftext", "") or ""

        if body.strip() in DELETED_BODIES:
            body = ""
        text = f"{title}\n\n{body}".strip() if body else title

        record = self.build(
            source_native_id=str(getattr(submission, "id", "")),
            text=text,
            url=f"https://www.reddit.com{getattr(submission, 'permalink', '')}",
            author_raw=self._author(submission),
            created_utc=_created_utc(getattr(submission, "created_utc", None)),
            meta={
                "subreddit": subreddit_name,
                "score": getattr(submission, "score", None),
                "num_comments": getattr(submission, "num_comments", None),
                "is_comment": False,
                "parent_id": None,
                "query": query,
            },
        )
        if record is not None:
            yield record

        if not include_comments:
            return

        try:
            # limit=0 removes MoreComments placeholders instead of expanding them,
            # which would otherwise cost one API call each on a large thread.
            submission.comments.replace_more(limit=0)
            comments = submission.comments.list()
        except Exception as exc:
            self.log.warning("could not expand comments for %s: %s", submission, exc)
            return

        for comment in comments:
            comment_body = getattr(comment, "body", "") or ""
            if comment_body.strip() in DELETED_BODIES:
                self.skipped_deleted += 1
                continue
            comment_record = self.build(
                source_native_id=str(getattr(comment, "id", "")),
                text=comment_body,
                url=f"https://www.reddit.com{getattr(comment, 'permalink', '')}",
                author_raw=self._author(comment),
                created_utc=_created_utc(getattr(comment, "created_utc", None)),
                meta={
                    "subreddit": subreddit_name,
                    "score": getattr(comment, "score", None),
                    "num_comments": None,
                    "is_comment": True,
                    "parent_id": str(getattr(comment, "parent_id", "") or ""),
                    "query": query,
                },
            )
            if comment_record is not None:
                yield comment_record

    @staticmethod
    def _author(item: Any) -> str:
        author = getattr(item, "author", None)
        if author is None:
            return DELETED_AUTHOR
        name = getattr(author, "name", None) or str(author)
        return name or DELETED_AUTHOR


__all__ = ["DELETED_AUTHOR", "DELETED_BODIES", "RedditCollector", "build_client"]
