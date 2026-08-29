# Implementation Plan — AJIO Wishlist-to-Purchase Discovery Engine

Derived from `problemStatement.md` (what and why) and `architecture.md` (how). This document sequences the build into phases with concrete deliverables, exit criteria, and risks.

**LLM:** Groq, `openai/gpt-oss-120b` (tagging, strict structured outputs) with `openai/gpt-oss-20b` (relevance triage).
**Credentials required:** Groq, YouTube Data API v3. (Reddit is optional and disabled by default.)

---

## 0. Platform constraints that shape the plan

Two properties of the chosen model drive several design decisions, so they are stated up front rather than discovered mid-build.

### 0.1 Why this model pin

`openai/gpt-oss-120b` is chosen over `llama-3.3-70b-versatile` for one decisive reason and two supporting ones.

**Strict schema validation.** Groq supports `strict: true` on only `openai/gpt-oss-20b` and `openai/gpt-oss-120b`; every other model, including `llama-3.3-70b-versatile`, is limited to best-effort JSON. What it delivers, though, is narrower than the name suggests, and Phase 1 established this by measurement: Groq validates the response *after* generation and rejects violations with a 400, rather than constraining decoding token by token. Three consecutive live calls each produced a violation — an invented number literal, and an enum value borrowed from a different dimension.

So the pin still earns its place, because a schema-validated rejection is infinitely better than silently malformed tags flowing into quantification. But it removes a class of failure only when the schema is designed for it: every enumerable field is an enum, no dimension carries a sentinel another lacks, and Phase 4's repair path is treated as a primary code path rather than a safety net. See `architecture.md` §7.2.

**Supporting factors:** double the free daily token budget (200k vs 100k TPD) and roughly half the paid cost ($0.15/$0.60 vs $0.59/$0.79 per 1M).

