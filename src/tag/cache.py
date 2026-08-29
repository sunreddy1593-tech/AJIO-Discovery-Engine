"""Per-document tagging cache (plan §4.7, `architecture.md` §7).

Tagging is the expensive stage, and a free-tier run legitimately spans days, so
no document is ever tagged twice. The cache is keyed on everything that can change
a document's tags — the model, the taxonomy version, the prompt version, and the
document's own content — so bumping the prompt or taxonomy correctly misses the
cache and re-tags, while re-running an unchanged corpus issues **zero** Groq calls
(a Phase 4 exit criterion and the real reproducibility mechanism, since Groq's
``seed`` is only best-effort).

Storage is the ``llm_cache`` table from Phase 1; this module owns the key scheme
and the (de)serialization, nothing else.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from src.common.hashing import content_id
from src.common.schemas import DocumentTags


def _tags_blob(tags: DocumentTags) -> str:
    """Serialize the coding, never the document identity.

    ``run_tagging`` passes a ``TaggedDocument`` into :func:`put` — that subclass
    carries ``doc_id`` because Groq's batched response has to name which document
    each coding belongs to. ``DocumentTags`` forbids extra fields, so dumping the
    subclass and reading it back as the parent is what crashed ``--dry-run``:
    ``doc_id: Extra inputs are not permitted``. The id is already in the cache
    key, so storing it again is redundant and the value must stay tag-only.
    """
    payload = tags.model_dump(mode="json")
    payload.pop("doc_id", None)
    return DocumentTags.model_validate(payload).model_dump_json()


def _tags_from_blob(raw: str) -> DocumentTags:
    """Parse a cached coding, dropping a leftover ``doc_id`` if one is present.

    Rows written before the strip in :func:`put` still have the field. Deleting
    them would throw away already-paid tags; ignoring just ``doc_id`` lets those
    rows round-trip, while any other extra key still fails as a real contract
    break.
    """
    payload = json.loads(raw)
    if isinstance(payload, dict):
        payload.pop("doc_id", None)
    return DocumentTags.model_validate(payload)


def cache_key(*, doc_id: str, text: str, model: str, taxonomy_version: str, prompt_version: str) -> str:
    """Stable key over model + versions + document content.

    ``content_id(text)`` is included as well as ``doc_id`` so that if a document's
    text is ever corrected in place, its cached tags are invalidated rather than
    silently reused.
    """
    return content_id(f"{model}|{taxonomy_version}|{prompt_version}|{doc_id}|{content_id(text)}")


def get(conn: sqlite3.Connection, key: str) -> DocumentTags | None:
    row = conn.execute(
        "SELECT response_json FROM llm_cache WHERE cache_key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    return _tags_from_blob(row[0])


def put(
    conn: sqlite3.Connection,
    key: str,
    tags: DocumentTags,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> None:
    """Store one document's tags and the token counts the call cost.

    Token counts are per call in Groq's response, not per document; the caller
    amortizes a batch's usage across its members before calling this, so the
    numbers stored here support honest cost reporting in the appendix.
    """
    conn.execute(
        """
        INSERT INTO llm_cache
            (cache_key, response_json, prompt_tokens, completion_tokens, reasoning_tokens, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (
            key,
            _tags_blob(tags),
            prompt_tokens,
            completion_tokens,
            reasoning_tokens,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def token_totals(conn: sqlite3.Connection) -> dict[str, int]:
    """Sum of cached token counts, for the cost line in the report appendix."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(prompt_tokens), 0),
               COALESCE(SUM(completion_tokens), 0),
               COALESCE(SUM(reasoning_tokens), 0),
               COUNT(*)
        FROM llm_cache
        """
    ).fetchone()
    return {
        "prompt_tokens": row[0],
        "completion_tokens": row[1],
        "reasoning_tokens": row[2],
        "cached_documents": row[3],
    }
