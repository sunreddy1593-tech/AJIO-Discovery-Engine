"""Turn a collected ``RawRecord`` into a normalized ``Document`` (Phase 3).

Normalization does three things and deliberately no more:

1. **De-identify.** The raw author handle is hashed with the salted, per-source
   ``author_hash`` and the plaintext handle is dropped entirely — it never
   reaches the ``documents`` table, so nothing downstream can re-identify an
   author (`architecture.md` §5, plan §3.2.1).
2. **Derive the cheap structural fields** every later gate reads — ``word_count``
   for the ``min_words`` rule, ``char_len`` for the ``min_chars`` pre-filter, and
   the deterministic ``doc_id`` that is the row's identity.
3. **Nothing else.** Exclusion, dedup and relevance are separate stages so the
   funnel counts stay honest and each rule can be unit-tested in isolation.

Language is *not* decided here. The Hindi exclusion in ``exclusions.py`` owns
that call so a single stage is responsible for it; ``lang`` is carried straight
from the collector's ``meta.lang`` when present as a weak hint only.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from src.common.hashing import author_hash, doc_id
from src.common.schemas import Document, RawRecord

# Word tokenization matches the exclusion rule (plan §3.1): collapse whitespace,
# strip punctuation-only tokens, count what remains. Defined once and imported by
# exclusions.py so the count that de-identifies and the count that excludes can
# never drift apart.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def word_count(text: str) -> int:
    """Whitespace/punctuation-aware word count, shared with the too_short rule."""
    return len(_WORD_RE.findall(text))


def normalize_record(record: RawRecord, *, salt: str) -> Document:
    """Map one validated ``RawRecord`` to a ``Document`` ready for the gates.

    ``is_relevant``, ``exclusion_reason`` and ``is_duplicate_of`` are left unset;
    the exclusion, dedup and relevance stages fill them in order, each recording
    why it did so, so a row can always explain its own fate.
    """
    text = record.text  # already stripped + encoding-checked by RawRecord
    return Document(
        doc_id=doc_id(record.source, record.source_native_id),
        source=record.source,
        source_native_id=record.source_native_id,
        url=record.url,
        author_hash=author_hash(record.source, record.author_raw, salt),
        created_utc=record.created_utc,
        text=text,
        lang=(record.meta or {}).get("lang"),
        char_len=len(text),
        meta=dict(record.meta or {}),
        word_count=word_count(text),
        ingested_at=datetime.now(timezone.utc),
    )
