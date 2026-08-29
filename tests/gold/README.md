# Gold set

Independent labels for scoring the tagger. The tagger's predictions stay in
`doc_tags`; they must not be in this folder until *after* you have labelled.

1. Draw a blind worksheet (already done if `gold_worksheet.jsonl` exists):

   ```
   .venv\Scripts\python.exe -m scripts.build_gold_worksheet --n 40 --seed 7
   ```

2. **Label `gold_worksheet.jsonl` without opening `doc_tags`, the explorer's
   tag view, or `opportunity_report.md` quotes for those `doc_id`s.** Fill
   `blocker_type`, `uncertainty_type`, `wishlist_motivation`,
   `info_sought_elsewhere`, `segment_cue` (JSON arrays of taxonomy strings),
   `intent_class` (one of `genuine_intent` / `bookmark_only` / `ambiguous`),
   and `evidence` (`[{"tag": "...", "quote": "..."}]` copied from the text).
   Empty arrays are allowed when that dimension does not apply. `intent_class`
   must not stay `""`.

3. Save the filled file as **`gold_set.jsonl`** (keep the worksheet as the
   unlabelled original).

4. Score:

   ```
   .venv\Scripts\python.exe -m scripts.score_gold_set
   ```

That writes `outputs/tagger_validation.md` with measured macro-F1 and evidence
precision. It does not re-tag and does not write to `documents` or `doc_tags`.