**Availability, confirmed against the live account.** `models.list()` on our key returns `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `openai/gpt-oss-safeguard-20b`, `qwen/qwen3.6-27b`, `groq/compound`, `allam-2-7b`, and the Whisper and prompt-guard models. **No Llama 3.x model is offered**, so the original `llama-3.3-70b-versatile` pin would not have run at all, and `llama-3.1-8b-instant` is likewise unavailable for triage. Triage therefore moves to `openai/gpt-oss-20b`: identical API surface, one rate governor, one strict-schema code path, and its own per-model quota so it does not compete with tagging. `qwen/qwen3.6-27b` is the documented fallback if triage throughput becomes a problem, since it is the only available model supporting `reasoning_effort: none` and so emits no reasoning tokens at all.

**Costs of the choice, accepted knowingly:** GPT-OSS is a reasoning model whose reasoning tokens bill as output, so `reasoning_effort` is pinned to `low` and `include_reasoning` to false. Its TPM is also lower (8,000 vs 12,000), which slows per-minute throughput but not the daily ceiling that actually binds a batch job.

**Schema consequences for Phase 1.** Strict mode requires every property to be listed in `required` and every object to set `additionalProperties: false`. Two things follow:

1. `DocumentTags` must emit all fields on every response, using `[]` for multi-label dimensions that do not apply rather than omitting the key.
2. **Batched results cannot be keyed by `doc_id`.** Since arbitrary property names are forbidden, the response is a fixed-name array — `{"documents": [{"doc_id": ..., ...}]}` — with each item carrying its own id. A wrapper model around `DocumentTags` is therefore part of Phase 1, not an afterthought in Phase 4.

### 0.2 The token ceiling, not the request count, is the binding limit

| Tier | RPM | RPD | TPM | TPD |
| --- | --- | --- | --- | --- |
| Free — `openai/gpt-oss-120b` | 30 | 1,000 | 8,000 | **200,000** |
| Free — `openai/gpt-oss-20b` | 30 | 1,000 | 8,000 | 200,000 |
| Developer (paid) — `gpt-oss-120b` | 1,000 | — | 250,000+ | — |

The RPD and TPM figures are confirmed from live `x-ratelimit-*` headers on this account; TPD is not header-exposed and comes from Groq's published limits, so the governor tracks it locally. Limits are enforced per organization **but bucketed per model**, so triage on the 20b and tagging on the 120b draw from separate quotas. The first ceiling reached returns HTTP 429.

Because triage no longer has a high-RPD model available (the old plan relied on `llama-3.1-8b-instant` at 14,400 RPD), triage must batch too — `triage_docs_per_request: 20` keeps ~6,000 documents inside ~300 requests, well under the 1,000 RPD cap.

**Budget math, now measured rather than projected.** `scripts/measure_token_overhead.py` samples **real corpus documents**, stratified by length, against the **production** prompt (`tagging_v1.md`) and the schema generated from `src/tag/taxonomy.py`. The previous figure of 540 was a hand-scaled projection from a one-dimension stub schema and six 15-word stand-ins — neither of which is what the tagger sends, and neither of which matches a corpus whose median is 8 words.

Measured 2026-08-21, `reasoning_effort=low`, `docs_per_request=6`, weighted across four length strata (30.3% / 29.4% / 21.5% / 18.8% at 3–5 / 6–10 / 11–20 / 21+ words):

| | |
| --- | --- |
| Eligible documents (hard exclusions only) | 26,539 |
| Mean words / median words | 15.3 / 8 |
| **Weighted tokens per document** | **645** |
| of which prompt (fixed schema + prompt, amortized) | ~470 |
| Prompt share of tokens | 72% |

A mixed-sample batch at the same size landed at 762 tokens/document (prompt 2,977, completion 1,593 of which 489 reasoning). The 645 figure is the one `TOKENS_PER_DOC` uses, because four stratified batches beat one mixed draw; the 762 figure is the variance you should expect from any single call.

**Per-document cost is not a constant, and most of it is the prompt.** A batch-size sweep on a corpus-representative mix:

| Batch size | Tokens/doc | Prompt/doc | Completion (batch) | Headroom vs 4096 | Eligible-corpus $ |
| --- | --- | --- | --- | --- | --- |
| **6 (current)** | 762 | 496 | 1,593 | 2,503 | $6.20 |
| 12 | 436 | 296 | 1,688 | 2,408 | $3.42 |
| 20 | 308 | 169 | 2,772 | 1,324 | $2.88 |

Raising `docs_per_request` is the single largest cost lever, and it is **not pulled**: plan §4 holds the batch at 6 until the gold set says quality survives a larger one. Completions at 20 still had 1,324 tokens of headroom, so 4.1.4 is not the blocker — attention over 20 documents is.

The useful structural finding is unchanged and sharper: **the taxonomy prompt and schema are a fixed ~2,800–3,500 tokens per call.** Short documents do not make tagging cheap; they make the fixed cost *more* of the bill. That is the opposite of what "the newly admitted documents are shorter, so the real figure is likely below $4.75" assumed, and it is why the gate had to be re-measured rather than scaled.

`--dry-run` against the rebuilt corpus (7,127 relevant documents, the number that is actually tagged):

| Corpus tagged | Total tokens | Free tier (200k TPD) | Developer tier cost @ $0.15/$0.60 per 1M, 72/28 split |
| --- | --- | --- | --- |
| 600 docs | ~387k | 2 days | ~$0.11 |
| 2,000 docs | ~1.3M | 7 days | ~$0.36 |
| 4,000 docs (Phase 3 band ceiling) | ~2.6M | 13 days | ~$0.71 |
| 7,020 docs — `--dry-run`, 2026-08-21 (YouTube-only pre-purchase) | 4.53M | 23 days | $1.25 |
| 7,127 docs — `--dry-run`, 2026-08-24 | 4.60M | 23 days | $1.27 |
| **800 docs — `--dry-run`, 2026-08-25, after `tag_sample`** | **516k** | **3 days** | **$0.14** |
| **800 docs — `--dry-run`, 2026-08-28, after tagging** | **0 remaining** | **0** | **$0.00** |
| 26,718 docs (eligible ceiling, if everything were tagged) | 17.2M | 87 days | ~$4.76 |

**The "$4.75 / under-three-dollars is broken" claim was a unit error, the same class as Phase 2's raw-vs-document floor.** 26,718 is documents *surviving hard exclusions* (26,539 before Quora). Tagging bills *relevant* documents. `--dry-run` reports **$1.27**, which is under three dollars. The free-tier gate still fires (23 days ≫ 2), so the recorded choice is paid tier, not a corpus cap forced by price. 7,127 is above the Phase 3 band of 1,500–4,000; that is a *sampling* decision, not a *solvency* one. See Phase 4.

**Consequence:** the pipeline is built free-tier-safe — resumable, checkpointed after every batch, cache-first — so a run can legitimately span days. A corpus of 7,127 relevant documents costs $1.27 on the paid tier versus three weeks on the free tier. Phase 4's gate was taken the third way it always allowed: **sample**, at 800 documents, which is three free-tier days rather than twenty-three. That sample is now tagged; a second run costs nothing. See §4.

**Mitigations built into the design:** batching, a 20b triage cascade that keeps 120b spend on documents that are actually about wishlist behavior, per-document caching so no token is ever spent twice, and a local token-bucket governor that reads `x-ratelimit-*` headers and pauses before breaching rather than absorbing 429s.

---

## Phase overview

| Phase | Focus | Est. effort | Blocking dependency | Status |
| --- | --- | --- | --- | --- |
| 0 | Scaffold, config, secrets | 0.5 day | — | **Complete** |
| 1 | Core contracts: schemas, DB, hashing | 0.5 day | 0 | **Complete** |
| 2 | Collection (10 sources, 6 of them scraped) | 3 days | 1 | **Complete — 6 of 6 exit criteria met** (audited 2026-08-24). 55,913 raw → 26,718 documents. `quora_manual` collected into `data/raw` (182 records / 179 documents from 204 imported answers); `ajio_manual` is disabled — AJIO has no on-site free text to collect |
| 3 | Corpus build: exclusions, dedupe, triage | 1 day | 2 | Live table is the 2026-08-24 `--no-tier2` rebuild: **7,127 relevant**. Rejected-pool audit labelled 2026-08-28: **FAIL** (13/50). Filter *code* narrowed the same day; the corpus was not rebuilt (`--force` died on `doc_tags` FK). Band floor met; ceiling retired by sampling. Tier-2 never completed (§3.6) |
| 4 | Groq tagging engine | 3 days | 3 | **800/800 tagged and cached.** `--dry-run` on 2026-08-28 reads 0 remaining. Gold set absent; F1 and evidence precision **not measured** |
| 5 | Quantification | 1 day | 4 | **Complete** — ran 2026-08-28 over the 800 tagged sample (108 `genuine_intent`). CSVs in `data/processed/` |
| 6 | Synthesis and report | 1 day | 5 | **Complete** — `outputs/opportunity_report.md` rendered 2026-08-28 (seven sections). Gold-set quality is disclosed as unmeasured rather than invented |
| 7 | Reproducibility hardening, full run, QA | 1 day | 6 | Machinery in place (`README.md`, `scripts/verify_hardening.py`). Cache gate holds. Clean wipe of `data/interim` not run. Architecture §11 gold-set gates unmeasured |
| 8 | Read-only explorer (Stitch UI over frozen outputs) | 1 day | 5, 6 | **Complete** — Streamlit explorer at `app/explorer.py`; sidebar widget navigation; Ask optional until `GROQ_API_KEY`; does not re-run the engine |

**Complete through the report and explorer, with three named holes.** Phases 0, 1, 2, 5, 6 and 8 have met their exit criteria as written. Phase 4 tagged the 800-document sample end to end (`doc_tags` = `llm_cache` = 800; a second `--resume` is free) but never labelled a gold set, so macro-F1 and evidence precision are explicitly unmeasured. Phase 7's cache and quantify-reproducibility checks exist and the cache gate holds; a clean wipe of `data/interim` has not been run, because that directory holds the tags. Phase 3's funnel and unit tests hold; the rejected-pool audit was labelled on 2026-08-28 and **failed** (13/50, 26% false rejections). The filter code was then narrowed (emoji remainder-too-short, incidental Devanagari, stock/availability keywords) but `discovery.db` is still the 2026-08-24 any-emoji table: a `--force --no-tier2` persist the same evening died on `doc_tags`' foreign key, so the live relevant count is still **7,127**. The 1,500–4,000 band's floor is met; its ceiling was a tagging-cost bound and sampling closed it. Tier-2 has still never completed (`triage_cache` is empty). See "Where the build actually stands" below.

Phases 2 and 4 are the risky ones; everything else is mechanical. Phase 2 doubled from the original estimate when the roster changed: six HTML sources means six selector sets to build and maintain, versus two API clients before. Phase 4 grew by a day for two reasons found while probing in Phase 1: schema violations are routine rather than exceptional, making the retry/repair ladder real work instead of a wrapper, and the gold set now labels justifying spans rather than tags alone, which is slower to build but the only way to measure whether the tagger's quotes support its tags.

**Phase 0 verified on completion:** 16 tests passing; `check_credentials.py` reports PASS for Groq models, Groq inference, the tagging schema, and YouTube; both pinned models confirmed present on the active key.

### Where the build actually stands — audited 2026-08-28

488 tests collected on Python 3.12.7. Collection numbers come from `scripts/audit_collection.py` (6 of 6, 2026-08-24). The relevant count is the 2026-08-24 `--force --no-tier2` table, still live. Tagging, quantification, and synthesis ran over that table's 800-document sample.

| Gate | Target | Actual | Where |
| --- | --- | --- | --- |
| Raw records collected | ≥ 15,000 | **55,913** ✓ | Phase 2 |
| **Documents surviving the hard exclusions** | ≥ 1,500 | **26,718** ✓ | Phase 2 |
| **Pre-purchase documents surviving** | ≥ 2,000 | **21,962** ✓ | Phase 2 |
| Every enabled source populated | 8 of 8 | **8 of 8** ✓ — `quora_manual` collected 2026-08-24 (182 records); `ajio_onsite` and `trustpilot` accepted as zero-yield; `ajio_manual` disabled, so out of the denominator | Phase 2 |
| Documents marked relevant | 1,500–4,000 | **7,127** — floor met; ceiling was a cost bound, closed by sampling (5,443 pre-purchase; 107 Quora, 5,336 YouTube) | Phase 3 |
| Rejected-pool audit | < 10% false rejections per stratum | **FAIL** — 13/50 (26%) on the any-emoji corpus. Snapshot: `outputs/rejected_pool_audit_v1_any_emoji.*` | Phase 3 |
| Documents selected for tagging | a recorded choice | **800** in `tag_sample` (seed 42, census + proportional), drawn 2026-08-25 and logged to `run_log` | Phase 4 |
| Documents tagged | ≥ 95% of selected | **800 of 800** (100%), all in `llm_cache`. `--dry-run` remaining: 0 | Phase 4 |
| Gold-set macro-F1 / evidence precision | ≥ 0.65 / ≥ 0.80 | **Not measured** — `tests/gold/gold_set.jsonl` does not exist | Phase 4 |
| Opportunity report | generated from CSVs | **`outputs/opportunity_report.md`**, 2026-08-28 | Phase 6 |

**One state is easy to misread, so it is worth naming.** `python -m src.collect.manual` reports `OK quora_manual … 204 document(s) in 1 file(s)`, and `scripts/audit_collection.py` now reports `quora_manual … complete 182 records`. Both were always scoring different layers: the loader scores the *import directory*, the audit scores the *part files*. Collect ran on 2026-08-24 and closed that gap (204 imported → 182 written, 22 duplicates skipped, 179 projected to survive the hard exclusions). The remaining layer closed the same day: `build_corpus --force --no-tier2` folded those rows into `discovery.db`, so relevance and tagging figures are no longer a YouTube-only pre-purchase corpus. Of 5,443 relevant pre-purchase documents, 107 are Quora and 5,336 are YouTube (98.0%).

**The floor correction is implemented, and it changed the verdict twice over.** Phase 2's floors are now stated in *documents surviving the hard exclusions* rather than raw records, in `collection.floors.{pre_purchase,total}_documents`; the record floors are kept as leading indicators, printed last and labelled as context. `run_collection` scores each record against Phase 3's real rules — imported from `src/store/exclusions.py`, not reimplemented — as it is written, and reports both units per source and per purchase stage. Two consequences, in opposite directions:

- The pre-purchase gate now *genuinely* passes rather than passing on a number that never reaches the tagger: **21,783 pre-purchase documents**, from 45,900 YouTube comments at a 47.5% survival rate. The 180-document figure was never a collection failure; it was 4,494 comments, and widening the query terms to twelve fixed it.
- The source-coverage gate that used to fail now passes: eight enabled sources, two accepted as zero-yield (`ajio_onsite`, `trustpilot`), none unfilled.

Consequences worth stating plainly before any tokens are spent:

- **The hand-collection task is done, and so is the re-collect, and so is the corpus rebuild.** `data/manual/quora/is-ajio-reliable.jsonl` holds **204 answers across 10 threads**, well past the 15–25 time-box; Collect wrote 182 of them to `data/raw/quora_manual/2026-08-24/` (22 duplicates skipped); `build_corpus --force --no-tier2` the same day wrote **107 relevant** Quora documents into `discovery.db` (3 emoji, 2 near-duplicates, 70 zero-hit drops). Fixtures still do not belong in `data/manual/`, and a test now asserts it by bytes and by marker text rather than by requiring the directory to be empty.
- **Three synthetic records were removed from the raw corpus.** `data/manual/ajio/test_import.md` was an invented smoke-test file in the production import directory, and its two Q&A entries and one review were the corpus's *entire* AJIO contribution — each carrying a citable `ajio.com/p/469558637` URL that would have appeared in the report as real customer voice.
- **Phase 3 has been rebuilt at the 3-word gate, now with Quora in the table.** `discovery.db` holds **7,127 relevant documents** (5,443 pre-purchase, 153 post-purchase, 1,531 mixed) from 26,718 eligible. That is above the 1,500–4,000 band: the floor that used to fail now overshoots. The 73%-app-reviews problem is gone among relevant documents too: `mixed` is 1,531 of 7,127, or 21%. Pre-purchase relevant is no longer YouTube-only: 5,336 YouTube + 107 Quora. The live table is still this 2026-08-24 rebuild; the 2026-08-28 `--force` attempt did not persist.
- Tier-2 LLM triage has still never completed (`triage_cache` is empty), but the reason it could not has been removed. A `--force` run (no `--no-tier2`) on 2026-08-24 was the first live attempt: 98 batches returned 200, then Groq's 200k TPD cap raised `RateLimitError` (`Used 199124, Requested 1670`) and the build exited 1 before persist. As of 2026-08-26 the stage checkpoints every batch into `triage_cache`, resumes from it, and stops before the daily cap rather than absorbing a 429 (§3.4). It is the lever that can still move the relevant count *down* into the band without deleting rows — three free-tier days — and it is **not** being spent: sampling already closed the band's cost ceiling, and the rejected-pool audit said the emoji rule should move first. Do not start a three-day `build_corpus` without `--no-tier2` unless that decision is taken explicitly.
- The rejected-pool audit was labelled on 2026-08-28, on the any-emoji corpus, and **failed**: 13 of 50 false rejections (26%), with `contains_emoji` at 5/10 and `tier1_zero_hits_contentless` at 5/10. Labelling was a reading of the rows, not a Groq call. Filter code was then narrowed (§3.6); a new 50-draw against the rebuilt table has not been scored because the rebuild did not persist. Snapshot of the FAIL: `outputs/rejected_pool_audit_v1_any_emoji.jsonl`.
- **A new input arrived that is deliberately not part of the corpus.** `data/aggregates/ajio/` holds 51 products' worth of AJIO-published rating and fit/quality percentages, read only by Phase 6. It is not a source, not a document, and not counted in any figure above. See §2.2.
- **The tagging job ran on 800 documents, and the corpus is untouched.** `tag_sample` (seed 42, target 800) holds 11.2% of the relevant corpus: the three small sources whole, the rest proportional. **800 of 800 are in `doc_tags` and `llm_cache`.** Nothing was deleted and no `is_relevant` value changed — dropping the table restores the full 7,127-document job. Two numbers coexist on purpose, and the report quotes both: the corpus is 7,127, the tagged sample is 800. Intent split in the sample: 108 `genuine_intent`, 69 `bookmark_only`, 623 `ambiguous`. See §4.

---

## Phase 0 — Scaffold and configuration

**Objective.** A runnable skeleton where every later phase has a home and no secret ever reaches a file that could be committed.

**Deliverables**

- `requirements.txt` — pinned versions of the dependency list in `architecture.md` §12, with `groq` as the LLM client.
- `.env.example` — documents required keys (`GROQ_API_KEY`, `YOUTUBE_API_KEY`, `HASH_SALT`) and optional ones for disabled sources (`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`), with no values.
- `.env` — real credentials, listed in `.gitignore` on the first commit.
- `.gitignore` — `.env`, `data/`, `__pycache__/`, `*.db`.
- `config.yaml` — the run config from `architecture.md` §4, with the Groq model block.
- `src/common/config.py` — loads and merges `.env` and `config.yaml` into a typed `Settings` object; raises at import time if a required key is missing.
- `src/common/logging.py` — structured logger writing to console and `logs/<run_id>.log`, forcing UTF-8 on both handlers *and* on the process's own `stdout`/`stderr`.
- `src/common/encoding.py` — the two encoding boundaries in one module: tolerant decoding of hand-saved files, and UTF-8 hardening of console output. Added in flight during Phases 2–3 rather than planned here; recorded as a Phase 0 deliverable because it belongs to the common layer that everything else imports. See §3.4.
- Full package tree with `__init__.py` files.

**Tasks**

1. Create the directory tree exactly as specified in `architecture.md` §2.
2. Implement `Settings` with pydantic-settings; compute and expose `config_hash` (a sha256 of the resolved non-secret config) for run provenance.
3. Write `scripts/check_credentials.py` that makes one trivial live call per required provider — a Groq chat completion and a YouTube quota-cheap call — prints a pass/fail table, and lists which sources are enabled. Optional providers are checked only when both enabled in `config.yaml` and credentialed. Three properties are worth probing rather than assuming, because each has already failed once or would fail invisibly:
   - both pinned models appear in `models.list()` (this is what caught the retired Llama pin);
   - a real completion returns **non-empty** content, since reasoning tokens can consume a small budget entirely and an empty string otherwise reads as success;
   - the tagging model actually honors strict `json_schema` decoding, since the whole model pin rests on it and a silent regression would only surface deep into a multi-day Phase 4 run.

**Exit criteria**

- `python scripts/check_credentials.py` reports pass for Groq models, Groq inference, Groq strict schema, and YouTube.
- Missing *optional* credentials do not fail the run; missing *required* ones do.
- Importing any module with a missing required key fails loudly with a named error.

**Risk.** If Reddit is ever re-enabled, its script-app credentials need a correctly formatted `REDDIT_USER_AGENT` (`platform:app-id:version (by /u/username)`) or requests are throttled hard. The checker validates that format whenever Reddit is enabled.

---

## Phase 1 — Core contracts

**Objective.** Lock the data model before any collector writes a byte, so no phase has to reshape another's output later.

**Deliverables**

- `src/common/schemas.py` — pydantic models `RawRecord`, `Document`, `DocumentTags`, `EvidenceSpan`, matching `architecture.md` §5–§7 exactly, plus the `SOURCE_STAGE` mapping that classifies each source as `pre_purchase` / `post_purchase` / `mixed`. Both AJIO sources — on-site and manual import — are resolved by `meta.content_type`, since their Q&A and reviews sit on opposite sides of the purchase.
- `src/common/db.py` — SQLite connection factory, `init_db()` running the DDL from §6, and idempotent `upsert_documents()` / `upsert_tags()` helpers using `ON CONFLICT DO NOTHING`.
- `src/common/hashing.py` — `doc_id()`, `author_hash()` (salted from `.env`), `content_id()` for manually imported files whose only stable identity is their text, and `simhash_fingerprint()`. Written directly over `hashlib.blake2b` rather than taken from `datasketch`, which turned out to provide only MinHash and LSH — Jaccard similarity measures with no bitwise fingerprint to take a Hamming distance over. `datasketch` is dropped from `requirements.txt` as a result.
- `tests/test_schemas.py`, `tests/test_db.py`, `tests/test_strict_schema.py`, `tests/test_hashing.py`.

**Tasks**

1. Define enums for every taxonomy dimension in `src/tag/taxonomy.py` with a module-level `TAXONOMY_VERSION = "v1"`, and have `DocumentTags` reference those enums so an invalid tag cannot be persisted.
2. Make the tagging schema strict-mode compatible per §0.1: no optional fields, `additionalProperties: false` on every nested object, `[]` as the empty value for multi-label dimensions, and an enum for every numeric field. Expose `tagging_response_schema()`, which generates the schema from `TaggingResponse` and *raises* unless it passes every strict rule, so the failure lands at startup rather than after a batch has spent tokens. Add `tests/test_strict_schema.py` covering each rule individually — including the two that no documentation states and only a live 400 revealed: no enum may mix whole and fractional numbers, and results cannot be keyed by `doc_id`.
3. Enable SQLite WAL mode and foreign keys; add indexes on `documents(source)`, `documents(is_relevant)`, `doc_tags(taxonomy_version)`.
4. Write the run-log helper that stamps `run_id`, `stage`, `config_hash`, and record counts.

**Exit criteria**

- `pytest tests/` passes.
- Inserting the same `Document` twice leaves exactly one row.
- A `DocumentTags` payload with an unknown blocker value raises a validation error.
- `tagging_response_schema()` returns without raising, and a live call round-trips it through `gpt-oss-120b` back into a validated `TaggingResponse`.
- `SOURCE_STAGE` covers every source name returned by `CollectionConfig.enabled_sources()`, so a newly added source cannot silently default to an unknown stage.

**Phase 1 verified on completion:** 83 tests passing, and `check_credentials.py` round-trips the production schema through `gpt-oss-120b` back into `TaggingResponse`. Four decisions changed because they were tested against the live API rather than reasoned about:

| Finding | Change |
| --- | --- |
| `strict: true` validates after generation, not during — three consecutive calls returned violations | Every enumerable field became an enum; the repair path was promoted to a primary Phase 4 concern |
| Groq rejects an enum mixing whole and fractional numbers | `confidence` became `confidence_pct`, an integer percent in steps of 10 |
| A live call put `"none"` into `wishlist_motivation`, where it is not legal | `none` removed from `info_sought_elsewhere`; `[]` was already the way every other dimension expresses absence |
| `datasketch` has no simhash, and near-duplicates measure 4–11 bits apart, not ≤ 3 | Simhash implemented over `blake2b`; `near_duplicate_hamming` recalibrated from 3 to 12 |

---

## Phase 2 — Collection

**Objective.** A raw corpus on disk, large enough that Phase 3's filters still leave a usable analysis set. Target ~12,000–15,000 raw records.

### 2.1 Source roster and why the order matters

The metric is about *pre-purchase* hesitation, but most of this roster is post-purchase. Build order follows evidential value for the research question, not ease of scraping.

| Order | Source | Stage | Why |
| --- | --- | --- | --- |
| 1 | AJIO on-site **Q&A** | pre-purchase | The richest source available. "Does this run small?" *is* the blocker being hunted |
| 2 | YouTube comments | pre-purchase | Haul/review videos attract exactly the "should I buy this" audience |
| 3 | AJIO on-site reviews | post-purchase | Fit and quality feedback that reveals what pre-purchase uncertainty was justified |
| 3b | AJIO **manual import** | pre/post | **Disabled.** Was the supported fallback for the Akamai block; AJIO turns out to publish no review or Q&A prose on site at all, so there is nothing to hand-collect. Loader, collector and file format retained |
| 4 | Play Store + App Store | mixed | High volume, but skews toward app bugs and short one-liners |
| 5 | MouthShut | post-purchase | Long-form Indian reviews; friendliest robots policy of the review sites |
| 6 | ComplaintsBoard, ConsumerComplaints.in | post-purchase | Severity signal, but heavily delivery/refund grievances |
| 7 | Quora (manual import) | pre-purchase | Real deliberation language, but human-collected only |
| 8 | Trustpilot | post-purchase | Attempted last; robots.txt disallows `/reviews/`, so expect near-zero yield |
| — | Reddit | pre-purchase | **Disabled.** Collector retained; flip `enabled: true` and add `REDDIT_*` keys to restore |

**The structural risk to state plainly:** with Reddit off, only a few sources speak to pre-purchase hesitation, and two of them are manual. The 2026-08-19 probe confirmed on-site Q&A is refused by an Akamai edge (see 1.1.13 in `edge-case.md`), so the live pre-purchase routes are YouTube and the manual Quora import. The manual AJIO import (`ajio_manual`, 3b above) was meant to stand in for the blocked on-site collector and cannot: the site carries no free-text reviews or Q&A anywhere, only aggregate star-rating bars and fit/quality percentages, so both AJIO routes are empty for structural reasons rather than for want of effort. If what remains cannot fill the floor, the corpus becomes almost entirely post-purchase and the discovery engine will surface returns, delivery, and refund complaints instead of wishlist blockers. Q&A collection was therefore the first thing attempted, and neither route survived contact with the site: defeating the bot management is out of scope (`edge-case.md` §1.1.13d), and there is no prose behind it that defeating it would have reached.

### 2.2 The AJIO aggregate side-channel — added 2026-08-23, and deliberately not a source

Both AJIO routes are closed because the site publishes no prose. It does publish numbers: a star-rating distribution and fit/quality percentage breakdowns on every product page. Those are now collected — `data/aggregates/ajio/<product_id>.json`, **51 products, 6,882 raters** — and they are recorded in this phase because that is where they were collected, not because they enter it.

**Nothing about them touches Collect, Store, Tag or Quantify.** `src/store/aggregates.py` is the only reader and `src/synthesize/ajio_aggregates.py` the only consumer. `ajio_aggregate` is absent from `SOURCE_STAGE`, `KNOWN_SOURCES`, `STAGE_BY_CONTENT_TYPE`, `SOURCE_ORDER`, the config's source blocks and the audit's denominator, and `paths.aggregates_dir` is excluded from `ensure_dirs()` so Collect does not even create the directory. Two tests assert the wall on the registries and on the import graph rather than trusting it, because the way this breaks is a convenience import added later — `build_corpus` reusing the loader, say — after which the numbers enter the corpus with no visible step.

**The reason for the wall is arithmetic.** Every metric in Phase 5 counts people: prevalence, distinct-author share, per-author caps. One row here summarises hundreds of raters, so admitting it as a document would weight a crowd like an individual and inflate whatever it agreed with — and there would be nothing in the funnel to show it happened, since a number that validates as a record produces no loss to notice. Read beside the corpus instead, the same row is a real cross-check, which is what Phase 6 does with it.

What the 51 files actually say, as loaded (`scan_ajio_aggregates`, 0 skipped, 0 superseded):

| | |
| --- | --- |
| Products | 51 |
| Raters behind them (`rating_count`, summed) | 6,882 |
| Mean average rating | **3.7**, all 51 derived, none reported by AJIO |
| Products carrying AJIO's fit prompt | 47 of 51 |
| Mean "not a perfect fit" response | **31.5%** |
| Products whose *most-answered* fit option is a misfit | 1 of 47 (one tight, none loose) |
| Products carrying the quality prompt | 51 of 51 |
| Mean Bad + Very Bad | **16.2%** ("Average", AJIO's own midpoint, is not counted as bad) |

**The two fit rows look contradictory and are not.** "Perfect" is one label while a misfit is split across four — Loose, Too Loose, Tight, Too Tight — so "Perfect" takes the plurality on 46 of 47 products while roughly a third of respondents still say the garment did not fit them. The figure worth citing is the **31.5%**, not the 1 of 47: the plurality is an artifact of AJIO's label design, the share is the finding. It is also the strongest corroboration available for whatever the text corpus says about fit, which is the one theme the aggregates and the corpus both speak to.

Two properties of the data drove design decisions rather than being absorbed:

- **AJIO reports no average anywhere**, so the reader derives one from the star buckets and records in `average_rating_source` whether the figure was `reported`, from the `distribution`, or unknown. All 51 are currently derived. A derived average is a weaker claim than a published one, so Phase 6 is required to disclose which it is quoting rather than printing a bare number.
- **The buckets do not sum to 100** — 96 to 100 across the 51 files, median 97 — because AJIO rounds each independently. The derived mean therefore divides by the buckets' actual sum. Dividing by 100 would treat the shortfall as ratings of zero stars, which cannot exist on a 1–5 scale, and would pull every average down about 0.1 in the same direction: a one-sided bias that no report could disclose, because nothing would record that it happened.

**Blast radius, learned from the manual loader.** Two bad grabs showed up during collection — a 0-byte file, and two JSON objects concatenated by a grabber that ran twice into one path — so the loader skips and warns per *file* instead of failing the batch. Both have since been re-grabbed and the directory is currently clean, but the tolerance stays, because fifty good products must not be lost to one bad one. Identity is the `product_id` and recency wins on `extracted_at`, so re-grabbing is safe; a record with no `product_id` is refused, since unattributable numbers would carry a dead citation URL into the report.

**The reproducibility gap is closed — 2026-08-24.** The grabber was run ad hoc when the 51 files were collected, which left the input reproducible only by memory. It is now committed as `scripts/manual_extract/ajio_bars.js` with a bookmarklet build and a documented procedure, beside the two prose extractors. That makes the *method* versioned; it does not make the *output* rebuildable, because the grabber runs in a logged-in browser over products a person picked and AJIO's counts move daily. So the honest framing is **method-reproducible, not command-reproducible**, and the remaining obligation is disclosure rather than tooling: `src/synthesize/limitations.py` renders it as a limitations paragraph, taking the snapshot date range and the product count from the records so neither can go stale silently.

**Deliverables**

- `src/collect/base.py` — the `Collector` ABC, shared rate limiting, and JSONL writer with manifest support.
- `src/collect/scraping.py` — shared `robots.txt` gate, polite per-domain fetch, retry policy. Every HTML source goes through this, so compliance is implemented once.
- `src/collect/ajio_onsite.py` — product reviews **and** Q&A, tagged in `meta.content_type` so the two can be analyzed separately. Needs browser-grade headers; resolves product ids from `category_urls` when `product_urls` is empty.
- `src/collect/youtube.py` — Data API v3 `search.list` for haul/review videos, then `commentThreads.list` per video. Quota-aware: `search.list` costs 100 units against a 10,000/day default, so video ids are cached in `data/raw/youtube/_video_ids.json` and reused across runs.
- `src/collect/play_store.py`, `src/collect/app_store.py` — AJIO plus Myntra as a comparison set.
- `src/collect/mouthshut.py`, `src/collect/complaints_board.py`, `src/collect/consumer_complaints_in.py`, `src/collect/trustpilot.py` — paginated listing scrapers built on `scraping.py`.
- `src/collect/manual.py` — shared loader/validator for both manual directories. Scans for `.json`/`.jsonl`/`.txt`/`.md`, skips README, normalizes to `{id, source, url, text, author, timestamp}`, and raises `EmptyImportError` on a zero-doc dir. `python -m src.collect.manual` is the CLI. **Makes no network calls.** After this file, filling the dirs is a person-task; there is no more Collect code for these two sources.
- `src/collect/quora_manual.py` — thin wrap around the shared loader; markdown split of question from answers still lives here. **Makes no network calls.**
- `src/collect/ajio_manual.py` — thin wrap around the shared loader; markdown `## Q&A` / `## Reviews` parsing still lives here and never infers `content_type` from the prose (§1.1.14). **Makes no network calls,** and does not import the on-site collector that owns the HTTP session.
- `scripts/manual_extract/` — console snippet / bookmarklet (`ajio_extract.js`, `quora_extract.js`, `ajio_bars.js` and its bookmarklet build) and optional Playwright-over-CDP helper that attaches to an already-open Chrome profile. Not imported by Collect. Playwright is not in `requirements.txt`.
- `src/collect/run_collection.py` — `--sources`, `--force`, `--max-records` CLI that skips disabled sources.
- `data/manual/ajio/README.md`, `data/manual/quora/README.md` — JSON shape, extract tools, and the time-box (15–25 Quora answers; the AJIO time-box is withdrawn, since the site has nothing to collect). Both are ignored by their own importer, along with `_`-prefixed and dotted files, a filter that exists because the Quora README once parsed into nine phantom pre-purchase documents. Fixtures live under `tests/fixtures/manual/`, never in these directories.
- `data/aggregates/ajio/README.md` — the aggregate record schema, the rounding caveat, and the rule that this directory is read by Phase 6 alone. Not a Collect deliverable; see §2.2.
- `scripts/audit_collection.py` — scores all six exit criteria below against the part files on disk and exits non-zero if one is unmet. Added because every criterion here is about a *corpus*, and a corpus claim that is scored by hand goes stale silently; this one had, by a factor of four.

