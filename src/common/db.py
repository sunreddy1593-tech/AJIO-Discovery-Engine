"""SQLite storage for the corpus, tags, cache, and run log (`architecture.md` §6).

Writes are idempotent by construction: ``UNIQUE (source, source_native_id)`` plus
``ON CONFLICT DO NOTHING`` means re-running collection and rebuilding the corpus
cannot duplicate a row. That is what lets a run be resumed after a rate-limit stop
without tracking how far it got.

Datetimes are converted to ISO-8601 strings at the boundary rather than relying on
sqlite3's implicit adapters, which are deprecated and emit warnings on modern
Python. Reading them back is the caller's business; storage keeps them textual and
sortable.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.common.schemas import Document, DocumentTags

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id            TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    source_native_id  TEXT NOT NULL,
    url               TEXT,
    author_hash       TEXT,
    created_utc       TEXT,
    text              TEXT NOT NULL,
    lang              TEXT,
    char_len          INTEGER,
    meta_json         TEXT,
    text_fingerprint  TEXT,
    is_duplicate_of   TEXT REFERENCES documents(doc_id),
    word_count        INTEGER,
    exclusion_reason  TEXT,
    relevance_score   REAL,
    is_relevant       INTEGER,
    ingested_at       TEXT,
    UNIQUE (source, source_native_id)
);

CREATE TABLE IF NOT EXISTS doc_tags (
    doc_id            TEXT NOT NULL REFERENCES documents(doc_id),
    taxonomy_version  TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    model             TEXT NOT NULL,
    tags_json         TEXT NOT NULL,
    tagged_at         TEXT,
    PRIMARY KEY (doc_id, taxonomy_version, prompt_version, model)
);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key         TEXT PRIMARY KEY,
    response_json     TEXT NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    reasoning_tokens  INTEGER,
    created_at        TEXT
);

CREATE TABLE IF NOT EXISTS run_log (
    run_id      TEXT,
    stage       TEXT,
    config_hash TEXT,
    started_at  TEXT,
    finished_at TEXT,
    records_in  INTEGER,
    records_out INTEGER,
    notes       TEXT
);

-- Tier-2 triage verdicts, deliberately without a foreign key to documents.
-- Triage runs *before* the rebuild's insert, and a --force rebuild deletes every
-- documents row, so an FK would either reject the write or cascade the cache away
-- -- and the whole point of this table is to outlive both. doc_id is derived from
-- (source, source_native_id) and is stable across rebuilds, so a verdict cached
-- today is still about the same text tomorrow.
CREATE TABLE IF NOT EXISTS triage_cache (
    doc_id          TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    is_relevant     INTEGER NOT NULL,
    decided_at      TEXT,
    PRIMARY KEY (doc_id, model, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_documents_source        ON documents(source);
CREATE INDEX IF NOT EXISTS idx_documents_is_relevant   ON documents(is_relevant);
CREATE INDEX IF NOT EXISTS idx_documents_fingerprint   ON documents(text_fingerprint);
CREATE INDEX IF NOT EXISTS idx_doc_tags_taxonomy       ON doc_tags(taxonomy_version);
CREATE INDEX IF NOT EXISTS idx_run_log_run_id          ON run_log(run_id);
"""

