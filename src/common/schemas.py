"""The data contracts every stage reads and writes (`architecture.md` §5–§7).

Three ideas carry most of the weight here:

1. **Rejection happens at the boundary.** A record with empty text, a future
   timestamp, or mojibake is caught when the collector builds a ``RawRecord``,
   not discovered later as a strange row in the database.
2. **Purchase stage is derived, not stored.** ``SOURCE_STAGE`` maps a source to
   pre-/post-purchase so reclassifying a source never requires rewriting rows.
3. **The tagging schema is generated, never hand-written.** ``DocumentTags``
   is the single definition of the coding frame; the JSON schema sent to Groq is
   derived from it and checked against strict-mode rules at build time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.tag.taxonomy import (
    MULTI_LABEL_DIMENSIONS,
    BlockerType,
    EvidenceTag,
    InfoSoughtElsewhere,
    IntentClass,
    OutcomeMentioned,
    SegmentCue,
    UncertaintyType,
    WishlistMotivation,
)

# --------------------------------------------------------------------------
# Sources and purchase stage
# --------------------------------------------------------------------------


class PurchaseStage(StrEnum):
    PRE_PURCHASE = "pre_purchase"
    POST_PURCHASE = "post_purchase"
    MIXED = "mixed"


class AjioContentType(StrEnum):
    """AJIO's two content types sit on opposite sides of the purchase.

    Conflating them is a P0 error (`edge-case.md` §1.1.14): Q&A is someone
    deciding whether to buy, reviews are someone reporting on having bought.
    """

    QA = "qa"
    REVIEW = "review"


#: Every source the pipeline knows how to ingest, with the purchase stage its
#: content speaks to. The North Star metric is about *pre-purchase* hesitation,
#: so this mapping is what stops a corpus of delivery complaints from being
#: mistaken for evidence about wishlist abandonment.
SOURCE_STAGE: dict[str, PurchaseStage] = {
    "youtube": PurchaseStage.PRE_PURCHASE,
    "quora_manual": PurchaseStage.PRE_PURCHASE,
    "reddit": PurchaseStage.PRE_PURCHASE,
    "mouthshut": PurchaseStage.POST_PURCHASE,
    "trustpilot": PurchaseStage.POST_PURCHASE,
    "complaints_board": PurchaseStage.POST_PURCHASE,
    "consumer_complaints_in": PurchaseStage.POST_PURCHASE,
    "play_store": PurchaseStage.MIXED,
    "app_store": PurchaseStage.MIXED,
    # Both resolved per record from meta.content_type; see purchase_stage().
    "ajio_onsite": PurchaseStage.MIXED,
    "ajio_manual": PurchaseStage.MIXED,
}

KNOWN_SOURCES: frozenset[str] = frozenset(SOURCE_STAGE)

#: Shared by both AJIO sources so the two can never drift apart on the question
#: that matters most about them (`edge-case.md` §1.1.14).
_AJIO_STAGE_BY_CONTENT_TYPE: dict[str, PurchaseStage] = {
    AjioContentType.QA.value: PurchaseStage.PRE_PURCHASE,
    AjioContentType.REVIEW.value: PurchaseStage.POST_PURCHASE,
}

#: Sources whose stage cannot be read off the source name alone.
STAGE_BY_CONTENT_TYPE: dict[str, dict[str, PurchaseStage]] = {
    "ajio_onsite": _AJIO_STAGE_BY_CONTENT_TYPE,
    "ajio_manual": _AJIO_STAGE_BY_CONTENT_TYPE,
}


def purchase_stage(source: str, meta: dict[str, Any] | None = None) -> PurchaseStage:
    """Resolve the purchase stage of a single record.

    Raises on an unknown source rather than defaulting: a newly added collector
    must make a deliberate choice about which side of the purchase it speaks to,
    because that choice determines how its evidence is weighted.
    """
    if source not in SOURCE_STAGE:
        raise ValueError(
            f"unknown source {source!r}; add it to SOURCE_STAGE in src/common/schemas.py "
            f"with an explicit purchase stage. Known: {sorted(KNOWN_SOURCES)}"
        )
    refinement = STAGE_BY_CONTENT_TYPE.get(source)
    if refinement is None:
        return SOURCE_STAGE[source]

    content_type = (meta or {}).get("content_type")
    if content_type not in refinement:
        raise ValueError(
            f"source {source!r} requires meta.content_type in {sorted(refinement)} to resolve "
            f"its purchase stage, got {content_type!r}"
        )
    return refinement[content_type]


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------

#: Above this share of U+FFFD the text was decoded from the wrong encoding and is
#: not worth repairing (`edge-case.md` §1.2.6).
MAX_REPLACEMENT_CHAR_RATIO = 0.02

REPLACEMENT_CHAR = "\ufffd"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RawRecord(_Strict):
    """One unit of collected content, exactly as the collector saw it.

    Written to ``data/raw/<source>/<run_date>/part-000.jsonl`` and never mutated
    afterwards, so a re-run can rebuild the corpus without re-scraping.
    """

    source: str
    source_native_id: str
    url: str | None = None
    author_raw: str | None = None
    created_utc: datetime | None = None
    text: str
    meta: dict[str, Any] = Field(default_factory=dict)
    collected_at: datetime
    collector_version: str

    @field_validator("source")
    @classmethod
    def _known_source(cls, value: str) -> str:
        if value not in KNOWN_SOURCES:
            raise ValueError(f"unknown source {value!r}; known: {sorted(KNOWN_SOURCES)}")
        return value

    @field_validator("source_native_id")
    @classmethod
    def _native_id_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_native_id must not be blank; it is half of the doc_id")
        return value

    @field_validator("text")
    @classmethod
    def _text_is_usable(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("text is empty or whitespace-only")
        ratio = text.count(REPLACEMENT_CHAR) / len(text)
        if ratio > MAX_REPLACEMENT_CHAR_RATIO:
            raise ValueError(
                f"text is {ratio:.0%} replacement characters, so it was decoded with the wrong "
                "encoding and cannot be repaired"
            )
        return text

    @field_validator("created_utc", "collected_at")
    @classmethod
    def _as_utc(cls, value: datetime | None) -> datetime | None:
        """Naive timestamps are assumed UTC; everything is stored UTC-aware.

        Mixing naive and aware datetimes makes comparisons raise at runtime, and
        the recency weighting in Stage 4 compares every document against a cutoff.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _clamp_future_timestamps(self) -> RawRecord:
        """App-store reviews carry device clocks, which are sometimes years ahead.

        Left alone, a future date would win every recency weighting in Stage 4.
        The original is preserved in ``meta`` so the clamp is auditable.
        """
        if self.created_utc is not None and self.created_utc > self.collected_at:
            self.meta["created_utc_original"] = self.created_utc.isoformat()
            self.meta["created_utc_clamped"] = True
            self.created_utc = self.collected_at
        return self

    @model_validator(mode="after")
    def _stage_is_resolvable(self) -> RawRecord:
        """Fail at collection if the record's purchase stage cannot be determined.

        For AJIO this means ``meta.content_type`` must say whether the text is a
        question or a review; discovering it missing during quantification would
        mean re-collecting.
        """
        purchase_stage(self.source, self.meta)
        return self

    @property
    def purchase_stage(self) -> PurchaseStage:
        return purchase_stage(self.source, self.meta)


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------