**Tasks**

1. Implement `base.Collector` with the manifest logic first; every collector inherits skip-if-already-collected behavior for free.
2. Implement `scraping.py` second: fetch and cache `robots.txt` per domain, refuse disallowed paths, enforce `per_domain_delay_seconds`, and send `scraper_user_agent`. No collector may bypass it.
3. For each source, capture the `meta` fields listed in `architecture.md` §5 — especially `rating` on review sources, which Phase 5 uses as a weak severity prior, and `content_type` on AJIO so Q&A and reviews stay separable.
4. Write listing URLs, company paths, and query terms into `config.yaml`, never into code, so the collection strategy is versioned and reviewable.
5. Log per-source yield to `run_log`, and print a pre/post-purchase split so a lopsided corpus is visible immediately rather than at synthesis.

**Exit criteria**

Scored by `python scripts/audit_collection.py`, not by hand.

- `data/raw/<source>/<run_date>/` populated for every enabled source with a valid `_manifest.json`. A source whose zero yield is a settled decision rather than a fault — AJIO on-site's Akamai block, Trustpilot's robots refusal — is listed in the audit's `ACCEPTED_ZERO_YIELD` with its reason, so "this yields nothing and that is fine" has to be written down and reviewed rather than inferred. A source *disabled* in `config.yaml` — `ajio_manual`, `mouthshut`, `reddit` — leaves the denominator entirely, since the audit scores `enabled_sources()` rather than a list of its own: the same exemption, taken one step earlier, and one flag to flip to take it back.
- Re-running without `--force` makes zero network calls.
- Every line in every JSONL file validates as a `RawRecord`.
- **At least 1,500 documents surviving the hard exclusions**, and **at least 2,000 of them pre-purchase.** Stated in documents, not raw records, because a floor evaluated one stage before the filters that remove three quarters of the corpus certifies a signal the next stage deletes — see §3.3 for the run that proved it. The raw-record targets (15,000 total, 2,000 pre-purchase) are retained as leading indicators: cheap, immediate, and known to overstate.
- A `robots.txt` compliance log exists for every scraped domain.

**Risks**

- *AJIO blocks automated access* — with browser-grade headers `robots.txt` returns 200, but every content path is refused by an Akamai edge that fingerprints the client. Treated as the site's access decision rather than something to defeat: the mitigation was hand-collection into `data/manual/ajio/` via the `ajio_manual` collector — and that mitigation did not survive contact with the site either: AJIO publishes no free-text reviews or Q&A anywhere, only aggregate rating and fit/quality bars, so a person reading the page has nothing to save. `ajio_manual` is disabled as a result. This is the highest-impact risk in the phase and it landed in full: it removes the single best pre-purchase source with no fallback behind it.
- *Corpus skews post-purchase* — see 2.1. Mitigated by the pre-purchase floor in the exit criteria and by source-spread weighting in scoring.
- *YouTube quota exhaustion* — 10,000 units/day allows ~90 searches. Mitigated by caching video ids and spending quota on comment pagination, which costs 1 unit per call.
- *Scraper fragility* — six HTML sources means six selector sets that break when a site redesigns. Mitigated by asserting a minimum expected record count per source and failing loudly when a page yields zero parsed items, which is the signature of a changed layout. The live probe showed this is necessary but not sufficient: a wrong URL and a stale selector produce the identical symptom, so `scripts/verify_sources.py` re-checks both against one live page per source.
- *Trustpilot yields nothing compliant* — accepted. Record the robots restriction in the report's limitations rather than working around it.
- *Thin store reviews* — most are one-liners like "good app". They will be culled in Phase 3; this is expected, not a failure.

**Phase 2 built, and the corpus on disk now scores it.** All thirteen modules exist, the offline tests pass, and `scripts/audit_collection.py` reports **6 of 6** as of 2026-08-24. What the tests established first is the machinery: robots failing closed on 403 and timeout, a challenge page never reaching the corpus, an empty first page raising instead of passing silently, PII redacted before it is ever written to disk, a re-run of a collected date making zero network calls, every written line validating as a `RawRecord`, and a record staying one physical line no matter which line separator its author used. The live run then produced the corpus those criteria are about.

**Live verification, 2026-08-19.** The three items below were carried as "unverified" and have now been probed with `scripts/verify_sources.py`, which fetches one page per configured listing through the collectors' own session and parses it with the collector's own parser. The results changed the config rather than confirming it, and the guesses were wrong more often than they were right.

| Was unverified | What the probe found |
| --- | --- |
| **AJIO category URLs and JSON endpoint templates** | Still blocked, but the failure is now understood rather than assumed. With browser-grade headers `robots.txt` returns 200 — correcting the note in 1.1.13 that the policy file itself was refused — while every content path, including the sitemaps, is refused by an Akamai edge. So this is bot management, not policy, and not a missing header. Two consequences: the `/<slug>/c/<id>` shape is corroborated by AJIO's own `Disallow: /*/c/83?` while the individual slugs stay unconfirmed, and **both JSON endpoint templates have been emptied**, because `robots.txt` disallows `/api/*` — the guessed endpoints described a path this project has committed to not taking. The escalation was recorded here as a browser-driven fetch; the 2026-08-20 follow-up below revises that to a hand-collected import, for the reasons given there |
| **Selector sets for the five HTML sources** | Three of the five were wrong, and each failed differently. `complaintsboard.com/ajio` and `consumercomplaints.in/ajio-b110716` both **404**ed, so neither selector set had ever run against a real page; the corrected paths are `/ajio-b144612` and `/ajio-b115930`. The MouthShut listing was worse — it returned **200 with an unrelated "Nadiad Restaurants" page**, the failure shape the tripwire cannot catch by status alone. Both complaint parsers have been rewritten against live markup and now yield 5/5 and 20/20 items with authors, dates and unique ids; ComplaintsBoard reads schema.org microdata first, since it survives a CSS rename. MouthShut is **disabled**: its correct URL renders the review list client-side, so no selector set can reach it |
| **Whether the pre-purchase floor is reachable at all** | Unchanged and now more likely: with Reddit off, MouthShut disabled and AJIO blocked, the 2,000-record floor rests on YouTube and manual Quora. The floors moved into `config.yaml` under `collection.floors`, and the warning now names every source that could have contributed pre-purchase records along with what each actually produced and its status, so the shortfall points at a source instead of just a number |

The general lesson is worth keeping: a wrong URL, a blocked client and a renamed class all present as "zero items parsed", and the run cannot tell them apart. `scripts/verify_sources.py` exists to separate them in one page per source, before a run rather than during one.

**Follow-up, 2026-08-20 — the AJIO fallback now has a home.** 1.1.13's escalation was only real if there was somewhere for a hand-collected sample to land, and there was not. `src/collect/ajio_manual.py` is that collector: it reads `.txt`/`.md` files a person saved from `data/manual/ajio` and, like `quora_manual`, imports no HTTP client — a test asserts the absence, and additionally forbids importing `ajio_onsite`, since that module owns a `PoliteSession` and would leave a network path one attribute access away. Two rules carry the compliance weight. Content type comes from an explicit `## Q&A` / `## Reviews` header and is never inferred from the prose, because a hand-typed file is if anything easier to mix up than a JSON payload (§1.1.14); an unlabelled block is skipped and counted. The product id must be declared with a `product:` line, with no filename fallback, because identity may not depend on a filename (§1.2.8) and a name like `ajio-830216012-kurtas.md` carries a *category* id that would become a dead citation URL. This also revised the escalation guidance itself: the earlier note pointed at a browser-driven fetch (Playwright/Selenium), which against Akamai Bot Manager means stealth patching and is the same circumvention in a browser costume, so both the collector and `edge-case.md` now record that defeating the bot management is out of scope. One consequence for the floor: AJIO questions are often shorter than the eight-word hard exclusion — *"does this run small?"* is four words — so a meaningful share of what is hand-collected is dropped before tagging (§1.1.13e), and the shortfall message in the collection summary says so. See `edge-case.md` §1.1.13d–e.

