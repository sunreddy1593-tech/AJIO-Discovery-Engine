# Edge Cases and Corner Scenarios

Companion to `architecture.md` and `implementation-plan.md`. Each case states the scenario, why it matters, and the required handling. Anything marked **P0** silently corrupts results or halts a multi-day run and must be handled before the first full pass; **P1** degrades quality; **P2** is cosmetic or rare.

A guiding rule for the whole pipeline: **fail loudly on structural problems, degrade gracefully on data problems.** A malformed config should crash at startup. A single unparseable review should be logged, skipped, and counted — never crash a run that has been going for three days.

---

## 0. Cross-cutting

| # | Scenario | Priority | Handling |
| --- | --- | --- | --- |
| 0.1 | **Windows console cannot print non-ASCII** — PowerShell defaults to cp1252, so logging a review containing emoji or Devanagari raises `UnicodeEncodeError` and kills the run | **P0** | Force UTF-8 on all I/O: `PYTHONIOENCODING=utf-8`, `encoding="utf-8"` on every `open()`, and a logging handler that replaces unencodable characters. Never log raw document text at INFO level — log `doc_id` instead |
| 0.2 | **SQLite locked** by a second process or an open DB browser | **P0** | WAL mode, `timeout=30`, and a single-writer discipline: only one stage writes at a time. Detect `database is locked` and fail with a message naming the likely cause |
| 0.3 | **Run interrupted mid-write** (Ctrl-C, laptop sleep, 3-day run) | **P0** | Every DB write inside a transaction committed per batch. JSONL writers flush per record and the reader tolerates a truncated final line |
| 0.4 | **Config changed between stages** — collection ran under one `config_hash`, tagging under another | P1 | Stamp `config_hash` in `run_log` per stage; `run_synthesis` warns loudly if stages disagree, and the report prints the hashes it was built from |
| 0.5 | **Secrets leak into logs or outputs** | **P0** | Settings object marks credential fields with pydantic `SecretStr`; a test asserts no `.env` value appears in `logs/` or `outputs/` |
| 0.6 | **Naive vs. aware datetimes** mixing across sources | P1 | Normalize everything to UTC ISO-8601 at the collector boundary. Reject naive datetimes in `Document` validation |
| 0.7 | **Clock skew / re-running on the same date** overwrites a raw partition | P1 | Raw path includes `run_date`; a second run the same day appends `part-001.jsonl` rather than overwriting `part-000.jsonl` |

---

## 1. Collection

### 1.1 Source-specific failures

