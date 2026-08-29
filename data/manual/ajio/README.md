# Manual AJIO import — CLOSED, nothing to collect

**`ajio_manual` is `enabled: false` in `config.yaml`, and this directory is
expected to stay empty.** AJIO publishes no free text anywhere on the site: a
product page carries aggregate star-rating bars and fit/quality percentage
breakdowns, and no customer prose — no review bodies, no Q&A. There is therefore
nothing for a person to read and save, which is the same empty result the blocked
`ajio_onsite` collector gets, one layer up (`edge-case.md` §1.1.13f). It is not a
task nobody has got to yet, and it is not counted against Phase 2's
source-coverage criterion: the audit scores `enabled_sources()`, so a disabled
source leaves the denominator.

Everything below is kept, unchanged, for one reason: if AJIO ever publishes review
text, re-enabling this route is one flag in `config.yaml` and the loader, the
collector, the extract scripts and the file format are all still here and still
tested.

Save hand-collected AJIO product **Q&A** and **reviews** here as `.json` (preferred),
`.jsonl`, `.txt`, or `.md`. `src/collect/manual.py` is the loader both collectors
share: it skips README, normalizes every file to `{id, source, url, text, author,
timestamp}`, and **fails loudly if this directory yields zero documents** — for
`quora_manual`, which is still live, that zero is the one source-coverage gap
Phase 2 has left.

The collector **never touches the network** — a test asserts it imports no HTTP
client, and additionally that it does not import `ajio_onsite`, which owns a
`PoliteSession` and would leave a network path one attribute access away. The
bookmarklet and the CDP helper live under `scripts/manual_extract/`, not here.
After one conformant file lands, Collect is done: dropping more threads is the
only remaining work.

## Why this directory exists, and why it is empty

AJIO on-site Q&A *would* be the single richest pre-purchase source on the roster —
*"does this run small?"* is literally the blocker the project is hunting. Two
things stop it, and they were found in that order. The scraped collector is refused
by an Akamai edge on every content path, which is the site's access decision rather
than a bug (`edge-case.md` §1.1.13); defeating that bot management is out of scope,
in a browser costume or otherwise, so the supported escalation was a person reading
the site and saving what they read into this directory. Then the browsing found
there is no such text to save (§1.1.13f). The block and the absence are two walls
in front of the same empty room.

With Reddit disabled and both AJIO routes empty, YouTube comments are effectively
the only live pre-purchase source: **21,783 pre-purchase documents, all YouTube**.
That concentration is a bigger risk to the engine than any source count, and
`data/manual/quora/` is now the only hand-collected route that can break the
monoculture.

## JSON shape (bookmarklet / CDP)

This is what `scripts/manual_extract/ajio_extract.js` copies, and what
`cdp_extract.py` writes. An object, an array, `{ "documents": [ ... ] }`, or JSONL
all parse.

```json
{
  "documents": [
    {
      "source": "ajio_manual",
      "url": "https://www.ajio.com/p/441098234",
      "text": "Does this kurta run small? I am usually a medium.",
      "author": null,
      "timestamp": null,
      "meta": { "content_type": "qa", "product_id": "441098234" }
    },
    {
      "source": "ajio_manual",
      "url": "https://www.ajio.com/p/441098234",
      "text": "Kept this in my wishlist for three weeks watching the price.",
      "author": "meera",
      "timestamp": "12 May 2026",
      "meta": { "content_type": "review", "product_id": "441098234", "rating": 2 }
    }
  ]
}
```

`id` is optional (hashed from `text`). `meta.content_type` is **required** and is
never inferred from the prose. `product_id` can be omitted when the URL contains
`/p/<id>`.

How to fill this directory, without a headless client:

1. Open the product in your normal Chrome. Scroll Ratings & Reviews / Q&A into view.
2. Paste `scripts/manual_extract/ajio_extract.js` in the console, or attach
   Playwright over CDP to that already-open Chrome
   (`scripts/manual_extract/README.md`). Do not spawn headless Chromium — that is
   the fingerprint Akamai blocks.
3. Save the JSON here. `python -m src.collect.manual` prints this directory as
   `OFF` while the source is disabled and exits 0 once every *enabled* import
   directory — today only `data/manual/quora/` — has one conformant file.

Markdown is still accepted if you would rather type. Same rules as below.

## Markdown format

Two directives carry the compliance weight, and neither has a fallback.

```
product: 441098234
title: Anouk Women Straight Kurta      (optional)

## Q&A

Q: Does this run small? I am usually a medium.
A: Order one size up, it runs small on the shoulders.

Q: Is the fabric see-through in white?
A: Slightly, I wear a slip under it.

## Reviews

[4] Fits well through the waist but the sleeves are longer than the size
chart suggests. Fabric is better than I expected for the price.
- by meera, 12 May 2026

[2] Returned it. The colour is nothing like the photos.
```

**`product:` is required.** It takes a bare id or a `/p/<id>` URL. There is
deliberately no filename fallback: identity may not depend on a filename, and a
name like `ajio-830216012-kurtas.md` carries a *category* id, which would be
recorded as a product and produce a dead citation URL in the final report. A block
with no `product:` line above it is skipped and counted.

**The `## Q&A` / `## Reviews` header is required.** Content type is never inferred
from the prose (`edge-case.md` §1.1.14), because a hand-typed file is if anything
easier to mix up than a JSON payload, and the two sit on opposite sides of the
purchase: Q&A is pre-purchase deliberation, reviews are post-purchase experience.
Conflating them would file one as the other in every downstream metric. An
unlabelled block is skipped and counted. A line starting `Q:` may stand in for a
header, since that is unambiguous.

One `product:` line may appear more than once per file, so a single file can hold a
morning's browsing across several products.

## Rules worth knowing before you collect

- **An answer is not a document.** Answers are kept in `meta.answers` on their
  question rather than promoted to records of their own, because they are usually
  written by people who already bought the item — counting them would file
  post-purchase voice as pre-purchase deliberation.
- **Renaming a file later is safe.** Record identity is a hash of the text.
- **Short questions are wanted now.** Phase 3's length gate is three words, not
  eight, so *"does this run small?"* — the best question on the site — survives to
  the corpus instead of being dropped by construction. Collect them. What still
  gets dropped is a bare *"size?"* or *"fit?"*, so type the question as the
  customer asked it rather than abbreviating.
- **A file named `README` is ignored**, so this one is not imported. That filter
  exists because `data/manual/quora/README.md` was once parsed into nine phantom
  pre-purchase documents.
- **Do not put synthetic examples in this directory.** The block above is fenced
  as an example on purpose. A `test_import.md` full of invented Q&A previously
  contributed three records to the raw corpus, each with a real-looking citation
  URL pointing at a product that may not exist.
