# Opportunity report

Rendered by `src.synthesize.run_synthesis`. Aggregates corroborate; they are never primary evidence.

## Corpus summary

**55913 documents** in the corpus, **7127 analyzable** (relevant, not a duplicate).

| source | documents | analyzable |
| --- | ---: | ---: |
| `youtube` | 45900 | 5336 |
| `play_store` | 8626 | 1194 |
| `app_store` | 1000 | 337 |
| `consumer_complaints_in` | 200 | 148 |
| `quora_manual` | 182 | 107 |
| `complaints_board` | 5 | 5 |

Analyzable source mix: `youtube` 75%, `play_store` 17%, `app_store` 5%, `consumer_complaints_in` 2%, `quora_manual` 2%, `complaints_board` 0%. The corpus is no longer YouTube-only.

**Purchase-stage split** (analyzable): **5443 pre-purchase**, **153 post-purchase**, **1531 mixed**.
YouTube accounts for **5336 of 5443** analyzable pre-purchase documents (98%). That concentration — haul and influencer framing — is the mix Part 1 of the brief has to name, not bury in a total. Quora is the only other live pre-purchase route.

**Date range** of `created_utc`: 2017-04-19 to 2026-08-20.

**Funnel by exclusion reason** (rows retained, not deleted):

| reason | documents |
| --- | ---: |
| `too_short` | 18154 |
| `contains_emoji` | 10834 |
| `hindi_language` | 207 |
| `duplicate` | 1625 |
| `triage_irrelevant` (no hard-exclusion code) | 17966 |

**Tagger quality:** macro-F1 on `blocker_type` and **evidence precision** against a gold set have **not been measured** — no labelled gold set is in the repository. Do not read the quotes below as human-validated spans.

**Tagging denominators:** **7127 relevant**, **800 tagged** (sample seed `42`, target 800). Every prevalence figure below is computed over the tagged set, not the relevant corpus.
Per-source draw: `youtube` 420, `consumer_complaints_in` 148, `quora_manual` 107, `play_store` 94, `app_store` 26, `complaints_board` 5.
Censused in full (not drawn): `complaints_board`, `consumer_complaints_in`, `quora_manual`; remaining sources proportional to taggable size.

## Opportunity areas

Ranked themes from Stage 4. Evidence is attributed by source and `doc_id`; quotes are unflagged spans nearest the cluster (tag overlap) plus highest severity, never hand-picked. Source URLs may rot; the quote and `doc_id` remain the evidence of record.

opportunity_score = 100 × sqrt(prevalence_norm) × (mean_severity / 5) × mean_actionability × evidence_confidence; prevalence_norm is the author-weighted, recency-weighted share min-max'd across candidates (12-month half-life); evidence_confidence = (mean_confidence/100) × source_spread × (Wilson lower / prevalence) × attribution_factor; post_purchase_only clusters are ranked below pre-purchase-supported ones; ajio_aggregate is never an input

### Full corpus vs genuine intent

`genuine_intent` is the **108-document** subset showing real purchase intent rather than bookmarking or a complaint after the fact — the population a wishlist-to-purchase study actually has to move.

| theme | full score | genuine-intent score | rank movement |
| --- | ---: | ---: | --- |
| `brand_site_size_chart` | 0.37 | 0.96 | up 3 (9 → 6) |
| `price_absolute` | 0.25 | 0.56 | up 3 (10 → 7) |
| `fit_size_uncertainty` | 4.04 | 3.47 | up 2 (5 → 3) |
| `color_fabric_accuracy` | 1.26 | 0.31 | down 2 (6 → 8) |
| `return_friction` | 21.1 | 17.48 | holds (1) |
| `delivery_uncertainty` | 10.71 | 3.75 | holds (2) |

### 1. return_friction

- opportunity score: 21.1
- score components: sqrt(prevalence_norm)=1.00; severity_norm=0.80; actionability=0.97; evidence_confidence=0.27
- prevalence: 24.4% (Wilson 95% CI 21.5%–27.5%)
- supporting documents (Stage 4): 195
- distinct authors: 192
- genuine-intent documents: 46
- genuine-intent score: 17.48
- co-occurrence: blocker_type=delivery_uncertainty; blocker_type=quality_doubt; blocker_type=price_absolute

**Evidence**

