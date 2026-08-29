"""Measure real reasoning-token overhead for the pinned Groq models.

Phase 4 requires the ``--dry-run`` estimator to be based on observed usage rather
than a guessed constant, and ``max_completion_tokens`` has to be large enough to
cover reasoning *plus* visible output or the response comes back empty
(edge-case 4.1.4). This script produces both numbers from realistic payloads.

    .venv\\Scripts\\python.exe scripts\\measure_token_overhead.py

It costs a few tens of thousands of tokens. Re-run it whenever the model pin, the
prompt, the batch size, **or the corpus length distribution** changes.

That last trigger is why this script was rewritten. It used to tag six hand-written
documents of 15-17 words each and divide. Two things were wrong with that once the
Phase 3 length gate moved from 8 words to 3:

* **The sample was not the corpus.** The real corpus has a median of 8 words and a
  mean of 15.4 — the mean is dragged up by a thin tail, and 30% of documents are
  3-5 words. Six documents written at the mean measure the tail, not the middle.
* **Per-document cost is not a constant.** A batch's system prompt, schema and
  reasoning are close to fixed per *call*, so as documents get shorter the fixed
  cost is amortized over less text and cost per document *rises* even as cost per
  word falls. One number measured at one length cannot capture that, and the
  direction of the error is not obvious in advance — which is exactly the kind of
  thing that should not be guessed at a spending gate.

So: sample **real documents from the real corpus**, stratified by length, one batch
per stratum at the production batch size, then weight the per-stratum costs by the
corpus's own measured length distribution. The batch size matters and is taken from
``model.docs_per_request`` rather than chosen here — measuring 9 documents per call
and then billing 6 per call would misstate the fixed overhead by 50%.

**The prompt and schema are the production ones, imported rather than copied.** An earlier version of this script carried its own hand-written schema covering a
single taxonomy dimension, and the resulting figure was then multiplied up by hand
to reach ``TOKENS_PER_DOC = 540`` — a measurement of something the pipeline does not
send, scaled by a projection nobody re-derived afterwards. That constant is now 645,
measured here against production payloads, and this script prints a warning if the
two drift by more than 10%.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.common.config import get_settings  # noqa: E402
from src.common.schemas import tagging_response_schema  # noqa: E402
from src.store.exclusions import survives_hard_exclusions  # noqa: E402
from src.store.normalize import word_count  # noqa: E402
from src.tag.run_tagging import PROMPT_VERSION  # noqa: E402

# Upper bound of each stratum, in words; the last is open-ended. Chosen to split
# the observed distribution into roughly comparable populations rather than round
# numbers, so no stratum's cost is inferred from a handful of documents.
STRATA: list[tuple[str, int | None]] = [
    ("3-5 words", 5),
    ("6-10 words", 10),
    ("11-20 words", 20),
    ("21+ words", None),
]

SAMPLE_SEED = 0

TRIAGE_DOCS = [
    (f"t{i}", text)
    for i, text in enumerate(
        [
            "Delivery was delayed by four days and customer care did not respond at all",
            "Still deciding between two kurtas in my wishlist, cannot tell which fabric is better",
            "App keeps crashing when I open the wishlist page on my phone",
            "Bought this last week and the fit was perfect, very happy with the quality",
            "I watch haul videos before buying anything from here because sizing is unpredictable",
            "Refund has not been credited even after fifteen days of pickup",
            "Saved a lot of items but never end up buying because I am unsure about fit",
            "Their size chart says medium but it fits like a small, very inconsistent",
            "Wishlist items disappear when they go out of stock which is annoying",
            "Quality for the price is decent, would order again from this brand",
            "Keep postponing the purchase because I want to see it on someone my size first",
            "Order was cancelled automatically without any explanation from the seller",
            "Asked a question about length on the product page but nobody answered it",
            "The return window is too short to decide whether to keep the dress",
            "I compare the same item on two apps before deciding which one to buy from",
            "Packaging was torn but the product inside was fine thankfully",
            "Not sure if this is suitable for a wedding function or too casual",
            "Been in my cart for a month, waiting to see if the price drops",
            "Customer service resolved my exchange request quickly this time",
            "I need to know if it runs small before I commit to buying it",
        ],
        start=1,
    )
]

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "documents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string"},
                    "is_relevant": {"type": "boolean"},
                },
                "required": ["doc_id", "is_relevant"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["documents"],
    "additionalProperties": False,
}

def tagging_system(settings) -> str:
    """The production tagging prompt, read from disk.

    Not a paraphrase of it. A shorter stand-in here would understate ``prompt_tokens``
    on every call, and prompt tokens are the largest fixed cost in a batch.
    """
    path = (
        settings.project_root / "src" / "tag" / "prompts" / f"tagging_{PROMPT_VERSION}.md"
    )
    return path.read_text(encoding="utf-8")


TRIAGE_SYSTEM = (
    "For each document answer one question: does it describe deliberating over, saving, "
    "comparing, postponing, or abandoning an online fashion purchase? Post-purchase "
    "complaints about delivery, refunds, or app bugs are NOT relevant. "
    "Return one entry per input document with its doc_id unchanged."
)


def eligible_documents(settings) -> list[tuple[str, str, int]]:
    """Every raw record that survives the hard exclusions, with its word count.

    Read from the part files rather than ``discovery.db`` on purpose: the budget
    gate has to be answerable before, or independently of, a corpus rebuild, and
    the raw files are the only artifact that is never stale.
    """
    filters = settings.run.filters
    seen: set[tuple[str, str]] = set()
    docs: list[tuple[str, str, int]] = []
    for part in sorted(Path(settings.raw_dir).rglob("part-*.jsonl")):
        with part.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (record.get("source"), record.get("source_native_id"))
                if key in seen:
                    continue
                seen.add(key)
                text = (record.get("text") or "").strip()
                if not text or not survives_hard_exclusions(text, filters):
                    continue
                docs.append((record.get("source_native_id") or "", text, word_count(text)))
    return docs


def stratify(docs: list[tuple[str, str, int]]) -> list[dict]:
    """Split the corpus into the length strata and record each one's share."""
    total = len(docs)
    buckets: list[dict] = []
    lower = 0
    for label, upper in STRATA:
        members = [
            d for d in docs if d[2] > lower and (upper is None or d[2] <= upper)
        ]
        buckets.append(
            {
                "label": label,
                "documents": members,
                "count": len(members),
                "share": len(members) / total if total else 0.0,
            }
        )
        lower = upper if upper is not None else lower
    return buckets