ExclusionReason = Literal["too_short", "contains_emoji", "hindi_language"]


class Document(_Strict):
    """A normalized corpus row. Mirrors the ``documents`` table in §6.

    Excluded and duplicate documents are kept rather than deleted, with the
    reason recorded, so the collection funnel stays auditable end to end.
    """

    doc_id: str
    source: str
    source_native_id: str
    url: str | None = None
    author_hash: str | None = None
    created_utc: datetime | None = None
    text: str
    lang: str | None = None
    char_len: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    text_fingerprint: str | None = None
    is_duplicate_of: str | None = None
    word_count: int | None = None
    exclusion_reason: ExclusionReason | None = None
    relevance_score: float | None = None
    is_relevant: bool | None = None
    ingested_at: datetime | None = None

    @field_validator("source")
    @classmethod
    def _known_source(cls, value: str) -> str:
        if value not in KNOWN_SOURCES:
            raise ValueError(f"unknown source {value!r}; known: {sorted(KNOWN_SOURCES)}")
        return value

    @field_validator("created_utc", "ingested_at")
    @classmethod
    def _as_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _not_its_own_duplicate(self) -> Document:
        if self.is_duplicate_of == self.doc_id:
            raise ValueError("is_duplicate_of points at the document itself")
        return self

    @property
    def purchase_stage(self) -> PurchaseStage:
        return purchase_stage(self.source, self.meta)


