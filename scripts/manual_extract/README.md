# Manual extract helpers

Two ways to get visible AJIO reviews / Quora answers into the Collect JSON
shape without a headless client. Akamai blocks the automated fingerprint, not
you reading the page. Quora's robots.txt forbids bots using its content for
AI/ML — so this is the copy-paste, run on a page you already opened.

The extractors live here, **not** in `src/collect/`. Collect imports no HTTP
client and no Playwright; a test asserts it. After JSON lands in
`data/manual/ajio/` or `data/manual/quora/`, Collect is finished. Filling the
dirs is a person-task. Time-box it: **30–50 AJIO items**, **15–25 Quora
answers**. Do not chase volume.

Target **wishlist hesitation and purchase friction**, not a dump of every
star rating: size doubt, price-watching, "added to wishlist", quality/return
worries. YouTube already supplies 21,783 pre-purchase documents; these two
routes exist to break that monoculture.

## JSON shape

One file is an object `{ "documents": [ ... ] }` (an array, a single object, or
JSONL of objects also work). Each document:

| field | required | notes |
| --- | --- | --- |
| `id` | no | hashed from `text` if omitted |
| `source` / `route` | no | filled from the directory (`ajio_manual` / `quora_manual`) |
| `url` | AJIO: yes (or `meta.product_id`) | the `/p/<id>` page, or the Quora thread |
| `text` | yes | the question, review body, or answer |
| `author` | no | |
| `timestamp` | no | |
| `meta.content_type` | **AJIO only** | `qa` or `review` — never inferred (edge-case 1.1.14) |
| `meta.product_id` | AJIO | from `/p/<id>` if missing |
| `meta.question` | Quora | the thread title, carried onto every answer |

AJIO Q&A: the **question is the document**; answers go in `meta.answers`. Reviews
are post-purchase by schema even when they talk about the wishlist — collect
them anyway when they carry friction voice.

Validate without collecting:

```
.venv\Scripts\python.exe -m src.collect.manual
```

`python -m src.collect.manual` validates the enabled import dirs. `ajio_manual`
is disabled (AJIO has no on-site free text) and prints OFF; `quora_manual` is
filled and prints OK. Fixtures live under `tests/fixtures/manual/`, never here —
a synthetic file in the import directory previously reached the corpus posing as
a customer.

## 1. Console snippet / bookmarklet

1. Open the product or thread in your normal Chrome. Scroll until the reviews
   or answers you want are visible (expand "Continue reading" on Quora).
2. DevTools → Console → paste the contents of `ajio_extract.js` or
   `quora_extract.js`. It returns `{ documents, warnings }` and logs the count.
3. Copy that object: `copy(JSON.stringify($_, null, 2))` in Chrome, or wrap the
   file as a bookmarklet:

   ```
   javascript:(()=>{const r=/* paste the IIFE here */;navigator.clipboard.writeText(JSON.stringify(r,null,2)).then(()=>alert(r.documents.length+' docs copied')).catch(()=>prompt('copy JSON',JSON.stringify(r)));})();
   ```

   Simpler: save the IIFE as a bookmark whose URL is `javascript:` plus the
   file, then in the console run `copy(JSON.stringify(<last result>, null, 2))`.
4. Paste into `data/manual/ajio/<product>.json` or `data/manual/quora/<thread>.json`.

If selectors miss, select the visible text first — both snippets fall back to
`window.getSelection()`.

## 2. Playwright over CDP (real profile, not headless)

Playwright is optional. Do **not** `playwright install` a bundled Chromium and
point it at ajio.com — that is the fingerprint that gets blocked. Connect to
the Chrome you already use:

```
# PowerShell. Chrome must not already be running, or the flag is ignored.
Stop-Process -Name chrome -ErrorAction SilentlyContinue
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
```

Browse, scroll, then:

```
pip install playwright    # once; not added to requirements.txt
.venv\Scripts\python.exe scripts\manual_extract\cdp_extract.py --source ajio
.venv\Scripts\python.exe scripts\manual_extract\cdp_extract.py --source quora
```

`--url-contains /p/441` limits which open tabs are dumped.

## Finding pages (person-task)

**AJIO.** Product pages you would actually wishlist: kurtas, dresses, sneakers,
ethnic wear with noisy size charts. Open Ratings & Reviews *and* Questions.
Keep a Q&A that is four words long — "does this run small?" is the signal.

**Quora.** Google, do not crawl:

- `site:quora.com AJIO sizing`
- `site:quora.com AJIO returns`
- `site:quora.com AJIO "worth buying"`
- `site:quora.com AJIO reliability`
- `site:quora.com "added to wishlist" AJIO` / Myntra

Stop at 15–25 solid answers. More does not fix YouTube concentration; a
handful of deliberative threads does.
## ajio_bars.js — AJIO on-site aggregate grabber

`ajio_bars.js` captures AJIO's structured on-site feedback — the star-rating
distribution and the fit/quality "Customer Opinion" percentage breakdowns — as one
JSON record per product. AJIO publishes no free-text reviews or Q&A anywhere on the
site (which is why `ajio_manual` is `enabled: false`); these bars are the only
first-party signal it exposes, and they are numbers, not user text. They live in
`data/aggregates/ajio/` and are read only by Phase 6, never by Collect/Tag/Quantify.

This is a manual browser aid, not an automated collector. It runs in a real,
logged-in browser session on pages a person navigates to, and is deliberately not
wired into `run_collection` (AJIO is Akamai-protected and the product selection is
human-driven). As a result `data/aggregates/` is method-reproducible, not
command-reproducible: re-running the procedure yields a fresh point-in-time snapshot
(AJIO's counts move daily) over whatever products the operator picks, not a
byte-identical rebuild. The reader validates every file, so correctness does not
depend on the collection being scripted.

### How to run
1. Open an AJIO product page (URL contains `/p/<id>`) while logged in.
2. Scroll the Ratings and Customer Opinion sections into view.
3. Run the grabber — as a bookmarklet (the `javascript:`-prefixed build) or by
   pasting this file into the DevTools Console (F12 → Console). It copies one
   aggregate record to the clipboard and shows a summary (product id, average, top
   fit/quality option).
4. Save the clipboard into `data/aggregates/ajio/<product_id>.json`, one product per
   file. Don't save if the summary shows nothing parsed.

### Record schema
`source` "ajio_aggregate"; `product_id`; `product_title`; `url`; `extraction`
("bookmarklet"|"console"); `extracted_at` (ISO8601); `average_rating` (number|null);
`rating_count` (int|null); `rating_distribution` ("1".."5" → percent int); `opinions`
([{question, options{label→percent}}]).

### Average rating
The grabber reads AJIO's printed average when present, so the reader marks it
`average_rating_source: "reported"`. When the page doesn't expose it, the field is
left null and the reader derives it from `rating_distribution`
(`average_rating_source: "distribution"`). Reported is preferred; re-grab a product to
upgrade a derived record.

### Provenance
Every record stamps `extracted_at`, `url`, and `extraction`, so each file is
self-documenting about when and where it was collected. The report's limitations
section records the snapshot date range and that the sample is theme-driven, not
random.