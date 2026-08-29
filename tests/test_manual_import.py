"""The two manual Collect directories are first-class inputs.

Fixtures live under tests/fixtures/manual/, never under data/manual/: a synthetic
file in the production import directory previously reached the corpus posing as
customer text. ``data/manual/quora`` now holds real hand-collected threads, so the
rule is enforced by comparing content rather than by requiring an empty directory.
``ajio_manual`` is disabled in config.yaml because AJIO publishes no free text to
collect, so its empty directory is the expected state rather than a gap.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from src.collect.ajio_manual import AjioManualCollector, parse_file as parse_ajio
from src.collect.base import EmptyImportError
from src.collect.manual import (
    MANUAL_SOURCES,
    discover_files,
    load_dir,
    main as validate_main,
)
from src.collect.quora_manual import QuoraManualCollector, parse_file as parse_quora
from src.common.schemas import PurchaseStage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "manual"

AJIO_FIXTURE = FIXTURES / "ajio"
QUORA_FIXTURE = FIXTURES / "quora"


def _prose_ajio(path: Path):
    return parse_ajio(path)


def _prose_quora(path: Path):
    return parse_quora(path), []


def test_ajio_fixture_normalizes_to_the_shared_shape():
    result = load_dir(AJIO_FIXTURE, source="ajio_manual", parse_prose=_prose_ajio)
    assert [d.source for d in result.documents] == ["ajio_manual", "ajio_manual"]
    qa, review = result.documents
    for doc in result.documents:
        assert doc.id
        assert doc.url and "/p/441098234" in doc.url
        assert doc.text
        assert doc.meta.get("content_type") in {"qa", "review"}
        assert doc.meta.get("product_id") == "441098234"
    assert qa.meta["content_type"] == "qa"
    assert qa.author is None
    assert review.meta["content_type"] == "review"
    assert review.author == "meera"
    assert review.timestamp == "12 May 2026"


def test_quora_fixture_normalizes_to_the_shared_shape():
    result = load_dir(QUORA_FIXTURE, source="quora_manual", parse_prose=_prose_quora)
    assert len(result.documents) == 1
    doc = result.documents[0]
    assert doc.source == "quora_manual"
    assert doc.id
    assert doc.url and "quora.com" in doc.url
    assert "size charts contradict" in doc.text
    assert doc.meta.get("question", "").startswith("Is Ajio sizing")
    assert doc.author is None


def test_collectors_emit_raw_records_from_the_fixtures(tmp_path):
    """The collectors are a thin wrap around the loader, including JSON."""
    ajio_dir = tmp_path / "data" / "manual" / "ajio"
    quora_dir = tmp_path / "data" / "manual" / "quora"
    ajio_dir.mkdir(parents=True)
    quora_dir.mkdir(parents=True)
    (ajio_dir / "wishlist-kurta.json").write_text(
        (AJIO_FIXTURE / "wishlist-kurta.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (quora_dir / "ajio-sizing.json").write_text(
        (QUORA_FIXTURE / "ajio-sizing.json").read_text(encoding="utf-8"), encoding="utf-8"
    )

    class AjioCfg:
        import_dir = "data/manual/ajio"

    class QuoraCfg:
        import_dir = "data/manual/quora"

    ajio = list(AjioManualCollector(project_root=tmp_path).fetch(AjioCfg()))
    quora = list(QuoraManualCollector(project_root=tmp_path).fetch(QuoraCfg()))
    assert len(ajio) == 2
    assert [r.purchase_stage for r in ajio] == [
        PurchaseStage.PRE_PURCHASE,
        PurchaseStage.POST_PURCHASE,
    ]
    assert len(quora) == 1
    assert quora[0].url and "quora.com" in quora[0].url
    assert quora[0].source == "quora_manual"


def test_readme_only_dir_fails_loudly(tmp_path):
    """The live production state of both dirs: only README, so the loader raises."""
    directory = tmp_path / "data" / "manual" / "ajio"
    directory.mkdir(parents=True)
    (directory / "README.md").write_text(
        "product: 1\n\n## Q&A\n\nQ: Does this run small?\n", encoding="utf-8"
    )
    with pytest.raises(EmptyImportError) as raised:
        load_dir(directory, source="ajio_manual", parse_prose=_prose_ajio)
    assert "README" in str(raised.value) or "ignored" in str(raised.value).lower()
    assert discover_files(directory) == []


def test_malformed_json_dir_fails_loudly_rather_than_looking_empty(tmp_path):
    directory = tmp_path / "dump"
    directory.mkdir()
    (directory / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(EmptyImportError) as raised:
        load_dir(directory, source="quora_manual", parse_prose=_prose_quora)
    assert "broken.json" in str(raised.value)


def test_ajio_json_without_content_type_is_rejected(tmp_path):
    directory = tmp_path / "dump"
    directory.mkdir()
    (directory / "no-type.json").write_text(
        '{"text": "Does this run small on the shoulders?", "url": "https://www.ajio.com/p/441098234"}',
        encoding="utf-8",
    )
    with pytest.raises(EmptyImportError) as raised:
        load_dir(directory, source="ajio_manual", parse_prose=_prose_ajio)
    assert "content_type" in str(raised.value)


def test_one_bad_record_does_not_discard_the_rest_of_its_file(tmp_path):
    """A missing product id costs one record, not the file — and not the import.

    The JSON branch used to build its documents in one comprehension, so a single
    ValueError was caught at file level: three good records and one unresolvable
    product id imported as nothing, and in a one-file directory that surfaced as
    EmptyImportError for the whole route. The bad record is now skipped, named in
    warnings, and the file still counts as read.
    """
    directory = tmp_path / "dump"
    directory.mkdir()
    (directory / "browsing.json").write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "url": "https://www.ajio.com/p/441098234",
                        "text": "Does this kurta run small? I am usually a medium.",
                        "meta": {"content_type": "qa", "product_id": "441098234"},
                    },
                    {
                        # A search results page, so there is no /p/<id> to fall back
                        # to and no product id to attribute the question to.
                        "url": "https://www.ajio.com/search/?text=kurta",
                        "text": "Is the fabric see-through in white?",
                        "meta": {"content_type": "qa", "product_id": None},
                    },
                    {
                        "url": "https://www.ajio.com/p/469558637",
                        "text": "Kept this in my wishlist for three weeks watching the price.",
                        "meta": {"content_type": "review", "product_id": "469558637"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = load_dir(directory, source="ajio_manual", parse_prose=_prose_ajio)

    assert len(result.documents) == 2
    assert [d.meta["product_id"] for d in result.documents] == ["441098234", "469558637"]
    assert result.files_read == ["browsing.json"]
    assert result.files_skipped == []
    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert "browsing.json[1]" in warning and "product id" in warning


def test_cli_against_the_fixtures_exits_zero(capsys):
    code = validate_main(["--dir", str(AJIO_FIXTURE), "--source", "ajio_manual"])
    assert code == 0
    assert "2 document" in capsys.readouterr().out
    code = validate_main(["--dir", str(QUORA_FIXTURE), "--source", "quora_manual"])
    assert code == 0


def test_cli_against_production_dirs_reports_one_route_off_and_one_filled(capsys):
    """The CLI's two live states: ajio_manual OFF, quora_manual OK.

    This asserted ``exit 1`` while ``data/manual/quora`` held only README, which was
    the correct signal for as long as it was true. Real threads have since landed,
    so the expectation is inverted rather than deleted: what needs proving is that
    the signal tracks the directory instead of being a constant, and it only proves
    that if it changes when the directory does.

    ajio_manual stays OFF, not FAIL — an empty directory for a source AJIO gives
    nothing to collect for is not an outstanding task.
    """
    ajio = PROJECT_ROOT / "data" / "manual" / "ajio"
    quora = PROJECT_ROOT / "data" / "manual" / "quora"
    assert discover_files(ajio) == []
    assert discover_files(quora), "quora_manual should hold hand-collected threads"
    assert validate_main([]) == 0

    output = capsys.readouterr().out
    assert "OFF   ajio_manual" in output
    assert "OK    quora_manual" in output


def test_fixtures_were_not_dropped_into_the_production_import_dirs():
    """A synthetic file in data/manual/ previously reached the corpus as evidence.

    This used to assert both directories were empty, which was an equivalent check
    only while they were. ``quora_manual`` now holds real threads, so the rule is
    stated as what it always meant: nothing under ``data/manual/`` may be a copy of
    a fixture. Byte-identity catches the copied file, and the marker catches a
    copy someone reformatted on the way in.
    """
    fixture_bytes = {
        path.read_bytes()
        for folder in FIXTURES.iterdir()
        if folder.is_dir()
        for path in folder.iterdir()
        if path.is_file()
    }
    assert fixture_bytes, "no fixtures to compare against"

    for folder in (
        PROJECT_ROOT / "data" / "manual" / "ajio",
        PROJECT_ROOT / "data" / "manual" / "quora",
    ):
        assert (FIXTURES / folder.name).is_dir()
        for path in discover_files(folder):
            assert path.read_bytes() not in fixture_bytes, f"{path} is a copy of a fixture"
            assert "size charts contradict" not in path.read_text(
                encoding="utf-8", errors="replace"
            ), f"{path} contains fixture text"

    # AJIO stays empty: the source is disabled because there is nothing to collect.
    assert discover_files(PROJECT_ROOT / "data" / "manual" / "ajio") == []


def test_manual_loader_imports_no_network_library():
    """Same guarantee as the two collectors: the shared path cannot fetch."""
    source_path = PROJECT_ROOT / "src" / "collect" / "manual.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {
        "requests", "urllib", "urllib3", "http", "httpx", "aiohttp",
        "socket", "praw", "selenium", "playwright", "mechanize",
    }
    assert not (imported & forbidden), imported & forbidden
    assert MANUAL_SOURCES == ("ajio_manual", "quora_manual")


def test_readme_next_to_json_is_skipped_not_fatal(tmp_path):
    directory = tmp_path / "ajio"
    directory.mkdir()
    (directory / "README.md").write_text(
        "product: 1\n\n## Q&A\n\nQ: Does this run small?\n", encoding="utf-8"
    )
    (directory / "wishlist-kurta.json").write_text(
        (AJIO_FIXTURE / "wishlist-kurta.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    result = load_dir(directory, source="ajio_manual", parse_prose=_prose_ajio)
    assert len(result.documents) == 2
    assert "wishlist-kurta.json" in result.files_read


def test_collect_modules_do_not_import_the_cdp_helper():
    """Playwright stays under scripts/, so Collect cannot grow a network path."""
    collect = PROJECT_ROOT / "src" / "collect"
    for path in collect.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not any("manual_extract" in name or "cdp_extract" in name for name in imported), (
            path.name,
            imported,
        )
        assert "connect_over_cdp" not in path.read_text(encoding="utf-8"), path.name


def test_extract_iifes_share_the_document_contract():
    extract = PROJECT_ROOT / "scripts" / "manual_extract"
    ajio_js = (extract / "ajio_extract.js").read_text(encoding="utf-8")
    quora_js = (extract / "quora_extract.js").read_text(encoding="utf-8")
    assert ajio_js.strip().endswith(")();")
    assert quora_js.strip().endswith(")();")
    for body in (ajio_js, quora_js):
        assert "documents" in body
        assert "warnings" in body
        assert "window.getSelection" in body
    assert "content_type" in ajio_js
    assert "product_id" in ajio_js
    assert "ajio_manual" in ajio_js
    assert "quora_manual" in quora_js
    assert "meta" in quora_js and "question" in quora_js


def test_cdp_helper_writes_json_without_playwright(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "cdp_extract",
        PROJECT_ROOT / "scripts" / "manual_extract" / "cdp_extract.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    dest = tmp_path / "data" / "manual" / "ajio"
    path = module.dump_path(dest, "ajio", "https://www.ajio.com/p/441098234", stamp="20260822T000000Z")
    assert path.name == "ajio-441098234-20260822T000000Z.json"
    module.write_payload(path, {"documents": [{"text": "Does this run small on the shoulders?"}]})
    assert path.is_file()

    class Page:
        def __init__(self, url):
            self.url = url

    pages = module.matching_pages(
        [Page("https://www.ajio.com/p/1"), Page("https://www.quora.com/foo"), Page("about:blank")],
        "ajio",
    )
    assert [p.url for p in pages] == ["https://www.ajio.com/p/1"]