_DOCUMENT_COLUMNS = (
    "doc_id",
    "source",
    "source_native_id",
    "url",
    "author_hash",
    "created_utc",
    "text",
    "lang",
    "char_len",
    "meta_json",
    "text_fingerprint",
    "is_duplicate_of",
    "word_count",
    "exclusion_reason",
    "relevance_score",
    "is_relevant",
    "ingested_at",
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the pragmas this pipeline depends on.

    WAL lets a long tagging run write checkpoints while a read-only query inspects
    progress. Foreign keys are off by default in SQLite and must be enabled per
    connection, which is what makes tags for a non-existent document an error
    rather than an orphan row discovered during quantification.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create the schema if absent and return an open connection. Idempotent."""
    conn = connect(db_path)
    conn.executescript(SCHEMA_SQL)
    return conn


def _document_row(document: Document) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        **document.model_dump(exclude={"meta", "created_utc", "ingested_at", "is_relevant"}),
        "meta_json": json.dumps(document.meta, sort_keys=True, ensure_ascii=False),
        "created_utc": _iso(document.created_utc),
        "ingested_at": _iso(document.ingested_at) or _now_iso(),
        "is_relevant": None if document.is_relevant is None else int(document.is_relevant),
    }
    return tuple(values[column] for column in _DOCUMENT_COLUMNS)


def upsert_documents(conn: sqlite3.Connection, documents: Iterable[Document]) -> int:
    """Insert documents, ignoring ones already present. Returns rows actually added.

    ``DO NOTHING`` rather than ``DO UPDATE``: raw collected content is immutable,
    so a conflict means "already have it", not "needs refreshing". Derived columns
    such as ``is_duplicate_of`` and ``is_relevant`` are set by later stages with
    explicit UPDATEs, which is also why duplicates must be marked after the whole
    batch is inserted — the self-referencing foreign key requires the target row
    to exist.
    """
    rows = [_document_row(document) for document in documents]
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in _DOCUMENT_COLUMNS)
    columns = ", ".join(_DOCUMENT_COLUMNS)
    before = conn.total_changes
    conn.executemany(
        f"INSERT INTO documents ({columns}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
        rows,
    )
    return conn.total_changes - before


def replace_documents(conn: sqlite3.Connection, documents: Iterable[Document]) -> int:
    """Rewrite document rows in place so ``--force`` can reclassify without dropping tags.

    ``doc_tags.doc_id`` references ``documents(doc_id)``. A plain
    ``DELETE FROM documents`` with foreign keys on fails after tagging — the
    2026-08-28 persist after forty minutes of exclusions. ``triage_cache`` was
    designed without that FK so it could outlive a wipe; tags were not. ``doc_id``
    is derived from ``(source, source_native_id)`` and comes back unchanged, so
    updating the row keeps the tag attached.

    Foreign keys are suspended only for the swap: ``is_duplicate_of`` is cleared
    first so a pointer cannot land on a row that has not been rewritten yet.
    """
    ordered = sorted(documents, key=lambda d: d.is_duplicate_of is not None)
    rows = [_document_row(document) for document in ordered]
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in _DOCUMENT_COLUMNS)
    columns = ", ".join(_DOCUMENT_COLUMNS)
    assignments = ", ".join(
        f"{column}=excluded.{column}" for column in _DOCUMENT_COLUMNS if column != "doc_id"
    )
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("UPDATE documents SET is_duplicate_of = NULL")
        conn.executemany(
            f"INSERT INTO documents ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(doc_id) DO UPDATE SET {assignments}",
            rows,
        )
        return len(rows)
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def upsert_tags(
    conn: sqlite3.Connection,
    doc_id: str,
    tags: DocumentTags,
    *,
    taxonomy_version: str,
    prompt_version: str,
    model: str,
    tagged_at: datetime | None = None,
) -> int:
    """Persist one document's coding. Returns 1 if written, 0 if already present.

    The primary key includes the taxonomy, prompt, and model versions, so re-tagging
    with a changed prompt adds a row instead of overwriting history — the reason a
    past report can still be reproduced after the taxonomy moves on.
    """
    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO doc_tags (doc_id, taxonomy_version, prompt_version, model, tags_json, tagged_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (
            doc_id,
            taxonomy_version,
            prompt_version,
            model,
            tags.model_dump_json(),
            _iso(tagged_at) or _now_iso(),
        ),
    )
    return conn.total_changes - before


class RunLogEntry:
    """Mutable counters for one stage of one run, written out on exit."""

    def __init__(self, run_id: str, stage: str) -> None:
        self.run_id = run_id
        self.stage = stage
        self.records_in = 0
        self.records_out = 0
        self.notes: list[str] = []

    def note(self, message: str) -> None:
        self.notes.append(message)


@contextmanager
def run_log(
    conn: sqlite3.Connection, run_id: str, stage: str, config_hash: str
) -> Iterator[RunLogEntry]:
    """Record a stage's execution, including when it fails.

    The row is written in a ``finally`` block so a crashed or rate-limited stage
    still leaves a trace with its counts and the exception text. A stage that
    stopped halfway is the normal case on a free-tier budget, and an absent log
    row would make it look like the stage never ran.
    """
    entry = RunLogEntry(run_id, stage)
    started_at = _now_iso()
    try:
        yield entry
    except BaseException as exc:
        entry.note(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        conn.execute(
            """
            INSERT INTO run_log
                (run_id, stage, config_hash, started_at, finished_at,
                 records_in, records_out, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                stage,
                config_hash,
                started_at,
                _now_iso(),
                entry.records_in,
                entry.records_out,
                "; ".join(entry.notes) or None,
            ),
        )
