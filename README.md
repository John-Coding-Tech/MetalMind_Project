# MetalMind

An AI-driven supplier intelligence platform for sourcing metals and composite panels. MetalMind is **not a price comparison tool** — it is a risk-aware decision engine that helps procurement teams identify the most trustworthy supplier, not the cheapest one.

**Core principle: Trust > Price > Speed**

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create a .env file with your API keys
TAVILY_API_KEY=your_tavily_key
GEMMA_API_KEY=your_gemma_key
DATABASE_URL=postgresql://...   # optional — falls back to SQLite

# 3. Start the server
uvicorn main:app --reload
```

Open `http://localhost:8000` in your browser. The frontend is served automatically as static files.

### Health check

```
GET /api/health?check_serper=true&check_tavily=true&check_gemma=true
```

---

## What It Does

Type a plain-English sourcing request. MetalMind handles everything from there:

```
"6061 aluminium plate from China, budget USD 2800–3500/ton"
"ACP marble finish India or Vietnam PVDF coated"
"stainless steel 304 sheet below $3/kg"
```

The system searches live supplier data across multiple sources, scores each supplier on risk and value, selects a recommended winner, and uses AI to audit and explain the decision. You can then save suppliers, track RFQ progress, upload quotes, and record factory verification status.

---

## Architecture

The analysis pipeline runs in three sequential layers whenever `POST /api/analyze` is called.

```
User query (natural language)
        │
        ▼
┌───────────────────────────┐
│  1. Query Parser          │  Regex + Gemma → category, countries,
│  engine/query_parser.py   │  material grade, price range, variant
└───────────┬───────────────┘
            │
            ▼
┌───────────────────────────┐
│  2. Multi-Source Search   │  Serper (primary) + Tavily (fallback)
│  services/multi_search.py │  Fan-out across (country × angle) plans
└───────────┬───────────────┘
            │
            ▼
┌───────────────────────────┐
│  3. Cleaner               │  Normalise name, URL, price (regex +
│  modules/cleaner.py       │  currency conversion), relevance filter
└───────────┬───────────────┘
            │
            ▼
┌───────────────────────────┐
│  LAYER 1: Rule Engine     │  Risk scoring → Value scoring → Ranking
│  modules/risk_scorer.py   │  This is the primary decision maker.
│  modules/value_scorer.py  │  AI cannot override these scores.
│  modules/ranker.py        │
└───────────┬───────────────┘
            │
            ▼
┌───────────────────────────┐
│  LAYER 2: AI Cross-Check  │  Gemma audits the rule-based winner.
│  engine/ai_crosscheck.py  │  Returns is_valid + risk warnings only.
└───────────┬───────────────┘
            │
            ▼
┌───────────────────────────┐
│  LAYER 3: Trust Signal    │  Combines rule verdict + AI verdict
│  main.py                  │  into "safe" / "warning" / "risk" label
└───────────┬───────────────┘
            │
            ▼
         Response
   (winner, top-3, all suppliers,
    trust label, price reference band)
```

On-demand AI Insight (`POST /api/insight`) is a separate, user-triggered call that generates a natural-language explanation for any specific supplier. It never changes scores.

---

## Agentic AI Design

MetalMind uses AI in three distinct roles, each with deliberately constrained authority:

### Role 1 — Query Parser (`engine/query_parser.py`)

The parser converts a natural-language message into a structured search plan. It uses a three-layer fallback:

1. **Regex always runs first** — deterministic keyword extraction for category, countries, material grade, variant, price range, and spec standards. Latency: ~0 ms.
2. **LLM called only when regex is not confident** — a one-shot Gemma prompt with a hard 1-second timeout extracts structured fields from vague queries. On timeout or failure, falls back to the regex result.
3. **Raw passthrough** — if neither layer produces a usable category, the raw query string is passed through for a best-effort search.

The LLM adds a `needs_clarification` guardrail: if a query is genuinely too vague (e.g. just "metal"), it returns a clarification question instead of triggering an expensive blind search.

### Role 2 — AI Cross-Check (`engine/ai_crosscheck.py`)

After the rule engine ranks suppliers and selects a winner, Gemma acts as an independent auditor. It reviews the winner's profile against the top alternatives and returns a validation verdict only — it does **not** assign scores or re-rank. Possible outcomes:

- `is_valid: true` — rule engine choice confirmed
- `is_valid: true` + `risk_warnings` — confirmed but with flagged concerns
- `is_valid: false` — serious problem detected (fraud signal, country mismatch, fake contact info); a small negative adjustment is applied to the winner's value score

