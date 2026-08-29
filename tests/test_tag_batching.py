"""Size-aware tagging batches (`edge-case.md` §1.2.4 / §4.1.2).

The census pulled in long complaints and Quora answers, so a fixed six-document
call could exceed Groq's per-request input limit, return 413, and then loop on
429s. These tests pin the two properties that stop that: a batch is packed to a
token budget, and a single over-limit document is truncated rather than dropped.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.tag.llm_client import (
    TaggingClient,
    TaggingFailedError,
    _is_payload_too_large,
    _line_tokens,
    approx_tokens,
    fit_document,
    pack_batches,
    truncate_to_tokens,
)


def _doc(i: int, tokens: int) -> dict:
    """A document whose payload line is about ``tokens`` long."""
    body = "word " * max(1, tokens * 4 // 5)
    return {"doc_id": f"d{i}", "text": body.strip()}


def _batch_cost(batch: list[dict], overhead: int) -> int:
    return overhead + sum(_line_tokens(d) for d in batch)


# --- packing --------------------------------------------------------------


def test_long_docs_are_split_so_each_request_stays_under_budget():
    overhead, budget, max_doc, max_count = 1000, 2000, 700, 6
    docs = [_doc(i, 500) for i in range(6)]
    batches = pack_batches(
        docs,
        max_count=max_count,
        max_doc_tokens=max_doc,
        input_budget=budget,
        overhead=overhead,
    )
    assert len(batches) > 1
    for batch in batches:
        assert len(batch) <= max_count
        assert _batch_cost(batch, overhead) <= budget


def test_a_single_over_limit_document_is_truncated_and_kept():
    original = _doc(0, 4000)
    before = original["text"]
    batches = pack_batches(
        [original],
        max_count=6,
        max_doc_tokens=80,
        input_budget=200,
        overhead=100,
    )
    assert len(batches) == 1
    assert len(batches[0]) == 1
    fitted = batches[0][0]
    assert fitted["doc_id"] == "d0"
    assert fitted["text"] != before
    assert approx_tokens(fitted["text"]) <= 80
    assert original["text"] == before  # corpus text is untouched
    assert _batch_cost(batches[0], 100) <= 200


def test_short_docs_still_travel_six_to_a_batch():
    """The count cap is an upper bound, not a new smaller default."""
    docs = [_doc(i, 10) for i in range(12)]
    batches = pack_batches(
        docs,
        max_count=6,
        max_doc_tokens=700,
        input_budget=5000,
        overhead=3000,
    )
    assert [len(b) for b in batches] == [6, 6]


def test_truncate_never_empties_a_nonempty_document():
    assert truncate_to_tokens("still in my wishlist", 0) != ""
    fitted, truncated = fit_document(_doc(1, 500), max_tokens=1)
    assert truncated
    assert fitted["text"]


# --- 413 handling ---------------------------------------------------------


class _TooLarge(Exception):
    status_code = 413
    body = {"error": {"message": "Payload Too Large"}}


def test_413_is_detected_as_payload_too_large():
    assert _is_payload_too_large(_TooLarge("413 Payload Too Large")) is True


def test_schema_400_is_not_treated_as_payload_too_large():
    class _Schema400(Exception):
        status_code = 400
        body = {"error": {"code": "json_validate_failed", "failed_generation": "{}"}}

    assert _is_payload_too_large(_Schema400("400 Bad Request")) is False


def _tagged_json(doc_ids: list[str]) -> str:
    from src.common.schemas import DocumentTags, EvidenceSpan, TaggedDocument
    from src.tag.taxonomy import BlockerType, IntentClass, OutcomeMentioned

    base = DocumentTags(
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
        evidence=[EvidenceSpan(tag=BlockerType.FIT_SIZE_UNCERTAINTY, quote="sizes run small")],
    ).model_dump()
    docs = [TaggedDocument(doc_id=i, **base).model_dump(mode="json") for i in doc_ids]
    return json.dumps({"documents": docs})


def _client(create):
    settings = SimpleNamespace(
        credentials=SimpleNamespace(groq_api_key=SimpleNamespace(get_secret_value=lambda: "x")),
        run=SimpleNamespace(
            model=SimpleNamespace(
                name="openai/gpt-oss-120b",
                temperature=0,
                seed=1,
                reasoning_effort="low",
                max_completion_tokens=64,
                max_doc_tokens=80,
                docs_per_request=6,
            ),
            rate_limits=SimpleNamespace(
                tagging=SimpleNamespace(rpm=1000, tpm=1_000_000, rpd=10_000, tpd=2_000_000)
            ),
        ),
    )
    client = TaggingClient(settings=settings)
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return client


def test_413_on_a_batch_splits_and_does_not_loop():
    """A 413 on six docs becomes two smaller calls, then succeeds. No last_error bloat."""
    calls: list[str] = []

    def create(**kwargs):
        payload = kwargs["messages"][1]["content"]
        system = kwargs["messages"][0]["content"]
        calls.append(payload)
        assert "PRIOR ERROR TO FIX" not in system
        ids = [line[1 : line.index("]")] for line in payload.splitlines() if line.startswith("[")]
        if len(ids) > 1:
            raise _TooLarge("413 Payload Too Large")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_tagged_json(ids)))],
            usage=SimpleNamespace(total_tokens=10, prompt_tokens=8, completion_tokens=2),
        )

    client = _client(create)
    docs = [_doc(i, 20) for i in range(2)]
    tagged, _ = client.tag_batch("prompt", docs)
    assert {t.doc_id for t in tagged} == {"d0", "d1"}
    assert any(len(c.splitlines()) > 1 for c in calls)  # the failing combined call
    assert sum(1 for c in calls if len(c.splitlines()) == 1) == 2


def test_413_on_a_lone_document_truncates_instead_of_dropping():
    def create(**kwargs):
        payload = kwargs["messages"][1]["content"]
        text = payload.split("] ", 1)[1]
        if approx_tokens(text) > 200:
            raise _TooLarge("413 Payload Too Large")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_tagged_json(["big"])))],
            usage=SimpleNamespace(total_tokens=10, prompt_tokens=8, completion_tokens=2),
        )

    client = _client(create)
    huge = {"doc_id": "big", "text": "word " * 2000}
    tagged, _ = client.tag_batch("prompt", [huge])
    assert [t.doc_id for t in tagged] == ["big"]
    assert huge["text"] == "word " * 2000  # original not mutated


def test_a_document_that_cannot_be_shrunk_is_skipped_not_looped():
    def create(**kwargs):
        raise _TooLarge("413 Payload Too Large")

    client = _client(create)
    with pytest.raises(TaggingFailedError, match="skipping"):
        client.tag_batch("prompt", [{"doc_id": "x", "text": "tiny"}])
