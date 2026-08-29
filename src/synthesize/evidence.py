"""Pick illustrative quotes for a ranked opportunity (architecture.md §9).

Selection is deterministic: unflagged evidence first, then proximity to the
cluster (tag overlap) plus severity, then source diversity. Quotes are never
hand-picked. A flagged span is never shown — the screen exists so a reader is
not asked to trust a passage that does not say what it is cited for.

PII is redacted and markdown metacharacters escaped before anything is written
(edge-case.md §6.2, §6.3). Long quotes are truncated at a word boundary.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from src.quantify.screen import document_is_flagged

MAX_QUOTE_CHARS = 240
EVIDENCE_PER_THEME = 4

_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE = re.compile(r"\b(?:\+?\d[\d\s\-()]{8,}\d)\b")
_ORDER = re.compile(
    r"\b(?:order|awb|tracking|shipment)[\s#:]*[A-Z0-9\-]{6,}\b",
    re.I,
)
_HANDLE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{2,}")


@dataclass(frozen=True)
class Quote:
    doc_id: str
    source: str
    url: str | None
    text: str
    severity: int
    overlap: float


def redact_pii(text: str) -> str:
    """Strip phone, email, order/AWB tokens, and @handles (edge-case.md §6.2)."""
    text = _EMAIL.sub("[email]", text)
    text = _ORDER.sub("[order-id]", text)
    text = _HANDLE.sub("[handle]", text)
    text = _PHONE.sub("[phone]", text)
    return text


def escape_markdown(text: str) -> str:
    """Keep a quote from breaking a table, a code span, or a heading."""
    text = text.replace("|", "\\|").replace("`", "'")
    stripped = text.lstrip()
    if stripped.startswith(("#", ">")):
        text = text[: len(text) - len(stripped)] + "\\" + stripped
    return text


def truncate(text: str, *, limit: int = MAX_QUOTE_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    clipped = collapsed[: limit].rsplit(" ", 1)[0].rstrip(".,;:")
    return clipped + "…"


def parse_cluster_members(
    cluster: str | None,
    fallback: str,
    dimension: str | None = None,
) -> list[tuple[str, str]]:
    """``dimension=label`` members of a merged cluster, or the theme name alone."""
    members: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    if cluster:
        for part in cluster.split(";"):
            part = part.strip()
            if "=" in part:
                dim, label = part.split("=", 1)
                key = (dim.strip(), label.strip())
            elif part:
                key = (dimension or "", part)
            else:
                continue
            if key[1] and key not in seen:
                seen.add(key)
                members.append(key)
    if fallback:
        key = (dimension or "", fallback)
        if key not in seen:
            members.append(key)
    return members


def _payload(tags_json: str) -> dict:
    try:
        payload = json.loads(tags_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _doc_labels(payload: dict) -> set[str]:
    labels: set[str] = set()
    for key in (
        "blocker_type",
        "uncertainty_type",
        "wishlist_motivation",
        "info_sought_elsewhere",
        "segment_cue",
    ):
        labels.update(str(value) for value in payload.get(key) or [])
    return labels


def _matching_quote(payload: dict, labels: set[str]) -> str | None:
    needles = {item.casefold() for item in labels}
    for span in payload.get("evidence") or []:
        if not isinstance(span, dict):
            continue
        tag = str(span.get("tag") or "").casefold()
        quote = (span.get("quote") or "").strip()
        if quote and tag in needles:
            return quote
    return None


def _span_list(payload: dict) -> list:
    spans = []
    for span in payload.get("evidence") or []:
        if not isinstance(span, dict):
            continue
        tag = span.get("tag")
        quote = span.get("quote") or ""
        if tag and quote:
            spans.append(type("Span", (), {"tag": tag, "quote": quote})())
    return spans


def _unflagged_quote(
    members: list[tuple[str, str]],
    labels_on_doc: set[str],
    text: str,
    payload: dict,
) -> str | None:
    """First cluster-member quote that carries no screen flag, or None."""
    spans = _span_list(payload)
    if not spans:
        return None
    for dimension, label in members:
        if label not in labels_on_doc:
            continue
        if document_is_flagged(
            dimension=dimension,
            label=label,
            text=text,
            evidence=spans,
        ):
            continue
        quote = _matching_quote(payload, {label})
        if quote:
            return quote
    return None


def select_quotes(
    conn: sqlite3.Connection,
    theme: str,
    *,
    cluster: str | None = None,
    dimension: str | None = None,
    supporting_ids: Sequence[str] | None = None,
    limit: int = EVIDENCE_PER_THEME,
    used: set[str] | None = None,
) -> list[Quote]:
    """2–4 unflagged quotes, diverse by source, ranked by overlap then severity."""
    members = parse_cluster_members(cluster, theme, dimension)
    labels = {label for _dim, label in members}
    used_quotes = used if used is not None else set()
    rows = conn.execute(
        """
        SELECT d.doc_id, d.source, d.url, d.text, t.tags_json
        FROM documents d
        JOIN doc_tags t ON t.doc_id = d.doc_id
        WHERE d.is_relevant = 1 AND d.is_duplicate_of IS NULL
        """
    ).fetchall()

    wanted = set(supporting_ids) if supporting_ids else None
    candidates: list[Quote] = []
    for row in rows:
        if wanted is not None and row["doc_id"] not in wanted:
            continue
        payload = _payload(row["tags_json"])
        doc_labels = _doc_labels(payload)
        if not (doc_labels & labels):
            continue
        text = row["text"] or ""
        quote = _unflagged_quote(members, doc_labels, text, payload) or ""
        if not quote:
            continue
        cleaned = truncate(escape_markdown(redact_pii(quote)))
        key = cleaned.casefold()
        if not cleaned or key in used_quotes:
            continue
        overlap = len(doc_labels & labels) / len(labels) if labels else 0.0
        severity = int(payload.get("severity") or 0)
        candidates.append(
            Quote(
                doc_id=row["doc_id"],
                source=row["source"],
                url=row["url"],
                text=cleaned,
                severity=severity,
                overlap=overlap,
            )
        )

    candidates.sort(key=lambda q: (-q.overlap, -q.severity, q.source, q.doc_id))
    picked: list[Quote] = []
    seen_sources: set[str] = set()
    leftovers: list[Quote] = []
    for quote in candidates:
        if quote.source in seen_sources:
            leftovers.append(quote)
            continue
        picked.append(quote)
        seen_sources.add(quote.source)
        used_quotes.add(quote.text.casefold())
        if len(picked) >= limit:
            return picked
    for quote in leftovers:
        picked.append(quote)
        used_quotes.add(quote.text.casefold())
        if len(picked) >= limit:
            break
    return picked


def format_quote(quote: Quote) -> str:
    """One markdown bullet: source, doc_id, optional URL, verbatim quote."""
    head = f"- `{quote.source}` `{quote.doc_id}`"
    if quote.url:
        head += f" ([source]({quote.url}))"
    return f'{head}: "{quote.text}"'


def render_evidence_appendix(by_theme: dict[str, list[Quote]]) -> str:
    """The supporting file: every quote the report showed, grouped by theme."""
    lines = [
        "# Evidence appendix",
        "",
        "Verbatim quotes cited in `opportunity_report.md`. Each line is a "
        "source, `doc_id`, and the span as stored. Links may rot; the quote "
        "and `doc_id` remain the evidence of record.",
        "",
    ]
    if not by_theme:
        lines.append("No quotes were selected.")
        return "\n".join(lines) + "\n"
    for theme, quotes in by_theme.items():
        lines.append(f"## {theme}")
        lines.append("")
        if not quotes:
            lines.append("No unflagged quote available for this theme.")
        else:
            lines.extend(format_quote(q) for q in quotes)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
