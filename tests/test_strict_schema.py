"""The tagging schema must satisfy Groq's strict-mode rules at build time.

Every rule checked here was learned from a live 400 rather than from the docs, so
the point of these tests is to make a schema mistake cost a red test rather than a
failed batch partway through a multi-day tagging run.
"""

from __future__ import annotations

import pytest

from src.common.schemas import (
    DocumentTags,
    TaggedDocument,
    TaggingResponse,
    strict_schema_violations,
    tagging_response_schema,
)


def test_tagging_response_schema_is_strict_compatible():
    assert strict_schema_violations(TaggingResponse.model_json_schema()) == []
    assert tagging_response_schema()["properties"].keys() == {"documents"}


def test_every_object_forbids_additional_properties():
    schema = tagging_response_schema()
    objects = [schema, *schema["$defs"].values()]
    for node in objects:
        if node.get("type") == "object":
            assert node["additionalProperties"] is False


def test_every_property_is_required():
    """Strict mode has no notion of an optional field; absence must be expressed as `[]`."""
    schema = tagging_response_schema()
    for name, node in schema["$defs"].items():
        if node.get("type") != "object":
            continue
        assert set(node["properties"]) == set(node["required"]), name


def test_doc_id_is_the_first_property_in_each_item():
    """Constrained decoding emits properties in schema order.

    Putting the id first makes the model state which document it is coding before
    it codes it, which is what the batch reconciliation step relies on.
    """
    item_properties = list(tagging_response_schema()["$defs"]["TaggedDocument"]["properties"])
    assert item_properties[0] == "doc_id"


def test_numeric_fields_are_enums_not_ranges():
    """A free `number` field is not actually constrained during generation.

    A live call returned `"confidence": 0. nine` — the decoder lifted the word
    "nine" from the document text and Groq rejected the whole batch. Enumerating
    the legal values removes the free-generation path.
    """
    properties = tagging_response_schema()["$defs"]["TaggedDocument"]["properties"]
    for field in ("severity", "actionability_non_monetary", "confidence_pct"):
        assert "enum" in properties[field], field
        assert "minimum" not in properties[field]
        assert "maximum" not in properties[field]


def test_mixed_numeric_enums_are_reported():
    """Groq rejects `[0.0, 0.1, ... 1.0]` as mixing 'integer' and 'number'.

    Pydantic emits a single `"type": "number"` for such a field and so gives no
    local warning, which is why this rule is checked explicitly.
    """
    schema = {
        "type": "object",
        "properties": {"confidence": {"type": "number", "enum": [0.0, 0.1, 1.0]}},
        "required": ["confidence"],
        "additionalProperties": False,
    }
    violations = strict_schema_violations(schema)
    assert any("whole and fractional" in violation for violation in violations)

    schema["properties"]["confidence"]["enum"] = [0, 10, 100]
    assert strict_schema_violations(schema) == []


def test_unsupported_constraint_keywords_are_reported():
    schema = {
        "type": "object",
        "properties": {"quote": {"type": "string", "minLength": 1}},
        "required": ["quote"],
        "additionalProperties": False,
    }
    assert any("minLength" in violation for violation in strict_schema_violations(schema))


def test_missing_required_entry_is_reported():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a"],
        "additionalProperties": False,
    }
    assert any("'b' is not in required" in violation for violation in strict_schema_violations(schema))


def test_nested_objects_are_walked():
    """A violation buried in an array item must still be found."""
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                    "required": ["x"],
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    assert any("additionalProperties" in v for v in strict_schema_violations(schema))


def test_tagging_response_schema_raises_on_an_incompatible_model():
    """The generator is a gate, not a formatter."""
    from pydantic import BaseModel, ConfigDict, Field

    class Loose(BaseModel):
        model_config = ConfigDict(extra="forbid")
        score: int = Field(ge=0, le=10)

    assert any("minimum" in v for v in strict_schema_violations(Loose.model_json_schema()))


def test_tagged_document_carries_all_tag_fields():
    """The batch item must not drift from the payload persisted in doc_tags."""
    tag_fields = set(DocumentTags.model_fields)
    item_fields = set(TaggedDocument.model_fields)
    assert tag_fields < item_fields
    assert item_fields - tag_fields == {"doc_id"}
