"""Two-tier relevance triage (plan §3.2.5-6, `architecture.md` §3).

The North Star metric is about *pre-purchase* deliberation, so the corpus must be
narrowed to text that actually describes saving, comparing, postponing, or
abandoning a fashion purchase — not delivery, refund, or app-bug complaints that
dominate review sites. Two tiers, cheapest first:

**Tier 1 — keyword/regex (free).** A document with zero hits against the relevance
vocabulary is dropped here, before any token is spent. The vocabulary is loose on
purpose; Phase 3's exit audit gates false rejections at < 10%, so over-inclusion
is the safe error and Tier 2 does the precise cut.

**Tier 2 — cheap LLM (optional, gpt-oss-20b).** Survivors get a single yes/no
classification from the triage model, batched 20 per call against its own 200k TPD
bucket (~56 tokens/doc measured), stating explicitly that delivery/refund/app-bug
complaints are NOT relevant. This is what protects the expensive 120b tagging
budget in Phase 4.

Tier 2 needs a live ``GROQ_API_KEY``. When it is absent the stage runs Tier 1
only and records ``tier2: skipped`` in the funnel, so the pipeline is fully
runnable offline and the report can state which triage actually ran. Tier 1 alone
is a valid, if looser, corpus — the design degrades rather than blocks.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from src.common.encoding import read_text_tolerant
from src.common.schemas import Document

# --------------------------------------------------------------------------
# Tier 1 — keyword / regex
# --------------------------------------------------------------------------


def load_keywords(path: str | Path) -> list[str]:
    """Read the vocabulary file, ignoring comments and blank lines.

    Read tolerantly because this file is hand-edited. A BOM here is the quietest
    version of the encoding bug: it decodes without error, glues ``\\ufeff`` onto
    the first term — which ``str.strip()`` does not remove, since U+FEFF is not
    whitespace in Python — and that term then silently never matches. The only
    symptom would be a slightly smaller relevant set with nothing in the log.
    """
    lines = read_text_tolerant(path).splitlines()
    terms = []
    for line in lines:
        term = line.strip()
        if term and not term.startswith("#"):
            terms.append(term.casefold())
    if not terms:
        raise ValueError(f"relevance vocabulary at {path} is empty")
    return terms


def compile_keyword_matcher(terms: Sequence[str]) -> re.Pattern[str]:
    """One alternation regex with word boundaries, longest terms first.

    Longest-first ordering means a multi-word phrase like ``size chart`` is tried
    before the bare ``size``, so a hit is attributed to the most specific term.
    """
    ordered = sorted(set(terms), key=len, reverse=True)
    alternation = "|".join(re.escape(t) for t in ordered)
    return re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)


def keyword_hits(text: str, matcher: re.Pattern[str]) -> int:
    return len(matcher.findall(text))


# A deliberately small, closed list: function words plus the handful of verbs that
# carry no topical content on their own. Not imported from NLTK, because a corpus
# vocabulary that changes when a dependency updates would silently move the
# corpus boundary between runs, and nothing in the funnel would show it.
_STOPWORDS = frozenset(
    """
    a about after all also am an and any are as at be been being but by can cant
    could did do does doing done dont for from get gets got had has have having
    he her hers him his how i if in into is it its ive just like me more most much
    my no nor not now of off on once only or other our out over own re same she
    should so some such than that thats the their them then there these they this
    those through to too up us very was we well were what when where which while
    who why will with would you your yours
    """.split()
)


def content_word_count(text: str) -> int:
    """Words left after stopword removal (edge-case 2.8)."""
    words = re.findall(r"[^\W\d_]+", text.casefold())
    return sum(1 for w in words if w not in _STOPWORDS)


def score_tier1(
    documents: Sequence[Document],
    *,
    keywords_path: str | Path,
    min_content_words: int = 0,
) -> dict[str, int]:
    """Set ``relevance_score`` from keyword-hit density; drop zero-hit survivors.

    Only documents that survived exclusions and dedup are scored. A zero-hit
    document is marked ``is_relevant = False``; the row is kept so the funnel can
    count it. Non-zero-hit documents are left ``is_relevant = None`` for Tier 2 (or
    promoted to True if Tier 2 is skipped).

    ``min_content_words`` **splits the zero-hit drop into two reasons and decides
    nothing.** Edge-case 2.8 asked for it as a gate, and implementing it as one was
    a mistake worth recording, because at a 3-word length gate it deletes the exact
    text that lowering the gate admitted: *"still in my cart"* is two content words
    and an unambiguous wishlist-abandonment signal, and *"does this run small?"* is
    two. Its own example — "this is the one that I was looking at" — has zero
    keyword hits and is already dropped, so as a gate the rule adds no removals
    that the keyword test does not already make, and subtracts ones it should not.
    What it does add is attribution: "about nothing" and "about something else" are
    different findings for the rejected-pool audit, and only one of them is evidence
    that the vocabulary is too narrow.
    """
    matcher = compile_keyword_matcher(load_keywords(keywords_path))
    passed = 0
    dropped = 0
    dropped_low_content = 0
    for doc in documents:
        if doc.exclusion_reason is not None or doc.is_duplicate_of is not None:
            continue
        hits = keyword_hits(doc.text, matcher)
        wc = doc.word_count or len(doc.text.split()) or 1
        doc.relevance_score = round(hits / wc, 4)
        if hits:
            passed += 1
            continue
        doc.is_relevant = False
        if min_content_words and content_word_count(doc.text) < min_content_words:
            dropped_low_content += 1
        else:
            dropped += 1
    return {
        "tier1_passed": passed,
        "tier1_dropped_zero_hits": dropped,
        "tier1_dropped_low_content": dropped_low_content,
    }


# --------------------------------------------------------------------------
# Tier 2 — cheap LLM triage (optional)
# --------------------------------------------------------------------------

TIER2_PROMPT = (
    "You are screening short texts about online fashion shopping in India.\n"
    "Answer strictly whether each text describes DELIBERATING OVER, SAVING, "
    "COMPARING, POSTPONING, or ABANDONING an online fashion purchase "
    "(pre-purchase intent or wishlist behaviour).\n"
    "Delivery complaints, refund/return-status complaints, and app-bug reports "
    "are NOT relevant — mark those false.\n"
    'Return only JSON: {"documents": [{"doc_id": "...", "is_relevant": true|false}]}'
)

#: Bumping this invalidates cached verdicts, exactly as ``prompt_version`` does for
#: tagging: a verdict is only reusable if it answered the same question.
TIER2_PROMPT_VERSION = "v1"

#: Measured at 56 tokens/document (plan §3.2.6). Used only to decide whether the
#: *next* batch fits inside the daily budget, so an estimate is the right tool —
#: actual usage is read back from the response and is what the budget spends.
TIER2_TOKENS_PER_DOC = 56


def cached_verdicts(conn, *, model: str, prompt_version: str = TIER2_PROMPT_VERSION) -> dict[str, bool]:
    """Every verdict already on record for this model and prompt.

    Read in one query rather than per document: the corpus is tens of thousands of
    rows and this runs on every build, including the offline ones.
    """
    rows = conn.execute(
        "SELECT doc_id, is_relevant FROM triage_cache WHERE model = ? AND prompt_version = ?",
        (model, prompt_version),
    ).fetchall()
    return {row[0]: bool(row[1]) for row in rows}


def cache_verdicts(
    conn,
    verdicts: dict[str, bool],
    *,
    model: str,
    prompt_version: str = TIER2_PROMPT_VERSION,
) -> int:
    """Persist one batch's verdicts. Called after every batch, never at the end.

    This is the checkpoint the 2026-08-24 run did not have: 98 batches came back
    200, the 99th hit the daily token cap, and because nothing had been written
    yet, ~1,960 classifications died with the process (plan §3.3).
    """
    if not verdicts:
        return 0
    now = _now_iso()
    conn.executemany(
        """
        INSERT INTO triage_cache (doc_id, model, prompt_version, is_relevant, decided_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        [(doc_id, model, prompt_version, int(v), now) for doc_id, v in verdicts.items()],
    )
    conn.commit()
    return len(verdicts)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _resolve_groq_key(settings) -> str | None:
    """Pull the Groq key as plain text, tolerating SecretStr and placeholders."""
    creds = getattr(settings, "credentials", None)
    secret = getattr(creds, "groq_api_key", None)
    if secret is None:
        return None
    value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
    value = value.strip()
    # A dummy/placeholder key (used for offline runs) is treated as absent.
    if not value or value.lower() in {"changeme", "dummy", "placeholder"} or value.startswith("test"):
        return None
    return value


