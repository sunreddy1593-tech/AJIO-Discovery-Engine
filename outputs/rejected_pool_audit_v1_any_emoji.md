# Rejected-pool audit — Phase 3 exit criterion 4

Sample drawn with seed 42. The gate is a false-rejection rate below 10%, scored per stratum rather than as one number, because the three hard rules and the two triage tiers fail differently and are fixed differently.

| Stratum | Audited | False rejections | Rate | Gate |
| --- | --- | --- | --- | --- |
| `too_short` | 10 | 0 | 0% | PASS |
| `contains_emoji` | 10 | 5 | 50% | **FAIL** |
| `hindi_language` | 10 | 2 | 20% | **FAIL** |
| `tier1_zero_hits_contentful` | 10 | 1 | 10% | **FAIL** |
| `tier1_zero_hits_contentless` | 10 | 5 | 50% | **FAIL** |
| **All strata** | 50 | 13 | 26% | **FAIL** |

**Verdict: FAIL** — the criterion requires every stratum below the gate, not just the average, since a rule that is wrong half the time can hide behind four that are never wrong.

## How far this measurement goes

At 10–10 documents per stratum, a rate is resolvable only in steps of roughly 10%, so one false rejection puts a stratum at the gate. That is enough to detect a broken rule and not enough to certify a working one — a stratum landing near the line should be re-drawn at a larger `--per-stratum` before any rule is called sound.

## Sources represented in the sample

| Source | Documents |
| --- | --- |
| youtube | 43 |
| play_store | 7 |
