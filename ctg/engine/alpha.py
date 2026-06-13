"""Layer 4 — Alpha Discovery Engine.

Alpha = (what should happen)  vs  (what is happening).
We assemble per-symbol evidence from the agents' factual metrics and score a
directional edge for each name. Each contributor is a small, explainable factor:

  flow_pressure   institutional accumulation/distribution (bulk/block deals)
  positioning     index options bias spilling to large-caps (beta-weighted)
  valuation       cheap/expensive vs sector median
  mean_reversion  oversold/overbought (RSI) against the prevailing trend
  momentum        1m/3m trend persistence
  sentiment       narrative tilt
  graph_contagion second-order pull from correlated/linked names

The engine returns ranked candidates with a decomposed score so the CIO and the
dashboard can explain *why* an edge exists, not just that it exists.
"""
from __future__ import annotations

import numpy as np

from ..data.universe import Universe, load_universe
from ..logging_conf import get_logger
from ..storage.db import latest_agent_output
from .quant import price_features
from .knowledge_graph import neighbors

log = get_logger("engine.alpha")


def _clip(x, lo=-1.0, hi=1.0):
    return float(np.clip(x, lo, hi))


def discover(universe: Universe | None = None) -> list[dict]:
    u = universe or load_universe()

    flow = latest_agent_output("flow") or {}
    options = latest_agent_output("options") or {}
    valuation = latest_agent_output("valuation") or {}
    sentiment = latest_agent_output("sentiment") or {}
    regime = latest_agent_output("regime") or {}

    # index option bias -> small market beta tilt
    opt_bias = (options.get("view", {}) or {}).get("market_bias", "neutral")
    market_tilt = {"bullish": 0.25, "bearish": -0.25, "neutral": 0.0}.get(opt_bias, 0.0)
    # FII index-futures positioning (the doc's single most valuable flow signal):
    # heavy net-short FII => downward market tilt.
    fii_fno = (flow.get("fno", {}) or {}).get("fii", {})
    fii_long_pct = fii_fno.get("idx_fut_long_pct")
    if fii_long_pct is not None:
        market_tilt += _clip((fii_long_pct - 50) / 50.0 * 0.25, -0.25, 0.25)
    market_tilt = _clip(market_tilt, -0.45, 0.45)

    # flow deal pressure map (per symbol)
    deal_map = {d["symbol"]: d for d in flow.get("deal_pressure", [])}

    # valuation map
    val_map = {r["symbol"]: r for r in valuation.get("rankings", [])}

    # sentiment overall
    senti_score = (sentiment.get("view", {}) or {}).get("sentiment_score", 0.0) or 0.0

    regime_score = regime.get("risk_score", 50.0) or 50.0
    risk_on = (regime_score - 50) / 50.0  # -1..1

    candidates = []
    for sym in u.symbols:
        pf = price_features(sym)
        if not pf:
            continue
        factors: dict[str, float] = {}

        # 1) flow pressure
        d = deal_map.get(sym)
        if d:
            factors["flow_pressure"] = _clip(np.tanh(d["net_value_cr"] / 25.0))

        # 2) positioning (market tilt scaled by trend agreement)
        factors["positioning"] = _clip(market_tilt * (1 if pf["above_ma50"] else -1) * 0.6)

        # 3) valuation
        v = val_map.get(sym)
        if v:
            factors["valuation"] = _clip((1.0 - v["rel_pe"]) * 0.8)  # cheap -> positive

        # 4) mean reversion (RSI vs trend)
        rsi = pf.get("rsi14")
        if rsi is not None:
            if rsi < 30:
                factors["mean_reversion"] = _clip((30 - rsi) / 30.0)
            elif rsi > 70:
                factors["mean_reversion"] = _clip(-(rsi - 70) / 30.0)

        # 5) momentum
        mom = pf.get("mom_3m_pct")
        if mom is not None:
            factors["momentum"] = _clip(np.tanh(mom / 15.0))

        # 6) sentiment (market-wide, mild per-name)
        if senti_score:
            factors["sentiment"] = _clip(senti_score * 0.4)

        # 7) graph contagion: pull from strongly correlated neighbours' edges
        factors["graph_contagion"] = _clip(_contagion(sym, deal_map))

        if not factors:
            continue

        # Regime-aware weighting: in stress, fade momentum & lean defensive/valuation
        weights = _regime_weights(regime.get("label", ""))
        score = sum(weights.get(k, 1.0) * v for k, v in factors.items())
        # normalise by total applied weight
        wsum = sum(weights.get(k, 1.0) for k in factors)
        score = score / wsum if wsum else 0.0
        score = _clip(score)

        direction = "LONG" if score > 0 else "SHORT"
        candidates.append({
            "symbol": sym,
            "company": u.company(sym),
            "sector": u.sector(sym),
            "raw_score": round(score, 3),
            "abs_edge": round(abs(score), 3),
            "direction": direction,
            "factors": {k: round(v, 3) for k, v in factors.items()},
            "price_features": pf,
            "risk_on_context": round(risk_on, 2),
        })

    candidates.sort(key=lambda c: c["abs_edge"], reverse=True)
    # snapshot the full factor panel for the forward-return eval loop
    try:
        from .evals import snapshot_candidates
        snapshot_candidates(candidates)
    except Exception as exc:  # noqa: BLE001
        log.warning("snapshot failed: %s", exc)
    log.info("Alpha engine produced %d candidates", len(candidates))
    return candidates


def _contagion(sym: str, deal_map: dict) -> float:
    """Second-order: if correlated neighbours see institutional flow, inherit a fraction."""
    nb = neighbors(sym, depth=1)
    if not nb.get("found"):
        return 0.0
    pull = 0.0
    for e in nb["edges"]:
        if e["relation"] == "correlated":
            other = e["target"] if e["source"] == sym else e["source"]
            d = deal_map.get(other)
            if d:
                pull += np.tanh(d["net_value_cr"] / 50.0) * float(e["weight"]) * 0.3
    return _clip(pull)


def _regime_weights(label: str) -> dict[str, float]:
    # Start from learned weights (IC-adaptive) if the eval loop has run, else base.
    from .evals import BASE_WEIGHTS, get_learned_weights
    base = dict(get_learned_weights() or BASE_WEIGHTS)
    if "Stress" in label or "Risk-Off" in label:
        base["momentum"] = 0.5
        base["mean_reversion"] = 1.4   # bounces matter in stress
        base["valuation"] = 1.3
        base["flow_pressure"] = 1.5
    elif "Risk-On" in label or "Expansion" in label:
        base["momentum"] = 1.4
        base["mean_reversion"] = 0.7
    return base
