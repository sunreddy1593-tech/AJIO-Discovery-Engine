# AJIO Wishlist-to-Purchase Discovery Engine

## Role and product

You are a Product Manager on the Growth Team at **AJIO**.

AJIO is the chosen product. Millions of users browse fashion products, save items they like, and add products to their wishlists.

## Business context

A wishlist is a high-intent signal: the user has expressed explicit interest in an item but has stopped short of purchasing it.

Over time, users can accumulate dozens—or even hundreds—of wishlisted products, while only a small proportion eventually translate into purchases.

Improving wishlist-to-purchase conversion could:

- Increase purchase frequency
- Improve monetization from existing users
- Help the company extract greater value from high-intent demand already present on the platform

## Strategic goal (North Star metric)

**Increase the percentage of users who purchase at least one item from their wishlist within 30 days of adding it.**

## Constraints

- The underlying user problem is **not given**. It must be discovered from public user conversations and feedback.
- Solutions **cannot** offer monetary incentives to users (no discounts, coupons, cashback, or similar price-based levers). This also shapes how findings are framed: an opportunity area whose only lever is price is out of scope, so friction must be characterized in terms of information, confidence, effort, timing, and trust rather than cost.

## Part 1: Build an AI-powered discovery engine

Before proposing any solution, build an AI-powered system that analyzes user feedback at scale.

The engine must go beyond summarizing reviews or performing sentiment analysis. It should identify, quantify where possible, and compare potential opportunity areas that could influence the stated business metric.

### Data sources to analyze

- App Store reviews
- Play Store reviews
- Reddit discussions
- Fashion and shopping communities
- Social media conversations
- YouTube comments
- Product reviews and Q&A where relevant
- Other publicly available conversations about online fashion shopping

### Discovery questions the engine must help answer

1. Why do users add fashion products to their wishlist?
2. What prevents wishlisted products from eventually being purchased?
3. What uncertainties remain after users have identified a product they like?
4. What causes users to postpone a purchase?
5. How do users compare multiple shortlisted products?
6. What information do users seek outside Myntra/AJIO before purchasing?
7. What role do fit, size, styling, price, reviews, occasion, and social validation play?
8. When do users use the wishlist as genuine purchase intent versus simply as a bookmarking mechanism?
9. How do these behaviors differ across user segments?
10. What unmet needs emerge consistently across user conversations?

### Expected output of the discovery engine

- Identified user problems and unmet needs related to wishlist-to-purchase conversion
- Quantified (where possible) opportunity areas
- Comparison of opportunity areas by likely impact on the North Star metric
- Segment-level differences in wishlist behavior
- Evidence grounded in public user conversations, not generic fashion-e-commerce assumptions
- **Source mix disclosed** — counts by source, not only a total. A pre-purchase claim that is almost entirely YouTube haul comments is a different claim from one that also includes on-site Q&A and forum threads; "identify, quantify, compare" is only credible if Part 1 names that mix (and the YouTube concentration it is working against)

## What to build

A **code-first, multi-stage research pipeline in Python**, runnable from the editor as a sequence of scripts.

### Pipeline stages

1. **Collection** — pull public conversations from the sources listed above via APIs and scrapers, one collector module per source, each writing raw payloads to disk.
2. **Storage** — normalize raw payloads into a single document schema (id, source, url, author hash, timestamp, text, source-specific metadata) in a local database or columnar files; deduplicate and make collection idempotent.
3. **LLM tagging** — classify each document against a discovery taxonomy (wishlist motivation, blocker type, uncertainty type, information sought, segment cues, intent-vs-bookmark signal), with structured output, confidence, and quoted evidence spans. Cache responses keyed by document id and prompt version so re-runs do not re-spend tokens.
4. **Quantification** — aggregate tags into frequency, co-occurrence, and segment breakdowns; attach volume, severity, and confidence measures to each candidate opportunity area.
5. **Synthesis outputs** — produce the ranked, evidence-linked opportunity report and supporting tables.

### Engineering requirements

- **Reproducible**: every stage re-runnable from cached artifacts without re-scraping; raw data is written once and treated as immutable input to later stages.
- **Deterministic where possible**: fixed seeds, pinned model versions, temperature 0 for tagging, versioned prompts and taxonomy so results can be regenerated and diffed.
- **Configuration and secrets** in a `.env` file (never committed); no keys hardcoded in scripts.
- **Auditable**: each quantified claim traces back to specific source documents and quotes.

### Non-goals

Do **not** build:

- A chatbot or any chat UI
- An autonomous agent
- An MCP server
- Any interactive interface as the deliverable

The deliverable is a **structured dataset plus a quantified opportunity report**, not an interface.
