"""Phase 3 exclusion rules, tested in isolation (plan §3 exit criteria)."""

from src.store.exclusions import (
    classify_exclusion,
    contains_emoji,
    is_hindi,
    is_too_short,
)

FILTER_KW = dict(
    min_words=3,
    exclude_emoji=True,
    excluded_languages=["hi"],
    language_confidence=0.7,
    language_min_words=8,
)


# --- too_short: the boundary is exact at whatever the gate is set to ---


def test_the_boundary_is_exclusive_at_any_setting():
    """"Fewer than N" means N itself is kept (edge-case 3.1.4).

    Asserted at both the old gate and the new one, because the off-by-one is a
    property of the rule and not of the number: at 8 it shifted the corpus by the
    thousands of 7-word comments, and at 3 it decides whether a three-word question
    like "runs very small" is a document or a discard.
    """
    for gate in (3, 8):
        exact = " ".join(["word"] * gate)
        one_short = " ".join(["word"] * (gate - 1))
        assert is_too_short(one_short, min_words=gate) is True
        assert is_too_short(exact, min_words=gate) is False


def test_boundary_via_dispatcher():
    three = "runs very small"           # 3 words -> kept
    two = "runs small"                  # 2 words -> dropped
    assert classify_exclusion(two, **FILTER_KW) == "too_short"
    assert classify_exclusion(three, **FILTER_KW) != "too_short"


def test_the_question_the_gate_used_to_delete_now_survives():
    """The four-word AJIO question was excluded by construction at 8 (edge-case 1.1.13e).

    This is the whole reason the gate moved, so it is asserted rather than assumed.
    """
    question = "does this run small?"
    assert classify_exclusion(question, **{**FILTER_KW, "min_words": 8}) == "too_short"
    assert classify_exclusion(question, **FILTER_KW) is None


# --- contains_emoji: trailing emoji and ZWJ sequence both dropped ---


def test_single_trailing_emoji_detected():
    assert contains_emoji("great kurta and the fit is perfect 👍") is True


def test_zwj_sequence_detected():
    # family emoji = several codepoints joined by ZWJ, one logical emoji
    assert contains_emoji("loved shopping with the family 👨‍👩‍👧‍👦 today") is True


def test_plain_text_has_no_emoji():
    assert contains_emoji("plain english review with no symbols at all") is False


def test_a_substantive_review_with_a_trailing_emoji_is_kept():
    """The any-emoji rule was deleting the finding. Remainder-too-short does not.

    Phase 3 rejected-pool audit (2026-08-28): size questions and reviews that
    happened to end in a heart were `contains_emoji` false rejections.
    """
    text = "this dress fit me really well and looked amazing 😍"
    assert contains_emoji(text) is True
    assert classify_exclusion(text, **FILTER_KW) is None


def test_the_run_small_question_survives_a_trailing_emoji():
    question = "does this run small? 😅"
    assert classify_exclusion(question, **FILTER_KW) is None


def test_emoji_only_remainder_is_still_dropped():
    """Three emoji-as-tokens that ``\\w+`` still counts as words, then nothing left."""
    # If the tokenizer does not count emoji as words, too_short wins first —
    # either way the document does not reach the corpus as relevant prose.
    text = "yes 👍"
    reason = classify_exclusion(text, **FILTER_KW)
    assert reason in {"too_short", "contains_emoji"}


# --- hindi_language: Devanagari dropped, romanized Hinglish survives ---


def test_devanagari_is_hindi():
    assert is_hindi("यह कुर्ता बहुत अच्छा है और मुझे पसंद आया") is True


def test_romanized_hinglish_survives():
    text = "bhai ye kurta ka size chart bilkul galat hai maine return kiya"
    assert is_hindi(text) is False


def test_devanagari_inside_an_at_handle_does_not_drop_english():
    """A YouTube mention is not the comment. Audit row b5918990ce0ff160."""
    text = (
        "@I_am_मैंगोBhai If you don't receive the delivery send it back. "
        "The delivery company takes the charge when they accept the parcel."
    )
    assert is_hindi(text, min_words=8) is False
    assert classify_exclusion(text, **FILTER_KW) is None


def test_incidental_devanagari_particle_in_hinglish_is_kept():
    """Audit row 4a8008b1927c6293: one भी in a Latin Meesho-haul sentence."""
    text = "Dii short top or long top meesho houl भी dikhao"
    assert is_hindi(text, min_words=8) is False
    assert classify_exclusion(text, **FILTER_KW) is None


def test_hindi_dispatcher():
    text = "मुझे यह ड्रेस बहुत पसंद है लेकिन साइज़ गलत था"
    assert classify_exclusion(text, **FILTER_KW) == "hindi_language"


# --- language ID gets its own length floor now that min_words is 3 ---


def test_langdetect_is_not_consulted_on_short_text():
    """Edge-case 3.3.2 without the 8-word gate standing in front of it.

    The ordering used to guarantee langdetect never saw short text; at min_words=3
    it would, and it is unreliable there. The floor is enforced inside the rule, so
    a short string cannot be excluded on a statistical guess.
    """
    short = "kitna accha hai"
    assert is_hindi(short, min_words=8) is False


def test_script_detection_ignores_the_length_floor():
    """Devanagari is exact at any length, so the floor must not gate it.

    A three-word Hindi comment is still Hindi, and skipping the whole rule below
    the floor — rather than only its probabilistic half — would let it through.
    """
    assert is_hindi("बहुत अच्छा है", min_words=8) is True
    assert classify_exclusion("बहुत अच्छा है", **FILTER_KW) == "hindi_language"


# --- a clean, substantive English review passes every gate ---


def test_relevant_review_passes():
    text = "i added this dress to my wishlist but the size chart confused me completely"
    assert classify_exclusion(text, **FILTER_KW) is None


# --- ordering: short beats emoji beats hindi ---


def test_first_matching_reason_wins():
    # short AND has emoji -> too_short reported (runs first)
    assert classify_exclusion("nice 👍", **FILTER_KW) == "too_short"


def test_config_and_the_rule_agree_on_the_gate():
    """The gate the corpus is actually built with, not the one the tests pass in.

    Every other test here supplies `min_words` directly, which means all of them
    would still pass if `config.yaml` were reverted to 8 and the corpus silently
    shrank by tens of thousands of documents. This is the one assertion that fails
    in that case.
    """
    from src.common.config import get_settings

    filters = get_settings().run.filters
    assert filters.min_words == 3
    assert filters.language_min_words >= 8, (
        "langdetect's short-text floor must stay above the length gate, or "
        "edge-case 3.3.2's protection is gone"
    )
