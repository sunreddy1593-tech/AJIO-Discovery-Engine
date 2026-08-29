"""Two-pass duplicate detection (plan §3.2.4, `architecture.md` §3).

Cross-posted and boilerplate reviews inflate prevalence if counted twice, so
duplicates are *marked*, never deleted: each duplicate row gets ``is_duplicate_of``
pointing at the canonical row it repeats, and quantification counts one member per
group. Keeping the rows makes the merge auditable.

Two passes, cheap before expensive:

1. **Exact fingerprint.** ``simhash_fingerprint`` is deterministic, so identical
   normalized text yields an identical 64-bit fingerprint; a hash-map groups exact
   repeats in one pass.
2. **Near-duplicate simhash.** Survivors are compared by Hamming distance on the
   same fingerprint; anything within ``near_duplicate_hamming`` bits of an earlier
   canonical row is a near-duplicate. The threshold is calibrated (12, not the
   web-scale 3) because on review-length text a single reworded phrase already
   costs ~6 bits (config comment / edge-case §2.3b).

Short texts are exempt from the *near*-duplicate pass: below
``near_duplicate_min_words`` the fingerprint is dominated by too few shingles for
Hamming distance to mean anything, so two unrelated short reviews would collide.
They still participate in exact-match dedup.

Determinism: canonical choice is the row with the smallest ``doc_id`` in a group,
so the same corpus always elects the same representative regardless of read order.
"""

from __future__ import annotations

from collections.abc import Sequence

from src.common.hashing import hamming_distance, simhash_fingerprint
from src.common.schemas import Document
from src.store.normalize import word_count


def _fingerprint(document: Document) -> str:
    if document.text_fingerprint:
        return document.text_fingerprint
    return simhash_fingerprint(document.text)


def assign_fingerprints(documents: Sequence[Document]) -> None:
    """Populate ``text_fingerprint`` in place for every document."""
    for document in documents:
        document.text_fingerprint = _fingerprint(document)


def mark_duplicates(
    documents: Sequence[Document],
    *,
    near_duplicate_hamming: int,
    near_duplicate_min_words: int,
) -> dict[str, int]:
    """Mark exact and near-duplicates in place; return a counts summary.

    Only documents that are still eligible — not excluded — are considered, since a
    row already dropped by an exclusion rule should not anchor or absorb a group.
    """
    assign_fingerprints(documents)

    eligible = [
        d for d in documents if d.exclusion_reason is None and d.is_duplicate_of is None
    ]
    # Stable, deterministic order: canonical = smallest doc_id seen first.
    eligible.sort(key=lambda d: d.doc_id)

    exact = 0
    near = 0

    # Pass 1 — exact fingerprint groups.
    seen_exact: dict[str, Document] = {}
    for doc in eligible:
        canonical = seen_exact.get(doc.text_fingerprint)
        if canonical is None:
            seen_exact[doc.text_fingerprint] = doc
        else:
            doc.is_duplicate_of = canonical.doc_id
            exact += 1

    # Pass 2 — near-duplicate by Hamming distance among the exact-canonical rows.
    canonicals: list[Document] = [
        d for d in eligible if d.is_duplicate_of is None
    ]
    accepted: list[Document] = []
    for doc in canonicals:
        if word_count(doc.text) < near_duplicate_min_words:
            accepted.append(doc)  # too short to trust simhash; keep as its own row
            continue
        match = None
        for other in accepted:
            if word_count(other.text) < near_duplicate_min_words:
                continue
            if hamming_distance(doc.text_fingerprint, other.text_fingerprint) <= near_duplicate_hamming:
                match = other
                break
        if match is None:
            accepted.append(doc)
        else:
            doc.is_duplicate_of = match.doc_id
            near += 1

    # Flatten any chain (A->B->C) to its root (A->C, B->C). A near pass can
    # re-point an exact-canonical, leaving a two-hop link that would violate the
    # self-FK on insert; path compression makes every duplicate point at a root
    # whose own is_duplicate_of is None.
    by_id = {d.doc_id: d for d in documents}

    def root(doc_id: str, _seen: set[str]) -> str:
        target = by_id[doc_id].is_duplicate_of
        if target is None or target in _seen:
            return doc_id
        _seen.add(doc_id)
        return root(target, _seen)

    for doc in documents:
        if doc.is_duplicate_of is not None:
            resolved = root(doc.is_duplicate_of, {doc.doc_id})
            doc.is_duplicate_of = None if resolved == doc.doc_id else resolved

    return {
        "exact_duplicates": exact,
        "near_duplicates": near,
        "unique": len(accepted),
    }