def representative_sample(
    buckets: list[dict], size: int, rng: random.Random
) -> list[tuple[str, str, int]]:
    """``size`` documents drawn in proportion to the corpus length distribution.

    Used for the batch-size sweep, where the question is what a *typical* batch
    costs, so the mix has to look like the corpus rather than like one stratum.
    """
    sample: list[tuple[str, str, int]] = []
    for bucket in buckets:
        take = min(round(size * bucket["share"]), len(bucket["documents"]))
        if take:
            sample.extend(rng.sample(bucket["documents"], take))
    pool = [d for b in buckets for d in b["documents"]]
    while len(sample) < size and pool:
        sample.append(rng.choice(pool))
    return sample[:size]


def render(docs: list[tuple[str, str]]) -> str:
    return "\n".join(f"[{doc_id}] {text}" for doc_id, text in docs)


def probe(client, model, system, docs, schema, schema_name, max_completion_tokens, reasoning_effort):
    from groq import BadRequestError

    try:
        raw = client.chat.completions.with_raw_response.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": render(docs)},
            ],
            temperature=0,
            max_completion_tokens=max_completion_tokens,
            reasoning_effort=reasoning_effort,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        )
    except BadRequestError as exc:
        # Strict mode turns truncation into a 400 rather than a finish_reason of
        # "length", and the two kinds of 400 must be told apart by error code:
        #   json_validate_failed -> retryable; raise the budget or shrink the batch
        #   anything else        -> the schema itself is wrong; a build error
        body = getattr(exc, "body", None) or {}
        error = body.get("error", {}) if isinstance(body, dict) else {}
        print(f"  model                : {model}")
        print(f"  max_completion_tokens: {max_completion_tokens}")
        print(f"  HTTP                 : 400 BadRequestError")
        print(f"  error code           : {error.get('code')}")
        print(f"  failed_generation    : {error.get('failed_generation')!r}")
        print(f"  retryable            : {error.get('code') == 'json_validate_failed'}")
        print()
        return None
    completion = raw.parse()
    choice = completion.choices[0]
    usage = completion.usage
    content = choice.message.content or ""

    details = getattr(usage, "completion_tokens_details", None)
    reasoning_tokens = getattr(details, "reasoning_tokens", None) if details else None

    parsed_count = None
    if content:
        try:
            parsed_count = len(json.loads(content).get("documents", []))
        except json.JSONDecodeError:
            parsed_count = -1

    n = len(docs)
    print(f"  model                : {model}")
    print(f"  reasoning_effort     : {reasoning_effort}")
    print(f"  documents sent       : {n}")
    print(f"  finish_reason        : {choice.finish_reason}")
    print(f"  content empty        : {not content}")
    print(f"  documents returned   : {parsed_count}")
    print(f"  prompt_tokens        : {usage.prompt_tokens}")
    print(f"  completion_tokens    : {usage.completion_tokens}")
    print(f"  reasoning_tokens     : {reasoning_tokens}")
    print(f"  total_tokens         : {usage.total_tokens}")
    print(f"  tokens per document  : {usage.total_tokens / n:.1f}")
    print(f"  usage fields         : {sorted(usage.model_dump().keys())}")
    if details is not None:
        print(f"  completion details   : {details.model_dump()}")
    print()
    return {
        "documents": n,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": usage.total_tokens,
        "tokens_per_document": usage.total_tokens / n,
        "returned": parsed_count,
        "finish_reason": choice.finish_reason,
    }


