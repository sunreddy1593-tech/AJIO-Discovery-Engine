"""Deterministic attribution screen (plan §4.4 / §5.6).

Three flags, none of which reject a tag: ``no_cue_overlap`` against the cue
lexicon, ``quote_not_in_document`` (the quote is not a verbatim substring),
and ``quote_reused`` (the same span is cited for two tags). They exist to
down-weight ``evidence_confidence``, not to decide truth. This module never
writes to ``doc_tags``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from src.common.config import PROJECT_ROOT

CUES_PATH = PROJECT_ROOT / "config" / "tag_cues.yaml"


@lru_cache(maxsize=1)
def load_cues(path: str | None = None) -> dict[str, dict[str, list[str]]]:
    """``{dimension: {label: [cue, ...]}}``. Missing file → empty lexicon."""
    target = Path(path) if path else CUES_PATH
    if not target.is_file():
        return {}
    loaded = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def quote_in_document(quote: str, text: str) -> bool:
    """True when the quote is a verbatim substring, ignoring surrounding case/space."""
    needle = _norm(quote)
    return bool(needle) and needle in _norm(text)


def cue_overlap(quote: str, cues: list[str]) -> bool:
    """True when at least one cue term appears in the quote."""
    haystack = _norm(quote)
    return any(_norm(cue) in haystack for cue in cues if cue)


def flags_for_span(
    *,
    dimension: str,
    label: str,
    quote: str,
    text: str,
    quotes_on_doc: list[str],
    cues: dict[str, dict[str, list[str]]] | None = None,
) -> list[str]:
    """Screen flags for one evidence span. Empty means the span is unflagged."""
    lexicon = cues if cues is not None else load_cues()
    found: list[str] = []
    if not quote_in_document(quote, text):
        found.append("quote_not_in_document")
    tag_cues = (lexicon.get(dimension) or {}).get(label) or []
    if tag_cues and not cue_overlap(quote, tag_cues):
        found.append("no_cue_overlap")
    normalised = _norm(quote)
    if normalised and quotes_on_doc.count(normalised) > 1:
        found.append("quote_reused")
    return found


def document_is_flagged(
    *,
    dimension: str,
    label: str,
    text: str,
    evidence: list,
    cues: dict[str, dict[str, list[str]]] | None = None,
) -> bool:
    """True when any span for this label is flagged, or none exists."""
    quotes = [_norm(span.quote) for span in evidence]
    relevant = [span for span in evidence if _span_label(span) == label]
    if not relevant:
        return True
    for span in relevant:
        if flags_for_span(
            dimension=dimension,
            label=label,
            quote=span.quote,
            text=text,
            quotes_on_doc=quotes,
            cues=cues,
        ):
            return True
    return False


def _span_label(span) -> str:
    tag = span.tag
    return tag.value if hasattr(tag, "value") else str(tag)
