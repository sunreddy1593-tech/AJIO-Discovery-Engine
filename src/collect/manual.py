"""Shared loader for the two hand-collected sources.

``data/manual/ajio`` and ``data/manual/quora`` are first-class Collect inputs, not
side doors. This module is the single scan/validate/normalize path both collectors
use, so a JSON dump from a bookmarklet, a JSONL dump from a CDP session, and a
markdown file a person typed all become the same document shape before
``Collector.build`` ever sees them.

The shape is the storage contract in miniature (problemStatement storage stage):
``id``, ``source`` (alias ``route``), ``url``, ``text``, optional ``author`` and
``timestamp``. AJIO still requires ``meta.content_type`` because Q&A and reviews
sit on opposite sides of the purchase (edge-case 1.1.14); that is the one extra
the shared shape cannot drop.

**No HTTP client is imported**, and a test asserts it. The bookmarklet and the
CDP helper live under ``scripts/manual_extract/`` so this module cannot reach a
browser even by accident.

**A directory that yields zero documents after skipping README is an error**,
not an empty iterator. That is the signal that kept Phase 2 short of its
source-coverage criterion, and it has to stay loud for every *enabled* directory
until someone drops real threads in. ``ajio_manual`` is disabled in
``config.yaml`` — AJIO publishes no free text anywhere on site, so there is
nothing to hand-collect — which leaves ``quora_manual`` as the one route this
loader still gates. After it is filled, collecting is a person-task and this file
does not change.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from src.collect.base import EmptyImportError, is_import_documentation
from src.common.encoding import harden_stdio, read_text_tolerant
from src.common.hashing import content_id
from src.common.schemas import AjioContentType

ManualSource = Literal["ajio_manual", "quora_manual"]

SUPPORTED_SUFFIXES = (".json", ".jsonl", ".txt", ".md")
PROSE_SUFFIXES = (".txt", ".md")
JSON_SUFFIXES = (".json", ".jsonl")

MANUAL_SOURCES: tuple[ManualSource, ...] = ("ajio_manual", "quora_manual")

#: Relative to the project root; matches ``config.yaml``.
DEFAULT_IMPORT_DIRS: dict[ManualSource, str] = {
    "ajio_manual": "data/manual/ajio",
    "quora_manual": "data/manual/quora",
}


class ManualDocument(BaseModel):
    """One hand-collected record, normalized, before it becomes a ``RawRecord``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str = Field(min_length=1, validation_alias=AliasChoices("id", "native_id", "source_native_id"))
    source: ManualSource = Field(validation_alias=AliasChoices("source", "route"))
    url: str | None = None
    text: str = Field(min_length=1)
    author: str | None = None
    timestamp: str | None = Field(
        default=None,
        validation_alias=AliasChoices("timestamp", "created_utc", "created", "created_raw", "date"),
    )
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _fill_id_and_fold_content_type(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        meta = dict(payload.get("meta") or {})
        # Bookmarklets may put content_type / product_id / question at the top level.
        for key in ("content_type", "product_id", "question", "rating", "product_title", "thread_title"):
            if key in payload and key not in meta and payload[key] is not None:
                meta[key] = payload[key]
        payload["meta"] = meta
        text = (payload.get("text") or "").strip()
        payload["text"] = text
        if not payload.get("id") and not payload.get("native_id") and not payload.get("source_native_id"):
            if text:
                try:
                    payload["id"] = content_id(text)
                except ValueError:
                    pass
        return payload


@dataclass
class LoadResult:
    documents: list[ManualDocument]
    files_read: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


ProseParser = Callable[[Path], tuple[list[dict[str, Any]], list[str]]]


def discover_files(import_dir: Path) -> list[Path]:
    """Candidate files in ``import_dir``, README and dotted/_-prefixed names excluded.

    Non-recursive on purpose: a person dropping a thread in the directory they
    were told to use should not have to wonder whether a nested folder counts.
    """
    if not import_dir.is_dir():
        return []
    return sorted(
        path
        for path in import_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_SUFFIXES
        and not is_import_documentation(path)
    )


def _as_object_list(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, dict):
        if isinstance(payload.get("documents"), list):
            return [item for item in payload["documents"] if isinstance(item, dict)]
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    raise ValueError(f"expected an object, an array, or {{documents: [...]}}, got {type(payload).__name__}")


def parse_json_file(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    raw = read_text_tolerant(path)
    warnings: list[str] = []
    if path.suffix.lower() == ".jsonl":
        items: list[dict[str, Any]] = []
        for number, line in enumerate(raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                warnings.append(f"{path.name}:{number}: invalid JSON ({exc.msg})")
                continue
            items.extend(_as_object_list(parsed))
        return items, warnings
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name}: invalid JSON ({exc.msg})") from exc
    return _as_object_list(parsed), warnings


def document_from_mapping(item: dict[str, Any], *, source: ManualSource, origin: str) -> ManualDocument:
    """Validate one object against the shared shape, filling ``source`` if omitted."""
    payload = dict(item)
    payload.setdefault("source", source)
    declared = payload.get("source") or payload.get("route")
    if declared not in (None, source):
        raise ValueError(
            f"{origin}: source {declared!r} does not match this directory ({source})"
        )
    payload["source"] = source
    doc = ManualDocument.model_validate(payload)
    _check_source_rules(doc, origin=origin)
    return doc


def _check_source_rules(doc: ManualDocument, *, origin: str) -> None:
    if doc.source == "ajio_manual":
        content_type = doc.meta.get("content_type")
        allowed = {AjioContentType.QA.value, AjioContentType.REVIEW.value}
        if content_type not in allowed:
            raise ValueError(
                f"{origin}: AJIO records need meta.content_type in {sorted(allowed)} "
                f"(edge-case 1.1.14); got {content_type!r}"
            )
        product_id = doc.meta.get("product_id")
        if not product_id and doc.url:
            import re

            match = re.search(r"/p/(\d{6,})", doc.url)
            if match:
                doc.meta["product_id"] = match.group(1)
        if not doc.meta.get("product_id"):
            raise ValueError(
                f"{origin}: AJIO records need a product id in meta.product_id or the /p/<id> URL"
            )
        if not doc.url:
            doc.url = f"https://www.ajio.com/p/{doc.meta['product_id']}"


def documents_from_payloads(
    payloads: Sequence[dict[str, Any]],
    *,
    source: ManualSource,
    origin: str,
    warnings: list[str] | None = None,
) -> list[ManualDocument]:
    """Turn a collector-specific parse_file payload into the shared shape.

    When ``warnings`` is given, a payload that fails validation is skipped and
    the reason appended to that list, so one bad record does not lose the rest of
    the file. When it is ``None`` the failure propagates, preserving the original
    all-or-nothing contract for any caller that wants it.
    """
    docs: list[ManualDocument] = []
    for index, payload in enumerate(payloads):
        item = {
            "id": payload.get("native_id"),
            "source": source,
            "url": payload.get("url"),
            "text": payload.get("text"),
            "author": payload.get("author"),
            "timestamp": payload.get("created_raw") or payload.get("created_utc"),
            "meta": dict(payload.get("meta") or {}),
        }
        try:
            docs.append(document_from_mapping(item, source=source, origin=f"{origin}[{index}]"))
        except (ValueError, json.JSONDecodeError) as exc:
            if warnings is None:
                raise
            warnings.append(f"skipped item {index}: {exc}")
    return docs


def load_dir(
    import_dir: Path,
    *,
    source: ManualSource,
    parse_prose: ProseParser | None = None,
) -> LoadResult:
    """Scan ``import_dir``, skip README, normalize every conformant file.

    Raises :class:`EmptyImportError` when the directory does not exist, holds
    only documentation, or holds files that together produce zero valid
    documents. That last case is the quiet failure this loader exists to kill:
    a malformed dump used to look like an empty import.
    """
    import_dir = Path(import_dir)
    result = LoadResult(documents=[])
    if source not in MANUAL_SOURCES:
        raise ValueError(f"unknown manual source {source!r}; known: {list(MANUAL_SOURCES)}")

    if not import_dir.is_dir():
        raise EmptyImportError(
            f"no hand-collected files in {import_dir}: the directory does not exist. "
            f"{_empty_hint(source)}"
        )

    paths = discover_files(import_dir)
    if not paths:
        raise EmptyImportError(
            f"no hand-collected files in {import_dir} (README and _-prefixed files are "
            f"ignored on purpose). {_empty_hint(source)}"
        )

    for path in paths:
        try:
            if path.suffix.lower() in JSON_SUFFIXES:
                items, warnings = parse_json_file(path)
                result.warnings.extend(warnings)
                docs = []
                for i, item in enumerate(items):
                    try:
                        docs.append(
                            document_from_mapping(item, source=source, origin=f"{path.name}[{i}]")
                        )
                    except (ValueError, json.JSONDecodeError) as exc:
                        # One malformed record must not discard the rest of the
                        # file (and, if it is the only file, the whole import).
                        # An all-bad file still yields no docs and is skipped below.
                        result.warnings.append(f"{path.name}: {exc}")
            else:
                if parse_prose is None:
                    result.files_skipped.append(path.name)
                    result.warnings.append(
                        f"{path.name}: prose files need a source parser; save as .json instead"
                    )
                    continue
                payloads, warnings = parse_prose(path)
                result.warnings.extend(warnings)
                file_warnings: list[str] = []
                docs = documents_from_payloads(
                    payloads, source=source, origin=path.name, warnings=file_warnings
                )
                result.warnings.extend(f"{path.name}: {w}" for w in file_warnings)
        except (ValueError, json.JSONDecodeError) as exc:
            result.files_skipped.append(path.name)
            result.warnings.append(f"{path.name}: {exc}")
            continue

        if not docs:
            result.files_skipped.append(path.name)
            result.warnings.append(f"{path.name}: parsed, but produced no documents")
            continue
        result.files_read.append(path.name)
        for doc in docs:
            doc.meta.setdefault("source_file", path.name)
            doc.meta.setdefault("extraction", "manual_import")
            result.documents.append(doc)

    if not result.documents:
        detail = "; ".join(result.warnings) if result.warnings else "every file was skipped"
        raise EmptyImportError(
            f"{import_dir} has files, but none produced a valid document ({detail}). "
            f"{_empty_hint(source)}"
        )
    return result


def _empty_hint(source: ManualSource) -> str:
    if source == "ajio_manual":
        return (
            "ajio_manual is disabled in config.yaml: AJIO carries no free text on "
            "site — only rating, fit and quality bars — so there is no Q&A or review "
            "prose to hand-collect and this directory is expected to stay empty. "
            "Re-enable the source only if the site starts publishing review text, "
            "then fill it with the bookmarklet or CDP helper in "
            "scripts/manual_extract/."
        )
    return (
        "Quora is a pre-purchase route and YouTube currently supplies all of that "
        "evidence. Time-box 15–25 answers. Find threads with Google "
        "(site:quora.com AJIO sizing / returns / \"worth buying\"), then run the "
        "extract snippet in scripts/manual_extract/."
    )


def validate_project(project_root: Path) -> dict[ManualSource, LoadResult | EmptyImportError]:
    """Load both production import directories. Used by the CLI and by tests."""
    from src.collect.ajio_manual import parse_file as parse_ajio
    from src.collect.quora_manual import parse_file as parse_quora

    parsers: dict[ManualSource, ProseParser] = {
        "ajio_manual": parse_ajio,
        "quora_manual": lambda path: (parse_quora(path), []),
    }
    outcomes: dict[ManualSource, LoadResult | EmptyImportError] = {}
    for source in MANUAL_SOURCES:
        import_dir = project_root / DEFAULT_IMPORT_DIRS[source]
        try:
            outcomes[source] = load_dir(import_dir, source=source, parse_prose=parsers[source])
        except EmptyImportError as exc:
            outcomes[source] = exc
    return outcomes


def main(argv: Sequence[str] | None = None) -> int:
    harden_stdio()
    parser = argparse.ArgumentParser(
        description="Validate the two manual Collect directories and print document counts."
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="one directory to validate; default is both production import dirs",
    )
    parser.add_argument(
        "--source",
        choices=MANUAL_SOURCES,
        default=None,
        help="required with --dir",
    )
    args = parser.parse_args(argv)

    from src.common.config import get_settings

    settings = get_settings()
    if args.dir is not None:
        if args.source is None:
            parser.error("--source is required with --dir")
        from src.collect.ajio_manual import parse_file as parse_ajio
        from src.collect.quora_manual import parse_file as parse_quora

        prose = parse_ajio if args.source == "ajio_manual" else lambda path: (parse_quora(path), [])
        try:
            result = load_dir(args.dir, source=args.source, parse_prose=prose)
        except EmptyImportError as exc:
            print(f"FAIL  {args.source}: {exc}")
            return 1
        print(f"OK    {args.source}: {len(result.documents)} document(s) from {result.files_read}")
        for warning in result.warnings:
            print(f"  warning: {warning}")
        return 0

    enabled = settings.run.collection.enabled_sources()
    outcomes = validate_project(settings.project_root)
    failed = 0
    for source, outcome in outcomes.items():
        path = DEFAULT_IMPORT_DIRS[source]
        if source not in enabled:
            # Off in config.yaml is a decision, not an unfilled directory. Reported
            # as a FAIL it would keep sending whoever runs this to collect text the
            # site does not publish — see the ajio_manual block in config.yaml.
            print(f"OFF   {source:<14} {path}  — disabled in config.yaml; nothing to import")
            continue
        if isinstance(outcome, EmptyImportError):
            print(f"FAIL  {source:<14} {path}  — {outcome}")
            failed += 1
        else:
            print(
                f"OK    {source:<14} {path}  {len(outcome.documents)} document(s) "
                f"in {len(outcome.files_read)} file(s)"
            )
            for warning in outcome.warnings:
                print(f"  warning: {warning}")
    if failed:
        print(
            "\nAn enabled import directory with no conformant file keeps the "
            "source-coverage criterion unmet. That is the intended signal, not a "
            "parser bug."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