def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batches-per-stratum",
        type=int,
        default=1,
        help="repeat each stratum N times; >1 shows run-to-run variance in the estimate",
    )
    parser.add_argument(
        "--skip-strata",
        action="store_true",
        help=(
            "skip the per-stratum tagging probes (already measured) and go straight "
            "to --batch-sweep / triage / the 4.1.4 demo"
        ),
    )
    parser.add_argument(
        "--batch-sweep",
        type=int,
        nargs="+",
        default=None,
        help=(
            "also measure these batch sizes on a corpus-representative sample. "
            "Most of the per-document cost is the fixed prompt+schema divided by "
            "docs_per_request, so this is the sweep that prices the batch size."
        ),
    )
    args = parser.parse_args()

    settings = get_settings()
    from groq import Groq

    client = Groq(api_key=settings.credentials.groq_api_key.get_secret_value())
    model_cfg = settings.run.model
    batch_size = model_cfg.docs_per_request

    # Generated from src/tag/taxonomy.py, exactly as TaggingClient does it, so the
    # measurement covers all seven dimensions the tagger actually emits rather than
    # the one dimension the old hand-copied schema covered.
    tagging_schema = tagging_response_schema()

    corpus = eligible_documents(settings)
    if not corpus:
        print("\n  No eligible documents under data/raw. Run collection first.\n")
        return 1
    buckets = stratify(corpus)

    print("\n" + "=" * 66)
    print(" CORPUS LENGTH DISTRIBUTION  (what the estimate is weighted by)")
    print("=" * 66)
    print(f"  eligible documents: {len(corpus):,}")
    mean_words = sum(d[2] for d in corpus) / len(corpus)
    print(f"  mean words/doc    : {mean_words:.1f}")
    for bucket in buckets:
        print(f"    {bucket['label']:<14} {bucket['count']:>7,}  {bucket['share']:>6.1%}")
    print()

    rng = random.Random(SAMPLE_SEED)
    # Priced with run_tagging's own constants rather than a second copy of them,
    # so this script cannot report a cost the dry-run would not reproduce.
    from src.tag.run_tagging import PRICE_IN_PER_M, PRICE_OUT_PER_M, TOKENS_PER_DOC

    def _price(tokens: float, *, prompt_frac: float = 0.6) -> float:
        return (tokens * prompt_frac / 1_000_000 * PRICE_IN_PER_M) + (
            tokens * (1.0 - prompt_frac) / 1_000_000 * PRICE_OUT_PER_M
        )

    measured: list[dict] = []
    if args.skip_strata:
        print("  Skipping per-stratum probes (--skip-strata).")
        print()
    else:
        print("=" * 66)
        print(f" TAGGING BY STRATUM  (strict schema, batch of {batch_size} = docs_per_request)")
        print("=" * 66 + "\n")
        for bucket in buckets:
            if not bucket["documents"]:
                continue
            per_doc_runs: list[float] = []
            prompt_runs: list[float] = []
            completion_runs: list[float] = []
            for _ in range(max(1, args.batches_per_stratum)):
                sample = rng.sample(
                    bucket["documents"], min(batch_size, len(bucket["documents"]))
                )
                print(f"--- {bucket['label']} ({bucket['share']:.1%} of corpus) ---")
                result = probe(
                    client,
                    model_cfg.name,
                    tagging_system(settings),
                    [(doc_id or f"s{i}", text) for i, (doc_id, text, _) in enumerate(sample)],
                    tagging_schema,
                    "document_tags",
                    model_cfg.max_completion_tokens,
                    model_cfg.reasoning_effort,
                )
                if result:
                    n = result["documents"] or 1
                    per_doc_runs.append(result["tokens_per_document"])
                    prompt_runs.append(result["prompt_tokens"] / n)
                    completion_runs.append(result["completion_tokens"] / n)
            if per_doc_runs:
                measured.append(
                    {
                        "label": bucket["label"],
                        "share": bucket["share"],
                        "count": bucket["count"],
                        "per_doc": sum(per_doc_runs) / len(per_doc_runs),
                        "prompt_per_doc": sum(prompt_runs) / len(prompt_runs),
                        "completion_per_doc": sum(completion_runs) / len(completion_runs),
                        "runs": per_doc_runs,
                    }
                )

        if not measured:
            print("  Every stratum probe failed; no estimate can be made.\n")
            return 1

        print("=" * 66)
        print(" WEIGHTED PER-DOCUMENT COST")
        print("=" * 66)
        print(f"  {'stratum':<14} {'share':>7} {'tokens/doc':>12} {'contribution':>14}")
        weighted = 0.0
        weighted_prompt = 0.0
        covered = sum(m["share"] for m in measured)
        for m in measured:
            share = m["share"] / covered if covered else 0.0
            contribution = share * m["per_doc"]
            weighted += contribution
            weighted_prompt += share * m["prompt_per_doc"]
            spread = ""
            if len(m["runs"]) > 1:
                spread = f"  (range {min(m['runs']):.0f}-{max(m['runs']):.0f})"
            print(
                f"  {m['label']:<14} {share:>6.1%} {m['per_doc']:>12.1f} "
                f"{contribution:>14.1f}{spread}"
            )
        print(f"  {'':<14} {'':>7} {'WEIGHTED':>12} {weighted:>14.1f}")
        prompt_frac = weighted_prompt / weighted if weighted else 0.6
        print(f"  prompt share of tokens : {prompt_frac:.1%}  (the 60/40 split is a guess; this is measured)")
        print()

        total_tokens = len(corpus) * weighted
        cost = _price(total_tokens, prompt_frac=prompt_frac)
        heuristic = _price(total_tokens, prompt_frac=0.6)
        tpd = settings.run.rate_limits.tagging.tpd
        print("=" * 66)
        print(" PROJECTION OVER THE ELIGIBLE CORPUS")
        print("=" * 66)
        print(f"  documents                  {len(corpus):>12,}")
        print(f"  measured tokens/document   {weighted:>12.1f}")
        print(f"  projected tokens           {total_tokens:>12,.0f}")
        print(f"  cost @ measured split USD  {cost:>12.2f}")
        print(f"  cost @ 60/40 heuristic USD {heuristic:>12.2f}")
        if tpd:
            print(f"  free-tier days @ {tpd:,} TPD {total_tokens / tpd:>9.0f}")
        print("=" * 66)
        print(f"  run_tagging.TOKENS_PER_DOC is currently {TOKENS_PER_DOC}.")
        if abs(weighted - TOKENS_PER_DOC) / max(TOKENS_PER_DOC, 1) > 0.10:
            direction = "over" if TOKENS_PER_DOC > weighted else "under"
            print(
                f"  It {direction}states the measured cost by more than 10%. Update it to "
                f"{round(weighted)}\n"
                "  and re-run `python -m src.tag.run_tagging --dry-run`, which is the gate\n"
                "  that turns this number into a spending decision."
            )
        else:
            print("  That is within 10% of the measurement; no change needed.")
        print()

    if args.batch_sweep:
        print("=" * 66)
        print(" BATCH-SIZE SWEEP  (the fixed prompt is divided by this number)")
        print("=" * 66 + "\n")
        sweep_rows = []
        for size in args.batch_sweep:
            sample = representative_sample(buckets, size, rng)
            print(f"--- batch of {size} ---")
            result = probe(
                client,
                model_cfg.name,
                tagging_system(settings),
                [(doc_id or f"b{i}", text) for i, (doc_id, text, _) in enumerate(sample)],
                tagging_schema,
                "document_tags",
                model_cfg.max_completion_tokens,
                model_cfg.reasoning_effort,
            )
            if result:
                sweep_rows.append((size, result))
        if sweep_rows:
            print(f"  {'batch':>6} {'tok/doc':>9} {'prompt':>8} {'completion':>11} {'headroom':>9} {'corpus $':>10}")
            for size, r in sweep_rows:
                n = r["documents"] or 1
                prompt_frac = r["prompt_tokens"] / r["total_tokens"] if r["total_tokens"] else 0.6
                projected = len(corpus) * r["tokens_per_document"]
                c = _price(projected, prompt_frac=prompt_frac)
                headroom = model_cfg.max_completion_tokens - r["completion_tokens"]
                flag = "  <-- current" if size == batch_size else ""
                print(
                    f"  {size:>6} {r['tokens_per_document']:>9.1f} "
                    f"{r['prompt_tokens']/n:>8.0f} {r['completion_tokens']:>11} "
                    f"{headroom:>9} {c:>10.2f}{flag}"
                )
            print(
                "\n  headroom is max_completion_tokens minus what this batch actually used.\n"
                "  A batch that exhausts it fails as json_validate_failed (edge-case 4.1.4)\n"
                "  and the repair ladder halves it, so the saving is only real with margin.\n"
            )

    if not args.skip_strata:
        print("=" * 66)
        print(f" TRIAGE (strict schema, batch of {len(TRIAGE_DOCS)})")
        print("=" * 66 + "\n")
        probe(
            client,
            model_cfg.triage_name,
            TRIAGE_SYSTEM,
            TRIAGE_DOCS,
            TRIAGE_SCHEMA,
            "triage_result",
            model_cfg.max_completion_tokens,
            model_cfg.reasoning_effort,
        )

        print("=" * 66)
        print(" TAGGING with a deliberately tight budget (demonstrates 4.1.4)")
        print("=" * 66 + "\n")
        demo = rng.sample(corpus, min(batch_size, len(corpus)))
        probe(
            client,
            model_cfg.name,
            tagging_system(settings),
            [(doc_id or f"x{i}", text) for i, (doc_id, text, _) in enumerate(demo)],
            tagging_schema,
            "document_tags",
            64,
            model_cfg.reasoning_effort,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
