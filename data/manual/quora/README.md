# Manual Quora import

Drop saved Quora threads here as `.json` (preferred), `.jsonl`, `.txt`, or `.md`.
`src/collect/manual.py` is the shared loader: it skips README, normalizes every
file to `{id, source, url, text, author, timestamp}`, and **fails loudly if this
directory yields zero documents**. That zero is why Phase 2 reads 5/6.

The collector **never touches the network** — Quora's `robots.txt` prohibits bots
from using its content for AI or ML systems, so this source is human-collected by
design, and a test asserts the module imports no HTTP client. The extract snippet
lives under `scripts/manual_extract/`. After one conformant file lands, Collect is
done.

## Why bother

Quora is one of only three pre-purchase sources on the roster, and by far the most
deliberative: people explain at length *why* they did not buy something. Review
sites never elicit that. With Reddit disabled, an empty directory here costs the
corpus a meaningful share of its pre-purchase evidence.

**Time-box: 15–25 solid answers.** Find threads with Google, then extract the
visible answers. This source exists to break YouTube's monopoly on pre-purchase
evidence (21,783 documents, all comments), not to match it. Deliberative "why I
did not buy" language is the yield; a dump of every related thread is not.

## JSON shape (bookmarklet / CDP)

```json
{
  "documents": [
    {
      "source": "quora_manual",
      "url": "https://www.quora.com/Is-Ajio-sizing-reliable-compared-to-Myntra",
      "text": "I keep saving dresses on Ajio and never checking out because the size charts contradict the reviews.",
      "author": null,
      "timestamp": null,
      "meta": { "question": "Is Ajio sizing reliable compared to Myntra?" }
    }
  ]
}
```

One answer is one document; the question is carried in `meta.question`.

1. Google: `site:quora.com AJIO sizing` / `returns` / `"worth buying"` / `reliability`.
2. Open the thread yourself. Scroll until answers load; expand "Continue reading".
3. Paste `scripts/manual_extract/quora_extract.js` in the console, or
   `python scripts/manual_extract/cdp_extract.py --source quora` against an
   already-open Chrome (`--remote-debugging-port=9222`). Same snippet either way.
4. Save the JSON here. `python -m src.collect.manual` is the validator.

Markdown paste still works if you prefer typing. Same split rules as below.

## How to save a thread as markdown

1. Open a relevant thread in a browser. Useful searches: "why do I keep adding to
   wishlist", "Ajio sizing", "online shopping size fit India", "Myntra vs Ajio
   quality".
2. Select the question and the answers, copy, and paste into a new `.txt` file here.
3. Name the file anything descriptive. **Renaming later is safe** — document identity
   is a hash of the text, not the filename, so a rename cannot create a duplicate.

## Expected format

The parser is forgiving, but it splits best on this shape:

```
Why do I keep saving clothes on Ajio and never buying them?

First answer paragraph. Anything under 40 characters is dropped as noise.

Answer 2:

Second answer. Each answer becomes its own document, with the question carried
in meta.question so the context survives.
```

Rules the parser applies:

- The first block is treated as the question when it ends in `?`, starts with
  `Question:`/`Q:`, or is a markdown heading.
- Answers are split on blank lines and on markers like `Answer 2:` or `---`.
- Quora boilerplate (`12 views`, `Upvote`, `Share`, `Follow`) is stripped.
- Answers shorter than 40 characters are discarded.

Author handles are left null on a markdown paste rather than guessed. JSON from
the extract snippet may carry a visible name when the DOM has one; timestamps stay
null unless the page showed one, since a wrong date would distort recency
weighting in Phase 5.