**Collection as it actually stands, 2026-08-24.** Six of the ten sources yield records (MouthShut disabled, Reddit disabled, both AJIO routes structurally empty, Trustpilot robots-empty). Every figure below is `scripts/audit_collection.py` output, and both units are shown because the gap between them is the finding:

| Source | Stage | Raw records | Documents | Survive | Status |
| --- | --- | --- | --- | --- | --- |
| youtube | pre-purchase | 45,900 | 21,783 | 47.5% | complete |
| play_store | mixed | 8,626 | 3,723 | 43.2% | complete |
| app_store | mixed | 1,000 | 832 | 83.2% | complete |
| consumer_complaints_in | post-purchase | 200 | 196 | 98.0% | complete |
| complaints_board | post-purchase | 5 | 5 | 100% | complete |
| ajio_manual | pre/post | 0 | 0 | — | disabled; AJIO publishes no on-site free text, so there is nothing to hand-collect |
| quora_manual | pre-purchase | 182 | 179 | 98.4% | complete — collected 2026-08-24 from 204 imported answers (22 duplicates skipped) |
| trustpilot | post-purchase | 0 | 0 | — | complete; robots disallows `/reviews/`, as predicted |
| ajio_onsite | pre-purchase | 0 | 0 | — | `blocked` — Akamai refused every content path |
| mouthshut | post-purchase | — | — | — | disabled; review list renders client-side |
| **Total** | | **55,913** | **26,718** | **47.8%** | |

The `Documents` column is measured at the 3-word gate (§3.1). At the 8-word gate the same records yielded 14,552 documents at a 26.1% survival rate, so any figure quoted from an earlier pass of this document is roughly half of the current one and the difference is the threshold, not the collection.

**All six exit criteria are met** (audited 2026-08-24). Both floors pass in the unit that matters — 26,718 documents against 1,500, and 21,962 pre-purchase documents against 2,000 — every enabled source has a valid manifest, every line validates as a `RawRecord`, a re-run of a collected date makes zero network calls, and a compliance log exists for all five scraped domains. (The document floors were 14,552 and 11,806 when this paragraph was first written, then 26,539 and 21,783 after the length gate moved from 8 words to 3; the 2026-08-24 bump is 182 Quora records, not another threshold change.) Three things changed to get the corpus this large, and two of them were bugs rather than collection:

- **The corpus grew four-fold, and the earlier numbers were simply stale.** YouTube went from 4 search terms to 12 (45,900 comments from 4,494), and `app_store`'s dead app id was replaced, taking it from `zero_yield` to 1,000. Both were done before this pass; neither had reached this document, which is precisely the failure `audit_collection.py` now prevents. `scripts/verify_sources.py` re-probes both live and `app_store` passes: 50 reviews on page 1 for each of the two ids, resolved through the iTunes lookup API first so a dead id cannot masquerade as an unreviewed app.
- **One record was being silently destroyed by its own text, and criterion 3 caught it.** A YouTube comment laid a numbered list out with **U+2028 LINE SEPARATOR**. `str.splitlines()` treats that as a line break and no JSON serializer escapes it, since JSON only requires escaping below U+0020 — so the record was one line to a reader splitting on `\n` and six lines to `build_corpus`, which called `splitlines()` and counted six malformed lines. The loss was reported only as a number in the funnel, attributable to nothing. Fixed on both sides: `clean_text` folds all eight such characters into `\n` at collection, `RawWriter.write` escapes any that reach it anyway — the one-object-per-line guarantee belongs to the file format, not to a text cleaner upstream remembering — and both readers now split on newlines alone, which recovered the record already on disk without re-collecting.
- **Three synthetic records were removed.** See "Where the build actually stands" above.

**The sixth criterion closed on 2026-08-24.** `run_collection --sources quora_manual --force` wrote 182 records to `data/raw/quora_manual/2026-08-24/` (204 imported, 22 duplicates skipped, 179 projected to survive the hard exclusions, 3 dropped as emoji). `scripts/audit_collection.py` now reports 6 of 6. The shared loader in `src/collect/manual.py` still treats the import directory as a first-class Collect input and fails on zero documents — that signal fired correctly while the directory held only its README, and it is the right check if the file is ever removed. The loader deliberately does not fail the collection run's exit code: failing every run until someone hand-collects trains whoever runs it to ignore the exit code, so `audit_collection.py` remains the gate. It prints a disabled source as `OFF` rather than `FAIL`, since `ajio_manual`'s empty directory is the expected state and not an outstanding task. There is no more Collect code for either source.

**What the pre-purchase picture actually is now.** 21,962 of 26,718 documents (82%) are pre-purchase: 21,783 YouTube and 179 Quora. That is a better ratio than this phase ever hoped for, and it comes with a concentration risk the old shortfall warning was not built to express: YouTube is still 99.2% of the pre-purchase documents, so any YouTube-specific bias — haul-video audiences, comment-section self-selection, influencer framing — still propagates to almost the whole pre-purchase claim. Filling `quora_manual` was never about reaching the floor; it is about source diversity, and it is the only hand-collected route left. The real signal — wishlist hesitation and purchase friction — lives more in forum threads than in haul comments, and the on-site Q&A that would have been the best of it does not exist to collect. Phase 6's corpus summary (and Part 1 of the report) has to name that mix: identify, quantify, and compare reads as credible when it discloses the YouTube concentration and shows what broke it. The Quora sample will not match 21,783 and does not need to. These document counts are from the audit of `data/raw`; they now also sit in `discovery.db` after the 2026-08-24 rebuild. Of the 5,443 *relevant* pre-purchase documents, 107 are Quora (1.97%) and 5,336 are YouTube.

**Follow-up, 2026-08-22 — both manual dirs are first-class Collect inputs.** They still hold only README, which is why the audit stays at 5/6; that signal is correct. What changed is the loader: `src/collect/manual.py` scans each dir for `.json`/`.jsonl`/`.txt`/`.md` (skipping README), normalizes to `{id, source, url, text, author, timestamp}`, and fails on a zero-doc dir — including the case where files exist but none parse. JSON from a console snippet or from Playwright attached over CDP to an already-open Chrome is the intended fill path (`scripts/manual_extract/`); Collect itself still imports no HTTP client and no Playwright. Fixtures live under `tests/fixtures/manual/`. After real threads land, there is no more Collect code for these two sources. Time-box 15–25 Quora answers (Google `site:quora.com AJIO` + sizing/returns/"worth buying", then the same visible-DOM snippet). Headless AJIO remains out of scope: the block is the automated fingerprint, not a person reading the page. (Superseded in part by the entry below: the AJIO half of this route is closed, and the time-box for it is withdrawn.)

**Follow-up, 2026-08-22 — `ajio_manual` is disabled, because there is nothing on the site to hand-collect.** A browse of ajio.com found **no free-text reviews and no Q&A anywhere, sitewide**: a product page carries aggregate star-rating bars and fit/quality percentage breakdowns and no customer prose at all. So `ajio_manual` can never yield a document, and for a structural reason rather than a pending one — the same kind of reason `ajio_onsite` cannot, one layer up. `enabled: false` in `config.yaml` records that, and three reports were changed to stop reading it as an outstanding collection task: `run_collection`'s pre-purchase shortfall block prints a source disabled in config as *disabled in config (AJIO has no on-site free text)* rather than *not run*, `python -m src.collect.manual` prints it `OFF` rather than `FAIL`, and `audit_collection.py` needed no change at all because it already scores `enabled_sources()` — the disabled source simply leaves the denominator, which is why the source-coverage gate now reads 7 of 8 with `quora_manual` alone outstanding. Nothing was deleted: `SOURCE_STAGE`, `KNOWN_SOURCES`, `STAGE_BY_CONTENT_TYPE`, the loader, the collector, the fixtures and the file format all stay, so re-enabling is one flag if AJIO ever publishes review text. One loader bug surfaced while writing the AJIO fixtures for this and is fixed: the JSON branch of `load_dir` built its documents in a single comprehension, so one record with an unresolvable product id was caught at *file* level and discarded the whole file — and in a one-file directory, the whole import. Each record is now validated on its own, the bad one named in `warnings`, the rest kept.

**Follow-up, 2026-08-23 — Quora landed, and it is a different kind of text from everything else in the corpus.** `data/manual/quora/is-ajio-reliable.jsonl` holds **204 answers across 10 threads**, collected with the bookmarklet against threads a person opened, and `python -m src.collect.manual` validates all 204. Four things about the sample matter more than its size:

- **The threads were chosen against the research question, not against the brand name.** They are wishlist-intent questions — why people wishlist instead of buying, what stops an instant purchase, what uncertainty survives shortlisting, what information is sought outside AJIO/Myntra, how shortlisted items get compared — which is the deliberation language the North Star metric is about and the thing haul-video comments are worst at.
- **The answers are long.** Median 78 words, mean 77, range 8–431, against a corpus-wide median of 8. That inverts the cost profile Phase 4 measured: 204 documents of this length are worth more tokens per document than 204 YouTube comments, and they are also the documents most likely to carry a quotable justifying span.
- **71 of the 204 are truncated by Quora itself,** ending in `(more)` because the answer was served collapsed. That is a data-quality fact about the sample, not a parser bug, and it belongs in the report's limitations: a truncated answer can still support a tag, but its evidence span may be cut off mid-sentence, which the verbatim check will pass and a reader will notice.
- **Author and timestamp are absent** — 203 of 204 have no author string and none has a timestamp. Two consequences: author-level aggregation (§8) cannot deduplicate a prolific Quora answerer, and recency decay has nothing to decay, so these documents will sit at whatever the neutral weight is. Neither is fatal at n=204, and both should be stated rather than discovered in Phase 5.

The loader also grew one narrow rule for this file: `discover_files` now skips `_`-prefixed and dotted filenames as well as README, which is how `_is-ajio-reliable.json.txt.orig` — the raw pre-cleanup capture, kept beside the data on purpose — stays out of the import without being deleted. The fixture guard was rewritten at the same time, from "both directories are empty" to what it always meant: nothing under `data/manual/` may be byte-identical to a fixture or carry fixture marker text. The old form was an equivalent check only while the directories were empty, which is exactly the kind of assertion that passes forever and proves nothing.

One design decision worth recording because it changes what the corpus counts: **an AJIO Q&A answer is metadata on its question, not a document of its own.** Answers are typically written by people who already bought the item, so promoting them to documents under a source mapped to `pre_purchase` would file post-purchase voice as deliberation — the exact conflation §1.1.14 exists to prevent. They are kept in `meta.answers` where they add context without being counted. Both AJIO collectors, scraped and manual, follow this rule.

**Follow-up, 2026-08-24 — Collect ran, Phase 2 is 6 of 6.** `python -m src.collect.run_collection --sources quora_manual --force` imported the 204 answers and wrote **182 records** to `data/raw/quora_manual/2026-08-24/` (22 duplicates skipped on `(source, source_native_id)`; 179 projected to survive the hard exclusions; 3 dropped as emoji). `scripts/audit_collection.py` now reports **6 of 6**: eight enabled sources populated, two accepted as zero-yield, 55,913 unique raw records, 26,718 documents, 21,962 of them pre-purchase. No other source was re-collected — YouTube and the scrapers were left on their 2026-08-20 partitions so the run would not spend quota fixing a source that is already complete.

**Follow-up, 2026-08-24 — Phase 3 rebuilt, Quora is in `discovery.db`.** `--force` without `--no-tier2` was the first live tier-2 attempt: 98 batches returned 200, then Groq TPD (200,000) raised `RateLimitError` (`Used 199124, Requested 1670`) and the build exited 1 *before persist*, so the classifications never reached the table. The rebuild that closed the gap is `--force --no-tier2`. Of the 182 collected Quora records, 107 are relevant (3 emoji, 2 near-duplicates, 70 zero-hit drops). See §3.3.

---

## Phase 3 — Corpus construction

**Objective.** Turn raw records into a deduplicated, relevance-scored `documents` table, and decide exactly which documents deserve 120b tokens.

**Deliverables**

- `src/store/normalize.py`, `src/store/dedupe.py`, `src/store/exclusions.py`, `src/store/relevance.py`, `src/store/build_corpus.py`.
- `config/relevance_keywords.txt` — the wishlist/saved-items/fit/return/comparison vocabulary.
- `data/interim/discovery.db` populated.
- `scripts/audit_rejected_pool.py` — draws and scores the fourth exit criterion's 50-document audit. Added 2026-08-26 for the same reason as `scripts/audit_collection.py`: a criterion about a *corpus* that is scored by hand is not scored at all, and this one had been outstanding since the first build.
- `tests/test_dedupe.py`, `tests/test_exclusions.py`, `tests/test_relevance.py`, `tests/test_rejected_audit.py`.

### 3.1 Hard exclusion rules

These run in `src/store/exclusions.py` immediately after normalization and **before** dedup and any LLM triage, so excluded text never reaches a paid token. Each rule writes a reason code to `documents.exclusion_reason` and sets `is_relevant = 0`; rows are retained, not deleted, so the funnel stays auditable.

| Rule | Reason code | Implementation |
| --- | --- | --- |
| Fewer than `filters.min_words` words — **3**, revised down from 8 | `too_short` | Tokenize on whitespace after collapsing runs of spaces and stripping punctuation-only tokens; drop if `word_count < min_words` |
| Contains any emoji | `contains_emoji` | Detection unchanged (emoji package + regex). **Exclusion** is remainder-too-short: after `strip_emoji()`, the leftover is still below `min_words`. A trailing heart on a real comment is kept. Live `discovery.db` still reflects the old any-emoji drop (10,834 rows) until a `--force` persist succeeds (§3.6) |
| Hindi language | `hindi_language` | Drop Devanagari in the comment body, or `langdetect` `hi` at confidence ≥ 0.7 **and** ≥ `filters.language_min_words` (8) words. `@handle` mentions are stripped before the script test. A handful of Devanagari particles in an otherwise Latin sentence (`houl भी dikhao`) is treated as romanized Hinglish, not Hindi |

**Emoji detection** must cover the full set, not just the common blocks: `U+1F300–U+1FAFF` (pictographs, supplemental symbols, extended-A), `U+2600–U+27BF` (misc symbols and dingbats), `U+FE0F` (variation selector), `U+200D` (ZWJ sequences), and regional-indicator pairs `U+1F1E6–U+1F1FF`. A ZWJ sequence such as a family emoji is a single logical emoji built from several codepoints, so match on the presence of any of these rather than trying to count emoji.

Use the `emoji` package's `emoji.emoji_count(text) > 0` as the primary check with the regex as a fallback — hand-rolled ranges drift as Unicode adds blocks.

**Ordering matters.** Exclusions run before dedup so the funnel counts are honest, and before the tier-2 LLM triage so no Groq tokens are spent on text that was never eligible.

**The length gate moved from 8 words to 3, and it was not a tuning change.** At 8 the rule was removing 35,345 of 55,731 records — 63% of everything collected — and the shape of what it removed was the problem rather than the volume: the shortest form of a pre-purchase question is a pre-purchase question, and *"does this run small?"* is four words. `edge-case.md` §1.1.13e had already recorded that the richest source on the roster was excluded by construction, and treated it as a cost to be absorbed. It is cheaper to fix the threshold. Measured effect on the corpus: **14,552 → 26,539 eligible documents (+82%)**, pre-purchase **11,806 → 21,783**.