# --------------------------------------------------------------------------
# Tagging
# --------------------------------------------------------------------------


class EvidenceSpan(_Strict):
    """A tag and the words that justify it.

    ``quote`` must be copied verbatim from the source document. That is not
    checkable here — it needs the document text — so Stage 3 verifies it and
    retries the offending document once (`architecture.md` §7.2).
    """

    tag: EvidenceTag
    quote: str


class DocumentTags(_Strict):
    """The coding of one document, as persisted in ``doc_tags.tags_json``.

    Every field is required and multi-label dimensions use ``[]`` rather than
    being omitted, because Groq's strict decoding forbids optional properties.
    """

    is_relevant: bool
    wishlist_motivation: list[WishlistMotivation]
    blocker_type: list[BlockerType]
    uncertainty_type: list[UncertaintyType]
    info_sought_elsewhere: list[InfoSoughtElsewhere]
    segment_cue: list[SegmentCue]
    intent_class: IntentClass
    outcome_mentioned: OutcomeMentioned
    # Every numeric field is an enum of its legal values, for two separate reasons.
    #
    # Strict mode rejects the `minimum`/`maximum` keywords that pydantic emits for
    # a constrained int, so those are unavailable. More importantly, constrained
    # decoding does *not* guarantee a valid number for an unbounded `number`
    # field: an earlier run of this schema returned `"confidence": 0. nine` —
    # the decoder pulled the word "nine" out of the document text ("Delivery took
    # nine days") and Groq rejected the whole batch with json_validate_failed.
    # An enum removes the free-generation path entirely (`edge-case.md` §4.2.10).
    #
    # Discretizing confidence costs nothing real: a self-reported 0.93 from an LLM
    # is not calibrated finely enough for the second digit to carry meaning, and
    # quantification only ever thresholds on it.
    #
    # It is an integer percent rather than a 0.0-1.0 enum because Groq infers enum
    # types from the values and rejects a mix of integral and fractional ones:
    # [0.0, 0.1, ... 1.0] fails with "cannot include both 'integer' and 'number'".
    # The `confidence` property below restores the 0-1 reading for callers.
    severity: Literal[1, 2, 3, 4, 5]
    actionability_non_monetary: Literal[0, 1]
    confidence_pct: Literal[0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    evidence: list[EvidenceSpan]

    @property
    def confidence(self) -> float:
        """Confidence on the 0-1 scale used by the scoring formula in §8.3."""
        return self.confidence_pct / 100

    @model_validator(mode="after")
    def _asserted_tags_have_evidence(self) -> DocumentTags:
        """No tag without a quote. This is the line between coding and guessing.

        A tagger that can assert ``fit_size_uncertainty`` without pointing at the
        words that show it can produce a confident, unfalsifiable report — which
        is the specific failure this whole pipeline exists to avoid.
        """
        quoted = {span.tag.value for span in self.evidence}
        missing: list[str] = []
        for dimension, _ in MULTI_LABEL_DIMENSIONS:
            for value in getattr(self, dimension):
                if value.value not in quoted:
                    missing.append(f"{dimension}={value.value}")
        if missing:
            raise ValueError("asserted without evidence: " + ", ".join(sorted(missing)))
        return self

    @model_validator(mode="after")
    def _quotes_are_not_blank(self) -> DocumentTags:
        for span in self.evidence:
            if not span.quote.strip():
                raise ValueError(f"evidence for {span.tag.value!r} has a blank quote")
        return self


class _DocIdFirst(BaseModel):
    """Carrier for ``doc_id`` so it precedes the tags in the generated schema.

    Constrained decoding emits properties in schema order, so putting the id
    first makes the model echo which document it is coding before it codes it.
    ``tests/test_strict_schema.py`` pins the order.
    """

    doc_id: str


class TaggedDocument(DocumentTags, _DocIdFirst):
    """One element of a batched tagging response."""


class TaggingResponse(_Strict):
    """The batch envelope sent to and returned by Groq.

    Results are an array rather than an object keyed by ``doc_id``: strict mode
    requires ``additionalProperties: false``, which forbids arbitrary property
    names, so ``{"<doc_id>": {...}}`` is not expressible (`edge-case.md` §4.2.6).
    """

    documents: list[TaggedDocument]


# --------------------------------------------------------------------------
# Strict-mode schema generation
# --------------------------------------------------------------------------

#: JSON Schema keywords Groq's strict decoder does not accept. Pydantic emits
#: several of them from ordinary field constraints, so the models above are
#: written to avoid them and this list is the tripwire.
UNSUPPORTED_STRICT_KEYWORDS: frozenset[str] = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minItems",
        "maxItems",
        "uniqueItems",
        "contains",
        "minContains",
        "maxContains",
        "minProperties",
        "maxProperties",
        "patternProperties",
        "propertyNames",
        "unevaluatedItems",
        "unevaluatedProperties",
        "default",
    }
)