| # | Scenario | Priority | Handling |
| --- | --- | --- | --- |
| 1.1.1 | **YouTube quota exhausted mid-run** (403 `quotaExceeded`, 10,000 units/day) | **P0** | Catch the specific reason string, write the manifest for what was collected, exit 0 with a "resume tomorrow" message. Never treat as a generic 403 |
| 1.1.2 | **Comments disabled on a video** (403 `commentsDisabled`) | P1 | Skip that video, log it, continue. A per-video failure must not abort the source |
| 1.1.3 | **App Store RSS caps at ~500 reviews** (10 pages × 50) | P1 | Known ceiling; do not retry past it. Record the cap in the manifest so the report's limitations section can cite it |
| 1.1.4 | **Play Store continuation token expires or loops** | P1 | Track seen review ids; stop when a page yields zero new ids, and cap total pages |
| 1.1.5 | **robots.txt disallows the path** | **P0** | Check before fetching and skip. This is a compliance requirement, not an optimization |
| 1.1.6 | **robots.txt itself is unreachable** (404, 403, timeout) | **P0** | 404 means no restrictions and crawling may proceed; **403 or a timeout means treat as disallowed**. Fail closed, since a site actively blocking the policy file is not inviting automated access |
| 1.1.7 | **Scraped page parses to zero records** — the signature of a site redesign | **P0** | Raise rather than log-and-continue. A silently empty source looks identical to a genuinely quiet one, and the corpus would be missing a whole population without anyone noticing |
| 1.1.8 | **Cloudflare interstitial or captcha returned with HTTP 200** | **P0** | Detect challenge markers in the body, not just the status code. Treating a challenge page as content pollutes the corpus with boilerplate that will be tagged as real user voice |
| 1.1.9 | **Listing pagination loops or repeats** on complaint boards | P1 | Track seen record ids per domain; stop when a page yields zero new ids |
| 1.1.10 | **Trustpilot review paths are robots-disallowed** | P1 | Expected, not a bug. Collector logs the restriction and yields nothing; the report's limitations section states it |
| 1.1.11 | **MouthShut content signal** says `ai-train=no, use=reference` | **P0** | We classify and quote (reference) and never fine-tune a model, which is within the signal. Do not add any training or embedding-persistence step for this source without revisiting the terms |
| 1.1.12 | **Quora manual import gets used as a crawl target** | **P0** | `quora_manual.py` and the shared loader `manual.py` must contain no HTTP client at all. Tests assert both modules import no network library, since the compliance guarantee is only as strong as the code. The extract snippet that reads a page the human already opened lives under `scripts/manual_extract/`, not here |
| 1.1.13 | **AJIO returns 403 to non-browser clients** — an Akamai edge refuses every content path, including the sitemaps | **P0** | Browser-grade `User-Agent` plus a persistent session with realistic headers — already sent, and still refused as of the 2026-08-19 probe. `robots.txt` itself now returns 200 with those headers, which narrows the diagnosis: this is bot management, not policy, since `robots.txt` allows `/p/` and `/c/` for every agent. Escalate to a browser-driven fetch or a hand-collected sample. Do **not** silently proceed without the corpus's best pre-purchase source — though 1.1.13f closed both escalations: there is no on-site prose behind the block for either to reach |
| 1.1.13b | **AJIO's `robots.txt` disallows `/api/*`** | **P0** | The JSON review and Q&A endpoints are therefore unreachable by policy, not just unverified. `review_api_template` and `qa_api_template` ship empty; a template pointing into `/api/` is refused before the first byte, and the collector disables it after one refusal rather than re-requesting a forbidden URL once per product |
| 1.1.13c | **A configured listing URL returns 200 with the wrong page** | **P0** | The failure the zero-parse tripwire cannot diagnose: status says healthy, parse says empty, and the cause reads as a broken selector. Observed live — the configured MouthShut listing served an unrelated restaurants category page. `scripts/verify_sources.py` fetches one page per configured listing and reports the page title alongside the parsed-item count, which is what separates a wrong URL from a wrong selector |
| 1.1.13d | **The hand-collected AJIO sample needs somewhere to land** | **P0** | 1.1.13's fallback is only real if there is a collector for it. `src/collect/manual.py` is the shared loader: it scans `data/manual/ajio` (and the Quora dir) for `.json`/`.jsonl`/`.txt`/`.md`, skips README, normalizes to `{id, source, url, text, author, timestamp}`, and raises `EmptyImportError` on a zero-doc dir — including files that exist but do not parse. `ajio_manual` is a thin wrap around that loader. Like `quora_manual` it imports no HTTP client — additionally forbidden from importing `ajio_onsite`, which owns a `PoliteSession`. JSON from a bookmarklet or from Playwright attached over CDP to an already-open Chrome is the intended fill path (`scripts/manual_extract/`); Collect does not import that helper, and Playwright is not in `requirements.txt`. Spawning headless Chromium is the fingerprint Akamai blocks and stays out of scope. Markdown is still accepted: content type comes from an explicit `## Q&A` / `## Reviews` header and is **never** inferred from the prose (1.1.14); product id must be declared with a `product:` line or `/p/<id>` URL, with no filename fallback (1.2.8). **Superseded in effect by 1.1.13f:** the landing place is correct code and stays, but AJIO publishes no on-site prose to land in it, so `ajio_manual` is disabled rather than awaiting a person |
| 1.1.13e | **Hand-collected questions are shorter than the word gate** | P1 → **resolved by fixing the gate** | *"Does this run small?"* is four words, so a large share of the richest pre-purchase content was excluded by the 8-word rule before tagging. This was originally filed as a cost to absorb — "the rule stands", with the shortfall message warning that hand-collection had to overshoot. That was the wrong conclusion: a filter that removes the clearest example of the thing being measured is not tight, it is wrong. **The gate is now 3 words (3.1.6)** and the question survives. The residual case is a human *abbreviating* to "size?", which the collector READMEs now address |
| 1.1.13f | **There is no on-site free text to hand-collect** — AJIO publishes no customer review prose and no Q&A anywhere on the site | **P0, materialized** | Found 2026-08-22 by browsing the site the way 1.1.13d assumed a person would: a product page carries aggregate star-rating bars and fit/quality percentage breakdowns, and no customer prose at all. So 1.1.13's escalation was never available — the Akamai block and the absence are two walls in front of the same empty room, and defeating the first would have reached nothing behind it. `ajio_manual` is therefore `enabled: false` in `config.yaml`, which drops it out of the source-coverage denominator because the audit scores `enabled_sources()` rather than a list of its own; the run summary prints it as *disabled in config* rather than *not run*, and `python -m src.collect.manual` prints `OFF` rather than `FAIL`, so neither report reads as an unfilled directory someone still owes. Nothing is deleted — loader, collector, fixtures, `SOURCE_STAGE`, `KNOWN_SOURCES` and `STAGE_BY_CONTENT_TYPE` are untouched — so this is one flag to flip if AJIO ever publishes review text |
| 1.1.14 | **AJIO Q&A and reviews conflated** | **P0** | `meta.content_type` distinguishes `qa` from `review`. Merging them destroys the pre/post-purchase split that the whole analysis depends on. Both AJIO sources share one `STAGE_BY_CONTENT_TYPE` mapping so the scraped and hand-collected paths cannot drift apart on it |
| 1.1.15 | **AJIO product URLs go stale** — discontinued products 404 | P1 | Skip and count. Resolve product ids from `category_urls` at run time rather than relying on a hand-maintained URL list |
| 1.1.16 | **Reddit re-enabled without credentials** | P1 | `enabled: true` with absent `REDDIT_*` keys fails the credential check with a message naming both the flag and the keys |
| 1.1.17 | **Reddit deleted content and authors**, `MoreComments` objects | P1 | Retained in the collector for when it is re-enabled: drop `[deleted]`/`[removed]` bodies, use a `__deleted__` author sentinel, and call `replace_more(limit=0)` |
| 1.1.18 | **One malformed record discards its whole file** in a manual import | **P0** | The JSON branch of `load_dir` built its documents in a single comprehension over `document_from_mapping`, so the file-level `except` caught one record's `ValueError` — an AJIO record with no resolvable product id, say — and threw away every good record beside it; in a one-file directory that surfaced as `EmptyImportError` for the whole route, which reads as "nothing was collected" rather than "one record was bad". Each record is validated on its own now, and a failure is named in `warnings` and skipped. `document_from_mapping` still raises per record: the blast radius belonged to the caller, not to the validator. Three invariants have to survive that fix and are tested — an all-bad file still yields zero documents and lands in `files_skipped`, a directory yielding zero documents across all files still raises `EmptyImportError`, and structurally invalid top-level JSON still skips the whole file, because there are no records in it to salvage |