def run_tier2(
    documents: Sequence[Document],
    *,
    settings,
    enable: bool = True,
    batch_size: int = 20,
    conn=None,
) -> dict[str, int]:
    """Classify Tier-1 survivors with the triage model. No-op without credentials.

    Returns a summary dict. When the Groq key is missing (or ``enable`` is False)
    this promotes every Tier-1 survivor (non-zero hits, ``is_relevant is None``)
    to relevant and reports ``tier2: skipped`` so the funnel is honest about which
    triage actually ran.

    **A stopped run keeps its work.** Pass ``conn`` and every batch's verdicts are
    written to ``triage_cache`` as they arrive, and read back on the next run. The
    stage stops on its own before breaching the daily token budget rather than
    absorbing a 429, and a rate limit that arrives anyway ends the stage instead of
    the process. Three days of ~3,500 documents each is the designed shape of this
    run; without the cache each day would re-classify the previous day's work and
    the stage could never finish (plan §3.3).

    **Whatever is left unclassified stays on its Tier-1 verdict.** Leaving it
    ``None`` would write untriaged rows the tagger silently skips, so the corpus
    size would depend on where the quota happened to run out, with nothing in the
    funnel to show it. Promoting it to Tier-1's answer keeps that visible instead:
    the funnel reports how many documents tier 2 actually judged.
    """
    survivors = [
        d for d in documents
        if d.exclusion_reason is None
        and d.is_duplicate_of is None
        and d.is_relevant is None
    ]

    api_key = _resolve_groq_key(settings) if enable else None
    if not api_key:
        for doc in survivors:
            doc.is_relevant = True  # keep the looser Tier-1 corpus
        return {"tier2_status": "skipped", "tier2_promoted": len(survivors)}

    # --- live path: batched strict-schema yes/no on gpt-oss-20b ---
    import json

    from groq import Groq, RateLimitError

    client = Groq(api_key=api_key)
    model = settings.run.model.triage_name
    batch_size = settings.run.model.triage_docs_per_request or batch_size
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["documents"],
        "properties": {
            "documents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["doc_id", "is_relevant"],
                    "properties": {
                        "doc_id": {"type": "string"},
                        "is_relevant": {"type": "boolean"},
                    },
                },
            }
        },
    }

    # Yesterday's verdicts, so a multi-day run resumes instead of restarting.
    known = cached_verdicts(conn, model=model) if conn is not None else {}
    pending = [d for d in survivors if d.doc_id not in known]
    from_cache = 0
    for doc in survivors:
        if doc.doc_id in known:
            doc.is_relevant = known[doc.doc_id]
            from_cache += 1

    budget = _tpd_budget(settings)
    tokens_used = 0
    classified = 0
    stop_reason = None

    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]

        # Stop *before* the breach rather than after it. The alternative is to let
        # the server answer 429, which costs the request and tells us the same thing.
        if budget and tokens_used + len(batch) * TIER2_TOKENS_PER_DOC > budget:
            stop_reason = "tpd_budget"
            break

        payload = "\n".join(f"[{d.doc_id}] {d.text}" for d in batch)
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0,
                reasoning_effort="low",
                messages=[
                    {"role": "system", "content": TIER2_PROMPT},
                    {"role": "user", "content": payload},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "triage", "strict": True, "schema": schema},
                },
            )
        except RateLimitError:
            # The daily cap, reached despite the local budget — a second run today,
            # or a budget set higher than the account's true ceiling. Everything
            # classified so far is already in the cache, so this ends the stage,
            # not the build.
            stop_reason = "rate_limited"
            break

        verdicts = json.loads(resp.choices[0].message.content)["documents"]
        by_id = {v["doc_id"]: v["is_relevant"] for v in verdicts}
        decided: dict[str, bool] = {}
        for doc in batch:
            # A document the model omitted is kept, not dropped: tier 2 exists to
            # remove documents it has judged, and a missing answer is not a judgement.
            verdict = bool(by_id.get(doc.doc_id, True))
            doc.is_relevant = verdict
            decided[doc.doc_id] = verdict
        classified += len(decided)
        tokens_used += _usage_tokens(resp) or len(batch) * TIER2_TOKENS_PER_DOC

        if conn is not None:
            cache_verdicts(conn, decided, model=model)

    unclassified = [d for d in survivors if d.is_relevant is None]
    for doc in unclassified:
        doc.is_relevant = True  # keep Tier-1's answer; see the docstring

    judged = [d for d in survivors if d not in unclassified]
    relevant = sum(1 for d in judged if d.is_relevant)
    return {
        "tier2_status": "partial" if stop_reason else "ran",
        "tier2_relevant": relevant,
        "tier2_irrelevant": len(judged) - relevant,
        "tier2_classified": classified,
        "tier2_from_cache": from_cache,
        "tier2_unclassified": len(unclassified),
        "tier2_tokens": tokens_used,
        "tier2_stop_reason": stop_reason or "",
    }


def _tpd_budget(settings) -> int:
    """The triage model's daily token ceiling, or 0 when it is not configured."""
    limits = getattr(getattr(settings.run, "rate_limits", None), "triage", None)
    return int(getattr(limits, "tpd", 0) or 0)


def _usage_tokens(resp) -> int:
    usage = getattr(resp, "usage", None)
    return int(getattr(usage, "total_tokens", 0) or 0)
