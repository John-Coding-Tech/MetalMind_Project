"""
engine/comparator.py

Phase 2 comparison layer.

Normalizes the expert system's output into a uniform schema, runs the
independent AI evaluation, and produces a structured diff per supplier.

Does NOT modify any existing module.

Unified schema (shared by expert and AI):
    {
      "score":      float in [0,1],
      "decision":   "recommended" | "not_recommended",
      "risk_score": float in [0,1],
      "reasons":    [str],
      "risk_flags": [str],
    }

Comparison schema:
    {
      "decision_match":  bool,
      "score_gap":       float,
      "risk_gap":        float,
      "conflict_type":   "aligned" | "expert_high_ai_low" | "ai_high_expert_low",
      "ai_extra_risks":  [str],
    }
"""

from modules.risk_scorer   import RiskLevel
from modules.value_scorer  import ValuedSupplier
from engine.ai_engine      import ai_evaluate
from config                import DECISION_SCORE_THRESHOLD


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFLICT_ALIGNED = "aligned"
_CONFLICT_EXP_HI  = "expert_high_ai_low"
_CONFLICT_AI_HI   = "ai_high_expert_low"


# ---------------------------------------------------------------------------
# Expert → unified-dict adapter
# ---------------------------------------------------------------------------

def expert_to_dict(valued: ValuedSupplier) -> dict:
    """
    Convert a ValuedSupplier (which wraps ScoredSupplier) into the
    unified schema. This is an ADAPTER — it does not modify any existing
    expert module; it only reads their outputs.

    Decision rule (Risk > Price):
        recommended  iff  value_score >= 0.5  AND  risk_level != High
    """
    scored = valued.scored

    decision = (
        "recommended"
        if valued.value_score >= DECISION_SCORE_THRESHOLD
        and scored.risk_level != RiskLevel.HIGH
        else "not_recommended"
    )

    # The existing expert system emits only free-form risk_reasons.
    # We populate both reasons and risk_flags from the same source; the
    # comparator's set-difference will surface AI-unique flags.
    reasons = list(scored.risk_reasons)

    return {
        "score":      round(valued.value_score, 4),
        "decision":   decision,
        "risk_score": round(scored.risk_score, 4),
        "reasons":    reasons,
        "risk_flags": reasons,
    }


# ---------------------------------------------------------------------------
# Core comparison
# ---------------------------------------------------------------------------

def compare_results(expert: dict, ai: dict) -> dict:
    """
    Compare two unified-schema dicts and return a structured diff.

    conflict_type semantics:
        aligned            — decisions match
        expert_high_ai_low — expert recommends, AI does not
        ai_high_expert_low — AI recommends, expert does not
    """
    expert_decision = expert.get("decision")
    ai_decision     = ai.get("decision")

    decision_match = expert_decision == ai_decision

    score_gap = round(
        abs(float(expert.get("score", 0)) - float(ai.get("score", 0))),
        4,
    )
    risk_gap = round(
        abs(float(expert.get("risk_score", 0)) - float(ai.get("risk_score", 0))),
        4,
    )

    expert_flags   = set(expert.get("risk_flags") or [])
    ai_flags       = set(ai.get("risk_flags") or [])
    ai_extra_risks = sorted(ai_flags - expert_flags)

    if decision_match:
        conflict_type = _CONFLICT_ALIGNED
    elif expert_decision == "recommended" and ai_decision == "not_recommended":
        conflict_type = _CONFLICT_EXP_HI
    else:
        conflict_type = _CONFLICT_AI_HI

    return {
        "decision_match":  decision_match,
        "score_gap":       score_gap,
        "risk_gap":        risk_gap,
        "conflict_type":   conflict_type,
        "ai_extra_risks":  ai_extra_risks,
    }


# ---------------------------------------------------------------------------
# Integration helper — per-supplier expert + AI + comparison bundle
# ---------------------------------------------------------------------------

def evaluate_supplier(valued: ValuedSupplier) -> dict:
    """
    Run expert adapter + independent AI evaluation + comparator for ONE
    supplier. Matches the Phase 2 integration spec.

    Returns:
        {
          "supplier":   <name>,
          "expert":     {...},
          "ai":         {...},
          "comparison": {...},
        }
    """
    expert_result = expert_to_dict(valued)
    ai_result     = ai_evaluate(valued.scored.record)
    comparison    = compare_results(expert_result, ai_result)

    return {
        "supplier":   valued.scored.record.name,
        "expert":     expert_result,
        "ai":         ai_result,
        "comparison": comparison,
    }


def evaluate_all(valued_suppliers: list[ValuedSupplier]) -> list[dict]:
    """
    Convenience wrapper: run the full expert + AI + comparison bundle for
    every supplier in a list.
    """
    return [evaluate_supplier(v) for v in valued_suppliers]