### 1.2 Data shape

| # | Scenario | Priority | Handling |
| --- | --- | --- | --- |
| 1.2.1 | **Empty or whitespace-only text** | **P0** | Reject at `RawRecord` validation with a counted reason, before it reaches storage |
| 1.2.2 | **Missing timestamp** | P1 | Store `NULL`; recency weighting treats it as the corpus median rather than assuming "now", which would wrongly boost it |
| 1.2.3 | **Timestamp in the future** (device clock skew in app reviews) | P1 | Clamp to `collected_at`, flag in `meta` |
| 1.2.4 | **Extremely long document** — a 10,000-character Reddit post | **P0** | Truncate to a configured token budget *for tagging only*, keeping the full text in `documents.text`. An untruncated outlier can single-handedly blow the 8,000 TPM ceiling |
| 1.2.5 | **HTML entities and markdown artifacts** (`&amp;`, `&gt;`, `>` quote blocks, zero-width spaces) | P1 | Unescape and normalize at the collector boundary; strip Reddit quote lines (`^>`) since they duplicate parent text |
| 1.2.6 | **Mojibake / mixed encodings** | P1 | Force UTF-8 decode with `errors="replace"`; documents exceeding a replacement-character ratio are dropped as corrupt |
| 1.2.7 | **Pagination overlap produces duplicates within one run** | P1 | Collector keeps an in-run seen-id set; the DB unique constraint is the backstop, not the primary defense |
| 1.2.8 | **Manually saved Quora file has no stable id** | **P0** | Derive `source_native_id` from a hash of the file's normalized content, not its filename. Renaming a file must not create a duplicate document |
| 1.2.9 | **Manual file mixes question and many answers in one blob** | P1 | Split on blank-line/answer-marker boundaries into one record per answer, with the question carried in `meta.question`. A single 5,000-word blob would otherwise be tagged as one document and count once |
| 1.2.10 | **Complaint boards embed order ids and phone numbers** in the body | **P0** | These sources carry far more PII than app reviews. Redact at collection *and* again at render (see 6.2), rather than relying on the reporting layer alone |
| 1.2.11 | **Scraped review has no author at all** | P1 | Use an `__anonymous__` sentinel before hashing, and exclude such records from the distinct-author counts that gate an opportunity's reportability (5.6) |

---

## 2. Storage, normalization, and dedup

