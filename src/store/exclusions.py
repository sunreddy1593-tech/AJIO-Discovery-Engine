"""The three hard exclusion rules (plan §3.1, `architecture.md` §3).

These run immediately after normalization and **before** dedup and any LLM
triage, so excluded text never costs a paid token. Each rule stamps a reason
code onto ``documents.exclusion_reason`` and sets ``is_relevant = 0``; rows are
retained, not deleted, so the funnel stays auditable.

The rules are applied in a fixed order and the first match wins, because the
funnel report attributes each drop to exactly one reason. Order is chosen so the
cheapest, most certain test runs first:

1. ``too_short``      — a pure length test, no library, never wrong.
2. ``contains_emoji`` — after stripping emoji, the remainder is still below
   ``min_words`` (emoji-only). Detection uses the ``emoji`` package with a regex
   fallback. A trailing emoji on an otherwise long comment is **not** this
   reason; that was the cost the rejected-pool audit measured.
3. ``hindi_language`` — the only probabilistic test, so it runs last and only on
   text that has already survived the two certain ones.

Ordering used to do a second job that it no longer does. While ``min_words`` was 8,
rule 1 doubled as langdetect's protection from short text (edge-case 3.3.2): the
detector could not be handed anything under eight words because rule 1 had already
removed it. At ``min_words: 3`` that is no longer true, so rule 3 enforces its own
length floor via ``language_min_words`` rather than inheriting one from rule 1's
happening to be set high.
"""

from __future__ import annotations

import re
from typing import Any

from src.common.schemas import ExclusionReason
from src.store.normalize import word_count

# --------------------------------------------------------------------------
# Rule 1 — too short
# --------------------------------------------------------------------------


def is_too_short(text: str, *, min_words: int) -> bool:
    """Fewer than ``min_words`` words (plan §3.1). Shares word_count with normalize."""
    return word_count(text) < min_words


# --------------------------------------------------------------------------
# Rule 2 — contains any emoji
# --------------------------------------------------------------------------

# Fallback ranges per plan §3.1, covering more than the common block: pictographs
# and supplemental/extended symbols, misc symbols + dingbats, the variation
# selector and ZWJ that build multi-codepoint sequences, and regional-indicator
# pairs. A ZWJ family emoji is one logical emoji from several codepoints, so we
# match on the PRESENCE of any of these rather than trying to count.
_EMOJI_RANGES = (
    "\U0001F300-\U0001FAFF"  # pictographs, supplemental & extended-A symbols
    "\U00002600-\U000027BF"  # miscellaneous symbols and dingbats
    "\U0001F1E6-\U0001F1FF"  # regional indicators (flag halves)
    "\U0000FE0F"             # variation selector-16 (emoji presentation)
    "\U0000200D"             # zero-width joiner (ZWJ sequences)
)
_EMOJI_RE = re.compile(f"[{_EMOJI_RANGES}]")

try:  # `emoji` is the primary detector; regex is the fallback if it is absent.
    import emoji as _emoji

    def _emoji_present(text: str) -> bool:
        return _emoji.emoji_count(text) > 0 or bool(_EMOJI_RE.search(text))

except ImportError:  # pragma: no cover - exercised only without the optional dep

    def _emoji_present(text: str) -> bool:
        return bool(_EMOJI_RE.search(text))


def contains_emoji(text: str) -> bool:
    """True if any emoji appears anywhere in ``text`` (plan §3.1)."""
    return _emoji_present(text)


def strip_emoji(text: str) -> str:
    """Remove emoji codepoints, leaving surrounding words in place."""
    try:
        import emoji as _emoji_mod

        stripped = _emoji_mod.replace_emoji(text, replace=" ")
    except (ImportError, AttributeError):  # pragma: no cover
        stripped = _EMOJI_RE.sub(" ", text)
    return " ".join(stripped.split())


def emoji_is_the_substance(text: str, *, min_words: int) -> bool:
    """True when stripping emoji leaves too little text to clear ``min_words``.

    The original any-emoji rule deleted substantive comments that happened to
    carry a trailing heart — the cost Phase 3's rejected-pool audit is for.
    Plan §3.1's intended narrowing is emoji-only / remainder-too-short, not
    "one emoji anywhere". Detection (:func:`contains_emoji`) is unchanged.
    """
    if not contains_emoji(text):
        return False
    return is_too_short(strip_emoji(text), min_words=min_words)


# --------------------------------------------------------------------------
# Rule 3 — Hindi
# --------------------------------------------------------------------------

_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
_LATIN_RE = re.compile(r"[A-Za-z]")
# YouTube mentions are not the comment body. A Devanagari character inside
# ``@handle`` must not drop an otherwise English/Hinglish document — that was a
# false rejection in the Phase 3 rejected-pool audit.
_AT_HANDLE_RE = re.compile(r"@\S+")