A silent AI failure always returns `is_valid: true` so the rule-approved recommendation is never silently flipped to red.

### Role 3 — AI Insight (`engine/ai_insight.py`)

A user-triggered deep analysis of a single supplier. Gemma produces a structured explanation containing:
- `summary` — 2–3 sentence factual overview
- `key_strengths` — positive signals from the scraped page content
- `key_risks` — risks the rule engine may have captured or missed
- `hidden_signals` — subtle concerns that keyword matching cannot surface

The insight module is explicitly forbidden from scoring, ranking, or using recommendation language. It describes; the rule engine decides.

---

## Scoring Algorithms

All tunable constants live in `config.py`.

### Risk Scoring (`modules/risk_scorer.py`)

Each supplier is scored on a 0–1 scale by summing weighted penalties across five checks:

| Check | What it measures | Max penalty |
|-------|-----------------|-------------|
| URL quality | Directory aggregator (Alibaba, IndiaMART, Made-in-China, etc.) or missing URL | 0.40 |
| Description quality | ISO/cert keywords, manufacturing indicators, description length | 0.30 |
| Contact information | Phone pattern, email pattern, or contact keyword presence | 0.15 |
| Price reasonableness | Dataset-relative outlier (> 2 std devs below median) or absolute floor | 0.25 |
| Country/domain consistency | `.cn` domain claiming India, `.in` domain claiming China | 0.15 |

Penalties accumulate and are capped at 1.0. Risk level thresholds (from `config.py`):

```
risk_score < 0.25  →  Low
risk_score < 0.55  →  Medium
risk_score ≥ 0.55  →  High
```