def _mixed_numeric_enum_violations(values: Any, path: str) -> list[str]:
    """Groq infers an enum's type from its members and refuses a mixed numeric set.

    ``[0.0, 0.1, ... 1.0]`` is rejected as "cannot include both 'integer' and
    'number'", because the whole-number members read as integers. Pydantic emits a
    single ``"type": "number"`` and so gives no warning of this locally, which is
    why the rule is checked here rather than trusted to the model layer.
    """
    if not isinstance(values, list):
        return []
    integral = fractional = False
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if float(value).is_integer():
            integral = True
        else:
            fractional = True
    if integral and fractional:
        return [f"{path}: enum mixes whole and fractional numbers, which Groq rejects"]
    return []


def strict_schema_violations(schema: dict[str, Any]) -> list[str]:
    """Every reason ``schema`` would be rejected by Groq's strict decoder.

    Returning a list rather than raising lets the test report all problems at
    once. Called at build time so a schema mistake surfaces as a failing test
    rather than a 400 in the middle of a multi-day tagging run.
    """
    violations: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
            return
        if not isinstance(node, dict):
            return

        for keyword in sorted(set(node) & UNSUPPORTED_STRICT_KEYWORDS):
            violations.append(f"{path}: unsupported keyword {keyword!r}")

        if "enum" in node:
            violations.extend(_mixed_numeric_enum_violations(node["enum"], path))

        if node.get("type") == "object" or "properties" in node:
            properties = node.get("properties", {})
            if node.get("additionalProperties") is not False:
                violations.append(f"{path}: additionalProperties must be false")
            required = set(node.get("required", []))
            for name in sorted(set(properties) - required):
                violations.append(f"{path}: property {name!r} is not in required")
            for name in sorted(set(required) - set(properties)):
                violations.append(f"{path}: required names {name!r}, which is not a property")

        for key, value in node.items():
            if key in {"properties", "$defs"} and isinstance(value, dict):
                for name, sub in value.items():
                    walk(sub, f"{path}.{key}.{name}")
            elif key in {"items", "anyOf", "allOf", "oneOf", "prefixItems"}:
                walk(value, f"{path}.{key}")

    walk(schema, "$")
    return violations


def tagging_response_schema() -> dict[str, Any]:
    """The exact JSON schema to send as ``response_format.json_schema.schema``.

    Raises if it is not strict-compatible, so the failure lands at startup rather
    than after the first batch has already spent tokens.
    """
    schema = TaggingResponse.model_json_schema()
    violations = strict_schema_violations(schema)
    if violations:
        raise ValueError(
            "TaggingResponse is not strict-mode compatible:\n  " + "\n  ".join(violations)
        )
    return schema
