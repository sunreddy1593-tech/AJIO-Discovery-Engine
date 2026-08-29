# Tagger validation

Scored against an independently labelled gold set. The worksheet was drawn **blind** (no tagger tags in the file). Predictions come from `doc_tags` (`taxonomy_version=v1`), not from a new tagging run.

- Sample size: **40** labelled documents (drawn with `--n 40`)
- Seed: **7**
- Sources: `app_store` 2, `complaints_board` 1, `consumer_complaints_in` 7, `play_store` 5, `quora_manual` 6, `youtube` 19

## Gates (architecture §11 / plan §4)

| Metric | Gate | Measured | Verdict |
| --- | ---: | ---: | --- |
| Macro-F1 `blocker_type` | ≥ 0.65 | 0.326 | FAIL |
| Evidence precision (quote ⊂ document) | ≥ 0.80 | 0.929 (39/42) | PASS |

## Macro-F1 by multi-label dimension

Per-label precision/recall/F1, then macro-averaged over labels that appear in gold or in the tagger's predictions on this sample.

| Dimension | Macro-F1 |
| --- | ---: |
| `wishlist_motivation` | 0.000 |
| `blocker_type` | 0.326 |
| `uncertainty_type` | 0.337 |
| `info_sought_elsewhere` | 0.000 |
| `segment_cue` | 0.000 |
| **All theme labels** | 0.172 |

**`intent_class` accuracy** (not a Phase 4 gate): 32/40 = 0.800.

## Per-label detail

### `wishlist_motivation`

| Label | P | R | F1 | support |
| --- | ---: | ---: | ---: | ---: |
| `price_watch` | 0.000 | 0.000 | 0.000 | 1 |
| `decide_later` | 0.000 | 0.000 | 0.000 | 0 |
| `compare_options` | 0.000 | 0.000 | 0.000 | 1 |
| `awaiting_occasion` | 0.000 | 0.000 | 0.000 | 1 |
| `budget_timing` | 0.000 | 0.000 | 0.000 | 2 |
| `inspiration_bookmark` | 0.000 | 0.000 | 0.000 | 2 |
| `size_unavailable` | 0.000 | 0.000 | 0.000 | 1 |

### `blocker_type`

| Label | P | R | F1 | support |
| --- | ---: | ---: | ---: | ---: |
| `fit_size_uncertainty` | 0.500 | 0.500 | 0.500 | 4 |
| `quality_doubt` | 0.667 | 0.500 | 0.571 | 4 |
| `color_fabric_accuracy` | 0.000 | 0.000 | 0.000 | 2 |
| `return_friction` | 0.636 | 0.636 | 0.636 | 11 |
| `delivery_uncertainty` | 0.500 | 0.500 | 0.500 | 4 |
| `trust_authenticity` | 1.000 | 0.250 | 0.400 | 4 |
| `price_absolute` | 0.000 | 0.000 | 0.000 | 1 |
| `price_expectation` | 0.000 | 0.000 | 0.000 | 1 |

### `uncertainty_type`

| Label | P | R | F1 | support |
| --- | ---: | ---: | ---: | ---: |
| `will_it_fit` | 0.750 | 0.500 | 0.600 | 6 |
| `is_quality_worth_it` | 0.333 | 0.333 | 0.333 | 3 |
| `true_color` | 0.000 | 0.000 | 0.000 | 1 |
| `can_i_return` | 1.000 | 0.600 | 0.750 | 10 |
| `better_alternative_exists` | 0.000 | 0.000 | 0.000 | 1 |

### `info_sought_elsewhere`

| Label | P | R | F1 | support |
| --- | ---: | ---: | ---: | ---: |
| `youtube_haul` | 0.000 | 0.000 | 0.000 | 1 |
| `other_marketplace_reviews` | 0.000 | 0.000 | 0.000 | 1 |
| `brand_site_size_chart` | 0.000 | 0.000 | 0.000 | 0 |

### `segment_cue`

| Label | P | R | F1 | support |
| --- | ---: | ---: | ---: | ---: |
| `frequent_shopper` | 0.000 | 0.000 | 0.000 | 2 |
| `budget_conscious` | 0.000 | 0.000 | 0.000 | 2 |

## Notes

- Evidence precision here is **verbatim-in-document**: the share of the tagger's stored quotes that appear in the source text (the same check `quote_in_document` uses). It is not span-overlap against your quotes.
- Do not treat these numbers as corpus-wide quality. They are this sample, seed 7, n=40.
