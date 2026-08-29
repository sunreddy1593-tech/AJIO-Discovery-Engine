# AJIO on-site aggregates — numbers, never documents

One JSON file per product, named `<product_id>.json`, written by the browser
grabber from what AJIO itself publishes on a product page: the star-rating
distribution and the fit/quality percentage breakdowns.

**These are aggregates, never documents.** There is no customer sentence anywhere
in this folder — AJIO publishes no free-text reviews or Q&A sitewide, which is why
`ajio_manual` stays `enabled: false` (`edge-case.md` §1.1.13f). What is here is a
percentage table the site computed from buyers who answered its own prompts.

**This folder is read ONLY by Phase 6 (synthesize), never by Collect, Structure,
Tag or Quantify.** The single reader is `src/store/aggregates.py`, and the single
consumer is `src/synthesize/ajio_aggregates.py`. Concretely, that means:

- `ajio_aggregate` is **not** a collect source. It is absent from `SOURCE_STAGE`,
  `KNOWN_SOURCES`, `STAGE_BY_CONTENT_TYPE`, the collector registry,
  `run_collection` and the audit's source counts, and a test asserts it stays
  absent. It has no manifest, no `data/raw` partition and no purchase stage.
- These records never pass through `src.collect.manual`,
  `document_from_mapping`, `build_corpus`, `dedupe` or `relevance`. They are not
  text, so every one of those stages would either reject them or, worse, admit a
  number as if it were a voice.
- Nothing here is ever turned into a review-like sentence. A percentage is quoted
  as a percentage, attributed to AJIO, or it is not quoted at all.

Why the separation is this strict: a document is one person saying one thing, and
the whole analysis counts people. A single row here summarises hundreds of raters
at once, so letting one into the corpus would weight it like an individual and
inflate whatever it agrees with, invisibly. Kept beside the corpus instead, the
same row is a genuinely useful cross-check — AJIO's own buyers on fit, next to
what the text corpus says about fit.

## Record schema

```json
{
  "source": "ajio_aggregate",
  "product_id": "410334633",
  "product_title": "Anouk Women Straight Kurta",
  "url": "https://www.ajio.com/p/410334633",
  "extracted_at": "2026-08-23T17:56:20Z",
  "average_rating": null,
  "rating_count": 59,
  "rating_distribution": { "5": 54, "4": 16, "3": 11, "2": 3, "1": 13 },
  "opinions": [
    {
      "question": "How was the Product fit?",
      "options": { "Perfect": 65, "Loose": 12, "Tight": 9, "Too Loose": 3, "Too Tight": 9 }
    },
    {
      "question": "How was the Product Quality?",
      "options": { "Excellent": 27, "Very Good": 29, "Average": 32, "Bad": 5, "Very Bad": 5 }
    }
  ]
}
```

| field | type | notes |
| --- | --- | --- |
| `source` | string | always `"ajio_aggregate"` |
| `product_id` | string | matches the filename |
| `product_title` | string or null | |
| `url` | string | the `/p/<id>` page the numbers came from |
| `extracted_at` | string | ISO 8601; the dedupe key when a product is grabbed twice |
| `average_rating` | number or null | **null in every file collected so far** — see below |
| `rating_count` | int or null | number of raters, as AJIO reports it |
| `rating_distribution` | object | star `"1"`..`"5"` → percent int |
| `opinions` | array | `{question, options}`, each `options` mapping label → percent int |

## Two things about the numbers themselves

**`average_rating` is null in all 51 files collected so far.** The grabber gets
the count and the distribution but not the average, so the reader derives one:
the weighted mean of the star buckets, `sum(star * pct) / sum(pct)`, rounded to
one decimal. It fills the field only when it is null and never overwrites a
captured value, and it records which figure was used in `average_rating_source`
(`"reported"`, `"distribution"`, or `None` when both are absent). Any report that
cites an average has to say which it was — a derived average is a weaker claim
than one the site published, and the two must not read alike.

**The percentages do not sum to 100.** Across the current 51 files the star
buckets sum to between 96 and 100, median 97, and only two files reach 100. That
is AJIO's own per-bucket rounding, not missing data, so the derived mean divides
by the actual sum rather than by 100. Dividing by 100 would treat the missing few
percent as ratings of zero stars — a rating that cannot exist — and drag every
average down by about 0.1. The same caveat applies to the `opinions` options.

## Collecting more

Run the browser grabber against a product page and save its JSON here as
`<product_id>.json`. Two failure modes are already known and both are handled by
the reader rather than by a person noticing: a **0-byte file** (the grab produced
nothing) and **two JSON objects concatenated** into one file (the grab ran twice
into the same file). Both are skipped with a warning that names the file, and
neither costs the rest of the batch. Re-grabbing a product is safe: the reader
dedupes on `product_id` and keeps the newest `extracted_at`.

Filenames must be the bare product id. A file named after a category id would
attribute the numbers to a product that does not exist, and the report would
carry a dead citation URL.
