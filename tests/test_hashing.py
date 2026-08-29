"""Identifiers must be identical across runs, processes, and machines.

If they are not, a second run cannot recognize what the first collected, and the
"re-runnable without re-scraping" guarantee is silently false.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from src.common.hashing import (
    ANONYMOUS_AUTHOR,
    ID_LENGTH,
    NEAR_DUPLICATE_MAX_HAMMING,
    anonymous_author_hash,
    author_hash,
    content_id,
    doc_id,
    hamming_distance,
    normalize_for_fingerprint,
    simhash_fingerprint,
)

SALT = "test-salt"


def test_doc_id_is_stable_and_source_scoped():
    assert doc_id("youtube", "abc") == doc_id("youtube", "abc")
    assert doc_id("youtube", "abc") != doc_id("reddit", "abc")
    assert len(doc_id("youtube", "abc")) == ID_LENGTH


def test_author_hash_never_reveals_the_handle():
    hashed = author_hash("youtube", "sunayana123", SALT)
    assert "sunayana123" not in hashed
    assert len(hashed) == ID_LENGTH


def test_author_hash_is_salted_and_source_scoped():
    """The same handle on two sites is not evidently the same person."""
    assert author_hash("youtube", "u", SALT) != author_hash("youtube", "u", "other-salt")
    assert author_hash("youtube", "u", SALT) != author_hash("mouthshut", "u", SALT)


def test_missing_author_collapses_to_a_recognizable_sentinel():
    """Edge case 1.2.11: anonymous records must be excludable from author counts."""
    expected = anonymous_author_hash("mouthshut", SALT)
    for absent in (None, "", "   "):
        assert author_hash("mouthshut", absent, SALT) == expected
    assert expected == author_hash("mouthshut", ANONYMOUS_AUTHOR, SALT)


def test_content_id_survives_renaming_and_whitespace_differences():
    """Edge case 1.2.8: a manually saved file's identity is its text, not its name."""
    original = "Why do I keep saving dresses on Ajio and never actually buying them?"
    respaced = "  Why do I keep saving dresses  on Ajio\nand never actually buying them?  "
    assert content_id(original) == content_id(respaced)
    assert content_id(original) != content_id(original + " Also the sizing is confusing.")


def test_content_id_refuses_wordless_text():
    with pytest.raises(ValueError):
        content_id("!!! ???")


def test_normalization_drops_urls_and_quoted_lines():
    """Edge cases 2.4 and 2.7: a shared link is not shared content, and a quote is not a copy."""
    assert normalize_for_fingerprint("see https://ajio.com/p/123 for size") == "see for size"
    assert normalize_for_fingerprint("> parent said this\nmy reply here") == "my reply here"


def test_simhash_is_deterministic_across_processes():
    """Built-in hash() is randomized per process; this asserts we did not use it.

    A per-process fingerprint would change which documents are considered
    duplicates on every run, without ever raising an error.
    """
    text = "the medium size runs small so i left it in my wishlist for now"
    expected = simhash_fingerprint(text)
    code = (
        "from src.common.hashing import simhash_fingerprint;"
        f"print(simhash_fingerprint({text!r}))"
    )
    for seed in ("0", "1", "random"):
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": seed, "PATH": ""},
            check=True,
        )
        assert result.stdout.strip() == expected


BASE = (
    "I added this kurta to my wishlist last month but I am still not sure whether "
    "the medium size will fit me properly and the size chart is confusing"
)

NEAR_DUPLICATES = (
    BASE.replace("confusing", "unclear"),
    BASE.replace("not sure", "unsure"),
    BASE.replace("wishlist", "wishlst"),
    BASE + " Please help me decide soon.",
    "Hi everyone. " + BASE,
)

UNRELATED = (
    "The delivery agent never showed up and customer support closed my complaint "
    "without any explanation at all which is very frustrating",
    "Great quality fabric for the price and the stitching was neat, I would happily "
    "order this brand again for my sister",
    "I keep three similar tops saved and cannot decide which one actually suits my "
    "body type or the occasion I need it for",
)


def test_near_duplicates_fall_within_the_configured_threshold():
    """The threshold is calibrated, not inherited: 3 bits would have caught none of these."""
    for variant in NEAR_DUPLICATES:
        distance = hamming_distance(simhash_fingerprint(BASE), simhash_fingerprint(variant))
        assert distance <= NEAR_DUPLICATE_MAX_HAMMING, variant


def test_unrelated_reviews_stay_well_outside_the_threshold():
    """The margin matters more than the threshold: dedup errors delete real signal."""
    for other in UNRELATED:
        distance = hamming_distance(simhash_fingerprint(BASE), simhash_fingerprint(other))
        assert distance > NEAR_DUPLICATE_MAX_HAMMING + 4, other


def test_configured_threshold_matches_the_code(monkeypatch):
    """config.yaml and the calibrated constant must not drift apart."""
    from src.common.config import load_run_config

    run_config, _ = load_run_config()
    assert run_config.filters.near_duplicate_hamming == NEAR_DUPLICATE_MAX_HAMMING


def test_case_and_whitespace_differences_collapse_to_an_exact_match():
    """Normalization means cosmetic variants never need the near-duplicate path."""
    shouted = BASE.upper().replace(" ", "   ") + "!!!"
    assert simhash_fingerprint(shouted) == simhash_fingerprint(BASE)


def test_identical_text_has_distance_zero():
    text = "wishlist full of dresses i never end up buying because of return hassle"
    assert hamming_distance(simhash_fingerprint(text), simhash_fingerprint(text)) == 0


def test_word_order_matters_to_the_fingerprint():
    """Bigram shingles are what stop reshuffled vocabulary from colliding."""
    a = "the size chart is wrong and the fabric is thin"
    b = "the fabric is thin and the size chart is wrong"
    assert simhash_fingerprint(a) != simhash_fingerprint(b)


def test_wordless_text_yields_a_zero_fingerprint():
    assert simhash_fingerprint("!!!") == "0" * 16


def test_hamming_distance_rejects_mismatched_widths():
    with pytest.raises(ValueError):
        hamming_distance("abcd", "abcdef")
