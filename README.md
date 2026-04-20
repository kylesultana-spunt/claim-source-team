# spunt-pipeline

Ingestion, classification, and automated-verdict pipeline for the spunt.mt fact-checker covering the Maltese general election.

This repo is the "back office" half of your setup: GitHub holds the code and the CSV data, a scheduled GitHub Actions workflow runs the pipeline, and the updated CSVs get committed back so Cloudflare Pages can serve them to the public site.

## Architecture

```
┌─────────────────┐      ┌───────────────────────────┐      ┌──────────────────┐
│  Maltese news   │─RSS─▶│  collector.py  →  inbox   │      │   Cloudflare     │
│  outlets        │      │  extractor.py  →  pending │      │   Pages serves   │
└─────────────────┘      │  analyser.py   →  queues  │─CSV─▶│   /data/*.csv    │
                         │  verdict.py    →  verdicts│      │   to index.html  │
                         │                           │      │                  │
                         │  GitHub Actions commits   │      │  Reads same-     │
                         │  data/*.csv back to repo  │      │  origin CSVs     │
                         └───────────────────────────┘      └──────────────────┘
```

## Data flow

| Stage      | Script         | Reads                           | Writes                              |
|------------|----------------|---------------------------------|-------------------------------------|
| collect    | `collector.py` | `config/sources.yml`, RSS feeds | `data/inbox.csv` (raw articles)     |
| extract    | `extractor.py` | `data/inbox.csv`                | `data/pending_claims.csv`           |
| analyse    | `analyser.py`  | `data/pending_claims.csv`       | `data/fact_check_queue.csv`, `data/review_queue.csv`, `data/rhetoric_archive.csv` |
| verdict    | `verdict.py`   | `data/fact_check_queue.csv`     | `data/verdicts.csv`                 |

The three queue CSVs keep exactly the schema your current frontend reads (`claim_text`, `atomic_claim`, `speaker`, …, `status`), so the site doesn't need changes. The new `verdicts.csv` is additive — point the frontend at it when you're ready to show verdicts.

## Quickstart (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-…

# Full pipeline:
python -m spunt all --root . -v

# Or a single stage:
python -m spunt collect
python -m spunt extract
python -m spunt analyse
python -m spunt verdict
```

## GitHub Actions setup

1. Add `ANTHROPIC_API_KEY` as a repository secret (Settings → Secrets → Actions).
2. Optional: set repository *variables* `SPUNT_MODEL_BULK`, `SPUNT_MODEL_REASONING`, `SPUNT_MODEL_VERDICT` to override the model defaults.
3. The workflow runs every 4 hours on `schedule`, or manually from the Actions tab. `workflow_dispatch` accepts a `stage` input if you want to run just one step (useful for re-analysis or re-verdicting after a prompt change).
4. The job commits any `data/` changes back to `main` with the bot identity `spunt-bot`.

## What changed vs. the previous pipeline

Looking at the uploaded CSVs I spotted four issues worth calling out, each addressed here:

1. **Near-duplicate inbox rows.** The same underlying claim appeared 5+ times in `inbox.csv` with small wording differences (e.g. the Tourism Accommodation Regulations 2026 paragraph). The new extractor deduplicates on a token-set fuzzy match (`rapidfuzz`, threshold 88) against every claim already in any queue, *before* writing, so a repeated press release can't bloat the queues.
2. **Speaker = "unknown" even when the article attributes the quote.** The extraction prompt now explicitly lists the known politicians and their aliases, and the collector passes that list in. The prompt instructs the model never to output "unknown" when the article names the speaker.
3. **Two-phase classification.** The previous setup seemed to extract-and-route in one step. Splitting into `pending_claims.csv` → classifier gives us atomic commits: if the classifier crashes mid-batch, the extracted claims are still safe and the next run picks up where we stopped.
4. **Automated verdicts with editorial guardrails.** The verdict stage runs fully automated as requested, but `requires_review` is force-set to `TRUE` whenever confidence ≤ 3 or fewer than 2 independent evidence sources were found. The frontend can render a "pending editorial review" badge so readers see the provenance.

## Directory layout

```
spunt-pipeline/
├── README.md
├── requirements.txt
├── .github/workflows/pipeline.yml   # scheduled run + commit
├── config/
│   ├── sources.yml                  # outlets + politician aliases
│   └── prompts/
│       ├── extract.md
│       ├── classify.md
│       └── verdict.md
├── data/                            # the live CSV "database"
│   ├── inbox.csv
│   ├── pending_claims.csv           # intermediate, safe to delete
│   ├── fact_check_queue.csv         # status = approved_for_check
│   ├── review_queue.csv             # status = queued_for_review
│   ├── rhetoric_archive.csv         # status = archived_rhetoric
│   └── verdicts.csv                 # NEW: automated verdicts
├── src/spunt/                       # python package
│   ├── schema.py      # column definitions + row dataclasses
│   ├── storage.py     # atomic CSV reads/writes
│   ├── dedup.py       # normalize + fuzzy near-dup
│   ├── llm.py         # anthropic client wrapper
│   ├── collector.py
│   ├── extractor.py
│   ├── analyser.py
│   ├── verdict.py
│   └── cli.py
└── tests/
    ├── test_dedup.py
    └── test_schema.py
```

## Model choices

| Stage      | Default model                   | Why                                            |
|------------|---------------------------------|------------------------------------------------|
| extract    | `claude-sonnet-4-6`             | Fast, accurate at JSON + attribution.          |
| classify   | `claude-sonnet-4-6`             | Cheap per call; decision is structured.        |
| verdict    | `claude-opus-4-6`               | Needs strong reasoning + web_search tool use.  |

Override via env: `SPUNT_MODEL_BULK`, `SPUNT_MODEL_REASONING`, `SPUNT_MODEL_VERDICT`.

## Editorial notes (important)

Automated verdicts on political claims during an election are high-stakes. Even though the pipeline will publish end-to-end, I strongly recommend:

- Treat `requires_review == TRUE` as a hard gate in the frontend — either hide those verdicts or render them with a prominent "Pending editorial review" label.
- Keep the pipeline auditable: every verdict row records the model, checked_at timestamp, and evidence URLs with retrieval date. Don't strip these fields before display.
- Archive evidence URLs with the Wayback Machine so contested sources don't disappear mid-election. (Easy next step — add an `archive.py` post-processor.)
