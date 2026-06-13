"""Layer 6 — Probabilistic Scenario Engine.

For each high-edge candidate, produce Bull/Base/Bear scenarios with probabilities
and target moves. Quant baseline uses realised volatility + edge direction;
the LLM (when available) refines the narrative and probabilities.
"""
from __future__ import annotations

import json

import numpy as np

from ..llm.client import llm
from ..logging_conf import get_logger

log = get_logger("engine.scenarios")


def build_scenarios(candidate: dict, regime: dict, use_llm: bool = True) -> dict:
    pf = candidate.get("price_features", {})
    vol = (pf.get("vol_annualised_pct") or 25.0) / 100.0
    horizon_days = 21
    sigma = vol * np.sqrt(horizon_days / 252.0)  # ~1m move stdev
    move = round(float(sigma * 100), 1)
    edge = candidate["raw_score"]

    # tilt probabilities by edge (-1..1) and regime risk
    p_bull = 0.33 + 0.18 * edge
    p_bear = 0.33 - 0.18 * edge
    p_bull = float(np.clip(p_bull, 0.1, 0.7))
    p_bear = float(np.clip(p_bear, 0.1, 0.7))
    p_base = round(1 - p_bull - p_bear, 2)

    quant = {
        "horizon": "~1 month",
        "expected_move_pct_1sigma": move,
        "scenarios": [
            {"name": "Bull", "prob": round(p_bull, 2), "target_move_pct": round(move, 1)},
            {"name": "Base", "prob": p_base, "target_move_pct": 0.0},
            {"name": "Bear", "prob": round(p_bear, 2), "target_move_pct": round(-move, 1)},
        ],
    }

    if not use_llm or not llm().available:
        quant["thesis"] = _rule_thesis(candidate, regime)
        quant["source"] = "rule_based"
        return quant

    prompt = (
        "Given this Indian-equity edge and market regime, refine a 1-month "
        "scenario tree. Keep probabilities summing to ~1.0.\n\n"
        f"CANDIDATE: {json.dumps(candidate, default=str)}\n"
        f"REGIME: {json.dumps(regime, default=str)}\n"
        f"QUANT_BASELINE: {json.dumps(quant)}\n\n"
        'Return JSON: {"thesis":"2-sentence reason","scenarios":'
        '[{"name":"Bull","prob":0.0,"target_move_pct":0.0,"trigger":".."},'
        '{"name":"Base","prob":0.0,"target_move_pct":0.0,"trigger":".."},'
        '{"name":"Bear","prob":0.0,"target_move_pct":0.0,"trigger":".."}],'
        '"key_risk":".."}'
    )
    out = llm().complete_json(prompt, default=None)
    if not isinstance(out, dict) or "scenarios" not in out:
        quant["thesis"] = _rule_thesis(candidate, regime)
        quant["source"] = "rule_based"
        return quant
    out["expected_move_pct_1sigma"] = move
    out["horizon"] = "~1 month"
    out["source"] = "llm"
    return out


def _rule_thesis(candidate: dict, regime: dict) -> str:
    f = candidate["factors"]
    top = sorted(f.items(), key=lambda kv: abs(kv[1]), reverse=True)[:2]
    drivers = ", ".join(f"{k} ({v:+.2f})" for k, v in top)
    return (f"{candidate['direction']} {candidate['symbol']} ({candidate['sector']}): "
            f"primary drivers {drivers}; regime {regime.get('label','n/a')}.")
