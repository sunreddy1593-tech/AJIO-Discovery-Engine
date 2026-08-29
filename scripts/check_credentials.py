"""Verify every external credential with one trivial live call each.

Run this before Phase 2. It is deliberately the only script that tolerates
broken configuration, since diagnosing broken configuration is its job.

    .venv\\Scripts\\python.exe scripts\\check_credentials.py

Exit code 0 means every provider passed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.common.config import (  # noqa: E402
    ConfigFileError,
    MissingConfigError,
    get_settings,
    missing_credentials,
    missing_optional_credentials,
)

# platform:app-id:version (by /u/username)
USER_AGENT_PATTERN = re.compile(r"^[\w.-]+:[\w.-]+:[\w.\-+]+\s+\(by\s+/u/[\w-]+\)$")

# A public, long-lived video used only to spend 1 quota unit.
YOUTUBE_PROBE_VIDEO_ID = "dQw4w9WgXcQ"


class Result:
    def __init__(self, provider: str, ok: bool, detail: str):
        self.provider = provider
        self.ok = ok
        self.detail = detail


def check_groq(settings) -> list[Result]:
    results: list[Result] = []
    try:
        from groq import Groq

        client = Groq(api_key=settings.credentials.groq_api_key.get_secret_value())
    except Exception as exc:
        return [Result("Groq (auth)", False, f"{type(exc).__name__}: {exc}")]

    # 1. Auth + model availability. models.list() costs no tokens.
    pinned = settings.run.model.name
    triage = settings.run.model.triage_name
    try:
        available = {m.id for m in client.models.list().data}
        missing = [m for m in (pinned, triage) if m not in available]
        if missing:
            results.append(
                Result(
                    "Groq (models)",
                    False,
                    f"not available to this key: {', '.join(missing)}",
                )
            )
        else:
            results.append(Result("Groq (models)", True, f"{pinned} + {triage} available"))
    except Exception as exc:
        return [Result("Groq (auth)", False, f"{type(exc).__name__}: {exc}")]

    # 2. A real inference call on the cheap triage model.
    #
    # GPT-OSS models spend reasoning tokens out of max_completion_tokens before
    # emitting any content, so a tight cap returns an empty string and looks like
    # a pass (edge-case 4.1.4). The budget is generous and empty content is
    # treated as a failure precisely so that trap is caught here, not in Phase 4.
    try:
        completion = client.chat.completions.create(
            model=triage,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            temperature=0,
            max_completion_tokens=512,
            reasoning_effort="low",
        )
        choice = completion.choices[0]
        reply = (choice.message.content or "").strip()
        usage = completion.usage
        detail = (
            f"{triage} replied {reply!r} "
            f"(completion_tokens={usage.completion_tokens}, finish={choice.finish_reason})"
        )
        if not reply:
            results.append(
                Result(
                    "Groq (inference)",
                    False,
                    f"{triage} returned empty content; raise max_completion_tokens. {detail}",
                )
            )
        else:
            results.append(Result("Groq (inference)", True, detail))
    except Exception as exc:
        results.append(Result("Groq (inference)", False, f"{type(exc).__name__}: {exc}"))

    # 3. The real tagging schema against the real model.
    #
    # Deliberately the production schema rather than a simplified stand-in: every
    # strict-mode rejection found so far came from a detail a toy schema would not
    # have (an unbounded number field, a mixed numeric enum). A round trip that
    # parses back into TaggingResponse is the only evidence that Phase 4's contract
    # actually holds against this account and this model.
    try:
        from src.common.schemas import TaggingResponse, tagging_response_schema

        completion = client.chat.completions.create(
            model=pinned,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Code each document against the taxonomy. Every tag asserted in a "
                        "multi-label dimension needs an evidence entry quoting the document "
                        "verbatim. Use an empty list when a dimension does not apply; never "
                        "invent a value the dimension does not list. Return one entry per "
                        "input document with its doc_id unchanged."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "[a1] This kurta has been in my wishlist for a month, "
                        "I still cannot tell if medium runs small"
                    ),
                },
            ],
            temperature=settings.run.model.temperature,
            max_completion_tokens=settings.run.model.max_completion_tokens,
            reasoning_effort=settings.run.model.reasoning_effort,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "document_tags",
                    "strict": settings.run.model.strict,
                    "schema": tagging_response_schema(),
                },
            },
        )
        content = (completion.choices[0].message.content or "").strip()
        if not content:
            raise ValueError("empty content under strict decoding")
        parsed = TaggingResponse.model_validate_json(content)
        if not parsed.documents:
            raise ValueError("strict decoding returned zero documents")
        details = getattr(completion.usage, "completion_tokens_details", None)
        reasoning = getattr(details, "reasoning_tokens", None) if details else None
        results.append(
            Result(
                "Groq (tagging schema)",
                True,
                f"{pinned} round-tripped the real schema; {len(parsed.documents)} doc(s), "
                f"reasoning_tokens={reasoning}",
            )
        )
    except Exception as exc:
        results.append(
            Result("Groq (tagging schema)", False, f"{pinned}: {type(exc).__name__}: {exc}")
        )

    return results


def check_reddit(settings) -> list[Result]:
    """Reddit is optional and disabled by default; only checked when configured."""
    results: list[Result] = []
    user_agent = settings.credentials.reddit_user_agent or ""

    # Phase 0 risk: a malformed user agent gets throttled hard in Phase 2 rather
    # than failing here, so validate the format explicitly.
    if USER_AGENT_PATTERN.match(user_agent):
        results.append(Result("Reddit (user agent)", True, user_agent))
    else:
        results.append(
            Result(
                "Reddit (user agent)",
                False,
                f"{user_agent!r} does not match "
                "'<platform>:<app-id>:<version> (by /u/<username>)'",
            )
        )

    try:
        import praw

        assert settings.credentials.reddit_client_id is not None
        assert settings.credentials.reddit_client_secret is not None
        reddit = praw.Reddit(
            client_id=settings.credentials.reddit_client_id.get_secret_value(),
            client_secret=settings.credentials.reddit_client_secret.get_secret_value(),
            user_agent=user_agent,
            check_for_async=False,
        )
        reddit.read_only = True
        submission = next(iter(reddit.subreddit("india").new(limit=1)))
        results.append(Result("Reddit (read)", True, f"fetched submission {submission.id}"))
    except StopIteration:
        results.append(Result("Reddit (read)", False, "authenticated but returned no submissions"))
    except Exception as exc:
        results.append(Result("Reddit (read)", False, f"{type(exc).__name__}: {exc}"))

    return results


def check_youtube(settings) -> list[Result]:
    try:
        from googleapiclient.discovery import build

        youtube = build(
            "youtube",
            "v3",
            developerKey=settings.credentials.youtube_api_key.get_secret_value(),
            cache_discovery=False,
        )
        response = (
            youtube.videos().list(part="id", id=YOUTUBE_PROBE_VIDEO_ID).execute()
        )
        found = len(response.get("items", []))
        return [Result("YouTube (read)", True, f"videos.list returned {found} item(s), 1 unit spent")]
    except Exception as exc:
        return [Result("YouTube (read)", False, f"{type(exc).__name__}: {exc}")]


def print_table(results: list[Result]) -> None:
    width = max(len(r.provider) for r in results)
    print()
    print(f"  {'PROVIDER'.ljust(width)}  STATUS  DETAIL")
    print(f"  {'-' * width}  ------  {'-' * 48}")
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        print(f"  {r.provider.ljust(width)}  [{status}]  {r.detail}")
    print()


def main() -> int:
    # stdout is forced to UTF-8 because provider errors can contain non-ASCII
    # and this script must not die with UnicodeEncodeError (edge-case 0.1).
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")

    absent = missing_credentials()
    if absent:
        print("\n  Missing credentials in .env:\n")
        for name in absent:
            print(f"    - {name}")
        print("\n  Copy .env.example to .env and fill these in, then re-run.\n")
        return 1

    try:
        settings = get_settings()
    except (MissingConfigError, ConfigFileError) as exc:
        print(f"\n  Configuration error:\n\n    {exc}\n")
        return 1

    print(f"\n  Config OK. model={settings.run.model.name}  config_hash={settings.config_hash[:12]}")
    enabled = settings.run.collection.enabled_sources()
    print(f"  Enabled sources: {', '.join(enabled)}")

    skipped = [name for name in missing_optional_credentials()]
    if skipped:
        print(f"  Optional credentials not set: {', '.join(skipped)}")

    results: list[Result] = []
    results.extend(check_groq(settings))
    results.extend(check_youtube(settings))

    # Reddit only matters if it is both enabled and credentialed.
    if settings.run.collection.reddit.enabled:
        if settings.credentials.has_reddit:
            results.extend(check_reddit(settings))
        else:
            results.append(
                Result(
                    "Reddit",
                    False,
                    "enabled in config.yaml but REDDIT_* credentials are not set in .env",
                )
            )
    else:
        print("  Reddit: disabled in config.yaml, skipping check")

    print_table(results)

    failures = [r for r in results if not r.ok]
    if failures:
        print(f"  {len(failures)} check(s) failed.\n")
        return 1

    print("  All credentials verified.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