Two protections were implicit in the old value and had to be made explicit, because they were being provided by the number 8 rather than by anything that said so:

- **Language ID lost its short-text guard.** `edge-case.md` §3.3.2 notes langdetect is unreliable on short text and relies on the ordering — the word gate runs first, so nothing short reaches the detector. That was true only while the gate was 8. `filters.language_min_words: 8` now states the floor where the unreliability actually lives, inside the Hindi rule, and the Devanagari script test is deliberately *not* gated by it since script detection is exact at any length. Without this, a three-word English comment could be dropped as Hindi on a coin-flip.
- **The all-stopword case stopped being an oddity, but enforcing §2.8 as written made things worse.** *"this is the one that I was looking at"* is the §2.8 example, filed P1 on the assumption that such text is rare — an assumption an 8-word gate was what made true. Implementing its content-word floor of 3 as an actual gate was tried and reverted, because at a 3-word length gate it deletes precisely what lowering the gate admitted: *"still in my cart"* is two content words and an unambiguous wishlist-abandonment signal, and *"does this run small?"* is two. Meanwhile §2.8's own example has zero keyword hits and was already being dropped. So the rule removes nothing that Tier 1 does not already remove, and subtracts things it should not. `min_content_words` now **splits the zero-hit drop into two reasons and decides nothing** — "about nothing" and "about something else" are different findings for the rejected-pool audit, and only the second is evidence that the vocabulary is too narrow.

**A second barrier stood behind the first, and it made the gate change worthless on its own.** With the gate at 3, *"does this run small?"* reached the corpus — and Tier 1 then dropped it for **zero keyword hits**. The relevance vocabulary listed `runs small` but not `run small`, and matching is word-boundary aware, so the auxiliary in *"does this run small?"* puts the verb in its bare form and the phrase missed by one character. The richest pre-purchase question on the roster was being deleted twice, by two independent stages, for two unrelated reasons — and the second was invisible because **no test existed for Tier 1 at all**. `config/relevance_keywords.txt` now lists the bare-verb forms and the common fit phrasings, and `tests/test_relevance.py` asserts the question survives *end to end* rather than stage by stage, which is the only formulation that would have caught this. The general lesson is worth more than the fix: a filter chain has to be tested as a chain, since every stage passed its own tests while the pipeline as a whole discarded the text.

**Cost note.** `min_chars: 40` is not applied anywhere in code, and at a 3-word gate it would contradict the word count if it were — *"does this run small?"* is 21 characters. The word count is the authoritative gate; `min_chars` is a documented bound that no stage enforces.

**Trade-off, then the measurement.** The emoji rule as first specified was absolute, so a substantive English review that happened to end in a single emoji was dropped along with the low-effort ones. Lowering the length gate roughly doubled that count — 5,671 → 10,831 — because exclusions are first-match-wins: a 5-word comment with an emoji used to be counted as `too_short` and is now attributed to the emoji rule. The 2026-08-28 rejected-pool audit measured that cost at **5/10 false rejections** in the `contains_emoji` stratum. The code now excludes only when the remainder after stripping emoji is still below `min_words` (`emoji_is_the_substance`); detection (`contains_emoji`) is unchanged. `config.yaml` `exclude_emoji` documents that narrowing. The live table has not been rebuilt, so the funnel still prints 10,834 `contains_emoji` drops.

### 3.2 Tasks

1. Normalize to `Document`, hashing author handles and dropping the raw handle entirely.
2. Apply the §3.1 hard exclusions and stamp `exclusion_reason`.
3. Language gate on survivors: keep English and romanized Hinglish, drop Devanagari and detected Hindi per the rule above.
4. Dedupe in two passes — exact fingerprint match, then bigram simhash at Hamming ≤ 12 — marking rather than deleting via `is_duplicate_of`. The threshold was calibrated in Phase 1 rather than inherited (`edge-case.md` §2.3b); re-check it here against hand-labeled pairs from the real corpus, since a threshold that is too loose merges distinct complaints and one that is too tight lets cross-posted text inflate prevalence.
5. **Tier-1 triage (free):** keyword/regex relevance score. Drop documents with zero keyword hits. `filters.min_content_words` sub-divides that drop rather than adding to it (`edge-case.md` §2.8): a zero-hit document with fewer content words than the floor is counted as "about nothing", the rest as "about something else". Only the second is evidence the vocabulary is too narrow, which is the distinction the rejected-pool audit needs.
6. **Tier-2 triage (cheap LLM):** run survivors through `openai/gpt-oss-20b` with a single yes/no prompt — "does this describe deliberating over, saving, comparing, postponing, or abandoning an online fashion purchase?", stating explicitly that delivery, refund, and app-bug complaints are *not* relevant. Batched 20 per call, returning a strict-schema `{"documents": [{"doc_id", "is_relevant"}]}` array. **Measured at 56 tokens per document**, so a separate 200k TPD bucket classifies ~3,500 documents per day for free, and this is what protects the 120b budget. At 7,127 survivors that is a three-day run, so it **must survive being stopped** — see the checkpoint design in §3.3.
7. Write `is_relevant` and `relevance_score`; log the funnel with a per-reason-code breakdown.
8. Draw and score the rejected-pool audit with `scripts/audit_rejected_pool.py` (criterion 4).

**Exit criteria**

- Funnel report printed and logged: raw → normalized → **exclusions (`too_short`, `contains_emoji`, `hindi_language`)** → deduped → tier-1 → tier-2 relevant.
- Unit tests confirm each exclusion rule in isolation: the length boundary is exclusive at whatever `min_words` is set to (asserted at both 3 and 8, since the off-by-one is a property of the rule and not of the number); a ZWJ-sequence emoji is still detected; a substantive review with a trailing emoji is **kept**; a Devanagari review is dropped while romanized Hinglish, handle-only Devanagari, and an incidental Devanagari particle survive; short text is not excluded on a langdetect guess, while short *Devanagari* still is. One test reads `config.yaml` rather than passing thresholds in, because every other test in the file would still pass if the gate were silently reverted.
- 1,500–4,000 documents marked relevant. **Floor met** at 7,127. The upper bound was a tagging-cost ceiling; Phase 4's 800-document sample closed it. Tier-2 is the remaining shrink lever and has not been run.
- A 50-document manual audit of the *rejected* pool shows < 10% false rejections, audited separately for the three hard-exclusion codes and for triage rejections. **Labelled 2026-08-28 against the any-emoji corpus: FAIL** (13/50). Code was narrowed; a redraw after a successful persist has not been scored. Labelling is deliberately not a model call.

### 3.3 First full run — the funnel, and a floor measured in the wrong unit

All five modules and both test files exist, `discovery.db` is populated, and the funnel reproduces stably across runs (`--force --no-tier2`, 2026-08-20):

```
raw records loaded           12702
normalized                   12702
  too_short                   9312     73.3% of raw
  contains_emoji               647
  hindi_language                24
duplicates marked              103     (95 exact, 8 near)
  tier-1 dropped (0 hits)     1381
  tier-2 status            skipped
RELEVANT (corpus)             1235
  pre_purchase                 180
  post_purchase                153
  mixed                        902
```

Two of the four exit criteria are met: the funnel is printed and written to `run_log` with a per-reason breakdown, and the exclusion rules are each unit-tested in isolation. Two are not — and the run also exposed a specification error in Phase 2's floor, recorded at the end of this section.

**1,235 relevant against a 1,500–4,000 band.** Below the floor, and the arithmetic says where the recoverable margin is. The hard exclusions remove 78.6% of the corpus before triage; of the 2,616 documents that reach Tier-1, 47.2% survive. The interesting number is **647 documents excluded for `contains_emoji`** — because exclusions are first-match-wins in the order short → emoji → Hindi, every one of those 647 had *already cleared the 8-word gate*. That is the measurement §3.1's trade-off note asked for, and it is large: at the observed Tier-1 survival rate, narrowing the emoji rule to emoji-only or emoji-dominant text would return roughly 305 documents and put the corpus at ~1,540, just inside the band. `exclude_emoji` is already a config flag, so this is the cheapest available route back over the floor and it should be tried before any decision to collect more.

**Tier-2 LLM triage has never run.** Every build to date passed `--no-tier2`, so 1,235 is a keyword-only result and the last rung of the funnel is unexercised against the live API. Note the direction of the error: Tier-2 only ever *removes* documents, so running it can only push 1,235 further below the floor. Sequence it after the emoji decision, not before.

**The rejected-pool audit has not been done.** No artifact exists in `outputs/`. This is the criterion most worth not skipping, since 78.6% of the corpus is now discarded by three rules whose combined false-rejection rate is unmeasured, and the emoji decision above should be made on audit evidence rather than on the arithmetic alone.

**The floor correction — implemented.** Phase 2's pre-purchase gate was specified as "at least 2,000 records from pre-purchase sources" and passed at 4,494, while 180 pre-purchase documents reached the corpus. A floor measured in raw records cannot do the job it was written for, because it is evaluated one stage before the filters that remove 96% of what it counted. Both floors now live in `collection.floors` in two explicit units — `*_records` as a leading indicator, `*_documents` as the gate — and both stages read the same numbers: `run_collection` scores every record it writes against `src/store/exclusions.survives_hard_exclusions`, and this phase's warning threshold comes from config rather than the literal `2000` it used to hard-code. The AJIO case makes the point concretely and was half-anticipated in §1.1.13e: *"does this run small?"* is four words, so the richest pre-purchase question in the roster was dropped by the 8-word rule by construction. **That observation is what eventually moved the rule itself** — see §3.1. A threshold that excludes the clearest example of the thing being measured is not a filter, and correcting the floor's *unit* while leaving the threshold in place would have measured the same mistake more precisely.

**Everything above this line is a record of the 8-word run and none of its arithmetic still holds.** Two things invalidated it in sequence: the corpus grew to 55,731 records, and the length gate then moved to 3 words. The rebuild that paragraph asked for was run on 2026-08-21 (`--force --no-tier2`):

```
raw records loaded           55731
  too_short                  18154
  contains_emoji             10831
  hindi_language               207
duplicates marked             1623     (1603 exact, 20 near)
  tier-1 dropped (0 hits)    15755     (2141 of those contentless)
  tier-2 status            skipped
RELEVANT (corpus)             7020
  pre_purchase                5336
  post_purchase                153
  mixed                       1531
```

**7,020 relevant against a 1,500–4,000 band.** The floor that used to fail at 1,235 now overshoots. Two of four exit criteria still hold (funnel printed and logged; exclusion rules unit-tested). The relevant-count criterion now fails in the other direction, and the rejected-pool audit is still outstanding. The emoji lever is larger than it has ever been — 10,831 documents excluded for an emoji after clearing the length gate — but at 7,020 relevant it is no longer a rescue; it is a question of whether the rule is *right*, which is what the audit is for. The designed tool for bringing 7,020 back into the band is tier-2, which has never run and only ever removes.

**Follow-up, 2026-08-24 — the same rebuild with Quora in the raw partition (`--force --no-tier2`):**

```
raw records loaded           55913
  too_short                  18154
  contains_emoji             10834
  hindi_language               207
duplicates marked             1625     (1603 exact, 22 near)
  tier-1 dropped (0 hits)    15825     (2141 of those contentless)
  tier-2 status            skipped
RELEVANT (corpus)             7127
  pre_purchase                5443
  post_purchase                153
  mixed                       1531
```

**7,127 relevant, of which 107 are Quora.** The 2026-08-21 figures (7,020 / 5,336 pre-purchase) were YouTube-only on the pre-purchase side. The +107 is 182 collected minus 3 emoji, 2 near-duplicates, and 70 zero-hit drops. Pre-purchase relevant is 5,336 YouTube + 107 Quora; YouTube is still 98.0% of that mix, so concentration is reduced, not gone. Eligible rose 26,539 → 26,718, matching Phase 2's audit. `--dry-run` now reports 7,127 docs, 4,596,915 tokens, **$1.27**, 23 free-tier days, 1,188 batches.

A `--force` run *without* `--no-tier2` was attempted first and is the first time the live path executed: 98 batches returned 200 (~1,960 documents classified in memory), then Groq's 20b TPD cap of 200,000 raised `RateLimitError` and the process exited 1 before `upsert_documents`. Persist is after triage, and triage has no checkpoint, so those classifications were discarded. The completed rebuild is the `--no-tier2` funnel above. **That defect is fixed as of 2026-08-26 — see §3.5.**

### 3.4 Tier-2 made resumable, and the audit given a tool — 2026-08-26

Two of Phase 3's four exit criteria were still open, and neither was waiting on a *decision*. Each was waiting on a piece of machinery that did not exist, so both were built.

**Tier-2 could not finish, for a structural reason rather than a quota one.** 7,127 survivors at 56 tokens each is 399k tokens against a 200k daily cap: a three-day job by design. But a run that classifies 3,500 documents and then exits without writing them starts from zero the next morning, so the stage was not slow — it was **non-convergent**, and it would have consumed every free day available without ever completing. The 2026-08-24 attempt is the evidence: 98 successful batches, ~1,960 classifications, all discarded by the 99th. Four changes make the run finish:

- **A `triage_cache` table, written after every batch rather than at the end.** The end is exactly where the last run never arrived. Keyed `(doc_id, model, prompt_version)`, so bumping the prompt invalidates verdicts instead of silently reusing answers to a different question.
- **The cache is read back at the start**, so day two classifies only what day one never reached. This is what converts three days of quota into a finished pass instead of three identical failed mornings.
- **A local TPD budget that stops before the breach**, matching the governor's posture everywhere else in the codebase: stopping on our own count is free, and letting the server answer 429 costs a request to learn the same thing.
- **A `RateLimitError` that ends the stage, not the process.** A 429 can still arrive — a second run the same day, or a cap lower than configured — and when it does, everything already classified is on disk and the build proceeds to persist.

**No foreign key ties `triage_cache` to `documents`, and that is the design rather than an omission.** Triage runs before the rebuild's insert, and `--force` deletes every `documents` row, so an FK would either reject the write or cascade the cache away — and outliving a `--force` rebuild is the entire purpose of the table. `doc_id` is derived from `(source, source_native_id)` and stable across rebuilds, so a verdict cached today is still about the same text tomorrow.

**Whatever tier-2 does not reach keeps its tier-1 verdict, and the funnel says how many.** Leaving those rows `is_relevant = NULL` would have been the quiet failure: the tagger skips untriaged rows, so the corpus size would silently depend on which batch the quota died in, with nothing on screen to show it. The funnel now prints classified / reused-from-cache / dropped / **not judged** / tokens spent, and a partial run says so in a `NOTE`. A partly-triaged corpus is a legitimate state; an *undisclosed* partly-triaged corpus is not.

