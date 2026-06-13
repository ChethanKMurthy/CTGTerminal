"""Risk & capital-allocation engine (the Dimon layer).

Turns ranked signals into a risk-disciplined target book and stress-tests it:
  * vol-targeted, conviction-weighted sizing (size down the volatile names),
  * per-name cap and per-SECTOR concentration cap,
  * drawdown de-grossing (cut exposure as equity falls from its peak),
  * historical VaR (95%) from holdings' realised returns,
  * scenario stress tests (crude, USDINR, FII exodus, rate, broad selloff)
    using the knowledge graph's sector->macro sensitivities + market beta.

"Fortress balance sheet": never let one name or one theme sink the book.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get_settings
from ..data.universe import SECTOR_MACRO_LINKS, load_universe
from ..logging_conf import get_logger
from ..storage.db import duck_df, kv_get, kv_set
from .quant import price_features

log = get_logger("engine.risk")


# ---------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------
def target_weights(signals: list[dict], equity: float, regime: dict,
                   top_n: int = 8, min_conviction: float = 30.0) -> dict:
    s = get_settings()
    max_pos = float(s.get("risk", "max_position_pct", default=0.10))
    max_gross = float(s.get("risk", "max_gross_exposure", default=1.0))
    max_sector = max_pos * 2.5  # sector concentration cap

    picks = [x for x in signals if x["conviction"] >= min_conviction and x.get("last")]
    picks = sorted(picks, key=lambda x: x["conviction"], reverse=True)[:top_n]
    if not picks:
        return {"weights": {}, "gross": 0.0, "degross_factor": 1.0, "notes": ["no qualifying signals"]}

    # drawdown de-grossing
    degross = _degross_factor(equity)
    # regime gross scaling: lighter in stress
    regime_score = regime.get("risk_score", 50.0) or 50.0
    regime_gross = float(np.clip(0.5 + regime_score / 100.0, 0.5, 1.0))
    gross_budget = max_gross * degross * regime_gross

    # conviction / volatility -> raw weight (vol targeting)
    raw = {}
    for x in picks:
        pf = price_features(x["symbol"]) or {}
        vol = (pf.get("vol_annualised_pct") or 25.0) / 100.0
        inv_vol = 1.0 / max(vol, 0.10)
        raw[x["symbol"]] = x["conviction"] * inv_vol

    tot = sum(raw.values()) or 1.0
    weights = {}
    sector_acc: dict[str, float] = {}
    u = load_universe()
    for x in picks:
        sym = x["symbol"]
        w = (raw[sym] / tot) * gross_budget
        w = min(w, max_pos)
        sec = u.sector(sym)
        if sector_acc.get(sec, 0.0) + w > max_sector:
            w = max(0.0, max_sector - sector_acc.get(sec, 0.0))
        if w <= 0.001:
            continue
        sector_acc[sec] = sector_acc.get(sec, 0.0) + w
        sign = 1 if x["direction"] == "LONG" else -1
        weights[sym] = sign * round(w, 4)

    notes = [f"degross×{degross:.2f}", f"regime_gross×{regime_gross:.2f}",
             f"sector_cap={max_sector:.0%}"]
    return {"weights": weights, "gross": round(sum(abs(v) for v in weights.values()), 3),
            "degross_factor": degross, "sector_exposure": {k: round(v, 3) for k, v in sector_acc.items()},
            "notes": notes}


def _degross_factor(equity: float) -> float:
    peak = kv_get("paper_equity_peak", equity) or equity
    peak = max(peak, equity)
    kv_set("paper_equity_peak", peak)
    dd = (equity / peak - 1.0) if peak else 0.0  # <=0
    if dd <= -0.15:
        return 0.5
    if dd <= -0.10:
        return 0.7
    if dd <= -0.05:
        return 0.85
    return 1.0


# ---------------------------------------------------------------------
# Risk measurement
# ---------------------------------------------------------------------
def _holdings_returns(symbols: list[str], days: int = 120) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    q = (
        "SELECT symbol, ts, close FROM prices WHERE interval='1d' AND symbol IN ("
        + ",".join(["?"] * len(symbols)) + ") ORDER BY symbol, ts"
    )
    df = duck_df(q, symbols)
    if df.empty:
        return pd.DataFrame()
    piv = df.pivot_table(index="ts", columns="symbol", values="close").sort_index().tail(days)
    return piv.pct_change().dropna(how="all")


def portfolio_var(holdings: list[dict], confidence: float = 0.95) -> dict:
    """Historical 1-day VaR & expected shortfall on the current book value."""
    syms = [h["symbol"] for h in holdings]
    rets = _holdings_returns(syms)
    if rets.empty:
        return {"available": False}
    val = {h["symbol"]: h["value"] for h in holdings}
    # signed exposure (qty can be negative for shorts)
    port_ret = pd.Series(0.0, index=rets.index)
    gross = sum(abs(v) for v in val.values()) or 1.0
    for sym in rets.columns:
        port_ret = port_ret.add((rets[sym].fillna(0)) * val.get(sym, 0.0), fill_value=0.0)
    losses = port_ret.dropna()
    if losses.empty:
        return {"available": False}
    var_q = np.percentile(losses, (1 - confidence) * 100)
    es = losses[losses <= var_q].mean()
    return {
        "available": True,
        "confidence": confidence,
        "var_1d_rupees": round(float(-var_q), 0),
        "var_1d_pct_of_gross": round(float(-var_q / gross * 100), 2),
        "expected_shortfall_rupees": round(float(-es), 0),
        "ann_vol_pct": round(float(losses.std() / gross * np.sqrt(252) * 100), 2),
    }


# ---------------------------------------------------------------------
# Stress tests
# ---------------------------------------------------------------------
# Scenario -> macro factor shocks (%). Sector P&L = sum(sensitivity*shock)*beta-ish
STRESS_SCENARIOS = {
    "Crude +15% (oil shock)": {"Brent Crude": +15},
    "USDINR +3% (rupee weakness)": {"USD/INR": +3},
    "Rates +50bps (RBI hawkish)": {"India Repo Rate": +0.5, "India 10Y Yield": +0.5},
    "FII exodus / risk-off": {"Global Risk Sentiment": -1, "USD/INR": +2},
    "Broad selloff -7%": {"_market": -7},
}


def stress_test(holdings: list[dict]) -> list[dict]:
    u = load_universe()
    out = []
    for name, shocks in STRESS_SCENARIOS.items():
        pnl = 0.0
        for h in holdings:
            sec = u.sector(h["symbol"])
            val = h["value"]  # signed (long +, short -)
            impact = 0.0
            if "_market" in shocks:
                impact += shocks["_market"] / 100.0  # full beta≈1 approximation
            for macro_node, sign in SECTOR_MACRO_LINKS.get(sec, []):
                if macro_node in shocks:
                    # sensitivity sign * shock magnitude, scaled
                    shock = shocks[macro_node]
                    impact += sign * (shock / 100.0) * 0.6
            pnl += val * impact
        out.append({"scenario": name, "pnl_rupees": round(pnl, 0)})
    return out


def risk_report() -> dict:
    from ..portfolio.paper import mark_to_market
    book = mark_to_market()
    holdings = book.get("holdings", [])
    u = load_universe()
    sector_exp: dict[str, float] = {}
    gross = sum(abs(h["value"]) for h in holdings) or 1.0
    for h in holdings:
        sec = u.sector(h["symbol"])
        sector_exp[sec] = sector_exp.get(sec, 0.0) + h["value"]
    sector_exp = {k: round(v / gross * 100, 1) for k, v in
                  sorted(sector_exp.items(), key=lambda kv: -abs(kv[1]))}
    return {
        "equity": book.get("equity"),
        "gross_exposure_pct": round(gross / (book.get("equity") or gross) * 100, 1),
        "var": portfolio_var(holdings),
        "stress_tests": stress_test(holdings),
        "sector_exposure_pct": sector_exp,
        "n_positions": len(holdings),
    }