| # | Scenario | Priority | Handling |
| --- | --- | --- | --- |
| 2.1 | **Cross-posted content** — the same complaint posted to three subreddits | P1 | Near-duplicate detection runs across the whole corpus, not per source. Otherwise one person's rant counts three times |
| 2.2 | **Same review on Play Store and App Store** by one user | P1 | Same cross-source fingerprint pass catches most of these |
| 2.3 | **Simhash is unreliable on short text** | **P0** | Apply near-duplicate clustering only to documents above ~25 words; below that, exact-fingerprint matching only. Otherwise unrelated short reviews collapse together and real signal disappears |
| 2.3b | **A Hamming threshold copied from web-scale practice marks nothing** | **P0** | The familiar "≤ 3 bits" assumes thousands of features per document. Measured on review-length text, a single reworded phrase costs 6 bits and near-duplicates reach 9, so 3 would silently leave cross-posted duplicates in the corpus inflating prevalence. Calibrated to 12, with unrelated reviews measured above 22. Re-check against the real corpus during the Phase 3 duplicate audit |
| 2.3c | **Fingerprints that differ between runs** | **P0** | Python's `hash()` is per-process randomized for strings, so a simhash built on it changes duplicate decisions every run without ever raising. Use an explicit cryptographic hash; `tests/test_hashing.py` asserts stability across three `PYTHONHASHSEED` values in subprocesses |
| 2.4 | **A comment quoting its parent** looks like a duplicate of it | P1 | Strip quoted lines before fingerprinting (see 1.2.5) |
| 2.5 | **Templated spam** — "Best app ever, download now" ×200 from different authors | P1 | Exact-fingerprint dedup catches identical text; the author-level aggregation in Phase 5 limits what survives |
| 2.6 | **One prolific author** with 40 documents about the same grievance | **P0** | Author-first aggregation, per `architecture.md` §8. Also cap any single `author_hash` at N documents per tag when computing prevalence |
| 2.7 | **Document is only a URL or only punctuation** | P1 | Strip URLs before word counting, so a bare link scores zero words and is excluded |
| 2.8 | **Text clears the length gate but is entirely stopwords** — "this is the one that I was looking at" | P1, **and the specified handling was wrong** | Originally: content-word count must be ≥ `filters.min_content_words` (3), as a tier-1 gate. Implemented as specified, measured, and reverted. Two reasons. The example itself has **zero keyword hits** and was already dropped by tier-1, so the gate removes nothing new; and at a 3-word length gate (3.1.6) it deletes what that gate exists to admit — *"still in my cart"* is two content words and an unambiguous wishlist signal, *"does this run small?"* is two. This edge case was only ever "rare" because an 8-word gate made it rare. **Current handling:** `min_content_words` splits the zero-hit drop into "about nothing" vs "about something else" and decides nothing. Only the second is evidence the vocabulary is too narrow. The stopword list is a closed literal rather than an NLTK import, since a corpus boundary that moves when a dependency updates would move invisibly |
| 2.9 | **`is_duplicate_of` chains** — A marks B, B marks C | P2 | Resolve to a canonical representative per cluster; never follow chains at query time |

---

## 3. Hard exclusion rules

The three rules from `implementation-plan.md` §3.1 have more corner cases than they appear to.

### 3.1 Word count (`< filters.min_words`, now **3**, revised from 8)

| # | Scenario | Priority | Handling |
| --- | --- | --- | --- |
| 3.1.1 | Punctuation-only tokens inflate the count — `"Bad . . . . . . . ."` | P1 | Count only tokens containing at least one alphanumeric character |
| 3.1.2 | Hyphenated and slashed compounds — `"size-chart"`, `"top/bottom"` | P2 | Count as one token. Document the choice; consistency matters more than the specific rule |
| 3.1.3 | Repeated-character padding — `"gooooood app very niceeee okay fine"` | P2 | Passes the word gate by design; tier-2 triage removes it |
| 3.1.4 | Exactly `min_words` words | **P0** | The rule is "fewer than `min_words` are excluded", so `min_words` itself is **kept**. Encode as `word_count < min_words` and assert both `min_words - 1` and `min_words` in tests — off-by-one here silently shifts the corpus. Asserted at 3 *and* 8, since the property belongs to the rule and not to the number |
| 3.1.5 | URLs counted as words | P1 | Strip URLs first (see 2.7) |
| 3.1.6 | **The threshold itself was the bug.** At 8 words the gate removed 63% of all collected records, and 1.1.13e records that it excluded *"does this run small?"* — the clearest single instance of the behaviour being measured — by construction | **P0, materialized** | Gate lowered to 3, which took the eligible corpus from 14,552 documents to 26,539. Two mitigations elsewhere in this document were silently *provided* by the value 8 and had to be made explicit when it moved: see 3.3.2 and 2.8 |
| 3.1.7 | A one-word reaction — `"nice"`, `"size?"` | P1 | Still excluded at 3 words, and this is the residual the gate is now for. Collectors that take hand-typed input say so in their READMEs, since a human abbreviating a question is the one way to reintroduce the 1.1.13e loss below the new gate |
| 3.1.8 | **A second filter deletes what the length gate admitted** | **P0, materialized** | Lowering the gate let *"does this run small?"* into the corpus, and tier-1 triage then dropped it for zero keyword hits: the vocabulary held `runs small` but not `run small`, and matching is word-boundary aware, so an auxiliary verb put the phrase one character out of reach. Invisible because **no test covered tier-1 at all**. Fixed in `config/relevance_keywords.txt`; the durable fix is `tests/test_relevance.py` asserting the question survives *end to end*, since every stage passed its own tests while the chain discarded the text. Any future change to one filter in this chain must be checked against the whole chain |

### 3.2 Emoji