**The rejected-pool audit now has a tool, and deliberately still needs a person.** `scripts/audit_rejected_pool.py --sample` draws a seeded, stratified worksheet to `outputs/rejected_pool_audit.jsonl`; `--score` reads the labels back, computes per-stratum rates, and writes `outputs/rejected_pool_audit.md`. The labelling itself ran on 2026-08-28 — see §3.6. Three design points are worth stating because each was a choice with an alternative:

- **Five strata, not one number.** The criterion asks for the three hard codes and triage scored separately because they fail differently and are fixed differently — a bad emoji rule is one config flag, a narrow vocabulary is an edit to a keyword file. The zero-hit drop is split further on the `min_content_words` distinction from §3.1: "about nothing" versus "about something else", where only the second is evidence the vocabulary is too narrow. The gate is applied per stratum, so a rule that is wrong half the time cannot hide behind four that are never wrong.
- **Equal allocation, not proportional.** A proportional draw of 50 would spend 46 slots on `too_short` and measure the emoji rule — the one with a config flag waiting on the answer — with two documents.
- **The rejecting stage is reconstructed, because no column records it.** `exclusion_reason` names the three hard rules; below them a zero `relevance_score` means tier 1 matched no keyword, while a non-zero score on a rejected row can only be tier 2, which sees a document only after the vocabulary matched.

**The first draw is on disk, and it already shows what the audit can and cannot settle.** Fifty documents across five strata (the sixth, `tier2_rejected`, is legitimately empty until tier 2 completes) from a rejected pool of 47,161. Ten per stratum resolves a rate only in steps of 10%, so a single false rejection puts a stratum exactly at the gate — enough to detect a *broken* rule, not enough to certify a good one at 9%. And the `too_short` draw came back with three identical one-word `Link` comments, which is the honest shape of that stratum: a random sample of it mostly measures boilerplate and will pass easily, without saying much about the short documents that carry signal. Both limits are printed in the report rather than left for a reader to work out, and the remedy for either is a larger `--per-stratum`.

**What remains open is applying the audit's corrections to the live table, not deciding whether to audit.** 7,127 sits above the 1,500–4,000 band. The band's floor is met. Its ceiling was a cost bound, and sampling closed it, so tier-2 is no longer required to "fit" tagging. The emoji rule's 10,834 exclusions were judged on 2026-08-28 and found too aggressive; the code now keeps a trailing emoji on a real comment. Until `--force --no-tier2` persists with tags in place, the funnel still describes the any-emoji corpus the tagger actually saw.

### 3.5 Encoding hardening, found in flight

Two bugs found while running this phase shared one cause — the host's legacy codepage deciding behavior — and were fixed together in `src/common/encoding.py`, one function per direction. Recorded here because both were found by running real data through the pipeline, and neither was anticipated in any phase's risk list.

**Input: a UTF-8 BOM read as UTF-8.** The first real manual AJIO import produced 3 records → 0, with four warnings that all blamed the file's *content*. The file was saved with a BOM, which decodes without error and glues an invisible U+FEFF onto the first line, so the `product:` directive stopped matching a pattern anchored at line start. This failure mode is dangerous precisely because nothing raises. `read_text_tolerant()` now decodes by BOM detection (UTF-8-sig, UTF-16 LE/BE, UTF-32) and all three readers of hand-saved files use it — the two manual collectors and `load_keywords()`. The keyword loader was the worst of the three: a BOM there silently disables the first keyword in the relevance vocabulary, with no warning anywhere, since `str.strip()` does not remove U+FEFF.

**Output: one character the console cannot encode.** The corpus build wrote and committed all 12,702 documents, printed the funnel, then died with `UnicodeEncodeError` on the `⚠` in its own pre-purchase warning — every stage's work done, exit code 1. `build_corpus` was the only entry point that never called `setup_logging()`, so its `stdout` kept cp1252. `harden_stdio()` now forces UTF-8 on `stdout` and `stderr`, `setup_logging()` calls it ahead of its own idempotence guard, and `run_tagging` — which had the same gap and no log file at all, on the stage designed to run for days — now opens a run log like the others. The warning marker is plain ASCII, because hardening means emitting UTF-8 bytes into a console that may still decode them as cp1252, and the one line an operator must not misread should not depend on the terminal.

`tests/test_encoding.py` covers both directions across five encodings and asserts the output-side bug *first*, so the fix is measured against a reproduced failure. `pytest` captures stdout through a UTF-8 object, which is exactly why the suite never caught this on its own.

### 3.6 Rejected-pool audit scored, filters narrowed, persist blocked — 2026-08-28

The fourth exit criterion is no longer "blocked on labelling". Fifty documents across five strata were labelled by reading the rows against each stratum's question (`outputs/_label_rejected_pool.py` records the verdicts; Groq was not asked). `--score` wrote `outputs/rejected_pool_audit.md`:

| Stratum | Wrong | Rate | Gate |
| --- | --- | --- | --- |
| `too_short` | 0/10 | 0% | PASS |
| `contains_emoji` | 5/10 | 50% | FAIL |
| `hindi_language` | 2/10 | 20% | FAIL |
| `tier1_zero_hits_contentful` | 1/10 | 10% | FAIL (gate is strictly below 10%) |
| `tier1_zero_hits_contentless` | 5/10 | 50% | FAIL |
| **All** | **13/50** | **26%** | **FAIL** |

False rejections that drove the code changes: size / COD / tailor-fit questions with a trailing emoji; romanized Hinglish with one `भी`; Devanagari only inside an `@handle`; "out of stock" / "still available" / offline-vs-online quality with zero keyword hits. The any-emoji worksheet is snapshotted at `outputs/rejected_pool_audit_v1_any_emoji.jsonl` so a later redraw does not pretend this FAIL never happened.

**Code, not the live table.** `emoji_is_the_substance()` excludes only when the remainder after stripping emoji is still below `min_words`. `is_hindi()` strips `@\S+` before the Devanagari test and treats Latin ≥ 24 letters with ≤ 4 Devanagari characters as incidental. `config/relevance_keywords.txt` gained `out of stock`, `sold out`, `in stock`, `still available`, `available`, `physical store`, `offline store`, `fabric`. Tests in `tests/test_exclusions.py` and `tests/test_relevance.py` pin those cases. `scripts/audit_collection.py` scores the cheap emoji gate the same way, so Phase 2's survival counts cannot drift from Phase 3.

**`--force` after tagging is a different bug from the 2026-08-24 triage loss.** The rebuild loaded 55,913 records and then died:

```
sqlite3.IntegrityError: FOREIGN KEY constraint failed
... build_corpus.py ... DELETE FROM documents
```

`doc_tags.doc_id REFERENCES documents(doc_id)` with `PRAGMA foreign_keys=ON`. `triage_cache` was designed without that FK so a wipe could proceed; tags were not. The 800 tags were never deleted — the DELETE did not commit — so the live table is still **7,127 relevant / 800 tags**. `src/common/db.py:replace_documents` rewrites rows in place (`ON CONFLICT DO UPDATE`) so a later `--force` can keep tags; `build_corpus.replace_document_rows` still issues `DELETE FROM documents` after suspending FKs and is what `--force` actually calls. Until a persist with the new rules succeeds, do not retag, do not wipe `data/interim`, and do not treat the 7,127 as having already absorbed the audit fixes.

**Band decision, recorded.** The floor of 1,500 is met. The ceiling of 4,000 was the tagging-cost bound that sampling already retired (800 tagged, three free-tier days). Tier-2 is not being run to force the corpus into that band.

---

## Phase 4 — Groq tagging engine

The core of the project, and where the constraints in §0 are paid for.

**Deliverables**

- `src/tag/taxonomy.py` (finalized), `src/tag/prompts/tagging_v1.md`, `src/tag/llm_client.py`, `src/tag/cache.py`, `src/tag/run_tagging.py`.
- `scripts/measure_token_overhead.py` — re-run 2026-08-21 against the production prompt and full taxonomy, on real corpus documents stratified by length. `TOKENS_PER_DOC` is 645, not a projection.
- `scripts/build_tag_sample.py` — `--target`, `--seed`, `--force`. Draws the sample the budget gate calls for into `tag_sample` and records the spec in `run_log`. Not part of the tagger: `run_tagging` reads the table if it holds rows and behaves exactly as before if it does not.
- `config/tag_cues.yaml` — per-tag cue lexicon backing the attribution screen.
- `tests/gold/gold_set.jsonl` — 100 hand-labeled documents, each label carrying the span that justifies it.
- `outputs/tagger_validation.md` — per-dimension precision/recall/F1 and kappa, **evidence precision and attribution accuracy** against the gold spans, and the cue screen's own error rates.

**Tasks**

1. **Prompt.** Write `tagging_v1.md` with the full taxonomy, one worked example per dimension, and a rule to return `intent_class: ambiguous` rather than guessing. Batch format: numbered documents in, `{"documents": [...]}` out — an array, since strict mode cannot express results keyed by `doc_id`. Because the schema already carries the structure, the prompt spends its budget on *label definitions and boundary cases* rather than formatting instructions. Two rules target attribution specifically, and both are worth their tokens: quote the **shortest span that by itself justifies** the tag, and if no span justifies it, **do not assert the tag**. Include the observed misattribution as a worked negative example — `size_unavailable` cited against *"This kurta has been in my wishlist for a month"* — since a real failure teaches the boundary better than an invented one.
2. **Client.** Wrap the `groq` SDK with `temperature=0`, pinned model, `seed`, `reasoning_effort="low"`, `include_reasoning=False`, and `response_format={"type": "json_schema", "json_schema": {"name": "document_tags", "strict": True, "schema": tagging_response_schema()}}` — the generator from Phase 1, which raises unless the schema is strict-compatible. Add a `RateLimitGovernor` that tracks RPM/TPM/RPD/TPD from `x-ratelimit-*` headers and sleeps preemptively. Retries via `tenacity` with jittered backoff honoring `retry-after`.
3. **Validation.** Schema validation covers shape only, so the semantic checks are the ladder in `architecture.md` §7.2: evidence exists (already enforced by `DocumentTags`), every quote is verbatim, and attribution is screened. A verbatim failure is repaired; everything else is flagged.
4. **Attribution screen.** Implement the three deterministic flags — `no_cue_overlap` against `config/tag_cues.yaml`, `quote_spans_document`, `quote_reused` — writing them to the tag row as `evidence_quality`. None of them rejects a tag: each is a screen with unknown error rates until §7.4 calibrates it against gold spans. They exist to route a human's attention and to down-weight scoring, not to decide truth.
5. **Branch on the 400 error code, not the status.** Verified against the live API: `json_validate_failed` is retryable, and covers both a truncated generation (empty `failed_generation`) and a schema violation in otherwise complete JSON. A 400 carrying `param` and `schema_path` instead means the schema itself is non-compliant and must abort the run. Conflating them either kills a multi-day run over one bad batch or spends a day's quota retrying a build error.
6. **Climb a repair ladder, never retry unchanged.** At `temperature=0` an identical request reproduces the identical violation, so a plain retry loops forever. Each attempt must change something: feed back the validator error, then halve the batch, then fall back to one document per call. Cap the attempts and leave the document untagged with a logged reason — a batch rejection already costs the tokens of all six documents in it, so an uncapped ladder is how a daily budget disappears (`edge-case.md` §4.2.11–4.2.12).
7. **Cache.** Check `llm_cache` per document before batching; only cache misses are assembled into requests. Store prompt, completion, and reasoning token counts per call for real cost reporting.
8. **Checkpointing.** Commit after every batch. `run_tagging.py --resume` picks up exactly where a 429 or a daily-limit stop left off — this is what makes a multi-day free-tier run practical.
9. **`--dry-run`** prints document count, estimated tokens, estimated cost, and estimated wall-clock time under current limits, without calling the API.
10. **Gold set, with spans.** Hand-label 100 documents stratified across sources before looking at model output, recording for each tag **the span that justifies it**, not just the tag. This is the only change that makes attribution measurable; a tag-only gold set can score whether a label was right but never whether the tagger had a reason for it. Budget roughly half a day — noticeably slower than labeling tags alone, and the reason Phase 4 is now 3 days.
11. **Score both families of metric.** Report per-dimension F1 *and* evidence precision against the gold spans, plus the cue lexicon's own precision/recall so the corpus-wide flag rate can be interpreted rather than merely counted.

**Exit criteria**

- ≥ 95% of the **selected** documents tagged and validated — the 800 in `tag_sample` while a sample is in force, the full relevant set when the table is absent. Whichever it is, both it and the 7,127 have to be quoted together, since a coverage figure against a sample says nothing about coverage of the corpus.
- Macro-F1 ≥ 0.65 on `blocker_type` against the gold set (the gate from `architecture.md` §11).
- **Evidence precision ≥ 0.80** against the gold spans. Deliberately a separate gate from F1: the probe already produced a case that would pass a shape check, pass a verbatim check, and still cite a quote that does not support its tag.
- A second full run over the same corpus issues **zero** Groq calls.
- Every stored evidence quote is a verbatim substring of its source text.
- Every tag row carries an `evidence_quality` value, so an unflagged attribution is distinguishable from one that was never screened.

**Decision gate — budget. Taken 2026-08-25: sample.** Run `--dry-run` before the full tagging pass. If the estimate exceeds ~2 days of free-tier capacity, either upgrade to the Developer tier or cap the corpus by sampling proportionally across sources. Do not silently truncate — record the sampling decision in `run_log` so the report's limitations section can state it. The dollar amount is whatever `--dry-run` prints; it is no longer hardcoded as "under $1". The recorded choice is the sampling one, at 800 documents; see "The budget gate is closed" below for why, and how the sample avoids being a silent truncation.

**Decision gate — repair on flag.** Measure the `no_cue_overlap` rate on the first ~200 documents before deciding whether a flagged attribution triggers a repair call. At a high flag rate, repairing every one costs more than the tagging pass itself; at a low rate it is nearly free. Decide from the measurement rather than committing now, and record the decision so the report can state whether flagged attributions were repaired or merely marked.

**Risks**

- *Reasoning-token overhead* — measured at ~285–350 tokens per call at `reasoning_effort=low`, close to the ~400 originally assumed and roughly fixed per call rather than per document. Re-measure after the full taxonomy prompt is written, since a longer prompt can invite longer reasoning.
- *Batch size vs. attention* — strict decoding removes malformed JSON as a concern, but large batches still degrade per-document labeling quality as later documents get less attention. Hold at 6 per call and validate against the gold set before raising it.
- *Tag inflation* — large models over-assign multi-label tags. Requiring a verbatim quote per tag is the strongest single constraint, and strict decoding does **not** enforce it. But the probe showed its limit: asked for a quote it did not have, the model supplied a real quote for the wrong reason, keeping the inflated tag and passing both checks. Requiring evidence relocates the failure from unsupported tags to misattributed quotes, so the countermeasure has to be measurement against gold spans (task 10) rather than another automatic check.
- *A screen mistaken for a guarantee* — the cue lexicon is the most likely thing here to be quietly over-trusted. It has both false positives and false negatives, so it is calibrated against gold spans before its output is interpreted, and it never rejects a tag on its own. If the flag rate is high, the honest reading is that the lexicon needs work, not that the tagger is broken.
- *Seed is best-effort on Groq* — bitwise-identical output across runs is not guaranteed. The cache is therefore the real reproducibility mechanism: once tagged, results are frozen on disk.

