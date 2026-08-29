"""Groq tagging client: strict-schema calls, rate governor, repair ladder (plan §4.2-6).

Three hard-won behaviours from the Phase 1 live probe are baked in here:

* **Strict schema is validated after generation, not during**, so a batch can be
  rejected with HTTP 400 `json_validate_failed`. That is retryable; a 400 that
  carries `param`/`schema_path` means the *schema itself* is non-compliant and the
  run must abort (plan §4.5). The two are branched on the error body, not the status.
* **At `temperature=0` an identical request reproduces the identical violation**,
  so a plain retry loops forever. The repair ladder changes something every step —
  feed back the validator error, then halve the batch, then one document per call —
  and is **capped**, because a rejected batch already costs the tokens of all its
  members and an uncapped ladder drains a daily budget (plan §4.6).
* **The token ceiling (TPD), not the request count, binds.** The governor tracks
  RPM/TPM/RPD/TPD from `x-ratelimit-*` headers and sleeps preemptively rather than
  absorbing 429s.

* **A 413 or oversized 400 is size, not schema.** The repair ladder used to append
  the error to the prompt and retry, which made the next request *larger* and
  then spun on 429s. Oversized batches are now split; a lone oversized document
  is truncated (tagging a prefix, never dropped) and retried without enlarging
  the prompt. Packing is size-aware up front so the 413 path is the backstop.

Running this needs a live ``GROQ_API_KEY``. It is import-safe without one; the key
is only touched when a call is actually made, so ``--dry-run`` and the unit tests
never require credentials.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from src.common.logging import get_logger
from src.common.schemas import TaggedDocument, tagging_response_schema

log = get_logger("tag.client")


class SchemaNonCompliantError(RuntimeError):
    """The schema was rejected as invalid — abort, do not retry (plan §4.5)."""


class TaggingFailedError(RuntimeError):
    """A document could not be tagged within the repair ladder's cap."""


#: Characters per approximate token. Close enough to split batches without a
#: tokenizer, and the 413 handler is the backstop if this undercounts.
CHARS_PER_TOKEN = 4
#: How many times a single oversized document may be halved after a 413
#: before we skip it rather than loop. Eight halvings turn a 10k-token
#: outlier into a prefix small enough to send; more than that is a hang.
MAX_SIZE_RETRIES = 8