| # | Scenario | Priority | Handling |
| --- | --- | --- | --- |
| 3.2.1 | **False positives from the dingbats block** — `✔` (U+2714), `✅`, `➡` sit in U+2600–27BF, but so do characters people use as bullet points | **P0** | Use `emoji.emoji_count(text) > 0` from the `emoji` package as the authority rather than raw ranges. It encodes the actual Unicode emoji property and avoids hand-rolled range drift |
| 3.2.2 | **Must not exclude** currency and typographic symbols — `₹`, `™`, `®`, `°`, `–`, `→` | **P0** | These are not emoji under the Unicode property and the `emoji` package correctly ignores them. A regex-only implementation would wrongly drop every review mentioning `₹` |
| 3.2.3 | **ZWJ sequences** — 👨‍👩‍👧 is one emoji from five codepoints | P1 | Presence check, not a count. Any match triggers exclusion regardless of composition |
| 3.2.4 | **Skin-tone and variation modifiers** appearing without a base | P2 | Presence check covers them |
| 3.2.5 | **Regional indicator pairs** — 🇮🇳 | P1 | Covered by the emoji package; verify in tests |
| 3.2.6 | **Text emoticons are not emoji** — `:)`, `<3`, `xD` | **P0** | Explicitly **not** excluded. The rule is emoji, not emoticons. Assert this in tests so a later regex "improvement" cannot quietly change corpus composition |
| 3.2.7 | **Substantive review ending in one emoji** | P1 | Excluded as specified. Phase 3 reports the count of documents excluded by emoji alone that would otherwise have passed all gates, so the cost is measured. Config flag `exclude_emoji` allows narrowing to emoji-dominant text if that number is large |

### 3.3 Hindi

| # | Scenario | Priority | Handling |
| --- | --- | --- | --- |
| 3.3.1 | **`langdetect` is non-deterministic** without a seed — the same text can return different languages across runs, breaking reproducibility | **P0** | Set `DetectorFactory.seed = 0` at import. This is the single most commonly missed bug in this stage |
| 3.3.2 | **Language ID is unreliable on short text** | P1 → **P0** | Ordering used to save us: the 8-word gate ran first, so nothing shorter reached the detector. **That protection was an accident of the threshold's value, and it evaporated when the gate moved to 3** (3.1.6) — nothing referenced it, so nothing broke and no test failed. The floor is now explicit and enforced inside the rule: below `filters.language_min_words` (8) langdetect's verdict is discarded. Script detection is **not** gated by it, since Devanagari is exact at any length and gating the whole rule would let short Hindi through. Still do not reorder these rules |
| 3.3.3 | **English review containing one Devanagari word** | P1 | Excluded under "any Devanagari character". Deliberate and simple, but log these separately — if the count is material, switch to a Devanagari-character *ratio* threshold |
| 3.3.4 | **Marathi, Nepali, Sanskrit** also use Devanagari | P2 | Caught by the script rule and coded as `hindi_language`. Rename the code to `devanagari_script` if precision in reporting matters |
| 3.3.5 | **Romanized Hinglish** — "size thoda chhota hai, wishlist mein pada hai" | **P0** | **Retained.** No Devanagari, and `langdetect` usually reports `en`. This is intentional: Hinglish carries much of the deliberation language the project is looking for |
| 3.3.6 | **Detector returns `hi` at low confidence for noisy English** | P1 | Only exclude at confidence ≥ 0.7; below that, keep and let triage decide |
| 3.3.7 | **`LangDetectException` on unparseable input** | P1 | Catch, default to "keep", and count. Never let a detector crash the corpus build |

---

## 4. LLM tagging

### 4.1 Request construction

| # | Scenario | Priority | Handling |
| --- | --- | --- | --- |
| 4.1.1 | **Every document in a batch is a cache hit** | P1 | Assemble batches from cache misses only; never send an empty request |
| 4.1.2 | **A single document exceeds the per-request token budget** | **P0** | Truncate per 1.2.4 before batching; if it still exceeds the budget alone, tag it solo with a raised limit or skip and log |
| 4.1.3 | **Batch straddles the TPM ceiling** | P1 | The governor estimates request tokens *before* sending and waits if the batch would breach TPM, rather than discovering it via 429 |
| 4.1.4 | **Reasoning tokens consume the entire `max_completion_tokens`** | **P0** | Confirmed live: `max_completion_tokens=64` on a 6-document batch produced `400 json_validate_failed` with an empty `failed_generation`, **not** `finish_reason: "length"`. Measured need is ~889 completion tokens for 6 documents (285 of them reasoning), so the configured 4,096 leaves ~4× headroom. Treat the 400 as retryable per 4.2.5 |
| 4.1.5 | **`max_tokens` used instead of `max_completion_tokens`** | **P0** | The deprecated alias does not account for reasoning, so a plausible-looking cap silently starves the visible output. This exact bug made `check_credentials.py` report a PASS with empty content. Use `max_completion_tokens` everywhere and never `max_tokens` |
| 4.1.6 | **A tight budget looks like success** — empty string is falsy but not an error | **P0** | Any code path that reads `message.content` must treat empty content as a failure, not an absent-but-valid answer. Assert non-empty before parsing |

### 4.2 Response handling

