# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

MetalMind is a **risk-aware supplier intelligence platform** — NOT a price comparison tool. The core principle is **Trust > Price**: reliability and risk profile matter more than lowest cost. It finds trusted ACP (Aluminium Composite Panel) and multi-metal suppliers from India and China using AI-enhanced scoring.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run development server
uvicorn main:app --reload

# Run production server (Railway/Heroku via Procfile)
uvicorn main:app --host 0.0.0.0 --port $PORT
```

No test suite exists currently.

## Environment Variables

```
TAVILY_API_KEY      # Web search + enrichment
GEMMA_API_KEY       # Google Generative AI (Gemma 3.27B)
DATABASE_URL        # PostgreSQL (Railway) — falls back to SQLite if unset
```

## Architecture

**Request flow:**

```
Natural language query
  → engine/query_parser.py       (extract material, quantity, region, price target)
  → services/multi_search.py     (Serper primary + Tavily fallback, deduplication)
  → modules/cleaner.py           (normalize supplier name, URL, price, description)
  → modules/risk_scorer.py       (rule-based 0–1 risk score)
  → engine/ai_crosscheck.py      (Gemma validates rule scores; escalates if delta > 0.25)
  → modules/value_scorer.py      (combines price + risk: value = price_score × (1 − 0.6 × risk_score))
  → modules/ranker.py            (selects top results)
  → engine/recommendation.py     (decision text)
  → engine/ai_insight.py         (on-demand deep analysis per supplier)
```

**Key layers:**

| Layer | Files | Responsibility |
|-------|-------|----------------|
| API | `main.py`, `routes/suppliers.py` | FastAPI routes: `/analyze`, `/api/suppliers/*`, `/api/attachments/*` |
| Search | `services/serper_client.py`, `services/tavily_client.py`, `services/multi_search.py` | Multi-source search, merging, deduplication |
| Scoring | `modules/risk_scorer.py`, `modules/value_scorer.py`, `modules/ranker.py` | Rule-based risk + value computation |
| AI Engine | `engine/ai_engine.py`, `engine/ai_crosscheck.py`, `engine/ai_insight.py` | Gemma integration for validation and deep analysis |
| Data | `models.py`, `db.py` | SQLAlchemy ORM, schema migrations on startup |
| Frontend | `frontend/` | Vanilla JS SPA — `index.html` (search/results), `my-suppliers.html` (saved list), `supplier-assessment.html` (detail profile) |
| Config | `config.py` | All tunable thresholds — edit here, not inline |

## Scoring Logic

**Risk score (0–1):**
- < 0.25 → LOW: ISO certs, PVDF coating mentions, verified manufacturer signals
- 0.25–0.55 → MEDIUM: partial info, mixed signals
- ≥ 0.55 → HIGH: directory-only listings (Alibaba, IndiaMART, Made-in-China), no website, no contact

**Value score formula** (`config.py`):
```python
value_score = price_score × (1 - RISK_WEIGHT × risk_score)
# RISK_WEIGHT = 0.6
# HIGH_RISK_VALUE_CAP = 0.30 (soft cap decay at 0.60–0.80 risk)
```

**Decision threshold:** `value_score ≥ 0.5 AND risk_level ≠ HIGH`

## Database

Two tables (`models.py`):
- **`SavedSupplier`** — supplier data + user decision fields (rating, tags, quoted price, MOQ, lead time, payment terms, incoterms, sample status, factory verification, material specs). Soft-deleted via `is_saved=False` to preserve user notes.
- **`SupplierAttachment`** — user-uploaded documents (PDF, Excel, Word, images) linked to suppliers.

Schema migrations run automatically on startup in `db.py` via `ALTER TABLE IF NOT EXISTS` patterns.

## Key Design Decisions

- **No fake/mock data** — all supplier results must come from Serper or Tavily APIs.
- **AI is a cross-checker, not the primary scorer** — rule-based scores run first; Gemma validates and can escalate risk if its assessment exceeds the rule score by > 0.25.
- **Config-driven thresholds** — all scoring constants live in `config.py`; never hardcode them inline.
- **Multi-metal pricing** — `config.py` holds category median prices for ACP, aluminum, steel, stainless steel, copper, brass, etc., used for normalized price scoring.
- **Frontend is static** — FastAPI serves `frontend/` as static files; no build step needed.
