"""AJIO reviews and Q&A collected by hand. **This module makes no network calls.**

AJIO's Akamai edge refuses this project's automated client on every content path
(`edge-case.md` §1.1.13). That refusal is the site's answer, and the response to
it is a person browsing ajio.com normally and saving what they read — not a
client disguised well enough to be served. This module is where that work lands.

It imports no HTTP client, for the reason ``quora_manual`` does not (§1.1.12): a
compliance guarantee is worth exactly as much as the code enforcing it, and
``tests/test_collectors.py`` asserts the absence rather than trusting this
paragraph. It also declines to import ``ajio_onsite``, despite sharing a URL
shape and a record layout with it — that module owns a ``PoliteSession`` and
reaches ``scraping.py``, so importing it would leave a network path one attribute
access away and make the guarantee unverifiable. The few duplicated constants
below are the price of that.

**Q&A and reviews still must not be conflated** (§1.1.14). A hand-typed file is
if anything easier to mix up than a JSON payload, so ``meta.content_type`` is
never inferred from the prose: it comes from an explicit section header, and a
block that carries no header and does not announce itself with ``Q:`` is skipped
and counted rather than guessed at.

**A question is a document; its answers are metadata.** Same reasoning as the
on-site collector: answers usually come from people who already bought the item,
so promoting them to documents would file post-purchase voice as pre-purchase
deliberation — the one error this source exists to avoid.

**Identity is content, not filename** (§1.2.8), so re-saving the same question
under a tidier name does not produce a second copy of it.

One thing that used to be true of this source and no longer is: AJIO questions are
often very short — *"does this run small?"* is four words — and while Phase 3's
length gate stood at eight words, a meaningful share of the richest pre-purchase
content on the roster was excluded before tagging by construction. The gate is now
three words, so those questions survive. What still applies is the shape of the
loss: a bare *"size?"* is one word and is still dropped, so a hand-collected
question is worth typing out as the customer asked it rather than abbreviating.

Expected file format, markdown or JSON. JSON from the bookmarklet or CDP helper is the intended fill path; markdown is still accepted. Both go through ``src.collect.manual.load_dir``. JSON needs ``meta.content_type`` of ``qa`` or ``review`` explicitly, for the same reason the markdown header is required — it is never inferred from the prose::

    product: 469558637
    title: Puma Men Round Neck T-shirt

    ## Q&A

    Q: Does this run small? I am usually a medium.
    A: Yes, order one size up.
    A: True to size for me.

    Q: Is the fabric see-through in white?

    ## Reviews

    [2] Kept this in my wishlist for a month because the size chart
    contradicts the brand's own. Ordered anyway and it was tight.
    - by meera, 12 May 2026

``product`` accepts a bare id or a full ``/p/<id>`` URL, and may reappear
anywhere in the body to switch products part-way through a file. It is required:
there is no filename fallback, because identity must not depend on a filename
(§1.2.8) and because a name like ``ajio-830216012-kurtas.md`` carries a *category*
id that would be silently recorded as a product. Section headers are matched
loosely, so ``## Questions`` and ``Reviews:`` both work.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, ClassVar

from src.collect.base import Collector
from src.collect.manual import load_dir
from src.common.encoding import read_text_tolerant
from src.common.hashing import content_id
from src.common.schemas import AjioContentType, RawRecord

SUPPORTED_SUFFIXES = (".json", ".jsonl", ".txt", ".md")

#: Duplicated from ``ajio_onsite`` rather than imported. See the module docstring:
#: importing that module would pull in the HTTP session this one must not have.
BASE_URL = "https://www.ajio.com"
_PRODUCT_ID_RE = re.compile(r"/p/(\d{6,})")
_BARE_ID_RE = re.compile(r"\b(\d{6,})\b")

#: Front-matter and in-body directives. ``product`` may appear again lower down to
#: switch products, which is what lets one file hold a morning's browsing.
_DIRECTIVE_RE = re.compile(
    r"^\s*(product|product_id|url|title)\s*[:=]\s*(.+?)\s*$", re.IGNORECASE
)

_HEADER_KINDS: dict[str, str] = {
    "q&a": AjioContentType.QA.value,
    "qa": AjioContentType.QA.value,
    "q and a": AjioContentType.QA.value,
    "question": AjioContentType.QA.value,
    "questions": AjioContentType.QA.value,
    "questions & answers": AjioContentType.QA.value,
    "questions and answers": AjioContentType.QA.value,
    "review": AjioContentType.REVIEW.value,
    "reviews": AjioContentType.REVIEW.value,
    "ratings & reviews": AjioContentType.REVIEW.value,
    "ratings and reviews": AjioContentType.REVIEW.value,
}

_QUESTION_PREFIX_RE = re.compile(r"^\s*(?:q|question)\s*[:.]\s*", re.IGNORECASE)
_ANSWER_PREFIX_RE = re.compile(r"^\s*(?:a|ans|answer)\s*\d*\s*[:.]\s*", re.IGNORECASE)

#: A leading ``[4]``, ``Rating: 4`` or ``4/5`` on a review block.
_RATING_RE = re.compile(
    r"^\s*(?:\[\s*(\d)\s*\]|rating\s*[:=]\s*(\d)|(\d)\s*(?:/\s*5\b|stars?\b))\s*[-–—:]?\s*",
    re.IGNORECASE,
)

#: Attribution trailer: ``- by meera, 12 May 2026``.
_BY_RE = re.compile(
    r"^\s*[-–—*]?\s*by\s*[:]?\s*([^,]{1,60}?)\s*(?:,\s*(.+?))?\s*$", re.IGNORECASE
)

_BLOCK_SPLIT_RE = re.compile(r"\n\s*\n")
_SEPARATOR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)

#: Low deliberately. The authoritative length rule is ``filters.min_words`` in
#: Phase 3; a 40-character floor like Quora's would drop "does this run small?",
#: which is the exact sentence this source exists to capture.
MIN_TEXT_CHARS = 10


def product_id_from(value: str | None) -> str | None:
    """The product id in a bare id, a ``/p/<id>`` URL, or a filename."""
    if not value:
        return None
    match = _PRODUCT_ID_RE.search(value) or _BARE_ID_RE.search(value)
    return match.group(1) if match else None


def section_kind(line: str) -> str | None:
    """The content type a header line announces, or None if it is not a header."""
    stripped = line.strip().strip("*_").strip()
    if stripped.startswith("#"):
        stripped = stripped.lstrip("#").strip()
    stripped = stripped.rstrip(":").strip()
    if not stripped or len(stripped) > 40:
        return None
    return _HEADER_KINDS.get(stripped.casefold())


def _blocks(text: str) -> list[str]:
    """Split a section into record-sized blocks.

    Blank lines are the primary separator, but a ``Q:`` line also starts a new
    block: a human transcribing a Q&A tab tends to type them consecutively, and
    without this every question in the section would fuse into one document.
    """
    text = _SEPARATOR_RE.sub("\n\n", text)
    text = re.sub(r"\n(?=\s*(?:q|question)\s*[:.]\s)", "\n\n", text, flags=re.IGNORECASE)
    return [block.strip() for block in _BLOCK_SPLIT_RE.split(text) if block.strip()]


def parse_qa_block(block: str) -> tuple[str, list[str]] | None:
    """A question and its answers. Answers never become their own document."""
    question_lines: list[str] = []
    answers: list[str] = []
    current: list[str] | None = None

    for line in block.splitlines():
        if _ANSWER_PREFIX_RE.match(line):
            answers.append(_ANSWER_PREFIX_RE.sub("", line).strip())
            current = None if not answers else answers
            continue
        if _QUESTION_PREFIX_RE.match(line):
            question_lines.append(_QUESTION_PREFIX_RE.sub("", line).strip())
            current = question_lines
            continue
        if current is answers and answers:
            answers[-1] = f"{answers[-1]} {line.strip()}".strip()
        else:
            question_lines.append(line.strip())
            current = question_lines

    question = " ".join(part for part in question_lines if part).strip()
    if len(question) < MIN_TEXT_CHARS:
        return None
    return question, [answer for answer in answers if answer]


def parse_review_block(block: str) -> tuple[str, int | None, str | None, str | None] | None:
    """A review body with its rating, author, and date, each optional."""
    lines = block.splitlines()

    author: str | None = None
    created: str | None = None
    if len(lines) > 1:
        trailer = _BY_RE.match(lines[-1])
        if trailer:
            author = (trailer.group(1) or "").strip() or None
            created = (trailer.group(2) or "").strip() or None
            lines = lines[:-1]

    body = "\n".join(lines).strip()

    rating: int | None = None
    rating_match = _RATING_RE.match(body)
    if rating_match:
        digit = next((group for group in rating_match.groups() if group), None)
        rating = int(digit) if digit else None
        body = body[rating_match.end() :].strip()

    if len(body) < MIN_TEXT_CHARS:
        return None
    return body, rating, author, created


def parse_file(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """One saved file to record payloads, plus warnings worth surfacing.

    Warnings are returned rather than logged so the parser stays a pure function
    of the file's text, which is what makes it testable without a collector.
    """
    raw = read_text_tolerant(path)
    payloads: list[dict[str, Any]] = []
    warnings: list[str] = []

    # Deliberately *not* falling back to the filename for the product id, though
    # it would be convenient. Identity has to be content-only (§1.2.8), and a
    # filename is neither stable nor trustworthy here: `ajio-830216012-kurtas.md`
    # carries a *category* id, which would be recorded as a product and quietly
    # produce a dead citation URL in the report.
    product_id: str | None = None
    title: str | None = None
    content_type: str | None = None

    # Directives are stripped out first so the remaining text splits cleanly into
    # blocks, and so a `product:` line switching products mid-file does not end up
    # inside the document text of the record that follows it.
    segments: list[tuple[str | None, str | None, str]] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            segments.append((product_id, content_type, "\n".join(pending)))
            pending.clear()

    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        directive = _DIRECTIVE_RE.match(line)
        if directive:
            key, value = directive.group(1).lower(), directive.group(2)
            if key == "title":
                title = value.strip() or None
            else:
                resolved = product_id_from(value)
                if resolved:
                    flush()
                    product_id = resolved
                else:
                    warnings.append(f"{path.name}: no product id in {value!r}")
            continue

        kind = section_kind(line)
        if kind:
            flush()
            content_type = kind
            continue

        pending.append(line)
    flush()

    for segment_product, segment_type, text in segments:
        for block in _blocks(text):
            declared = segment_type
            if declared is None and _QUESTION_PREFIX_RE.match(block):
                # An explicit "Q:" is unambiguous, so it may stand in for a header.
                declared = AjioContentType.QA.value
            if declared is None:
                warnings.append(
                    f"{path.name}: skipped a block with no '## Q&A' or '## Reviews' "
                    "header above it; content type is never guessed (edge-case 1.1.14)"
                )
                continue
            if not segment_product:
                warnings.append(
                    f"{path.name}: skipped a block with no product id above it; add "
                    "a 'product: <id or /p/ url>' line"
                )
                continue

            payload = _payload(block, segment_product, declared, path, title)
            if payload is None:
                continue
            payloads.append(payload)

    return payloads, warnings


def _payload(
    block: str, product_id: str, content_type: str, path: Path, title: str | None
) -> dict[str, Any] | None:
    meta: dict[str, Any] = {
        "product_id": product_id,
        "content_type": content_type,
        "rating": None,
        "product_title": title,
        "source_file": path.name,
        # Distinguishes these from the same site's scraped records wherever the
        # two ever sit side by side, so the report can say how it got them.
        "extraction": "manual_import",
    }
    author: str | None = None
    created: str | None = None

    if content_type == AjioContentType.QA.value:
        parsed_qa = parse_qa_block(block)
        if parsed_qa is None:
            return None
        text, answers = parsed_qa
        meta["answers"] = answers
        meta["answer_count"] = len(answers)
    else:
        parsed_review = parse_review_block(block)
        if parsed_review is None:
            return None
        text, rating, author, created = parsed_review
        meta["rating"] = float(rating) if rating is not None else None

    try:
        digest = content_id(text)[:12]
    except ValueError:
        return None  # punctuation only; nothing stable to key on

    return {
        "native_id": f"{content_type}-{product_id}-{digest}",
        "text": text,
        # Unlike a saved Quora thread, this record has a canonical URL worth
        # citing. Link rot is expected and accepted (edge-case 6.7).
        "url": f"{BASE_URL}/p/{product_id}",
        "author": author,
        "created_raw": created,
        "meta": meta,
    }


class AjioManualCollector(Collector):
    """Reads hand-collected AJIO files from disk. Never fetches anything."""

    source: ClassVar[str] = "ajio_manual"
    makes_network_calls: ClassVar[bool] = False
    #: No floor: an empty import directory is a normal state, because filling it
    #: is manual work that may not have happened yet.
    min_expected_records: ClassVar[int] = 0

    def __init__(self, project_root: Path):
        super().__init__()
        self.project_root = Path(project_root)
        self.files_read: list[str] = []
        self.files_skipped: list[str] = []
        self.parse_warnings: list[str] = []

    def files(self, cfg: Any) -> Sequence[Path]:
        from src.collect.manual import discover_files

        return discover_files(self.project_root / cfg.import_dir)

    def fetch(self, cfg: Any) -> Iterator[RawRecord]:
        loaded = load_dir(
            self.project_root / cfg.import_dir,
            source="ajio_manual",
            parse_prose=parse_file,
        )
        self.files_read = list(loaded.files_read)
        self.files_skipped = list(loaded.files_skipped)
        self.parse_warnings = list(loaded.warnings)
        for warning in loaded.warnings:
            self.log.warning("%s", warning)

        self.log.info("importing %s AJIO document(s) from %s file(s)", len(loaded.documents), len(loaded.files_read))
        for doc in loaded.documents:
            record = self.build(
                source_native_id=doc.id,
                text=doc.text,
                url=doc.url,
                author_raw=doc.author,
                created_utc=doc.timestamp,
                meta=doc.meta,
            )
            if record is not None:
                yield record


__all__ = [
    "BASE_URL",
    "MIN_TEXT_CHARS",
    "SUPPORTED_SUFFIXES",
    "AjioManualCollector",
    "parse_file",
    "parse_qa_block",
    "parse_review_block",
    "product_id_from",
    "section_kind",
]