def _devanagari_is_incidental(text: str) -> bool:
    """True when Devanagari is a particle in otherwise Latin Hinglish.

    The rejected-pool audit's hindi false rejection was a Meesho haul comment
    with a single ``भी``. Full Hindi sentences have many more Devanagari
    characters than this allows.
    """
    return len(_LATIN_RE.findall(text)) >= 24 and len(_DEVANAGARI_RE.findall(text)) <= 4


try:
    from langdetect import DetectorFactory, LangDetectException, detect_langs

    DetectorFactory.seed = 0
    _LANGDETECT = True
except ImportError:  # pragma: no cover
    _LANGDETECT = False


def is_hindi(
    text: str,
    *,
    excluded_languages: list[str] | None = None,
    confidence: float = 0.7,
    min_words: int = 1,
) -> bool:
    """Drop Devanagari outright, or a detected excluded language above confidence.

    Romanized Hinglish is retained by design (plan §3.2.3): it uses the Latin
    script, so the Devanagari test passes it and langdetect does not return ``hi``
    with high confidence for it. Only text actually written in Hindi is excluded.
    Devanagari inside an ``@handle`` is stripped before the script test, because
    a mention is not the comment body (Phase 3 rejected-pool audit). A handful of
    Devanagari particles in an otherwise Latin sentence (``houl भी dikhao``) is
    treated as romanized Hinglish, not as a Hindi document.

    ``min_words`` is the length below which langdetect's verdict is discarded and
    the Devanagari test stands alone. Edge-case 3.3.2 called statistical language
    ID unreliable on short text and relied on the word-count gate running first to
    guarantee nothing short reached it; that guarantee held only while the gate was
    set to 8. Since the gate is now 3, the floor is enforced here, where the
    unreliability actually lives. Script detection is exact at any length and so is
    never skipped — a three-word Devanagari comment is still excluded.
    """
    excluded = excluded_languages or ["hi"]
    script_text = _AT_HANDLE_RE.sub(" ", text)
    if _DEVANAGARI_RE.search(script_text) and not _devanagari_is_incidental(script_text):
        return True
    if not _LANGDETECT:
        return False
    if word_count(text) < min_words:
        return False
    try:
        for guess in detect_langs(text):
            if guess.lang in excluded and guess.prob >= confidence:
                return True
    except LangDetectException:
        # No detectable language (e.g. digits/punctuation only) — not our reason
        # to drop; a later gate can still exclude it.
        return False
    return False


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------


def classify_exclusion(
    text: str,
    *,
    min_words: int,
    exclude_emoji: bool,
    excluded_languages: list[str],
    language_confidence: float,
    language_min_words: int = 1,
) -> ExclusionReason | None:
    """Return the first exclusion reason that applies, or ``None`` if the text passes.

    Order is fixed (short → emoji → hindi) so the funnel attributes each drop to a
    single, stable reason code.
    """
    if is_too_short(text, min_words=min_words):
        return "too_short"
    if exclude_emoji and emoji_is_the_substance(text, min_words=min_words):
        return "contains_emoji"
    if is_hindi(
        text,
        excluded_languages=excluded_languages,
        confidence=language_confidence,
        min_words=language_min_words,
    ):
        return "hindi_language"
    return None


def classify_with_filters(text: str, filters: Any) -> ExclusionReason | None:
    """:func:`classify_exclusion` driven by a ``FiltersConfig``-shaped object.

    Phase 2 needs to know how many of the records it just collected will survive
    these rules, because a floor counted in raw records certifies a signal that
    the very next stage removes: 4,494 pre-purchase records became 180 documents,
    a 96% loss the floor could not see (plan §3.3). Collection therefore calls the
    real rules rather than approximating them — two definitions of the length
    gate would drift, and the drift would land in the one number the phase is
    judged on. That mattered when the gate moved from 8 words to 3: a hardcoded
    copy here would have kept scoring Phase 2 against a rule Phase 3 no longer
    applies, and the divergence would have looked like a collection change.
    """
    return classify_exclusion(
        text,
        min_words=filters.min_words,
        exclude_emoji=filters.exclude_emoji,
        excluded_languages=list(filters.excluded_languages),
        language_confidence=filters.language_confidence,
        language_min_words=getattr(filters, "language_min_words", 1),
    )


def survives_hard_exclusions(text: str, filters: Any) -> bool:
    """Whether ``text`` would reach the corpus rather than being excluded outright.

    "Would" is exact for the two certain rules and for Devanagari, and carries
    langdetect's own error rate on the rest — the same error rate Phase 3 carries,
    since it is the same call.
    """
    return classify_with_filters(text, filters) is None
