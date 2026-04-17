"""
config.py — Centralized tuning constants.

Single source of truth for all decision thresholds and scoring coefficients.
Previously these were duplicated across main.py, value_scorer.py,
comparator.py, and ai_engine.py; drift between copies caused subtle bugs.

ALL tunable numbers belong here.
"""

# ---------------------------------------------------------------------------
# Decision thresholds (0-1 scale)
# ---------------------------------------------------------------------------

# A supplier is "recommended" when value_score >= this threshold
# AND risk_level is not HIGH.
DECISION_SCORE_THRESHOLD: float = 0.5

# Risk-level cutoffs — applied uniformly across rule-based, AI-only,
# and AI-comparison pipelines so the same supplier never gets different
# risk labels in different modes.
RISK_LEVEL_LOW_MAX:    float = 0.25   # risk_score < LOW_MAX  → Low
RISK_LEVEL_MEDIUM_MAX: float = 0.55   # risk_score < MEDIUM_MAX → Medium, else High


# ---------------------------------------------------------------------------
# Value-scoring coefficients
# ---------------------------------------------------------------------------

# Formula: value_score = price_score * (1 - RISK_WEIGHT * risk_score)
RISK_WEIGHT: float = 0.6

# Soft-cap zone for high-risk suppliers — value smoothly decays toward
# HIGH_RISK_VALUE_CAP as risk_score moves from DECAY_START to DECAY_END.
# Previously this was a hard step at 0.7 causing discontinuous ranks.
HIGH_RISK_DECAY_START: float = 0.60
HIGH_RISK_DECAY_END:   float = 0.80
HIGH_RISK_VALUE_CAP:   float = 0.30


# ---------------------------------------------------------------------------
# AI vs Expert reconciliation
# ---------------------------------------------------------------------------

# If AI's risk_score exceeds expert's by more than this, flag tier as yellow
AI_RISK_ESCALATION: float = 0.25


# ---------------------------------------------------------------------------
# Pipeline sizing (can also be overridden via env vars in main.py)
# ---------------------------------------------------------------------------

AI_COMPARE_TOP_N_DEFAULT: int = 3
AI_ONLY_TOP_N_DEFAULT:    int = 3
AI_MAX_PARALLEL_DEFAULT:  int = 5