| # | Scenario | Priority | Handling |
| --- | --- | --- | --- |
| 4.2.1 | **Model returns fewer objects than documents sent** | **P0** | Reconcile by `doc_id`. Missing ids are re-queued individually, not silently dropped — silent loss biases prevalence toward whatever the model found easy |
| 4.2.2 | **Model returns a `doc_id` that was not in the batch** | **P0** | Discard the unknown id and log. Never insert tags for a document that was not sent |
| 4.2.3 | **Duplicate `doc_id` keys in one JSON object** | P1 | Python's parser keeps the last occurrence. Detect via `object_pairs_hook` and treat as a batch failure |
| 4.2.4 | **Output truncated mid-object** | **P0** | Under strict mode this surfaces as `400 json_validate_failed` rather than `finish_reason: "length"`, so both must be handled: raise `max_completion_tokens`, then re-run the batch at half size |
| 4.2.5 | **HTTP 400 — two distinct causes that must be told apart** | **P0** | Branch on the error `code`, never the status alone. `json_validate_failed` is **retryable**: the generation was truncated or violated the schema at runtime, so raise the token budget and shrink the batch. Any **other** 400 means the schema itself is non-compliant — a build error that must abort the run immediately with the validation message. Getting this backwards is expensive in both directions: treating truncation as fatal aborts a multi-day run, and retrying a bad schema burns a day's quota on a bug |
| 4.2.6 | **Batched results keyed by `doc_id`** | **P0** | Not expressible under strict mode: `additionalProperties: false` forbids arbitrary property names. Results must arrive as a fixed-name array (`{"documents": [...]}`) whose items each carry `doc_id`. Attempting the dynamic-key shape fails at schema validation, not at parse time |
| 4.2.14 | **Verbatim quote that does not support its tag** | **P0** | Observed live: `size_unavailable` asserted for a review that never mentions stock, evidenced by *"This kurta has been in my wishlist for a month"*. Existence and verbatim checks both pass. Requiring evidence does not remove unsupported tags — it converts them into misattributed quotes. No automatic check settles relevance, so it is screened cheaply (cue overlap, span ratio, quote reuse) and *measured* against hand-labeled gold spans, with evidence precision gated separately from tag F1 |
| 4.2.15 | **One quote cited for many tags** | P1 | A few coupled dimensions genuinely share evidence — `fit_size_uncertainty` with `will_it_fit` — so this is a flag above a configured count, never a rejection. Beyond that count it signals the tagger attributing lazily rather than reading |
| 4.2.16 | **Quote is effectively the whole document** | P1 | A span covering most of the text asserts nothing about *which part* supports the tag, so it is worthless as attribution while passing every other check. Flagged above a configured span ratio |
| 4.2.17 | **The cue screen mistaken for a guarantee** | **P0** | The lexicon has both false positives and false negatives. It must be calibrated against gold spans before its output is interpreted, and must never reject a tag on its own. A screen with unknown error rates is not evidence |
| 4.2.10 | **`strict: true` does not constrain generation** | **P0** | Confirmed three times live. Groq validates *after* generation and returns 400; it does not restrict tokens as they are produced. Observed: `"confidence": 0. nine` (the word lifted from the document's "nine days") on an unbounded `number` field, and `"wishlist_motivation": ["none"]` where `none` belongs to a different dimension. Mitigations are structural: enumerate every enumerable field, and give no dimension a sentinel that another dimension lacks |
| 4.2.11 | **Deterministic retry loop** — `temperature=0` reproduces a schema violation identically | **P0** | A plain retry cannot succeed. The repair attempt must change the request: feed back the validator error, then halve the batch, then fall back to one document per call. Cap attempts and leave the document untagged rather than looping; an unbounded loop on one pathological document would consume the whole daily budget |
| 4.2.12 | **Batch blast radius** — one bad field fails all 6 documents in the call | P1 | A batch rejection costs the tokens of every document in it. After a second failure, drop to single-document calls for that batch so one problem document cannot keep five good ones untagged |
| 4.2.13 | **Enum mixing whole and fractional numbers** | **P0** | `[0.0, 0.1 … 1.0]` is rejected as "cannot include both 'integer' and 'number'" — a *schema* 400, distinguishable from `json_validate_failed` by carrying `param` and `schema_path`. Pydantic emits a single `"type": "number"` and gives no local warning, so `strict_schema_violations()` checks this rule explicitly |
| 4.2.7 | **429 with `retry-after` beyond the run window** | P1 | Checkpoint, print resume instructions, exit 0. A daily-limit stop is a normal outcome of a free-tier run, not a failure |
| 4.2.8 | **TPD exhausted** | P1 | Same as 4.2.7, distinguished in the log so the user knows to wait for the UTC midnight reset |
| 4.2.9 | **Repair retry loops** on a document the model cannot satisfy | P1 | Exactly one repair attempt per document, then log and leave untagged. An unbounded loop on a pathological document would consume the whole daily budget |

### 4.3 Semantic validation

| # | Scenario | Priority | Handling |
| --- | --- | --- | --- |
| 4.3.1 | **Quote is not a verbatim substring** because the model normalized smart quotes, ellipses, or whitespace | **P0** | Compare after a normalization pass — NFKC, collapse whitespace, unify quote and dash characters, casefold. A naive `in` check rejects a large share of otherwise valid evidence |
| 4.3.2 | **Model paraphrases instead of quoting** | **P0** | Fails the substring check, triggers one repair retry, then the tag is dropped. This is the primary defense against hallucinated labels and must not be relaxed |
| 4.3.3 | **Quote spans the whole document** | P2 | Accept, but truncate for display in the report |
| 4.3.4 | **A tag is asserted with no evidence entry** | **P0** | Reject that tag while keeping the document's other valid tags. Do not discard the whole record |
| 4.3.5 | **Evidence references a tag not in the asserted list** | P1 | Drop the orphan evidence, log a consistency warning |
| 4.3.6 | **All dimensions come back empty** | P1 | Valid outcome. Persist as tagged-with-no-tags so it counts in the denominator — dropping it inflates every prevalence figure |
| 4.3.7 | **`intent_class` guessed rather than marked `ambiguous`** | P1 | Cannot be detected per document; caught in aggregate by the gold-set comparison |

### 4.4 Caching and determinism

| # | Scenario | Priority | Handling |
| --- | --- | --- | --- |
| 4.4.1 | **Batch composition affects a document's tags**, but the cache is per document | P1 | Accepted and documented. The cache stores what was produced; reproducibility comes from the frozen cache, not from re-derivation |
| 4.4.2 | **Prompt or taxonomy edited mid-run**, producing a corpus tagged under two versions | **P0** | Both versions are part of the cache key and the `doc_tags` primary key. Quantification filters to a single `(taxonomy_version, prompt_version, model)` triple and refuses to mix |
| 4.4.3 | **Cache hit from a different model** | P1 | Model is in the cache key, so this cannot happen. A test asserts it |
| 4.4.4 | **Corrupt cache row** (interrupted write) | P1 | Validate on read; treat a parse failure as a miss |

---

## 5. Quantification

| # | Scenario | Priority | Handling |
| --- | --- | --- | --- |
| 5.1 | **Division by zero in lift** when a tag has zero occurrences | **P0** | Return `NaN` and exclude from the matrix rather than emitting `inf`, which would top every ranking |
| 5.2 | **Tag present in 100% of documents** | P1 | Lift degenerates toward 1 and the tag is uninformative. Flag tags above 90% prevalence as likely taxonomy problems, not findings |
| 5.3 | **Wilson interval at n = 0, p = 0, or p = 1** | **P0** | Use the closed form that handles the boundaries; never compute the naive normal approximation |
| 5.4 | **Min-max normalization when all candidates are equal** | **P0** | Denominator is zero. Return 0.5 for all rather than dividing |
| 5.5 | **Fewer than 20 supporting documents** | P1 | Flag `low_confidence` and render the count next to every figure in the report |
| 5.6 | **A cluster supported entirely by one author** | **P0** | Excluded from ranking. Distinct-author count must be ≥ 3 for an opportunity to be reportable |
| 5.7 | **A tag appears in only one source** | P1 | Source-spread factor already penalizes it; additionally annotate as possibly source-specific (an app bug rather than a category need) |
| 5.7b | **A finding appears only in complaint-board sources** | **P0** | Complaint boards are self-selected post-purchase grievance, so a blocker seen nowhere else is probably a service failure rather than a wishlist blocker. Annotate with the pre/post-purchase mix of its supporting documents and require presence in at least one pre-purchase source before it can rank in the top tier |
| 5.8 | **All documents missing timestamps** | P1 | Recency weighting becomes uniform. Detect and disable the decay rather than silently weighting everything to the median |
| 5.9 | **Price-only cluster** | P1 | Actionability approaches zero, so the score approaches zero — by design. It still appears in the excluded-findings section with its raw volume |
| 5.10 | **Empty segment** — a `segment_cue` with no documents | P2 | Omit from the matrix; do not render an all-zero row |
| 5.11 | **Overlapping opportunity clusters** sharing most of their documents | P1 | Merge clusters above a Jaccard threshold, otherwise the same finding is ranked twice and looks like two |

---

## 6. Synthesis and reporting

| # | Scenario | Priority | Handling |
| --- | --- | --- | --- |
| 6.1 | **No opportunity clears the confidence threshold** | **P0** | The report still generates, states plainly that no finding met the bar, and shows the funnel so the reader can see whether the cause was corpus size or genuine absence of signal. An empty crash is the worst outcome here |
| 6.2 | **PII inside a verbatim quote** — phone number, email, order id | **P0** | Regex redaction before rendering: phone, email, order/AWB patterns, `@handles`. Publishing a user's order id from a public review is still a privacy failure |
| 6.3 | **Markdown injection from user text** — pipes breaking tables, backticks breaking code spans, `#` creating headings | **P0** | Escape `|`, backticks, and leading `#`/`>` in all rendered quotes. One review containing a pipe character can visibly corrupt a report table |
| 6.4 | **Profanity or abusive content in a selected quote** | P1 | Retain (it is real user voice) but prefer an equally representative alternative when one exists at similar centroid distance |
| 6.5 | **Same quote selected for multiple opportunities** | P2 | Deduplicate across sections; pick the next-best quote |
| 6.6 | **Very long quote** | P2 | Truncate to ~240 characters at a word boundary with an ellipsis, and link to the source URL for full context |
| 6.7 | **Source URL is dead or the comment was deleted since collection** | P2 | Expected over time. The quote and `doc_id` remain the evidence of record; the report notes that links may rot |
| 6.8 | **Report generated from stale CSVs** after a re-tag | P1 | Compare `run_id` timestamps across stages and refuse to render if quantification predates the newest tagging run |

---

## 7. Test coverage requirements

Every **P0** above needs a test. The non-obvious ones worth writing first:

```
test_json_validate_failed_retries   # 400 json_validate_failed is not fatal
test_schema_400_aborts_run          # any other 400 stops immediately
test_repair_attempts_are_capped     # temperature=0 makes a plain retry loop forever
test_irrelevant_quote_is_flagged    # verbatim is not the same as germane
test_cue_screen_never_rejects       # a screen routes attention, it does not decide truth
test_evidence_precision_gate        # report generation blocks below the threshold
test_simhash_stable_across_seeds    # PYTHONHASHSEED must not change fingerprints
test_numeric_fields_are_enums       # no unbounded number reaches the decoder
test_empty_content_is_a_failure     # empty string never counts as a valid answer
test_no_max_tokens_parameter        # only max_completion_tokens is ever sent
test_quora_module_makes_no_requests # compliance guarantee is enforced in code
test_ajio_manual_makes_no_requests  # the fallback for a blocked source cannot fetch
test_manual_block_without_header    # content type is never guessed from prose
test_robots_403_treated_as_denied   # fail closed, not open
test_zero_parsed_records_raises     # site redesign cannot pass silently
test_challenge_page_not_ingested    # Cloudflare interstitial is not content
test_manual_file_id_is_content_hash # renaming a file does not duplicate a doc
test_word_count_boundary            # N-1 excluded, N kept; asserted at 3 and 8
test_short_question_survives_at_3   # the 1.1.13e question the 8-word gate deleted
test_config_matches_the_rule        # reads config.yaml, so a silent revert fails
test_emoji_vs_emoticon              # ":)" kept, "🙂" excluded
test_currency_symbol_not_emoji      # "₹1299" kept
test_zwj_emoji_excluded             # multi-codepoint family emoji excluded
test_hinglish_retained              # romanized Hindi survives
test_devanagari_excluded            # Devanagari dropped, at any length
test_langdetect_skipped_when_short  # no statistical guess below language_min_words
test_langdetect_deterministic       # same input, same result, 100 iterations
test_quote_normalization            # smart quotes match verbatim source
test_missing_doc_id_requeued        # short response re-queues, never drops
test_unknown_doc_id_discarded       # hallucinated id never persisted
test_wilson_boundaries              # n=0, p=0, p=1 do not raise
test_minmax_all_equal               # no division by zero
test_empty_report_generates         # zero opportunities still renders
test_markdown_escaped_in_quotes     # pipe character cannot break a table
test_pii_redacted                   # phone/email/order id removed
```

---

## 8. Priority summary

**Handle before the first collection run:** 0.1, 0.2, 0.3, 0.5, 1.1.1, 1.1.5, 1.1.6, 1.1.7, 1.1.8, 1.1.11, 1.1.12, 1.1.13, 1.1.13d, 1.1.14, 1.2.1, 1.2.4, 1.2.8, 1.2.10

**Handle before the first tagging run:** 2.3, 2.3b, 2.3c, 2.6, 3.1.4, 3.2.1, 3.2.2, 3.2.6, 3.3.1, 3.3.5, 4.1.2, 4.1.4, 4.1.5, 4.1.6, 4.2.1, 4.2.2, 4.2.4, 4.2.5, 4.2.6, 4.2.10, 4.2.11, 4.2.13, 4.2.14, 4.2.17, 4.3.1, 4.3.2, 4.3.4, 4.4.2

**Handle before generating the report:** 5.1, 5.3, 5.4, 5.6, 6.1, 6.2, 6.3

The four most likely to cause silent, hard-to-detect damage are **1.1.7** (a redesigned page yielding zero records, which looks exactly like a quiet source), **3.3.1** (non-deterministic language detection quietly changing the corpus between runs), **4.3.1** (over-strict quote matching discarding most valid evidence and gutting prevalence), and **2.6** (one prolific author manufacturing a finding).

With Reddit disabled, **5.7b** deserves equal attention: the roster is now mostly post-purchase, so the engine's default failure mode is confidently reporting delivery and refund complaints as wishlist blockers.