def approx_tokens(text: str) -> int:
    """Cheap token estimate: four characters per token, rounding up."""
    if not text:
        return 0
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Keep a prefix of ``text`` that estimates at most ``max_tokens``.

    Cuts on a space when one exists so a tagging quote is not sliced mid-word.
    Never returns an empty string for non-empty input: a one-token prefix is
    still a document the tagger can code, which is the 1.2.4 contract.
    """
    if max_tokens <= 0:
        return text[:1] if text else ""
    if approx_tokens(text) <= max_tokens:
        return text
    budget = max(1, max_tokens * CHARS_PER_TOKEN)
    cut = text[:budget]
    if " " in cut[:-1]:
        cut = cut.rsplit(" ", 1)[0]
    return cut or text[:1]


def _line_tokens(doc: dict) -> int:
    return approx_tokens(f"[{doc['doc_id']}] {doc['text']}\n")


def fit_document(doc: dict, *, max_tokens: int) -> tuple[dict, bool]:
    """Copy of ``doc`` whose payload line fits in ``max_tokens``. Original unmoved."""
    fitted = {"doc_id": doc["doc_id"], "text": doc["text"]}
    if _line_tokens(fitted) <= max_tokens:
        return fitted, False
    wrapper = approx_tokens(f"[{doc['doc_id']}] \n")
    fitted["text"] = truncate_to_tokens(doc["text"], max(1, max_tokens - wrapper))
    return fitted, True


def overhead_tokens(prompt: str, schema: dict | None = None) -> int:
    """Fixed per-call cost: the system prompt plus the JSON schema Groq also sends."""
    schema_text = json.dumps(schema, separators=(",", ":")) if schema else ""
    return approx_tokens(prompt) + approx_tokens(schema_text)


def input_token_budget(settings) -> int:
    """Tokens available for one request's *input* (prompt + schema + documents).

    Reserved against the tagging TPM so a single call cannot 413 itself, then
    spend the minute on 429s. ``max_doc_tokens`` is the floor: a solo truncated
    document must always have somewhere to go.
    """
    tpm = settings.run.rate_limits.tagging.tpm
    completion = settings.run.model.max_completion_tokens
    per_doc = settings.run.model.max_doc_tokens
    return max(per_doc, tpm - completion)


def pack_batches(
    docs: list[dict],
    *,
    max_count: int,
    max_doc_tokens: int,
    input_budget: int,
    overhead: int,
) -> list[list[dict]]:
    """Split ``docs`` into requests that respect both the count cap and the size cap.

    ``docs_per_request`` stays the upper bound, so six short YouTube comments still
    travel together. Long census documents fill the remaining budget after the
    prompt/schema overhead; a document that does not fit even alone is truncated
    to that remaining budget rather than dropped (`edge-case.md` §1.2.4).
    """
    room = max(1, input_budget - overhead)
    batches: list[list[dict]] = []
    current: list[dict] = []
    used = 0

    for doc in docs:
        fitted, truncated = fit_document(doc, max_tokens=min(max_doc_tokens, room))
        cost = _line_tokens(fitted)
        if cost > room:
            fitted, truncated = fit_document(doc, max_tokens=room)
            cost = _line_tokens(fitted)
        if truncated:
            log.info(
                "truncated document %s from %d to %d approx tokens for the tagging call",
                doc["doc_id"],
                _line_tokens(doc),
                cost,
            )
        if current and (len(current) >= max_count or used + cost > room):
            batches.append(current)
            current = []
            used = 0
        current.append(fitted)
        used += cost
    if current:
        batches.append(current)
    return batches


def _status_code(exc: BaseException) -> int | None:
    return getattr(exc, "status_code", None)


def _is_payload_too_large(exc: BaseException) -> bool:
    """413 is always size; a 400 is size only when it is not a schema event."""
    status = _status_code(exc)
    if status == 413:
        return True
    body = _error_body(exc)
    error = body.get("error") if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    if error.get("code") == "json_validate_failed":
        return False
    if "param" in error or "schema_path" in error:
        return False
    blob = f"{exc} {json.dumps(body)}".lower()
    needles = ("too large", "payload", "too long", "context length", "maximum context")
    if status == 400 and any(n in blob for n in needles):
        return True
    return False


@dataclass
class RateLimitGovernor:
    """Preemptive local governor over Groq's four limits.

    Updated from response headers after every call and consulted before the next.
    A conservative sleep before breaching is cheaper than a 429 and its backoff.
    """

    rpm: int
    tpm: int
    rpd: int
    tpd: int
    _requests_this_minute: int = 0
    _tokens_this_minute: int = 0
    _requests_today: int = 0
    _tokens_today: int = 0
    _minute_start: float = field(default_factory=time.monotonic)

    def _roll_minute(self) -> None:
        if time.monotonic() - self._minute_start >= 60:
            self._minute_start = time.monotonic()
            self._requests_this_minute = 0
            self._tokens_this_minute = 0

    def await_capacity(self, projected_tokens: int) -> None:
        self._roll_minute()
        if self._requests_today >= self.rpd or self._tokens_today >= self.tpd:
            raise DailyLimitReached(
                f"daily cap reached: {self._requests_today}/{self.rpd} req, "
                f"{self._tokens_today}/{self.tpd} tok"
            )
        over_rpm = self._requests_this_minute + 1 > self.rpm
        over_tpm = self._tokens_this_minute + projected_tokens > self.tpm
        if over_rpm or over_tpm:
            sleep_for = max(0.0, 60 - (time.monotonic() - self._minute_start)) + 0.5
            log.info("governor sleeping %.1fs to stay under per-minute limits", sleep_for)
            time.sleep(sleep_for)
            self._roll_minute()

    def record(self, *, tokens: int, headers: dict | None = None) -> None:
        self._requests_this_minute += 1
        self._tokens_this_minute += tokens
        self._requests_today += 1
        self._tokens_today += tokens
        # Trust server headers over local counters when present.
        if headers:
            remaining_rpd = headers.get("x-ratelimit-remaining-requests")
            if remaining_rpd is not None:
                try:
                    self._requests_today = self.rpd - int(remaining_rpd)
                except (TypeError, ValueError):
                    pass


class DailyLimitReached(RuntimeError):
    """TPD or RPD exhausted — the run should checkpoint and resume tomorrow."""


class TaggingClient:
    """Thin wrapper around the Groq SDK for one strict-schema tagging call."""

    def __init__(self, *, settings, max_repair_attempts: int = 3):
        self.settings = settings
        self.model = settings.run.model.name
        self.max_repair_attempts = max_repair_attempts
        self.schema = tagging_response_schema()  # raises at startup if non-compliant
        self.governor = RateLimitGovernor(
            rpm=settings.run.rate_limits.tagging.rpm,
            tpm=settings.run.rate_limits.tagging.tpm,
            rpd=settings.run.rate_limits.tagging.rpd,
            tpd=settings.run.rate_limits.tagging.tpd,
        )
        self._client = None  # lazily created so import needs no key

    def _groq(self):
        if self._client is None:
            from groq import Groq

            key = self.settings.credentials.groq_api_key.get_secret_value()
            self._client = Groq(api_key=key)
        return self._client

    def tag_batch(
        self, prompt: str, batch: list[dict], *, _size_retries: int = 0
    ) -> tuple[list[TaggedDocument], dict]:
        """Tag a batch, climbing the repair ladder on retryable rejections.

        ``batch`` items are ``{"doc_id", "text"}``. Returns the tagged documents and
        a usage dict. Splits and recurses on repeated rejection; raises
        ``TaggingFailedError`` if a single document cannot be tagged within the cap.
        A 413/oversized-payload error splits the batch (or truncates a lone
        document) without appending the error to the prompt, which would only
        make the next request larger.
        """
        return self._attempt(prompt, batch, depth=0, last_error=None, size_retries=_size_retries)

    def _attempt(self, prompt, batch, *, depth, last_error, size_retries=0):
        payload = "\n".join(f"[{d['doc_id']}] {d['text']}" for d in batch)
        system = prompt if last_error is None else f"{prompt}\n\nPRIOR ERROR TO FIX:\n{last_error}"
        projected = (
            approx_tokens(system)
            + approx_tokens(payload)
            + overhead_tokens("", self.schema)
            + self.settings.run.model.max_completion_tokens
        )
        self.governor.await_capacity(projected)

        try:
            resp = self._groq().chat.completions.create(
                model=self.model,
                temperature=self.settings.run.model.temperature,
                seed=self.settings.run.model.seed,
                reasoning_effort=self.settings.run.model.reasoning_effort,
                max_completion_tokens=self.settings.run.model.max_completion_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": payload},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "document_tags", "strict": True, "schema": self.schema},
                },
            )
        except Exception as exc:  # noqa: BLE001 - inspect the error body to branch
            return self._on_error(exc, prompt, batch, depth, size_retries)

        usage = getattr(resp, "usage", None)
        total = getattr(usage, "total_tokens", 0) or 0
        self.governor.record(tokens=total)
        tagged = parse_tagging_response(getattr(resp.choices[0].message, "content", None))
        return tagged, {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": total,
        }

    def _on_error(self, exc, prompt, batch, depth, size_retries=0):
        body = _error_body(exc)
        code = (body.get("error", {}) or {}).get("code")
        if code and code != "json_validate_failed" and ("param" in body.get("error", {}) or "schema_path" in body.get("error", {})):
            raise SchemaNonCompliantError(f"schema rejected as invalid: {body}") from exc

        if _is_payload_too_large(exc):
            return self._on_payload_too_large(exc, prompt, batch, size_retries)

        if depth >= self.max_repair_attempts:
            raise TaggingFailedError(
                f"gave up on {len(batch)} document(s) after {depth} repair attempts: {exc}"
            ) from exc

        failed = (body.get("error", {}) or {}).get("failed_generation")
        if len(batch) > 1:  # halve the batch — change something every step
            mid = len(batch) // 2
            left, lu = self._attempt(
                prompt, batch[:mid], depth=depth + 1, last_error=str(exc), size_retries=size_retries
            )
            right, ru = self._attempt(
                prompt, batch[mid:], depth=depth + 1, last_error=str(exc), size_retries=size_retries
            )
            merged = {k: lu.get(k, 0) + ru.get(k, 0) for k in set(lu) | set(ru)}
            return left + right, merged
        # single document: retry with the validator error fed back, then give up
        return self._attempt(
            prompt, batch, depth=depth + 1, last_error=failed or str(exc), size_retries=size_retries
        )

    def _on_payload_too_large(self, exc, prompt, batch, size_retries):
        """413/oversized 400: shrink the request. Never enlarge it with last_error."""
        if len(batch) > 1:
            mid = max(1, len(batch) // 2)
            log.warning(
                "payload too large for %d documents (%s); splitting into %d and %d",
                len(batch),
                exc,
                mid,
                len(batch) - mid,
            )
            left, lu = self.tag_batch(prompt, batch[:mid], _size_retries=size_retries)
            right, ru = self.tag_batch(prompt, batch[mid:], _size_retries=size_retries)
            merged = {k: lu.get(k, 0) + ru.get(k, 0) for k in set(lu) | set(ru)}
            return left + right, merged

        doc = batch[0]
        if size_retries >= MAX_SIZE_RETRIES or approx_tokens(doc["text"]) <= 1:
            raise TaggingFailedError(
                f"document {doc['doc_id']} still exceeds the input budget after "
                f"truncation; skipping rather than looping"
            ) from exc
        halved = truncate_to_tokens(doc["text"], max(1, approx_tokens(doc["text"]) // 2))
        if halved == doc["text"]:
            raise TaggingFailedError(
                f"document {doc['doc_id']} could not be truncated further; skipping"
            ) from exc
        log.warning(
            "payload too large for document %s; truncating %d -> %d approx tokens and retrying",
            doc["doc_id"],
            approx_tokens(doc["text"]),
            approx_tokens(halved),
        )
        return self.tag_batch(
            prompt,
            [{"doc_id": doc["doc_id"], "text": halved}],
            _size_retries=size_retries + 1,
        )


def _error_body(exc: Exception) -> dict:
    """Best-effort extraction of a Groq API error body as a dict."""
    for attr in ("body", "response"):
        obj = getattr(exc, attr, None)
        if isinstance(obj, dict):
            return obj
        text = getattr(obj, "text", None)
        if text:
            try:
                return json.loads(text)
            except (ValueError, TypeError):
                pass
    try:
        return json.loads(str(exc))
    except (ValueError, TypeError):
        return {}


_UNEVIDENCED = re.compile(r"asserted without evidence:\s*(.+)")
_TAG_PAIR = re.compile(r"([a-z_]+)=([a-z0-9_]+)")


def _tag_value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _unevidenced_tags(exc: BaseException) -> list[tuple[str, str]]:
    """Parse ``dimension=value`` pairs out of the evidence validator's message.

    Prefer pydantic's ``errors()[].msg`` so the dumped ``input_value`` (which
    can contain the document text) is not scanned for coincidental ``a=b``
    pairs. Fall back to ``str(exc)`` only when that list is empty, and stop
    at pydantic's ``[type=`` trailer either way.
    """
    messages: list[str] = []
    errors = getattr(exc, "errors", None)
    if callable(errors):
        messages.extend(err.get("msg", "") for err in errors())
    if not messages:
        messages.append(str(exc))
    found: list[tuple[str, str]] = []
    for msg in messages:
        match = _UNEVIDENCED.search(msg)
        if match is None:
            continue
        tail = match.group(1).split("[type=", 1)[0]
        found.extend(_TAG_PAIR.findall(tail))
    return found


def parse_tagged_document(item: object) -> TaggedDocument | None:
    """Validate one model-coded document, dropping unevidenced tags rather than aborting.

    The evidence rule on ``DocumentTags`` stays strict: this is the run-level
    handler for when Groq asserts a tag it cannot quote. The offending values are
    removed, the rest of the coding is kept, and the drop is logged. A payload
    that still cannot be parsed is skipped (returns ``None``); the run continues.
    """
    if not isinstance(item, dict):
        log.warning("skipping unparseable tagging item (not an object): %r", item)
        return None
    doc_id = item.get("doc_id", "?")
    try:
        return TaggedDocument.model_validate(item)
    except Exception as exc:  # noqa: BLE001 — salvage only the evidence-rule case
        missing = _unevidenced_tags(exc)
        if not missing:
            log.warning("skipping document %s: unparseable tags (%s)", doc_id, exc)
            return None
        payload = dict(item)
        for dimension, value in missing:
            current = payload.get(dimension)
            if isinstance(current, list):
                payload[dimension] = [v for v in current if _tag_value(v) != value]
        log.warning(
            "dropped unevidenced tag(s) on document %s: %s",
            doc_id,
            ", ".join(f"{dimension}={value}" for dimension, value in missing),
        )
        try:
            return TaggedDocument.model_validate(payload)
        except Exception as second:  # noqa: BLE001
            log.warning(
                "skipping document %s after dropping unevidenced tags: %s", doc_id, second
            )
            return None


def parse_tagging_response(content: str | None) -> list[TaggedDocument]:
    """Parse a batch envelope into tagged documents, skipping what cannot be salvaged."""
    if not content:
        log.warning("skipping tagging response: empty content")
        return []
    try:
        payload = json.loads(content)
        items = payload["documents"]
    except (TypeError, ValueError, KeyError) as exc:
        log.warning("skipping tagging response: unparseable envelope (%s)", exc)
        return []
    if not isinstance(items, list):
        log.warning("skipping tagging response: 'documents' is not a list")
        return []
    tagged: list[TaggedDocument] = []
    for item in items:
        parsed = parse_tagged_document(item)
        if parsed is not None:
            tagged.append(parsed)
    return tagged
