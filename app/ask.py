"""One grounded Groq call over already-computed explorer data.

Not a re-run: the model never sees raw untagged text, never writes tags, and
must refuse to invent prevalence. Explorer tabs work with no API key; this
module is used only by the Ask tab.
"""

from __future__ import annotations

import json
import os
from typing import Any

from src.common.config import ENV_PATH, load_run_config

ASK_MODEL_FALLBACK = "openai/gpt-oss-20b"
MAX_QUESTION_CHARS = 500
MAX_COMPLETION_TOKENS = 1200

SYSTEM_PROMPT = """You are a grounded analyst for the AJIO Wishlist-to-Purchase Discovery Engine.

You answer a reviewer's question using ONLY the JSON snapshot that follows.
The snapshot is frozen Stage 4/5 output (opportunity scores, evidence quotes,
segment lift, AJIO on-site aggregates). You are not searching the web, not
re-tagging documents, and not re-scoring themes.

Rules:
- Use the numbers as given. Do not invent prevalence, scores, document counts, or quotes.
- Cite theme `label` values and `doc_id` values that appear in the snapshot.
- If the snapshot does not contain the answer, say exactly: not in the data.
- Distinguish full-corpus scores from genuine-intent scores. Genuine intent is the subset of tagged documents labelled genuine_intent (count is in the snapshot).
- AJIO aggregate figures are post-purchase and self-selected. They corroborate fit/quality themes; they are not corpus prevalence and must not be mixed into tagged-document percentages.
- Prefer the highest-ranked themes that actually speak to the question.
- Keep the answer tight: a short verdict, then evidence with labels and doc_ids.
"""


def groq_api_key() -> str | None:
    """GROQ_API_KEY from the process environment or ``.env``. None if absent."""
    env = os.environ.get("GROQ_API_KEY", "").strip()
    if env:
        return env
    if not ENV_PATH.is_file():
        return None
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip().upper() == "GROQ_API_KEY":
            token = value.strip().strip('"').strip("'")
            return token or None
    return None


def ask_model_name() -> str:
    """Cheap triage model from config.yaml; no credentials required to read it."""
    try:
        run, _raw = load_run_config()
        return run.model.triage_name or ASK_MODEL_FALLBACK
    except Exception:
        return ASK_MODEL_FALLBACK


def build_snapshot(
    *,
    scores: list[dict],
    quotes: list[dict],
    segments: list[dict],
    ajio: dict[str, Any],
    tagged: int,
    genuine_intent: int,
) -> dict[str, Any]:
    """Compact JSON the model is allowed to see. No supporting_doc_ids."""
    ajio_public = {
        key: ajio.get(key)
        for key in (
            "provenance",
            "products",
            "products_with_fit",
            "products_with_quality",
            "mean_misfit_pct",
            "mean_bad_quality_pct",
            "mean_average_rating",
            "ratings_reported",
            "ratings_derived",
            "top_fit_is_loose",
            "top_fit_is_tight",
        )
    }
    return {
        "disclaimer": (
            "Frozen pipeline outputs. Not a live search. "
            "Prevalence is over the tagged set, not the full relevant corpus."
        ),
        "tagged_documents": tagged,
        "genuine_intent_documents": genuine_intent,
        "opportunity_areas": scores,
        "evidence_quotes": quotes,
        "segment_matrix": segments,
        "ajio_aggregates": ajio_public,
    }


def snapshot_contains_forbidden_ids(snapshot: dict[str, Any]) -> bool:
    """Guard: supporting document-id dumps must never reach the model."""
    blob = json.dumps(snapshot)
    return "supporting_doc_ids" in blob


def answer_question(question: str, snapshot: dict[str, Any]) -> str:
    """One Groq chat completion. Raises if the key is missing or the call fails."""
    key = groq_api_key()
    if not key:
        raise RuntimeError(
            "Ask is disabled: GROQ_API_KEY is not set. Explorer tabs work without it."
        )
    cleaned = (question or "").strip()
    if not cleaned:
        raise ValueError("Type a question first.")
    if len(cleaned) > MAX_QUESTION_CHARS:
        raise ValueError(f"Question is longer than {MAX_QUESTION_CHARS} characters.")
    if snapshot_contains_forbidden_ids(snapshot):
        raise RuntimeError("Refusing to send supporting_doc_ids to the model.")

    from groq import Groq

    model = ask_model_name()
    client = Groq(api_key=key)
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        reasoning_effort="low",
        max_completion_tokens=MAX_COMPLETION_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Question:\n{cleaned}\n\n"
                    f"Snapshot:\n{json.dumps(snapshot, ensure_ascii=True, default=str)}"
                ),
            },
        ],
    )
    content = getattr(response.choices[0].message, "content", None) or ""
    text = content.strip()
    if not text:
        raise RuntimeError("The model returned an empty answer.")
    return text