- `app_store` `211b72eefa267e33` ([source](https://itunes.apple.com/in/review?id=1113425372&type=Purple%20Software)): "Their customer service, exchange and returns are disappointing."
- `consumer_complaints_in` `2cbc6c167e947180` ([source](https://www.consumercomplaints.in/comment-guidelines.html)): "My money is stuck for a fully prepaid order, and I have neither received the items nor any update on the refund."
- `play_store` `7d7676d9650bbcc6` ([source](https://play.google.com/store/apps/details?id=com.ril.ajio&reviewId=b14dda78-a255-419a-8812-8e09f45c3949)): "refund nahin diya"
- `youtube` `3d2381b3f092af5d` ([source](https://www.youtube.com/watch?v=wA97Lm-G4JI&lc=UgztsGoKRuL3lU6xuSZ4AaABAg.AFwpY4FFy2KAFxsA1IfTqR)): "10-10 days in return"

### 2. delivery_uncertainty

- opportunity score: 10.71
- score components: sqrt(prevalence_norm)=0.56; severity_norm=0.84; actionability=0.95; evidence_confidence=0.24
- prevalence: 7.8% (Wilson 95% CI 6.1%–9.8%)
- supporting documents (Stage 4): 62
- distinct authors: 62
- genuine-intent documents: 9
- genuine-intent score: 3.75
- co-occurrence: blocker_type=return_friction; uncertainty_type=can_i_return; blocker_type=trust_authenticity
- affected segments: `first_time_online_buyer` (n=1, lift=6.45)

**Evidence**

- `app_store` `21e32ef9cc07f580` ([source](https://itunes.apple.com/in/review?id=1113425372&type=Purple%20Software)): "delivery partner failed to pick up the product four times"
- `consumer_complaints_in` `2cbc6c167e947180` ([source](https://www.consumercomplaints.in/comment-guidelines.html)): "prepaid order that has not been delivered despite multiple reschedules and false updates"
- `play_store` `12ca6e66b5913223` ([source](https://play.google.com/store/apps/details?id=com.ril.ajio&reviewId=b8672f4b-1a65-4870-9d1b-a0625cce469f)): "they continuously push the delivery date for failed delivery attempt without informing"
- `youtube` `3d2381b3f092af5d` ([source](https://www.youtube.com/watch?v=wA97Lm-G4JI&lc=UgztsGoKRuL3lU6xuSZ4AaABAg.AFwpY4FFy2KAFxsA1IfTqR)): "10-10 days in delivery"

### 3. trust_authenticity

- opportunity score: 4.72
- score components: sqrt(prevalence_norm)=0.49; severity_norm=0.80; actionability=0.94; evidence_confidence=0.13
- prevalence: 5.9% (Wilson 95% CI 4.4%–7.7%)
- supporting documents (Stage 4): 47
- distinct authors: 47
- genuine-intent documents: 6
- genuine-intent score: 3.02
- co-occurrence: blocker_type=return_friction; uncertainty_type=can_i_return; blocker_type=quality_doubt

**Evidence**

- `consumer_complaints_in` `4d671a17ed868e2b` ([source](https://www.consumercomplaints.in/comment-guidelines.html)): "the product was fake and used product"
- `play_store` `710e139c7312244b` ([source](https://play.google.com/store/apps/details?id=com.myntra.android&reviewId=03ab9004-eb91-4680-9181-e0b31e1c6458)): "It's basically a scam masquerading as a legitimate business"
- `youtube` `015bd03bade18a9d` ([source](https://www.youtube.com/watch?v=9csse8TbWgc&lc=UgxITu4X-8KjuY_9KMF4AaABAg)): "Fake products and Myntra is fraud company"
- `quora_manual` `5ac705e9e8c4c891` ([source](https://www.quora.com/What-stops-you-from-buying-a-product-online-instantly)): "I stop buying a product online instantly when I see negative user-generated reviews, bad ratings, damaged product photos, videos, or people complaining about service. It kills trust in seconds."

### 4. quality_doubt

- opportunity score: 4.32
- score components: sqrt(prevalence_norm)=0.48; severity_norm=0.83; actionability=0.98; evidence_confidence=0.11
- prevalence: 5.8% (Wilson 95% CI 4.3%–7.6%)
- supporting documents (Stage 4): 46
- distinct authors: 46
- genuine-intent documents: 10
- genuine-intent score: 2.72
- co-occurrence: blocker_type=return_friction; uncertainty_type=can_i_return; blocker_type=trust_authenticity

**Evidence**

- `app_store` `e7345bafb9a6dd8f` ([source](https://itunes.apple.com/in/review?id=1113425372&type=Purple%20Software)): "poor quality, unreliable, and misleading"
- `consumer_complaints_in` `fbabc45d7371a429` ([source](https://www.consumercomplaints.in/comment-guidelines.html)): "The product quality is very cheap. And one of the saree is used"
- `youtube` `8ddfcb0958d52c8c` ([source](https://www.youtube.com/watch?v=irn1y3_q9HQ&lc=UgxPe5As4c1Pyz0WIDJ4AaABAg)): "Fabric ki quilty me difference h..myntra ki quilty jyada achi h"
- `play_store` `1fc2a9743073e099` ([source](https://play.google.com/store/apps/details?id=com.myntra.android&reviewId=eb61e5fd-cb38-449c-949b-ed02e277d844)): "I received the product without brand name and of bad quality"

### 5. fit_size_uncertainty

- opportunity score: 4.04
- score components: sqrt(prevalence_norm)=0.65; severity_norm=0.61; actionability=0.99; evidence_confidence=0.10
- prevalence: 11.2% (Wilson 95% CI 9.2%–13.6%)
- supporting documents (Stage 4): 90
- distinct authors: 83
- genuine-intent documents: 47
- genuine-intent score: 3.47
- co-occurrence: wishlist_motivation=decide_later; blocker_type=return_friction; wishlist_motivation=seeking_opinion

**Evidence**

- `consumer_complaints_in` `724f0048240a5c4d` ([source](https://www.consumercomplaints.in/comment-guidelines.html)): "One product doesn't fit well so when I wish to return they denied"
- `quora_manual` `69b854d91b105b4e` ([source](https://www.quora.com/unanswered/What-prevents-wishlisted-fashion-products-from-eventually-being-purchased)): "I want to try it on and see how it fits me"
- `youtube` `1f65be27efd8706f` ([source](https://www.youtube.com/watch?v=Uf81X8shUB8&lc=UgzlIk_4HwNwJCS1foV4AaABAg)): "size while measuring like for chest it shows L and while measrue across the shoulder it shows diff size what to do"
- `consumer_complaints_in` `0bff6978da1e7f59` ([source](https://www.consumercomplaints.in/comment-guidelines.html)): "I want to return the product as it did not fit me well"

### 6. color_fabric_accuracy

- opportunity score: 1.26
- score components: sqrt(prevalence_norm)=0.16; severity_norm=0.89; actionability=1.00; evidence_confidence=0.09
- prevalence: 0.9% (Wilson 95% CI 0.4%–1.8%)
- supporting documents (Stage 4): 7
- distinct authors: 7
- genuine-intent documents: 2
- genuine-intent score: 0.31
- co-occurrence: uncertainty_type=true_color; blocker_type=fit_size_uncertainty; blocker_type=return_friction
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

- `consumer_complaints_in` `9e94f45f92f3903f` ([source](https://www.consumercomplaints.in/comment-guidelines.html)): "they sent me the wrong color (black) instead"
- `quora_manual` `803ba25550da27ed` ([source](https://www.quora.com/unanswered/What-prevents-wishlisted-fashion-products-from-eventually-being-purchased)): "Fabrics, textures and cuts can’t be felt behind a computer screen."
- `consumer_complaints_in` `ae527240a252918f` ([source](https://www.consumercomplaints.in/comment-guidelines.html)): "color of the product is slightly different from what appeared in the photograph"

### 7. true_color

- opportunity score: 1.06
- score components: sqrt(prevalence_norm)=0.19; severity_norm=0.82; actionability=1.00; evidence_confidence=0.07
- prevalence: 1.1% (Wilson 95% CI 0.6%–2.1%)
- supporting documents (Stage 4): 9
- distinct authors: 9
- genuine-intent documents: 2
- genuine-intent score: 0.31
- co-occurrence: blocker_type=color_fabric_accuracy; blocker_type=trust_authenticity; blocker_type=return_friction
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

- `app_store` `e7345bafb9a6dd8f` ([source](https://itunes.apple.com/in/review?id=1113425372&type=Purple%20Software)): "product I received was completely different from the image shown"
- `consumer_complaints_in` `9e94f45f92f3903f` ([source](https://www.consumercomplaints.in/comment-guidelines.html)): "I specifically ordered the blue color, and Ajio failed to deliver what was advertised"

### 8. checkout_friction

- opportunity score: 0.56
- score components: sqrt(prevalence_norm)=0.26; severity_norm=0.76; actionability=0.87; evidence_confidence=0.03
- prevalence: 1.9% (Wilson 95% CI 1.1%–3.1%)
- supporting documents (Stage 4): 15
- distinct authors: 15
- genuine-intent documents: 6
- genuine-intent score: 0.0
- co-occurrence: blocker_type=price_absolute; blocker_type=return_friction; blocker_type=trust_authenticity
- affected segments: `first_time_online_buyer` (n=1, lift=26.67)
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

- `play_store` `27f2237db8207405` ([source](https://play.google.com/store/apps/details?id=com.ril.ajio&reviewId=4e118c80-7f9d-446c-b3d9-fd7a24c7f6af)): "whenever I'm trying to open cart to place an order, it says "You can't place a new order""
- `quora_manual` `88888e003cb395fe` ([source](https://www.quora.com/What-stops-you-from-buying-a-product-online-instantly)): "You don’t offer the payment methods the customer likes most."

### 9. brand_site_size_chart

- opportunity score: 0.37
- score components: sqrt(prevalence_norm)=0.17; severity_norm=0.47; actionability=1.00; evidence_confidence=0.05
- prevalence: 1.1% (Wilson 95% CI 0.6%–2.1%)
- supporting documents (Stage 4): 9
- distinct authors: 8
- genuine-intent documents: 8
- genuine-intent score: 0.96
- co-occurrence: uncertainty_type=will_it_fit; blocker_type=fit_size_uncertainty; wishlist_motivation=decide_later
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

- `youtube` `156cef31f803b044` ([source](https://www.youtube.com/watch?v=gnXtV37HTRs&lc=UgyBoc6m38yN_sAebJR4AaABAg)): "Chest 38 shoulder 18.5"
- `youtube` `25ab293ca21000a2` ([source](https://www.youtube.com/watch?v=gnXtV37HTRs&lc=UgyueI4WnmoosHazvfd4AaABAg)): "mara chest ka size hai 34"
- `youtube` `49726ae23117a86a` ([source](https://www.youtube.com/watch?v=gnXtV37HTRs&lc=UgzEeRnTAxErbAjhP3R4AaABAg.9gOCEwPnves9gODFt8DS8s)): "size chart me ek baar check kr lena"
- `youtube` `b2f5a08feb8f774d` ([source](https://www.youtube.com/watch?v=gnXtV37HTRs&lc=UgyS2ul-uonQfsTpNpF4AaABAg.9d3ThHtY8Tr9d3hAxgdLiZ)): "size chart me check krkry"

### 10. price_absolute

- opportunity score: 0.25
- score components: sqrt(prevalence_norm)=0.47; severity_norm=0.76; actionability=0.61; evidence_confidence=0.01
- prevalence: 5.5% (Wilson 95% CI 4.1%–7.3%)
- supporting documents (Stage 4): 44
- distinct authors: 44
- genuine-intent documents: 9
- genuine-intent score: 0.56
- co-occurrence: blocker_type=return_friction; uncertainty_type=can_i_return; wishlist_motivation=price_watch
- affected segments: `first_time_online_buyer` (n=1, lift=9.09)

**Evidence**

- `youtube` `9fde6fcf1b6edbd4` ([source](https://www.youtube.com/watch?v=hMjfTdrJDTU&lc=Ugx80IGGF3qpB78za1d4AaABAg)): "too costly"

### 11. cart_proxy

- opportunity score: 0.1
- score components: sqrt(prevalence_norm)=0.10; severity_norm=0.20; actionability=1.00; evidence_confidence=0.05
- prevalence: 0.5% (Wilson 95% CI 0.2%–1.3%)
- supporting documents (Stage 4): 4
- distinct authors: 4
- genuine-intent documents: 3
- genuine-intent score: 0.14
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

- `youtube` `315087445e91be37` ([source](https://www.youtube.com/watch?v=k50zxopryak&lc=UgxWF8ry6_gnBdiwSAd4AaABAg)): "I have this in my cart"
- `youtube` `5c3c5b58f60edd72` ([source](https://www.youtube.com/watch?v=QeKVT7d8XB0&lc=Ugz5lJxGwfmiB8CWqKF4AaABAg.9vbjxK76R4x9vdFf5VVC6C)): "Cart lo add chesi order"

### 12. youtube_haul

- opportunity score: 0.08
- score components: sqrt(prevalence_norm)=0.13; severity_norm=0.32; actionability=0.40; evidence_confidence=0.05
- prevalence: 0.6% (Wilson 95% CI 0.3%–1.5%)
- supporting documents (Stage 4): 5
- distinct authors: 5
- genuine-intent documents: 1
- genuine-intent score: 0.0
- co-occurrence: blocker_type=fit_size_uncertainty; uncertainty_type=will_it_fit; wishlist_motivation=budget_timing
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

- `youtube` `307cb7d6e12dbb5c` ([source](https://www.youtube.com/watch?v=AULxA33mA40&lc=Ugx9rR_PkgIaoIA8KMB4AaABAg)): "youtube me aaya"
- `youtube` `670a1a91284ec275` ([source](https://www.youtube.com/watch?v=hMjfTdrJDTU&lc=Ugzl2LYZbCnRlOweuvl4AaABAg)): "plz do a haul on affordable tops from Ajio.."
- `youtube` `72614a8ee7881c11` ([source](https://www.youtube.com/watch?v=hKHsaJ6_ooc&lc=UgxG1y4GDqNAx82L5vZ4AaABAg)): "Pls make a haul on budget friendly gym pants, baggy, leggings straight include everything"
- `youtube` `9640f16a0e3bde10` ([source](https://www.youtube.com/watch?v=mt2aPiW1NqI&lc=UgxvyWJ62zGphlqbipF4AaABAg)): "please do a haul on Elegant Hush suits"

### 13. budget_timing

- opportunity score: 0.07
- score components: sqrt(prevalence_norm)=0.10; severity_norm=0.50; actionability=0.25; evidence_confidence=0.05
- prevalence: 0.5% (Wilson 95% CI 0.2%–1.3%)
- supporting documents (Stage 4): 4
- distinct authors: 4
- genuine-intent documents: 1
- genuine-intent score: 0.0
- co-occurrence: blocker_type=price_absolute; wishlist_motivation=decide_later; wishlist_motivation=price_watch
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

- `quora_manual` `d84a710e8e50a9cd` ([source](https://www.quora.com/unanswered/When-do-you-use-the-wishlist-as-genuine-purchase-intent-versus-simply-as-a-bookmarking-mechanism-with-regard-to-fashion-apparel-products)): "not willing to buy now ( reason could be high price, not fitting into this months budget"
- `youtube` `72614a8ee7881c11` ([source](https://www.youtube.com/watch?v=hKHsaJ6_ooc&lc=UgxG1y4GDqNAx82L5vZ4AaABAg)): "budget friendly gym pants"

### 14. other_marketplace_reviews

- opportunity score: 0.06
- score components: sqrt(prevalence_norm)=0.07; severity_norm=0.27; actionability=1.00; evidence_confidence=0.03
- prevalence: 0.4% (Wilson 95% CI 0.1%–1.1%)
- supporting documents (Stage 4): 3
- distinct authors: 3
- genuine-intent documents: 0
- genuine-intent score: 0.0
- co-occurrence: wishlist_motivation=compare_options; wishlist_motivation=seeking_opinion
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

- `youtube` `b671cbe9cf656739` ([source](https://www.youtube.com/watch?v=dxt5GIexiuw&lc=Ugz-U4gbBjnRHjG296p4AaABAg)): "hall reviews share karo"
- `youtube` `b5fe613d20e50997` ([source](https://www.youtube.com/watch?v=hMjfTdrJDTU&lc=UgyQayYuXsCGCx6cDC14AaABAg.8rNJnp59FYe8rP0h1a_xnE)): "try looking at jabong or myntra"

### 15. price_watch

- opportunity score: 0.05
- score components: sqrt(prevalence_norm)=0.30; severity_norm=0.42; actionability=0.32; evidence_confidence=0.01
- prevalence: 2.4% (Wilson 95% CI 1.5%–3.7%)
- supporting documents (Stage 4): 19
- distinct authors: 19
- genuine-intent documents: 4
- genuine-intent score: 0.13
- co-occurrence: blocker_type=price_absolute; wishlist_motivation=budget_timing; wishlist_motivation=decide_later
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

- `quora_manual` `8abc271a2e136eb8` ([source](https://www.quora.com/unanswered/When-do-you-use-the-wishlist-as-genuine-purchase-intent-versus-simply-as-a-bookmarking-mechanism-with-regard-to-fashion-apparel-products)): "wait for things to go on sale"

### 16. compare_options

- opportunity score: 0.05
- score components: sqrt(prevalence_norm)=0.07; severity_norm=0.47; actionability=1.00; evidence_confidence=0.02
- prevalence: 0.4% (Wilson 95% CI 0.1%–1.1%)
- supporting documents (Stage 4): 3
- distinct authors: 3
- genuine-intent documents: 1
- genuine-intent score: 0.0
- co-occurrence: blocker_type=choice_overload; blocker_type=return_friction; info_sought_elsewhere=other_marketplace_reviews
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

- `youtube` `dad39da4107a1e4b` ([source](https://www.youtube.com/watch?v=FvFp0LekZl0&lc=UgwwdEvPUstIZDaB1Fl4AaABAg.9SF5yNmhdgk9Vv2s7CL8Y6)): "better Design and Better price than In Myntra"

### 17. decide_later

- opportunity score: 0.0
- score components: sqrt(prevalence_norm)=0.32; severity_norm=0.50; actionability=0.71; evidence_confidence=0.00
- prevalence: 3.0% (Wilson 95% CI 2.0%–4.4%)
- supporting documents (Stage 4): 24
- distinct authors: 22
- genuine-intent documents: 14
- genuine-intent score: 0.0
- co-occurrence: uncertainty_type=will_it_fit; blocker_type=fit_size_uncertainty; blocker_type=price_absolute

**Evidence**

No unflagged quote available for this theme.

### 18. seeking_opinion

- opportunity score: 0.0
- score components: sqrt(prevalence_norm)=0.25; severity_norm=0.50; actionability=1.00; evidence_confidence=0.00
- prevalence: 1.8% (Wilson 95% CI 1.0%–2.9%)
- supporting documents (Stage 4): 14
- distinct authors: 14
- genuine-intent documents: 12
- genuine-intent score: 0.0
- co-occurrence: blocker_type=fit_size_uncertainty; uncertainty_type=will_it_fit; info_sought_elsewhere=brand_site_size_chart
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

No unflagged quote available for this theme.

### 19. inspiration_bookmark

- opportunity score: 0.0
- score components: sqrt(prevalence_norm)=0.16; severity_norm=0.23; actionability=0.57; evidence_confidence=0.00
- prevalence: 0.9% (Wilson 95% CI 0.4%–1.8%)
- supporting documents (Stage 4): 7
- distinct authors: 7
- genuine-intent documents: 1
- genuine-intent score: 0.0
- co-occurrence: info_sought_elsewhere=youtube_haul; uncertainty_type=will_it_fit
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

No unflagged quote available for this theme.

### 20. price_expectation

- opportunity score: 0.0
- score components: sqrt(prevalence_norm)=0.13; severity_norm=0.76; actionability=0.40; evidence_confidence=0.00
- prevalence: 0.6% (Wilson 95% CI 0.3%–1.5%)
- supporting documents (Stage 4): 5
- distinct authors: 5
- genuine-intent documents: 0
- genuine-intent score: 0.0
- co-occurrence: blocker_type=price_absolute; blocker_type=trust_authenticity; blocker_type=delivery_uncertainty
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

No unflagged quote available for this theme.

### 21. better_alternative_exists

- opportunity score: 0.0
- score components: sqrt(prevalence_norm)=0.10; severity_norm=0.75; actionability=0.75; evidence_confidence=0.00
- prevalence: 0.5% (Wilson 95% CI 0.2%–1.3%)
- supporting documents (Stage 4): 4
- distinct authors: 4
- genuine-intent documents: 2
- genuine-intent score: 0.0
- co-occurrence: blocker_type=choice_overload; blocker_type=delivery_uncertainty; blocker_type=fit_size_uncertainty
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

No unflagged quote available for this theme.

### 22. choice_overload

- opportunity score: 0.0
- score components: sqrt(prevalence_norm)=0.07; severity_norm=0.53; actionability=1.00; evidence_confidence=0.00
- prevalence: 0.4% (Wilson 95% CI 0.1%–1.1%)
- supporting documents (Stage 4): 3
- distinct authors: 3
- genuine-intent documents: 1
- genuine-intent score: 0.0
- co-occurrence: uncertainty_type=better_alternative_exists; blocker_type=delivery_uncertainty; blocker_type=fit_size_uncertainty
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

No unflagged quote available for this theme.

### 23. styling_uncertainty

- opportunity score: 0.0
- score components: sqrt(prevalence_norm)=0.07; severity_norm=0.80; actionability=1.00; evidence_confidence=0.00
- prevalence: 0.4% (Wilson 95% CI 0.1%–1.1%)
- supporting documents (Stage 4): 3
- distinct authors: 3
- genuine-intent documents: 0
- genuine-intent score: 0.0
- co-occurrence: blocker_type=fit_size_uncertainty; uncertainty_type=will_it_fit; blocker_type=color_fabric_accuracy
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

- `quora_manual` `69b854d91b105b4e` ([source](https://www.quora.com/unanswered/What-prevents-wishlisted-fashion-products-from-eventually-being-purchased)): "looks on me"

### 24. size_unavailable

- opportunity score: 0.0
- score components: sqrt(prevalence_norm)=0.00; severity_norm=0.50; actionability=0.50; evidence_confidence=0.00
- prevalence: 0.2% (Wilson 95% CI 0.1%–0.9%)
- supporting documents (Stage 4): 2
- distinct authors: 2
- genuine-intent documents: 1
- genuine-intent score: 0.0
- co-occurrence: blocker_type=fit_size_uncertainty; uncertainty_type=will_it_fit
- low confidence: supporting n_docs below the Stage 4 threshold

**Evidence**

No unflagged quote available for this theme.

## Discovery questions

Each question from `problemStatement.md` is answered with at least one number from the tagged corpus (or Stage 4 ranking) and a `doc_id` citation. AJIO aggregate figures are not used here.

### Q1. Why do users add fashion products to their wishlist?

Wishlist motivations in the tagged set: **70 of 800** tagged documents carry `price_watch`, `decide_later`, `compare_options`, `awaiting_occasion`, `budget_timing`, `inspiration_bookmark`, `size_unavailable`, `seeking_opinion`, `cart_proxy` (`play_store` `1034a87c82962c3c`).

### Q2. What prevents wishlisted products from eventually being purchased?

Purchase blockers: **329 of 800** tagged documents carry `fit_size_uncertainty`, `quality_doubt`, `color_fabric_accuracy`, `return_friction`, `delivery_uncertainty`, `trust_authenticity`, `choice_overload`, `styling_uncertainty`, `social_validation_needed`, `checkout_friction`, `price_absolute`, `price_expectation` (`consumer_complaints_in` `724f0048240a5c4d`).

### Q3. What uncertainties remain after users have identified a product they like?

Open uncertainties: **215 of 800** tagged documents carry `will_it_fit`, `how_does_it_look_on_me`, `is_quality_worth_it`, `true_color`, `occasion_appropriate`, `can_i_return`, `better_alternative_exists` (`consumer_complaints_in` `724f0048240a5c4d`).

### Q4. What causes users to postpone a purchase?

Postpone / wait signals: **41 of 800** tagged documents carry `decide_later`, `budget_timing`, `price_watch`, `awaiting_occasion` (`quora_manual` `cb9fd6691717983c`).

### Q5. How do users compare multiple shortlisted products?

Comparison behaviour: **5 of 800** tagged documents carry `compare_options`, `choice_overload` (`youtube` `7ea6f37b5752d6dc`).

### Q6. What information do users seek outside Myntra/AJIO before purchasing?

Information sought off-site: **17 of 800** tagged documents carry `youtube_haul`, `friend_family_opinion`, `other_marketplace_reviews`, `brand_site_size_chart`, `instagram_styling`, `offline_store_tryon` (`youtube` `b671cbe9cf656739`).

### Q7. What role do fit, size, styling, price, reviews, occasion, and social validation play?

Of 800 tagged documents, tag volumes are fit/size **90**; styling **3**; price **59**; reviews **3**; occasion **0**; social validation **0** (`consumer_complaints_in` `724f0048240a5c4d`).

### Q8. When do users use the wishlist as genuine purchase intent versus simply as a bookmarking mechanism?

**108** tagged documents are `genuine_intent`, **69** `bookmark_only`, **623** `ambiguous`, out of 800 (`app_store` `59edff26c32c6f5d`).

### Q9. How do these behaviors differ across user segments?

**2 of 800** tagged documents carry a `segment_cue` (`app_store` `59edff26c32c6f5d`). Stage 4 `segment_matrix.csv` has **3** cell(s) with lift ≥ 2.

### Q10. What unmet needs emerge consistently across user conversations?

The leading unmet-need cluster is `return_friction` (195 supporting documents, score 21.1; `app_store` `59edff26c32c6f5d`).

## AJIO on-site aggregates

AJIO-reported aggregates, not corpus documents: percentages AJIO computed from buyers who answered its own on-site prompts. They are post-purchase and self-selected, so they corroborate a text theme rather than establishing one, and no document, tag or prevalence figure anywhere in this report is derived from them.

Coverage: **51 product(s)**.

- **Average rating:** 3.7 across 51 product(s) (51 derived here as the weighted mean of the star distribution). AJIO's star buckets are individually rounded and sum to 96–100%, so a derived mean divides by their actual sum rather than by 100; dividing by 100 would count the rounding shortfall as zero-star ratings and understate every average.
- **Fit:** 47 product(s) carry the fit prompt; mean misfit response 31.5%, with 0 product(s) skewing loose and 1 skewing tight.
- **Quality:** 51 product(s) carry the quality prompt; mean Bad + Very Bad 16.2%.

### Cross-reference against the text themes

- **return_friction** (195 document(s), prevalence 24.4%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **delivery_uncertainty** (62 document(s), prevalence 7.8%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **trust_authenticity** (47 document(s), prevalence 5.9%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **quality_doubt** (46 document(s), prevalence 5.8%) — corroborated: 51 product(s) carry AJIO's quality prompt; mean Bad + Very Bad 16.2% (AJIO's own middle option, Average, is not counted as bad).
- **fit_size_uncertainty** (90 document(s), prevalence 11.2%) — corroborated: 47 product(s) carry AJIO's fit prompt; mean misfit response 31.5%; 1 of 47 (2%) have a misfit option as their most-answered; skew: 0 loose, 1 tight.
- **color_fabric_accuracy** (7 document(s), prevalence 0.9%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **true_color** (9 document(s), prevalence 1.1%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **checkout_friction** (15 document(s), prevalence 1.9%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **brand_site_size_chart** (9 document(s), prevalence 1.1%) — corroborated: 47 product(s) carry AJIO's fit prompt; mean misfit response 31.5%; 1 of 47 (2%) have a misfit option as their most-answered; skew: 0 loose, 1 tight.
- **price_absolute** (44 document(s), prevalence 5.5%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **cart_proxy** (4 document(s), prevalence 0.5%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **youtube_haul** (5 document(s), prevalence 0.6%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **budget_timing** (4 document(s), prevalence 0.5%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **other_marketplace_reviews** (3 document(s), prevalence 0.4%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **price_watch** (19 document(s), prevalence 2.4%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **compare_options** (3 document(s), prevalence 0.4%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **decide_later** (24 document(s), prevalence 3.0%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **seeking_opinion** (14 document(s), prevalence 1.8%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **inspiration_bookmark** (7 document(s), prevalence 0.9%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **price_expectation** (5 document(s), prevalence 0.6%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **better_alternative_exists** (4 document(s), prevalence 0.5%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **choice_overload** (3 document(s), prevalence 0.4%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **styling_uncertainty** (3 document(s), prevalence 0.4%) — not corroborated: AJIO publishes no aggregate prompt covering this theme.
- **size_unavailable** (2 document(s), prevalence 0.2%) — corroborated: 47 product(s) carry AJIO's fit prompt; mean misfit response 31.5%; 1 of 47 (2%) have a misfit option as their most-answered; skew: 0 loose, 1 tight.

## Segment differences

`segment_cue` × `blocker_type` cells whose lift is at least **2** relative to the tagged-corpus baseline (3 of 3 cells).

| segment | blocker | n_docs | lift |
| --- | --- | ---: | ---: |
| `first_time_online_buyer` | `checkout_friction` | 1 | 26.666667 |
| `first_time_online_buyer` | `price_absolute` | 1 | 9.090909 |
| `first_time_online_buyer` | `delivery_uncertainty` | 1 | 6.451613 |

## Excluded by constraint

Price-driven tags are scored by Stage 4 but the no-incentives rule keeps them out of the action the report would recommend. Volumes are shown so a reader can see what that constraint removed rather than wondering whether it was missed.

| tag | tagged documents |
| --- | ---: |
| `price_absolute` | 44 |
| `price_expectation` | 5 |
| `price_watch` | 19 |
| `budget_timing` | 4 |

**60 of 800** tagged documents carry at least one price-driven tag (`app_store` `1e14833dbf373e52`).

## Limitations

YouTube is 5336 of 5443 analyzable pre-purchase documents (98%). Haul-video audiences, comment-section self-selection, and influencer framing therefore dominate any pre-purchase claim.

Among non-YouTube analyzable documents, 153 are post-purchase, 107 pre-purchase, and 1531 mixed — a post-purchase tilt once haul comments are set aside.

Public conversation over-represents strong opinions; reviews and complaint boards in particular skew to extremity. The corpus is English/Hinglish because `hindi_language` is a hard exclusion, so Hindi-only hesitation is out of scope.

`trustpilot` yielded nothing (robots-restricted / expected zero-yield) and is absent from the documents table.

Quora (`quora_manual`) is a manual-only sample of threads a person saved; a share of answers arrived truncated, and authors and timestamps are often missing. AJIO on-site prose is absent: Akamai blocks automated collection and the site publishes no free-text Q&A to import.

Phase 4 tagged a sample, not the corpus: **800 of 7127** relevant documents (seed `42`, target 800; censused: `complaints_board`, `consumer_complaints_in`, `quora_manual`; the rest drawn proportionally). Read from `run_log` stage `tag_sample`. Prevalence figures are over the tagged set.

Tags are machine-assigned. Mean `flagged_evidence_share` across ranked areas is 75% — that share of supporting documents rest on a quote a human would not have chosen as evidence. The report shows only unflagged quotes.

**Hand-collected data (Quora threads and AJIO aggregates).** Two inputs were gathered manually rather than through the automated pipeline: Quora answers (`quora_manual`) and AJIO's on-site rating/fit/quality aggregates (`data/aggregates/`). Both were collected in a logged-in browser session using tools committed to `scripts/manual_extract/`, and both are point-in-time snapshots of a live site, collected on 2026-08-23 (per each record's `extracted_at`). Coverage for the AJIO aggregates is purposive — N=51 products chosen to match the themes surfaced by the text corpus — not a random or exhaustive sample, so the aggregate figures characterise those products rather than AJIO's catalogue. The aggregates also reflect customers who purchased and rated (buyers), not the wishlist-abandoners this study targets, and are used only to corroborate themes established in the text corpus, never as primary evidence. The rendered section notes, per figure, whether an average was read directly from AJIO or derived from its rating distribution. Because these two sources are collected manually from a live site, they are method-reproducible (tool and procedure committed) but not command-reproducible; re-collection yields a fresh snapshot.
