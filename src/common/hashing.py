"""Stable identifiers and fingerprints (`architecture.md` §6).

Everything here must return the same value on every machine and every run, or the
"re-runnable without re-scraping" guarantee breaks: ids are how a second run
recognizes what the first already collected.

That rules out Python's built-in ``hash()``, which is randomized per process for
strings unless ``PYTHONHASHSEED`` is fixed. A simhash built on it would silently
produce different fingerprints on every run and quietly change which documents
are considered duplicates, so every hash below is an explicit cryptographic one.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

#: Truncation length for ids. 64 bits of sha256 is far beyond collision risk at
#: corpus sizes of ~10^4 while keeping ids readable in logs and query output.
ID_LENGTH = 16

#: Stand-in for scraped reviews with no author (`edge-case.md` §1.2.11). Hashing a
#: shared sentinel keeps the column non-null, but every anonymous record from a
#: source collapses to one hash, so these must be excluded from distinct-author
#: counts rather than treated as one very prolific person.
ANONYMOUS_AUTHOR = "__anonymous__"

SIMHASH_BITS = 64
_SIMHASH_HEX_LENGTH = SIMHASH_BITS // 4

#: Shingle width for simhash, chosen by measurement rather than convention.
#: Single tokens ignore word order entirely; trigrams are so sensitive that adding
#: one sentence to a 27-word review moved the fingerprint 11 bits. Bigrams gave the
#: widest margin between near-duplicates (max 9 bits) and unrelated reviews (min
#: 22 bits) on the calibration set, which is what NEAR_DUPLICATE_MAX_HAMMING rests on.
_SHINGLE_WIDTH = 2

#: Bit distance below which two documents are treated as near-duplicates.
#:
#: The architecture originally specified 3, a figure that comes from simhash on
#: web-scale documents with thousands of features. On 25-100 word reviews it is far
#: too tight: a single reworded phrase already costs 6 bits, so a threshold of 3
#: would have marked almost nothing and left cross-posted duplicates in the corpus
#: inflating prevalence. Provisional until re-calibrated on the real corpus in
#: Phase 3, where the duplicate-marking audit can measure it against hand labels.
NEAR_DUPLICATE_MAX_HAMMING = 12

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_QUOTE_LINE_RE = re.compile(r"^\s*>.*$", re.MULTILINE)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _sha256_hex(payload: str, length: int = ID_LENGTH) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def doc_id(source: str, source_native_id: str) -> str:
    """Stable synthetic id for a document.

    Derived from the source and its native id, so re-collecting the same review
    produces the same ``doc_id`` and the unique constraint absorbs it.
    """
    return _sha256_hex(f"{source}|{source_native_id}")


def author_hash(source: str, author_raw: str | None, salt: str) -> str:
    """Salted, source-scoped hash of an author handle.

    The raw handle is never persisted. The hash still supports per-author
    aggregation, which is what stops one prolific poster from inflating a finding
    (`architecture.md` §8). Scoping by source is deliberate: the same handle on two
    sites is not evidently the same person, and assuming so would understate
    distinct-author counts.

    The salt comes from ``.env`` and must not change once a corpus exists, or every
    author hash changes and author-level aggregation silently resets.
    """
    handle = (author_raw or "").strip() or ANONYMOUS_AUTHOR
    return _sha256_hex(f"{source}|{handle}{salt}")


def anonymous_author_hash(source: str, salt: str) -> str:
    """The hash every author-less record from ``source`` collapses to.

    Exposed so quantification can exclude it from distinct-author counts instead
    of hard-coding the sentinel in two places.
    """
    return author_hash(source, ANONYMOUS_AUTHOR, salt)


def content_id(text: str) -> str:
    """Identity for manually imported content whose only stable trait is its text.

    A Quora answer saved to a file has no native id, and the filename is not
    stable — a human renaming ``thread1.txt`` must not create a second copy of the
    same document (`edge-case.md` §1.2.8). Normalizing first means incidental
    whitespace or case differences between two saves also collapse to one id.
    """
    normalized = normalize_for_fingerprint(text)
    if not normalized:
        raise ValueError("cannot derive a content_id from text with no words")
    return _sha256_hex(normalized)


def normalize_for_fingerprint(text: str) -> str:
    """Reduce text to the form used for duplicate detection.

    Applied before both ``content_id`` and ``simhash_fingerprint`` so the two
    always agree about what counts as "the same text". Quoted lines are dropped
    because a reply that quotes its parent would otherwise look like a duplicate
    of it (`edge-case.md` §2.4), and URLs are dropped because a shared link is not
    shared content (§2.7).
    """
    text = unicodedata.normalize("NFKC", text)
    text = _QUOTE_LINE_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    return " ".join(_WORD_RE.findall(text.casefold()))


def _shingles(normalized: str) -> list[str]:
    tokens = normalized.split()
    if len(tokens) <= _SHINGLE_WIDTH:
        return tokens
    return [
        " ".join(tokens[i : i + _SHINGLE_WIDTH])
        for i in range(len(tokens) - _SHINGLE_WIDTH + 1)
    ]


def simhash_fingerprint(text: str) -> str:
    """64-bit simhash of ``text``, as 16 hex characters.

    Implemented here rather than taken from a library: ``datasketch``, which the
    plan originally named, provides only MinHash and LSH. Those measure Jaccard
    similarity between sets and offer no bitwise fingerprint, so the Hamming
    distance threshold the architecture specifies cannot be expressed with them.

    Similar texts produce fingerprints differing in few bits, so near-duplicates
    are found by Hamming distance rather than by comparing every pair. Text with
    no words yields an all-zero fingerprint; callers must not treat two such
    documents as duplicates of each other.

    Normalization runs first, so documents differing only in case, whitespace, or
    punctuation come out bit-identical rather than merely close.
    """
    shingles = _shingles(normalize_for_fingerprint(text))
    if not shingles:
        return "0" * _SIMHASH_HEX_LENGTH

    weights = [0] * SIMHASH_BITS
    for shingle in shingles:
        digest = hashlib.blake2b(shingle.encode("utf-8"), digest_size=SIMHASH_BITS // 8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(SIMHASH_BITS):
            weights[bit] += 1 if (value >> bit) & 1 else -1

    fingerprint = 0
    for bit in range(SIMHASH_BITS):
        if weights[bit] > 0:
            fingerprint |= 1 << bit
    return f"{fingerprint:0{_SIMHASH_HEX_LENGTH}x}"


def hamming_distance(left: str, right: str) -> int:
    """Bit difference between two hex fingerprints of equal width."""
    if len(left) != len(right):
        raise ValueError(f"fingerprint widths differ: {len(left)} vs {len(right)}")
    return bin(int(left, 16) ^ int(right, 16)).count("1")
