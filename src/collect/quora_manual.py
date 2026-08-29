"""Quora threads imported from disk. **This module makes no network calls.**

Quora's ``robots.txt`` prohibits bots from using its content for AI or ML systems,
so it is read only from files a human deliberately saved into
``data/manual/quora/``. That is why this module imports no HTTP client at all, and
why ``tests/test_collectors.py`` asserts the absence rather than trusting the
docstring: a compliance guarantee is only as strong as the code that enforces it
(`edge-case.md` §1.1.12). If you are tempted to add ``requests`` here, the answer
is no — collect by hand or drop the source.

Quora earns its place despite the manual cost because it is one of only three
pre-purchase sources and by far the most deliberative: people explain *why* they
did not buy something, at length, in a way review sites never elicit.

**Identity is content, not filename.** A human renaming ``thread1.txt`` to
``ajio-sizing.txt`` must not create a second copy of the same document, so
``source_native_id`` is derived from a hash of the normalized text (§1.2.8).

**One answer is one document.** A saved thread is a question plus many answers; a
single 5,000-word blob would be tagged once and counted once, badly. Answers are
split on blank lines and explicit markers, with the question carried in
``meta.question`` so every answer keeps the context it was responding to (§1.2.9).
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
from src.common.schemas import RawRecord

SUPPORTED_SUFFIXES = (".json", ".jsonl", ".txt", ".md")

#: Lines a human is likely to have written or pasted as the question.
_QUESTION_PREFIXES = ("question:", "q:", "#", "##", "###", "title:")

#: Explicit answer separators, in addition to blank-line splitting.
_ANSWER_MARKER_RE = re.compile(
    r"^\s*(?:answer\s*\d*\s*[:.\-]|a\d*\s*[:.]|---+|\*\*\*+|answered by\b.*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

#: Boilerplate a copy-paste from Quora tends to drag along.
_BOILERPLATE_RE = re.compile(
    r"^\s*(?:\d+[\d.,km]*\s*(?:views?|upvotes?|answers?)"
    r"|upvote|downvote|share|report|reply|follow\s*·?\s*\d*"
    r"|related questions?|sponsored|promoted by\b.*"
    r"|view \d+ upvotes?|·|\d+ (?:y|mo|d|w)\b)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

MIN_ANSWER_CHARS = 40


def split_thread(raw: str) -> tuple[str | None, list[str]]:
    """Separate the question from its answers.

    The heuristics are deliberately forgiving because the input is whatever a
    human pasted: an explicit ``Question:`` prefix or markdown heading is used when
    present, otherwise the first paragraph is treated as the question if it reads
    like one.
    """
    text = _BOILERPLATE_RE.sub("", raw or "").strip()
    if not text:
        return None, []

    marked = _ANSWER_MARKER_RE.sub("\n\n", text)
    paragraphs = [block.strip() for block in re.split(r"\n\s*\n", marked) if block.strip()]
    if not paragraphs:
        return None, []

    question: str | None = None
    first = paragraphs[0]
    lowered = first.lower()

    if any(lowered.startswith(prefix) for prefix in _QUESTION_PREFIXES):
        question = re.sub(
            r"^\s*(?:question|q|title)\s*[:.]\s*|^#+\s*", "", first, flags=re.IGNORECASE
        ).strip()
        paragraphs = paragraphs[1:]
    elif first.endswith("?") or len(first) < 200 and "?" in first:
        question = first
        paragraphs = paragraphs[1:]

    answers = [block for block in paragraphs if len(block) >= MIN_ANSWER_CHARS]
    return question, answers


def parse_file(path: Path) -> list[dict[str, Any]]:
    """One saved thread file to answer-level payloads."""
    raw = read_text_tolerant(path)
    question, answers = split_thread(raw)

    payloads: list[dict[str, Any]] = []
    for index, answer in enumerate(answers):
        payloads.append(
            {
                "native_id": content_id(answer),
                "text": answer,
                "meta": {
                    "thread_title": question or path.stem.replace("-", " ").replace("_", " "),
                    "question": question,
                    "source_file": path.name,
                    "answer_index": index,
                },
            }
        )
    return payloads


class QuoraManualCollector(Collector):
    """Reads saved threads from disk. Never fetches anything."""

    source: ClassVar[str] = "quora_manual"
    makes_network_calls: ClassVar[bool] = False
    #: No floor: an empty import directory is a normal state, since filling it is
    #: manual work that may not have happened yet.
    min_expected_records: ClassVar[int] = 0

    def __init__(self, project_root: Path):
        super().__init__()
        self.project_root = Path(project_root)

    def files(self, cfg: Any) -> Sequence[Path]:
        from src.collect.manual import discover_files

        return discover_files(self.project_root / cfg.import_dir)

    def fetch(self, cfg: Any) -> Iterator[RawRecord]:
        loaded = load_dir(
            self.project_root / cfg.import_dir,
            source="quora_manual",
            parse_prose=lambda path: (parse_file(path), []),
        )
        for warning in loaded.warnings:
            self.log.warning("%s", warning)

        self.log.info(
            "importing %s Quora document(s) from %s file(s)",
            len(loaded.documents),
            len(loaded.files_read),
        )
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
    "MIN_ANSWER_CHARS",
    "SUPPORTED_SUFFIXES",
    "QuoraManualCollector",
    "parse_file",
    "split_thread",
]
