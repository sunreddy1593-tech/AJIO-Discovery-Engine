"""Phase 7 gates: identical scores, cache-warm tagging, and the run-log appendix."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.common.config import PROJECT_ROOT
from src.common.db import init_db, upsert_documents
from src.common.schemas import Document, DocumentTags, EvidenceSpan
from src.quantify.metrics import QuantifyKnobs, load_analyzable, quantify
from src.quantify.run_quantify import SCORES_NAME, run as quantify_run, write_scores
from src.synthesize.run_log_appendix import HEADING, render_pipeline_appendix
from src.tag import cache, run_tagging
from src.tag.llm_client import TaggingClient
from src.tag.taxonomy import (
    TAXONOMY_VERSION,
    BlockerType,
    EvidenceTag,
    IntentClass,
    OutcomeMentioned,
)
from tests.test_quantify import fixture_corpus

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
MODEL = "openai/gpt-oss-120b"


def _tags() -> DocumentTags:
    return DocumentTags(
        is_relevant=True,
        wishlist_motivation=[],
        blocker_type=[BlockerType.FIT_SIZE_UNCERTAINTY],
        uncertainty_type=[],
        info_sought_elsewhere=[],
        segment_cue=[],
        intent_class=IntentClass.GENUINE_INTENT,
        outcome_mentioned=OutcomeMentioned.STILL_DECIDING,
        severity=4,
        actionability_non_monetary=1,
        confidence_pct=80,
        evidence=[
            EvidenceSpan(
                tag=EvidenceTag(BlockerType.FIT_SIZE_UNCERTAINTY.value),
                quote="size chart makes no sense",
            )
        ],
    )


def test_two_quantify_runs_write_identical_opportunity_scores(tmp_path):
    """Architecture.md §11 reproducibility gate, without touching the live corpus."""
    db = tmp_path / "discovery.db"
    conn = init_db(db)
    fixture_corpus(conn)
    docs = load_analyzable(conn)
    knobs = QuantifyKnobs(cluster_jaccard_min=0.5, author_salt="phase7")
    first = quantify(docs, knobs=knobs)
    second = quantify(docs, knobs=knobs)
    conn.close()

    a = tmp_path / "a" / SCORES_NAME
    b = tmp_path / "b" / SCORES_NAME
    write_scores(a, first.opportunities, first.sources)
    write_scores(b, second.opportunities, second.sources)
    assert a.read_bytes() == b.read_bytes()


def test_quantify_entrypoint_is_stable_across_two_writes(tmp_path):
    db = tmp_path / "discovery.db"
    conn = init_db(db)
    fixture_corpus(conn)
    conn.close()
    shared = dict(
        interim_db=db,
        logs_dir=tmp_path / "logs",
        config_hash="harden00000000",
        run=None,
        credentials=SimpleNamespace(hash_salt="phase7"),
    )
    quantify_run(SimpleNamespace(processed_dir=tmp_path / "p1", **shared))
    quantify_run(SimpleNamespace(processed_dir=tmp_path / "p2", **shared))
    assert (tmp_path / "p1" / SCORES_NAME).read_bytes() == (
        tmp_path / "p2" / SCORES_NAME
    ).read_bytes()


def _tag_settings(tmp_path, db):
    class _Model:
        name = MODEL
        docs_per_request = 6
        max_doc_tokens = 700
        max_completion_tokens = 4096

    class _Tagging:
        rpm = 30
        rpd = 1000
        tpm = 8000
        tpd = 200000

    class _RL:
        tagging = _Tagging()

    class _Run:
        model = _Model()
        rate_limits = _RL()

    return SimpleNamespace(
        project_root=PROJECT_ROOT,
        interim_db=db,
        logs_dir=tmp_path / "logs",
        config_hash="harden00000000",
        run=_Run(),
    )


def test_second_tagging_run_issues_zero_api_calls(tmp_path, monkeypatch):
    """Architecture.md §11 cache gate: a warm cache must not call Groq."""
    db = tmp_path / "discovery.db"
    conn = init_db(db)
    text = "wishlisted this kurta but the size chart makes no sense to me"
    doc = Document(
        doc_id="cachedoc00000001",
        source="youtube",
        source_native_id="n1",
        text=text,
        created_utc=NOW,
        word_count=len(text.split()),
        is_relevant=True,
        ingested_at=NOW,
    )
    upsert_documents(conn, [doc])
    key = cache.cache_key(
        doc_id=doc.doc_id,
        text=text,
        model=MODEL,
        taxonomy_version=TAXONOMY_VERSION,
        prompt_version=run_tagging.PROMPT_VERSION,
    )
    cache.put(conn, key, _tags(), prompt_tokens=100, completion_tokens=40)
    conn.close()

    calls: list[int] = []

    def boom(self, *args, **kwargs):
        calls.append(1)
        raise AssertionError("tag_batch must not run when every document is cached")

    monkeypatch.setattr(TaggingClient, "tag_batch", boom)
    summary = run_tagging.run(_tag_settings(tmp_path, db), resume=True)
    assert calls == []
    assert summary["tagged_this_run"] == 0
    assert summary["api_calls_this_run"] == 0

    conn = init_db(db)
    logged = conn.execute(
        "SELECT stage, records_out, notes FROM run_log WHERE stage = 'tag'"
    ).fetchone()
    conn.close()
    assert logged is not None
    assert logged["records_out"] == 0
    assert '"api_calls_this_run": 0' in logged["notes"]


def test_pipeline_appendix_renders_tokens_and_elapsed(tmp_path):
    from src.common.db import run_log as write_run

    conn = init_db(tmp_path / "discovery.db")
    cache.put(
        conn,
        "k",
        _tags(),
        prompt_tokens=1000,
        completion_tokens=200,
        reasoning_tokens=50,
    )
    with write_run(conn, "r1", "quantify", "hash") as entry:
        entry.records_in = 800
        entry.records_out = 24
        entry.note("phase 7")
    text = render_pipeline_appendix(conn)
    conn.close()
    assert HEADING in text
    assert "`quantify`" in text
    assert "1,250 tokens" in text
    assert "cached document" in text
    assert "method-reproducible, not command-reproducible" in text


def test_readme_names_the_venv_interpreter_and_the_run_order():
    text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe" in text
    assert "GROQ_API_KEY" in text
    assert "YOUTUBE_API_KEY" in text
    assert "src.synthesize.run_synthesis" in text
    assert "src.tag.run_tagging --resume" in text
    assert "zero" in text.lower()
    assert "method-reproducible" in text
