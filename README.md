# AJIO Wishlist-to-Purchase Discovery Engine

A code-first research pipeline: collect public conversations, tag them against a
wishlist taxonomy, score opportunity areas, and render a static markdown report.
It is **not** a chatbot, agent, or MCP server.

On this machine Python is **`.venv\Scripts\python.exe` (3.12.7)**. The Windows
`python` on PATH does not have the project dependencies (`ModuleNotFoundError:
yaml` is that interpreter). Every command below assumes you are in the project
root and using the venv binary.

## Setup

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

Fill `.env` (never commit it):

| Variable | Required | Used for |
| --- | --- | --- |
| `GROQ_API_KEY` | yes | Tagging (`openai/gpt-oss-120b`) and triage (`openai/gpt-oss-20b`) |
| `YOUTUBE_API_KEY` | yes | YouTube Data API v3 comments |
| `HASH_SALT` | yes | Author pseudonymization — set once and keep it |
| `REDDIT_*` | no | Reddit is disabled in `config.yaml` |

```powershell
.venv\Scripts\python.exe scripts\check_credentials.py
```

## Run order

```powershell
.venv\Scripts\python.exe scripts\check_credentials.py
.venv\Scripts\python.exe scripts\verify_sources.py
.venv\Scripts\python.exe -m src.collect.manual
.venv\Scripts\python.exe -m src.collect.run_collection
.venv\Scripts\python.exe scripts\audit_collection.py
.venv\Scripts\python.exe -m src.store.build_corpus          # --no-tier2 for offline
.venv\Scripts\python.exe -m scripts.audit_rejected_pool
.venv\Scripts\python.exe -m src.tag.run_tagging --dry-run
.venv\Scripts\python.exe -m scripts.build_tag_sample --target 800
.venv\Scripts\python.exe -m src.tag.run_tagging --resume
.venv\Scripts\python.exe -m src.quantify.run_quantification
.venv\Scripts\python.exe -m src.synthesize.run_synthesis --force
```

Re-render the report from an already-tagged corpus (no API calls):

```powershell
.venv\Scripts\python.exe -m src.synthesize.run_synthesis --force
```

Confirm the Phase 7 gates against the live corpus (does **not** wipe data):

```powershell
.venv\Scripts\python.exe scripts\verify_hardening.py
.venv\Scripts\python.exe -m pytest tests\ -q
```

## Expected runtime and cost

Figures are measured, not projected. Tagging tokens are ~645/document.

| Job | Tokens | Free tier (200k TPD) | Paid tagging cost |
| --- | --- | --- | --- |
| Tag sample (800 of 7,127, seed 42) | ~516k | **3 days** | **~$0.14** |
| All 7,127 relevant documents | ~4.60M | 23 days | ~$1.27 |

Collection of the current snapshot was ~55,913 raw records → 26,718 documents →
**7,127 relevant** (5,443 pre-purchase). Re-collecting YouTube re-spends quota
and writes a *new* date partition; it is not how you reproduce an existing
report.

A second `run_tagging --resume` over an unchanged corpus issues **zero** Groq
calls. The cache, not Groq's best-effort `seed`, is the freeze.

## Reproducing the report

A fresh clone plus `.env` plus the run-order commands rebuilds the report **from
`data/raw`**. Do **not** delete `data/interim` on a machine that has already
tagged: that directory holds `llm_cache` and `doc_tags`, which are the
reproducibility mechanism.

Two quantify runs from the same tagged corpus and config write identical
`opportunity_scores.csv` (asserted in `tests/test_hardening.py`).

**Hand-collected inputs** (Quora threads, AJIO on-site aggregates) are
method-reproducible — tools live in `scripts/manual_extract/` — but **not**
command-reproducible. Re-grabbing yields a fresh snapshot. That exemption is
stated in the report's Limitations section, with product count and date range
read from the records. Quora: 182 records in `data/raw/quora_manual/2026-08-24/`
(107 relevant after the corpus rebuild). AJIO aggregates: 51 products under
`data/aggregates/ajio/`.

`data/raw/`, `data/interim/`, and `data/processed/` are gitignored (large, and
raw text can carry personal details). `data/manual/` and `data/aggregates/` are
kept so a clone still has the inputs that cannot be rebuilt from a command.

## Outputs

Written by `src.synthesize.run_synthesis`:

| File | What it is |
| --- | --- |
| `outputs/opportunity_report.md` | The seven-section report |
| `outputs/evidence_appendix.md` | Cited quotes plus the pipeline run log (tokens, wall-clock) |
| `outputs/opportunity_scores.csv` | Copy of Stage 4 ranking |
| `outputs/segment_matrix.csv` | Segment × blocker lift |
| `outputs/tagger_validation.md` | Gold-set F1/precision — not measured until a gold set exists |

## Explorer (read-only)

An interactive Streamlit app over **already-computed** results, laid out to the
Stitch screens (editorial light canvas, AJIO red, 260px sidebar). It does not
collect, tag, or re-score. Ranked opportunity areas, source/dimension filters,
full-corpus vs genuine-intent, evidence quotes, the 800 tagged documents, and
AJIO corroboration are all offline. Ask is optional: one Groq call over the
CSV snapshot plus appendix quotes — not a pipeline re-run.

Placeholder numbers in the Stitch HTML mocks (e.g. 12,480 conversations) are
**not** shown; every figure is from `opportunity_scores.csv` and the tagged sample.

```powershell
.venv\Scripts\python.exe -m streamlit run app/explorer.py
```

Open the URL Streamlit prints (typically http://localhost:8501). Use the sidebar
to switch Overview, Opportunity Map, Evidence, Segments, AJIO Corroboration, and
Ask. Full-corpus vs genuine-intent is a toggle, not a re-score. Explorer tabs
work without `GROQ_API_KEY`. The Ask tab stays disabled until that key is in
`.env`.

## Evaluator app (demo + live tagger)

A one-file Streamlit UI (`app/streamlit_app.py`) for reviewers, styled to the
Stitch screens (sidebar, Test the Engine input/results, methodology, overview).
It does **not** rebuild the pipeline or write `documents` / `doc_tags`.

- **Test the Engine** (default): sample cards use three frozen corpus examples
  (Play Store return, YouTube fit question, Quora wishlist bookmark). Paste /
  JSONL live mode wraps `TaggingClient.tag_batch` for one string.
- **Overview / Opportunity Map / Evidence / Segments**: read-only views of the
  tagged corpus and `opportunity_scores.csv`. Mock KPIs from the Stitch HTML
  are not shown.
- **Methodology**: pipeline stages and tagging principles.

### Run locally

```powershell
.venv\Scripts\python.exe -m pip install -r app/requirements.txt
.venv\Scripts\python.exe -m streamlit run app/streamlit_app.py
```

Demo mode works with **no Groq key**. Live mode reads `GROQ_API_KEY` from
Streamlit secrets only — not from the repo `.env`. To try live locally, create
`.streamlit/secrets.toml` (gitignored) with:

```toml
GROQ_API_KEY = "gsk_..."
```

### Deploy to Streamlit Community Cloud

1. Point the Cloud app at `app/streamlit_app.py` (or this repo) with
   `app/requirements.txt` as the requirements file. The repo root must be on
   the Python path so `src.tag` imports resolve; the app inserts the project
   root itself.
2. In the Cloud app: **Settings → Secrets** and add:

```toml
GROQ_API_KEY = "gsk_..."
```

3. Without that secret, Live is disabled and Demo still works. Do not put the
   key in the repo or in `config.yaml`.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest tests\ -q
```