Price reasonableness uses dataset-relative detection when ≥ 3 suppliers have real prices (compares against the dataset's own median ± 2 standard deviations). For smaller datasets it falls back to an absolute floor of $6/sqm USD.

### Value Scoring (`modules/value_scorer.py`)

```
value_score = price_score × (1 − RISK_WEIGHT × risk_score)
```

Where:
- `RISK_WEIGHT = 0.6` — risk discounts value at 60% weight
- `price_score` — normalized 0–1 score within the supplier's `(category, unit)` bucket. Uses dataset median when ≥ 3 peers share the bucket; falls back to `CATEGORY_MEDIANS_USD` from `config.py` otherwise.
- Suppliers with no extracted price receive `price_score = 0.75` (the "half-reward" convention: not penalized like an expensive supplier, but not rewarded like a cheap one).

**High-risk soft cap:** When `risk_score` is in the 0.60–0.80 range, value smoothly decays toward a cap of 0.30 instead of cutting off abruptly. This prevents discontinuous rank jumps at the boundary.

**Decision threshold:** A supplier is "recommended" only when `value_score ≥ 0.50 AND risk_level ≠ HIGH`.

### Hybrid Pricing (Path C)

When no real price can be extracted from the web, a model estimate is computed:

```
point_estimate =
    CATEGORY_MEDIANS_USD[(category, unit)]   ← base anchor
    × COUNTRY_PRICE_MULTIPLIER[country]      ← regional cost tier
    × SUPPLIER_TYPE_MULTIPLIER[type]         ← factory vs trader markup
    × VARIANT_PRICE_MULTIPLIER[variant]      ← coating / finish premium
    × scale_discount                         ← large factory hint

output_range = [point × 0.80,  point × 1.30]   ← asymmetric, right-skewed
```

Estimates are always ranges (never a single figure), displayed in a secondary grey style labeled "⚠ model", and only shown when a real price is absent. Real prices always take precedence.

When a real price exists, it is classified against the per-country market midpoint:

| Range status | Meaning |
|---|---|
| `suspicious_low` | price < 60% of market midpoint |
| `within` | 60% – 160% of midpoint |
| `above` | 160% – 220% of midpoint |
| `far_above` | > 220% of midpoint |

---

## Multi-Metal Search

The system supports any of these product categories in natural language:

| Category | Examples |
|---|---|
| `acp` | Aluminium composite panel, Alucobond, cladding panel |
| `aluminum` | 6061 plate, aluminum coil, aluminium sheet |
| `steel` | Carbon steel, mild steel Q235, steel plate |
| `stainless_steel` | SS 304, 316L sheet |
| `copper` | Copper plate, C1100 |
| `brass` | Brass sheet |
| `zinc` | Zinc sheet |
| `titanium` | Titanium sheet |
| `tube` / `pipe` | Metal tube, structural pipe |

The query parser recognises category keywords, material grades (6061, 304, A36, etc.), surface variants (PVDF coated, marble, brushed, mirror), country names and city-level hints (Shanghai, Gujarat, etc.) in English and Chinese characters, and price ranges in USD/AUD/EUR/CNY/INR with unit context.

---

## Frontend Pages

All three pages are vanilla HTML + JavaScript, served as static files by FastAPI.

| Page | Route | Purpose |
|---|---|---|
| `index.html` | `/` | Chat search, parse preview, results table, winner card, AI Insight modal |
| `my-suppliers.html` | `/my-suppliers` | Saved supplier list with decision tracking and file attachments |
| `supplier-assessment.html` | `/supplier/{id}/edit` | Per-supplier profile: rating, tags, RFQ fields, trust verification checklist |

Key frontend flows:
1. User types a query → `POST /api/parse` returns a structured parse preview with editable fields
2. User confirms (or edits) the parse → `POST /api/analyze` runs the full pipeline
3. Results include a winner card, top-3 comparison, and a full ranked list
4. Clicking "AI Insight" on any row → `POST /api/insight` for on-demand deep analysis
5. Saving a supplier persists it with all user notes, quotes, and verification status

Analysis results are cached in `localStorage` so the page survives a browser refresh without re-running the search.

---

## API Reference

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/parse` | POST | Parse a query string into structured fields (no search, no cost) |
| `/api/analyze` | POST | Full pipeline: search → score → rank → AI audit |
| `/api/insight` | POST | On-demand AI insight for a named supplier |
| `/api/health` | GET | Service health; optional `?check_serper=true&check_tavily=true&check_gemma=true` |
| `/api/suppliers` | GET/POST | Saved supplier CRUD |
| `/api/suppliers/{id}` | GET/PATCH/DELETE | Single saved supplier management |
| `/api/attachments` | POST | Upload file to a saved supplier |
| `/api/attachments/{id}` | GET/DELETE | Fetch or remove an attachment |

The `POST /api/analyze` request body:

```json
{
  "query":       "6061 aluminum plate from China",
  "parsed":      null,
  "max_results": 5,
  "debug":       false
}
```

Pass `"debug": true` to receive a `trace` object in the response with pipeline timings, raw URL count, and parse details.

---

## Configuration

All scoring constants are in `config.py`. Editing it is the only change needed to tune behavior — nothing is hardcoded elsewhere.

Key constants:

| Constant | Default | Effect |
|---|---|---|
| `DECISION_SCORE_THRESHOLD` | `0.5` | Minimum value score for "recommended" verdict |
| `RISK_WEIGHT` | `0.6` | How much risk discounts the value score |
| `RISK_LEVEL_LOW_MAX` | `0.25` | Upper bound for Low risk label |
| `RISK_LEVEL_MEDIUM_MAX` | `0.55` | Upper bound for Medium risk label |
| `HIGH_RISK_VALUE_CAP` | `0.30` | Maximum value score for high-risk suppliers |
| `AI_RISK_ESCALATION` | `0.25` | AI–rule delta that triggers a risk flag |
| `ANALYZE_TOTAL_BUDGET` | `20.0` s | Hard wall-clock cap per `/api/analyze` request |
| `DISPLAY_SUPPLIER_LIMIT` | `10` | Max rows shown in the results table |
| `MISSING_PRICE_SCORE` | `0.75` | Price score assigned when no price is scraped |

---

## Database

SQLAlchemy ORM with PostgreSQL (via `DATABASE_URL` env var) or SQLite fallback. Schema migrations run automatically on startup in `db.py`.

**`SavedSupplier`** — the core saved-supplier record. Includes all rule/AI scores plus user-entered procurement data:
- Commercial terms: `quoted_price`, `MOQ`, `lead_time`, `payment_terms`, `incoterms`
- Trust verification: `sample_status`, `sample_quality`, `factory_verified_via`, `warranty_years`
- Material specs: `coating_confirmed`, `core_material`, `fire_rating_confirmed`
- Soft-delete via `is_saved=False` — unsaving a supplier preserves all notes and history

**`SupplierAttachment`** — documents uploaded against a supplier (PDF, Excel, Word, images). Files are stored on the local filesystem with metadata (filename, content type, path) in the database.

---

## Deployment

The `Procfile` targets Railway/Heroku:

```
web: uvicorn main:app --host 0.0.0.0 --port $PORT
```

Required environment variables in production:

```
TAVILY_API_KEY     # web search (required)
GEMMA_API_KEY      # AI cross-check and insights (required for AI features)
DATABASE_URL       # PostgreSQL connection string (optional — SQLite if unset)
SERPER_API_KEY     # primary search engine (optional — falls back to Tavily)
```