**Phase 4 sample tagged; gold set still absent.** All the deliverables exist except the gold set: `taxonomy.py`, `prompts/tagging_v1.md`, `llm_client.py` with the rate governor and the capped repair ladder, `cache.py`, `run_tagging.py`, `config/tag_cues.yaml`, and `tests/test_tagging_offline.py`. The 800-document sample was tagged: `doc_tags` = 800, `llm_cache` = 800. `--dry-run` on 2026-08-28 reports:

| | |
| --- | --- |
| relevant documents (selected) | **800** |
| already cached | **800** |
| to tag | **0** |
| batches | 0 |
| estimated tokens | **0** |
| cost, Developer tier | **$0.00** |
| free-tier days at 200k TPD | **0** |

A second `--resume` over this corpus issues zero Groq calls. That is the cache-gate exit criterion, and it holds.

The full-corpus dry-run (no `tag_sample`) remains the 2026-08-24 measurement below, kept so the sampling decision stays auditable:

| | |
| --- | --- |
| relevant documents | **7,127** |
| already cached | 0 (that dry-run predates tagging) |
| batches at 6 per call | 1,188 |
| estimated tokens | **4,596,915** |
| cost, Developer tier | **$1.27** |
| free-tier days at 200k TPD | **23** |

**The budget decision gate has fired on free-tier days, not on dollars.** 23 days exceeds the ~2-day threshold, so this phase cannot start on the free tier without recording a choice. At **$1.27** the paid tier is the obvious call, and it should be recorded in `run_log` rather than decided implicitly by whoever runs the command.

**The budget gate is closed — sampled to 800 documents, 2026-08-25, and tagged.** `scripts/build_tag_sample.py --target 800 --seed 42` wrote 800 doc_ids to `tag_sample`. The pre-tagging `--dry-run` read **800 documents / 134 batches / 516,000 tokens / $0.14 / 3 free-tier days**. After tagging, the same command reads **0 remaining**. What made the choice was the calendar rather than the money: $1.27 was never the obstacle, twenty-three days was, and the gate's third option — sample, and record it — is the only one that changes the schedule without changing the budget.

| Source | Taggable | Sampled | Share | Basis |
| --- | --- | --- | --- | --- |
| youtube | 5,336 | 420 | 7.9% | proportional |
| play_store | 1,194 | 94 | 7.9% | proportional |
| app_store | 337 | 26 | 7.7% | proportional |
| consumer_complaints_in | 148 | 148 | 100% | census |
| quora_manual | 107 | 107 | 100% | census |
| complaints_board | 5 | 5 | 100% | census |
| **Total** | **7,127** | **800** | **11.2%** | |

Four properties, each of which is a design decision rather than an outcome:

- **It is not a truncation, because the corpus is untouched.** No `documents` row changed: `is_relevant` and `is_duplicate_of` are exactly what Phase 3 wrote, and the sample lives only in `tag_sample`. The tempting alternative — flip `is_relevant` to 0 on the 6,327 documents the run cannot reach — would have written a *budget* decision into the column that records a *triage* decision, and no later reader could have separated "the triage judged this irrelevant" from "we ran out of days". `DROP TABLE tag_sample` restores the full job, which is the operational form of the same property.
- **The three small sources are taken whole, and that is the point of having two strata.** Sampling 260 documents proportionally would have saved ~230 tokens' worth of nothing while cutting `quora_manual` from 107 to about a dozen — the corpus's only hand-collected pre-purchase route, the one §2.1 spent the most effort filling, reduced to a number too small to say anything about. Census costs a rounding error and keeps it.
- **The proportional half reproduces the corpus mix rather than fixing it.** 7.9% / 7.9% / 7.7% across the three sampled sources means prevalence figures need no re-weighting step to be honest. It also means the YouTube concentration survives into the sample intact, which is deliberate: a draw that quietly rebalanced the mix would understate the monoculture Phase 6 is required to disclose.
- **The draw is reproducible, and the spec is on record.** Seed 42 over each source's doc_ids in sorted order, with the seed, target and per-source counts written to `run_log` under stage `tag_sample`. The sort is load-bearing rather than tidy: `random.sample` draws from a sequence, so leaving the order to SQLite would make a "seeded" sample change after a VACUUM. A report that says "we tagged 800 of 7,127" has to be able to say *which* 800, and this is what lets it.

One consequence for the exit criteria, stated rather than left implicit: **"≥ 95% of relevant documents tagged" is now measured against the 800, not the 7,127.** The two denominators must both appear wherever a coverage figure is quoted, because 800 of 7,127 is 11% of the corpus and no amount of tagging quality makes that read as full coverage.

**The recorded conclusion of "under three dollars" holds.** It looked broken only when the 26,718 *eligible* documents were billed as if they would all be tagged. Tagging bills *relevant* documents. 7,127 × 645 = 4.60M tokens = $1.27 at the measured 72/28 prompt/completion split. The $4.75 figure was the same unit error Phase 2 made with raw records vs. surviving documents, and it is retired.

Two things the gate did *not* decide, both now settled by the sample:

- **7,127 was above the Phase 3 band of 1,500–4,000; the tagged set is 800, below it.** The band's upper bound was a *cost* ceiling, and at $1.27 it had stopped binding — which is why the sample is sized at 800 rather than at 4,000. Coming in under the band's floor is a deliberate trade and the reason the census stratum exists: a proportional 800 would have been a thinner *sample of the same corpus*, whereas 800 with the small sources taken whole is a thinner sample that still contains every hand-collected pre-purchase document. The designed alternative was tier-2 triage, which has never completed and only ever removes; sampling is reversible and tier-2 is not.
- **1,188 batches exceeded the free-tier 1,000 RPD cap**, so even ignoring TPD a full free-tier run was two calendar days of requests. At 800 documents it is **134 batches**, comfortably inside the cap, so requests have stopped being a constraint on either tier. Raising `docs_per_request` to 12 would halve batches again and cut tokens/doc from 645 toward ~436 (sweep), but §4 still holds the batch at 6 until the gold set says quality survives — and with 134 batches there is no longer any pressure to pull that lever early.

**What the estimate rests on.** 516,000 is `800 × 645`, exactly as 4,596,915 was `7,127 × 645`; sampling changed the multiplier and nothing else. 645 was measured on 2026-08-21 by `scripts/measure_token_overhead.py` against `tagging_v1.md` and the full taxonomy schema, on real documents stratified by the corpus's own length distribution. It is no longer a projection. A mixed-sample batch at the same size cost 762 tokens/document; treat 645 as the mean and 762 as a plausible high draw.

One caveat the sample adds to that arithmetic: **645 is weighted for the corpus's length distribution, and the sample's is different.** The census stratum pulls in all 107 Quora answers, whose median is 78 words against a corpus median of 8, so the sample is longer per document than the corpus it was drawn from and 516k was more likely to run under than over. `TOKENS_PER_DOC` was left at 645 rather than re-weighted. The live `--dry-run` no longer estimates that job; it reports the cache as warm.

**Two of the six exit criteria are met; the rest wait on a gold set.** Coverage is 800 of 800 selected (100% of the sample, 11.2% of the 7,127). The cache gate holds: a second run issues zero Groq calls. Quotes stored on the 800 rows are the tagger's spans, not gold-set spans. `tests/gold/gold_set.jsonl` does not exist, and `outputs/tagger_validation.md` states that absence rather than inventing an F1 or a precision. Macro-F1 ≥ 0.65 and evidence precision ≥ 0.80 are therefore **not measured**. The gold set must still be labelled *before* looking at model output, and stratified over the sample rather than the corpus.

---

## Phase 5 — Quantification

**Objective.** Turn tags into defensible numbers.

**Deliverables**

- `src/quantify/metrics.py`, `cooccurrence.py`, `scoring.py`, `run_quantification.py`.
- `data/processed/tag_prevalence.csv`, `cooccurrence_lift.csv`, `segment_matrix.csv`, `opportunity_scores.csv`.
- `tests/test_scoring.py`.

**Tasks**

1. Author-level then document-level aggregation, per `architecture.md` §8.
2. Wilson 95% intervals on every prevalence; recency decay with a 12-month half-life; source-spread counts; and the pre/post-purchase supporting mix per candidate, with a `post_purchase_only` flag that keeps such clusters out of the top tier.
3. Compute all metrics twice — full corpus and the `intent_class = genuine_intent` subset — since the North Star metric only concerns real intent.
4. Co-occurrence lift matrices for blocker × uncertainty, blocker × segment, blocker × info-sought.
5. Cluster co-occurring tags into candidate opportunity areas and score them with the four-component formula.
6. Fold the **attribution factor** into `evidence_confidence`: the share of a cluster's supporting documents whose evidence carries no screen flag. A component rather than a filter, so a heavily flagged cluster stays visible and countable but cannot lead the ranking on volume alone.
7. Emit each score component as its own column so the ranking can be re-weighted by hand.

**What is not an input here.** The AJIO aggregates (§2.2) are never quantified. Every metric in this phase counts documents and authors, and an aggregate row is neither; it would arrive as a single high-agreement voice with no author to cap and no source to spread across. The comparison between AJIO's numbers and the corpus's happens once, in Phase 6, and in prose rather than in a score.

**Exit criteria**

- Every candidate opportunity carries prevalence, CI, mean severity, actionability, confidence, and supporting `doc_id` list.
- Unit tests confirm the score is monotonic in each component and that a price-only cluster scores near zero.
- Any tag with fewer than 20 supporting documents is flagged `low_confidence`.
- Each candidate reports its flagged-evidence share, so a cluster resting largely on weak attributions is visible as such rather than only expressed through a lower score.

**Ran 2026-08-28** (`run_log` stage `quantify`). Universe is the 800 tagged documents, not the 7,127 relevant. Genuine-intent subset is **108**. CSVs live in `data/processed/` and are copied into `outputs/` by synthesis. `tests/test_scoring.py`, `tests/test_quantify.py`, and `tests/test_cooccurrence.py` cover the unit gates. Attribution factor is the deterministic screen's unflagged share, not gold-set evidence precision.

---

## Phase 6 — Synthesis

**Objective.** The deliverable report.

**Deliverables**

- `src/synthesize/evidence.py`, `report.py`, `run_synthesis.py`, plus Jinja2 templates.
- `src/synthesize/ajio_aggregates.py` — **built and tested 2026-08-23**, ahead of the rest of the phase because its input arrived early (§2.2). Renders the AJIO aggregate section and cross-references it against the ranked themes.
- `src/synthesize/limitations.py` — **built and tested 2026-08-24**, for the same reason. Renders the Limitations section; takes corpus-derived caveats as input and always appends the hand-collection paragraph last. Product count and snapshot date range are read from the records.
- `src/synthesize/run_log_appendix.py` — pipeline token/wall-clock appendix, appended to the evidence file.
- `outputs/opportunity_report.md`, `outputs/evidence_appendix.md`, `outputs/opportunity_scores.csv`.

**Tasks**

1. Corpus summary section: counts by source **and the source mix**, **pre/post-purchase split**, date range, funnel yield by exclusion reason code, tagger F1, and **measured evidence precision** — the latter because it tells a reader how much weight the report's quotes can bear. It must also print **both tagging denominators** whenever a sample is in force — 7,127 relevant, 800 tagged, with the per-source draw — because every prevalence figure in the report is computed over the tagged set and a reader who assumes it covers the corpus is out by a factor of nine. The mix has to name the YouTube concentration (21,783 of 21,962 pre-purchase documents from one platform as of Phase 2; 5,336 of 5,443 relevant pre-purchase after the 2026-08-24 rebuild) rather than burying it in a total: "identify, quantify, compare" is only credible if Part 1 discloses the monoculture and shows what the two manual routes did to break it. Volume from AJIO/Quora will not match YouTube and does not need to; diversity is the point.
2. Ranked opportunity areas, each with score components, prevalence and CI, affected segments, defining co-occurrences, and 3–5 verbatim quotes with source URLs. Quotes are selected by proximity to cluster centroid plus highest severity — never hand-picked — and drawn only from evidence carrying no screen flag, so a reader is never shown a passage that does not say what it is cited for.
3. Answer all ten discovery questions from `problemStatement.md` with numbers and citations, one subsection each.
4. **AJIO aggregate section** (`ajio_aggregates.render_section`): the §2.2 side-channel, with the two disclosures that are mandatory rather than stylistic — whose numbers these are (AJIO-computed, from buyers answering its own prompts, therefore post-purchase and self-selected, therefore corroborating rather than establishing), and where any average came from (reported or derived). Percentages are quoted as percentages with their product count, never as a review-like sentence, and every share carries its own denominator because AJIO answers the fit prompt on 47 products and the quality prompt on 51. A theme AJIO asks nothing about is printed as *not corroborated* rather than dropped, so silence cannot pass for a number withheld; with no aggregates loaded at all the section states that the text corpus is the sole evidence base. The cross-reference resolves both taxonomy values and human labels, and imports the taxonomy enums so a rename fails a test here instead of quietly ending the comparison.
5. Segment-divergence section: where a segment's prevalence departs materially from the corpus baseline.
6. **Excluded-by-constraint section:** price-driven findings with their volumes, shown rather than hidden, so a reader can see what the no-incentives rule removed.
7. Limitations (`limitations.render_section`): source self-selection, review-extremity bias, English/Hinglish skew, the fact that public conversation over-represents strong opinions, YouTube haul/influencer framing of the pre-purchase majority, the corpus's post-purchase tilt among non-YouTube sources, robots-restricted sources that yielded nothing (Trustpilot), Quora's manual-only sample — its 10 threads, the 71 of 204 answers Quora served truncated, and its missing authors and timestamps — AJIO's Akamai block and the absence of any on-site prose behind it, the aggregate side-channel's derived averages and its method-but-not-command reproducibility, **the Phase 4 sample — 800 of 7,127, its seed and target, which sources were censused and which drawn proportionally, read from `run_log` rather than written down** — and the measured attribution error rate, stating plainly that tags are machine-assigned and a known share rest on a quote a human would not have chosen. The hand-collection paragraph is always last, and its product count and snapshot date range are read from the records so neither can go stale silently.

**Exit criteria**

- Report generates end to end from CSVs with no manual editing.
- Every quantitative claim links to source documents.
- The ten discovery questions are each answered with at least one number.
- No AJIO aggregate figure appears anywhere outside its own section, and no figure derived from a document is mixed with one derived from an aggregate.

**Rendered 2026-08-28** (`run_log` stage `synthesize`). `outputs/opportunity_report.md` has all seven sections: corpus summary (both tagging denominators, YouTube 98% of relevant pre-purchase), ranked opportunities, ten discovery questions with counts and `doc_id` citations, AJIO aggregates, segments, excluded-by-constraint, and limitations. Tagger F1 and evidence precision are printed as **not measured**. Do not read the quotes as gold-set-validated spans.

---

## Phase 7 — Hardening and full run

**Tasks**

1. Run the complete pipeline from clean `data/interim` and `outputs`.
2. Verify the reproducibility gate: two runs from identical raw data and config produce identical `opportunity_scores.csv`.
3. Confirm the cache gate: the second tagging run costs zero tokens.
4. Write `README.md` — setup, credentials, run order, expected runtime, cost, and the note that Python is invoked via `.venv\Scripts\python.exe` on this machine.
5. Record actual token spend and wall-clock time per stage in `run_log` and surface it in the report appendix.
6. Re-pin `requirements.txt` from `pip freeze` if any dependency moved during the build.
7. Settle the two hand-collected inputs. The Quora import is in `data/raw` (182 records, 2026-08-24) and in `discovery.db` (107 relevant after the same-day rebuild). The AJIO aggregate half is done: the grabber is committed and the limitations paragraph in `src/synthesize/limitations.py` states that the input is method-reproducible but not command-reproducible, so the reproducibility gate is satisfied by disclosure rather than by a rebuild (§2.2).

**Exit criteria**

- `pytest tests/` green.
- All quality gates in `architecture.md` §11 pass.
- A fresh clone plus `.env` plus the commands below reproduces the report.

**Status 2026-08-28.** `README.md` documents setup, credentials, run order, and the venv interpreter. `scripts/verify_hardening.py` checks the cache gate and two in-memory quantify passes without wiping data; `--dry-run` remaining is 0, so the cache gate holds. `tests/test_hardening.py` asserts identical `opportunity_scores.csv` from two quantify runs. The evidence appendix includes the pipeline run-log section. **Not done:** a clean wipe of `data/interim` (that would delete `llm_cache` and `doc_tags`); architecture §11's gold-set F1 and evidence-precision gates, which cannot pass or fail until a gold set exists.

---

## Phase 8 — Read-only explorer

**Objective.** A reviewer can explore already-computed Stage 4/5 outputs without re-running collection, tagging, or quantification. Visual language matches the Stitch screens (`stitch_ajio_intelligence_engine/`: Overview, Opportunity Map, Opportunity Detail, Evidence Explorer, Ask the Engine), plus Segments and AJIO Corroboration which the mocks named in the sidebar but did not ship as HTML.

**What it is not.** Not a chatbot, agent, or a second tagging run. Ask is one optional Groq call over `opportunity_scores.csv` and the evidence appendix. Mock KPI values in the Stitch HTML (12,480 conversations, Reddit-heavy source mix, 0–100 scores) are placeholders and must not appear.

**Tasks**

1. `app/explorer.py` + `app/ui.py` + `app/data.py` read `data/processed/` (falling back to `outputs/`) and the tagged corpus read-only.
2. Query-param navigation: Overview, Opportunity Map, theme detail, Evidence, Segments, AJIO Corroboration, Ask. Full-corpus vs genuine-intent is a top-bar toggle, not a re-score.
3. Pin `streamlit` in `requirements.txt`. Launch: `.venv\Scripts\python.exe -m streamlit run app/explorer.py`.

**Exit criteria**

- Explorer starts without `get_settings()` / YouTube / `HASH_SALT`.
- Ask stays disabled until `GROQ_API_KEY` is present; other screens still work.
- `supporting_doc_ids` never leave the machine bound for Groq.
- Loader tests in `tests/test_explorer_data.py` pass.

**Verified 2026-08-28.** Sidebar buttons and the Full Corpus / Genuine Intent toggle write query params and rerun — HTML `<a href="?page=">` from the Stitch mocks does not drive Streamlit, so it is not the navigation. Ask raises until `GROQ_API_KEY` is set; Overview / Map / Evidence still load. `compact_score_rows` strips `supporting_doc_ids` before `build_snapshot`. AppTest confirms a sidebar click lands on Opportunity Map. **488 tests collected** as of 2026-08-28 (482 at explorer verification; Phase 3 audit-fix tests added later). Launch: `.venv\Scripts\python.exe -m streamlit run app/explorer.py`.

---

## Execution sequence

On this machine Python is invoked as `.venv\Scripts\python.exe` (3.12.7).

```bash
python scripts/check_credentials.py
python scripts/verify_sources.py                # one live page per configured URL, before collecting
python -m src.collect.manual                    # loud check of the hand-collected dirs; no network
python -m src.collect.run_collection            # all enabled sources; --force to re-collect
python scripts/audit_collection.py              # scores Phase 2's exit criteria; --no-language-check to skip langdetect
python -m src.store.build_corpus                # --no-tier2 for a fully offline build
python -m scripts.audit_rejected_pool           # draws the 50-doc audit; --score once labelled
python -m src.tag.run_tagging --dry-run         # budget gate; no key needed
python -m scripts.build_tag_sample --target 800 # optional: narrow the tagging job; --force to redraw
python -m src.tag.run_tagging --resume
python -m src.quantify.run_quantification
python -m src.synthesize.run_synthesis
.venv\Scripts\python.exe -m streamlit run app/explorer.py   # optional: review frozen outputs; does not collect, tag, or score
```

Stages through synthesis have been run against real data. Collection and the 2026-08-24 `--force --no-tier2` rebuild folded Quora into `discovery.db` (**7,127 relevant**). `build_tag_sample` ran on 2026-08-25 (800 doc_ids). Tagging, quantification, and synthesis ran over that sample: `doc_tags` = `llm_cache` = 800, CSVs in `data/processed/`, report in `outputs/opportunity_report.md`. `--dry-run` remaining is 0. `audit_rejected_pool` was labelled and scored on 2026-08-28: **FAIL** (13/50) on the any-emoji table; a `--force` persist of the narrowed filters died on the `doc_tags` foreign key, so the live funnel is still 2026-08-24.

`build_corpus` without `--no-tier2` is safe to interrupt (`triage_cache`), and is **not** the next command: do not start the three-day triage pass unless that decision is taken explicitly. `--force --no-tier2` after tagging must keep `doc_tags`; the 2026-08-28 DELETE with FKs on did not.

The Quora import is in both `data/raw` and `discovery.db`. Re-running `run_collection` without `--sources` would re-collect all eight sources, because none has a manifest for today's date — the date partition working as designed, but ~55,000 records and a YouTube quota bill for a change that is already in the corpus. Do not wipe `data/interim`.

---

## Consolidated risk register

| Risk | Impact | Mitigation | Phase |
| --- | --- | --- | --- |
| Free-tier TPD caps the corpus | Weak segment-level claims | Batching + 20b triage cascade + resumable runs; paid tier is **$1.27** for 7,127 relevant documents (re-measured 2026-08-24). The "under $1" figure was a 1,235-document projection and is retired. **Materialized as time rather than money, and resolved by sampling:** 23 free-tier days at 7,127 documents, 3 at the 800 in `tag_sample` (2026-08-25) | 0, 4 |
| A multi-day LLM stage that restarts each morning | Never converges, and burns the free quota proving it — tier-2 lost ~1,960 classifications this way on 2026-08-24 | **Materialized and fixed 2026-08-26:** `triage_cache` is written per batch and read back on the next run, the stage stops before the local TPD budget rather than absorbing a 429, and a rate limit ends the stage instead of the process. Unreached documents keep their tier-1 verdict and the funnel prints how many, so a partial pass cannot be mistaken for a complete one | 3 |
| An over-aggressive filter deletes the finding | Undetectable downstream: the corpus looks plausible, the finding is simply absent | **Materialized 2026-08-28:** rejected-pool audit **FAIL** (13/50). Emoji remainder-too-short, incidental Hindi, and stock keywords are in code; the live table is still the any-emoji corpus because `--force` died on `doc_tags` FK (§3.6). Ten documents per stratum still resolve a rate only in steps of 10% | 3 |
| `--force` rebuild after tagging | Forty minutes of exclusions, then `IntegrityError` on `DELETE FROM documents`; tags were kept only because the DELETE did not commit | `replace_documents` in `db.py` rewrites in place. `--force` still goes through `replace_document_rows`, which suspends FKs then DELETE. Do not wipe `data/interim` | 3, 4 |
| The tagged sample read as the whole corpus | Every prevalence figure silently rescaled — 11% coverage quoted as if it were 100% | The sample is a side table, so the funnel still reports 7,127 and nothing overwrote `is_relevant`; the seed, target and per-source counts are in `run_log` under stage `tag_sample`; tests assert no `documents` row changes when a sample is built. The remaining exposure is *narrative* rather than structural, which is why Phase 6's corpus summary is required to print both denominators and the limitations section to name the sample | 4, 6 |
| Groq retires a pinned model mid-project | Pipeline stops running | `check_credentials.py` verifies both pinned models against `models.list()` on every run; `qwen/qwen3.6-27b` documented as the triage fallback | 0, 4 |
| Strict schema rejected with a 400 | Tagging cannot start | `tagging_response_schema()` raises at startup, `tests/test_strict_schema.py` gates every rule at build time, and `check_credentials.py` round-trips the real schema live | 1, 4 |
| Schema violations recur despite `strict: true` | Tokens burned on rejected batches | Every enumerable field is an enum and no dimension has a stray sentinel, which removed all three observed violations; the repair ladder is capped so a pathological document cannot drain the daily budget | 1, 4 |
| Near-duplicate threshold mis-set | Either duplicates inflate prevalence or distinct complaints get merged | Calibrated to measured distances rather than convention (12, not 3), with the margin asserted in tests and re-checked against the real corpus in Phase 3 | 1, 3 |
| Reasoning tokens inflate spend | Budget overrun on free tier | `reasoning_effort=low`, `include_reasoning=false`, and re-estimate from measured usage | 4 |
| YouTube quota exhaustion | Thin comment corpus | Cache video ids; spend quota on 1-unit comment calls | 2 |
| Corpus skews post-purchase after dropping Reddit | Engine discovers delivery/refund complaints instead of wishlist blockers | **Resolved, and replaced by a narrower risk.** Widening the YouTube terms and lowering the length gate took pre-purchase from 180 to 21,783 documents, 82% of the corpus; `mixed` app reviews fell from 73% to 17%. The floors are now stated in surviving documents so the gate cannot pass on records the next stage deletes (§3.3). What remains is **concentration, not skew**: 21,783 of 21,962 pre-purchase documents still come from one platform, so YouTube-specific bias now propagates to almost the whole pre-purchase claim. The only mitigation available was the manual route, and it has now produced 204 Quora answers on wishlist-intent threads — long, deliberative, and the first non-YouTube pre-purchase evidence in the project. 107 of them are relevant in `discovery.db` (2026-08-24 rebuild); 107 against 5,336 YouTube relevant pre-purchase makes them a diversity check rather than a counterweight, so the disclosure in limitations is owed either way | 2, 5, 6 |
| An aggregate row counted as a document | A crowd weighted as one voice, inflating whatever it agreed with, with nothing in the funnel to show it | AJIO's published rating and fit/quality percentages are real evidence and arrived with no natural home, which is what makes this tempting. `ajio_aggregate` is absent from every source registry, config block and stage map, `paths.aggregates_dir` is outside `ensure_dirs()`, and the wall is asserted on the registries *and* on the import graph of both modules, because the way it breaks is a convenience import added months later. The numbers reach the report in one section, labelled as AJIO's, with derived averages marked as derived (§2.2) | 2, 5, 6 |
| A hand-collected input has no versioned tool | The corpus cannot be reproduced from a fresh clone, only re-collected by hand | **Materialized, then resolved.** The 51 aggregate files were grabbed with a script that lived nowhere; `scripts/manual_extract/ajio_bars.js` and its bookmarklet build now sit beside the two prose extractors, so the procedure is reviewable. The residue is not fixable by tooling — a logged-in browser over a human product selection, against counts that move daily — so it is disclosed instead: **method-reproducible, not command-reproducible**, stated in the limitations section with the snapshot range read from the records. The files were never at risk, since the reader validates every one and refuses anything unattributable | 2, 6, 7 |
| A record's own text breaks the JSONL line contract | Records silently lost, attributable to nothing but a number in the funnel | **Materialized: one YouTube comment used U+2028, which `splitlines()` breaks on and no JSON serializer escapes, so it read back as six malformed lines.** Fixed at both boundaries — `clean_text` folds all eight such characters to `\n`, `RawWriter` escapes any that reach it regardless, and both readers split on newlines alone. Found by the exit criterion that had never been run against the corpus, which is the argument for `scripts/audit_collection.py` | 2, 3 |
| A corpus claim scored by hand goes stale | Decisions taken against numbers that stopped being true | **Materialized: this document carried 12,702 raw records while 55,731 were on disk, and reported a floor as passed that the next stage undid.** Every Phase 2 criterion is now computed from the part files by `scripts/audit_collection.py`, which exits non-zero when one is unmet | 2 |
| AJIO on-site Q&A blocked by Akamai bot management | Loses the single best pre-purchase source | Browser-grade headers are refused; the supported fallback is a hand-collected sample via `ajio_manual` into `data/manual/ajio/`. Defeating the bot management is out of scope (`edge-case.md` §1.1.13d) | 2 |
| Scraper selectors break on site redesign | Silent zero-yield source | Fail loudly when a page parses to zero items; per-source minimum record assertions | 2 |
| Over-aggressive relevance filter | Silently drops the real finding | **Materialized twice:** first `runs small` vs `run small`; then the 2026-08-28 audit's stock/availability and fabric misses. Bare-verb forms and those terms are in `relevance_keywords.txt`. `tests/test_relevance.py` asserts the chain, not each stage | 3 |
| Hard exclusions shrink the corpus below usable size | Weak or unstable prevalence estimates | **Materialized, and the rule was wrong rather than merely tight.** The 8-word gate is now 3: 26,718 documents survive against a 1,500 floor. The remaining lever — 10,834 emoji drops after the length gate — was audited 2026-08-28 at 50% false rejections in that stratum. Code now keeps a trailing emoji on a real comment; the live funnel still prints 10,834 until persist succeeds (§3.6) | 2, 3 |
| Host encoding silently changes behavior | A hand-saved file parses to nothing and blames its own content; a finished stage exits non-zero on one printable character | Both directions fixed in `src/common/encoding.py`: BOM-aware decoding for every hand-saved file, UTF-8 forced on `stdout`/`stderr` at each entry point. `tests/test_encoding.py` asserts each bug before its fix, since `pytest`'s UTF-8 capture hides the output-side failure | 0, 2, 3 |
| LLM tag inflation | Inflated prevalence | Mandatory verbatim quote per tag; gold-set F1 gate | 4 |
| Verbatim-but-irrelevant quotes | A report full of confident, unfalsifiable quotes — the exact failure the pipeline exists to prevent | Observed live on the Phase 1 probe. Gold set labels justifying spans, evidence precision gated at 0.80 separately from F1, deterministic attribution screen down-weights `evidence_confidence`, and illustrative quotes in the report are drawn only from unflagged evidence | 4, 5, 6 |
| Source bias (reviews skew negative) | Distorted prevalence | Source-spread factor in scoring; stated in limitations | 5, 6 |
| Sparse tags read as findings | Overconfident conclusions | Wilson intervals; `low_confidence` flag under n=20 | 5 |
