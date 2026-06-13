"""Live option-trade suggestion engine — all 24 NSE Bank Nifty strategies.

Implements the full strategy set from the NSE "Bank Nifty Options Strategies"
booklet and ranks them by RELEVANCY to the live market view:

  Bullish : Long Call, Short Put, Bull Call Spread, Bull Put Spread,
            Synthetic Call, Covered Call w/ Futures, Collar, Long Combo
  Bearish : Long Put, Short Call, Bear Call Spread, Bear Put Spread,
            Protective Call (Synthetic Long Put), Covered Put
  Neutral : Long/Short Straddle, Long/Short Strangle,
            Long/Short Call Butterfly, Long/Short Call Condor, Long/Short Box

Strategy selection blends three live signals (direction, IV regime, dealer
gamma). Every payoff metric (max profit/loss, breakevens, payoff curve) is
derived from the exact at-expiry P&L of the real legs, so they stay correct for
multi-leg and futures-based structures alike.

NOTE: free option data is ~10-min delayed (last snapshot after close). Premiums
move fast — research setups, not executable quotes. Options/futures carry high,
sometimes unlimited, risk. Not investment advice.
"""
from __future__ import annotations

import math
from datetime import datetime

import pandas as pd

from ..logging_conf import get_logger
from ..storage.db import duck_df, latest_agent_output
from .quant import LOT_SIZE, options_metrics

log = get_logger("engine.options_strat")


# ---------------------------------------------------------------------
# chain helpers
# ---------------------------------------------------------------------
def _chain(underlying: str) -> pd.DataFrame:
    return duck_df(
        "SELECT * FROM option_chain WHERE underlying=? AND ts=("
        "SELECT max(ts) FROM option_chain WHERE underlying=?) ORDER BY strike",
        [underlying, underlying],
    )


def _india_vix() -> float | None:
    df = duck_df("SELECT last FROM index_levels WHERE name='INDIA VIX' ORDER BY ts DESC LIMIT 1")
    try:
        return float(df["last"].iloc[0]) if not df.empty else None
    except (ValueError, TypeError):
        return None


def _nearest(df: pd.DataFrame, target: float) -> pd.Series | None:
    if df.empty:
        return None
    return df.loc[(df["strike"] - target).abs().idxmin()]


def _strike_step(df: pd.DataFrame) -> float:
    s = sorted(df["strike"].unique())
    diffs = [b - a for a, b in zip(s, s[1:]) if b - a > 0]
    return float(min(diffs)) if diffs else 50.0


def _ltp(row: pd.Series, opt: str) -> float:
    v = row.get("ce_ltp" if opt == "CE" else "pe_ltp")
    try:
        return float(v) if v and float(v) > 0 else 0.0
    except (ValueError, TypeError):
        return 0.0


def _leg(df, target, opt, action, qty=1):
    """Build one leg. opt in {CE,PE,FUT}. Returns None if no valid premium."""
    if opt == "FUT":
        return {"action": action, "type": "FUT", "strike": None, "ltp": 0.0, "qty": qty}
    r = _nearest(df, target)
    if r is None:
        return None
    k, p = float(r["strike"]), _ltp(r, opt)
    if p <= 0:
        return None
    return {"action": action, "type": opt, "strike": k, "ltp": round(p, 2), "qty": qty}


def _legs(*legs):
    return None if any(l is None for l in legs) else list(legs)


# ---------------------------------------------------------------------
# generic payoff-derived economics (works for every structure)
# ---------------------------------------------------------------------
def _payoff_curve(legs, lot, spot, n: int = 121) -> list[dict]:
    lo, hi = spot * 0.60, spot * 1.40
    pts = []
    for i in range(n):
        S = lo + (hi - lo) * i / (n - 1)
        pnl = 0.0
        for l in legs:
            q = l.get("qty", 1)
            if l["type"] == "FUT":
                v = (S - spot) if l["action"] == "BUY" else (spot - S)
            else:
                k = l["strike"]
                intr = max(S - k, 0) if l["type"] == "CE" else max(k - S, 0)
                v = (intr - l["ltp"]) if l["action"] == "BUY" else (l["ltp"] - intr)
            pnl += v * q
        pts.append({"s": round(S, 0), "pnl": round(pnl * lot, 0)})
    return pts


def _net_premium(legs, lot) -> float:
    tot = 0.0
    for l in legs:
        if l["type"] == "FUT":
            continue
        sign = 1 if l["action"] == "SELL" else -1
        tot += sign * l["ltp"] * l.get("qty", 1)
    return round(tot * lot, 0)  # + credit / - debit


def _metrics(legs, lot, spot) -> dict:
    pts = _payoff_curve(legs, lot, spot)
    ys = [p["pnl"] for p in pts]
    has_fut = any(l["type"] == "FUT" for l in legs)
    right_slope = ys[-1] - ys[-2]
    left_slope = ys[1] - ys[0]

    max_profit = None if right_slope > 1 else max(ys)
    max_loss = None if (right_slope < -1 or (has_fut and left_slope > 1)) else min(ys)

    bes = []
    for a, b in zip(pts, pts[1:]):
        if (a["pnl"] <= 0 <= b["pnl"]) or (a["pnl"] >= 0 >= b["pnl"]):
            if b["pnl"] != a["pnl"]:
                s = a["s"] + (b["s"] - a["s"]) * (0 - a["pnl"]) / (b["pnl"] - a["pnl"])
                bes.append(round(s))
    return {
        "net_premium": _net_premium(legs, lot),
        "max_profit": None if max_profit is None else round(max_profit),
        "max_loss": None if max_loss is None else round(max_loss),
        "breakevens": sorted(set(bes)),
        "payoff": pts,
    }


# ---------------------------------------------------------------------
# the 24 strategy builders.  ctx = (df, spot, sig, step)
#   sig  ~ 1-sigma move rounded to a strike step
# Each builder returns a list of legs (or None if strikes/premia missing).
# ---------------------------------------------------------------------
def _b(ctx):
    return ctx["df"], ctx["spot"], ctx["sig"], ctx["step"]


STRATEGIES: list[dict] = [
    # ---- Bullish ----
    {"key": "long_call", "name": "Long Call", "stance": "bullish", "style": "buy",
     "build": lambda c: _legs(_leg(c["df"], c["spot"], "CE", "BUY"))},
    {"key": "short_put", "name": "Short Put", "stance": "bullish", "style": "sell",
     "build": lambda c: _legs(_leg(c["df"], c["spot"] - c["sig"], "PE", "SELL"))},
    {"key": "bull_call_spread", "name": "Bull Call Spread", "stance": "bullish", "style": "buy",
     "build": lambda c: _legs(_leg(c["df"], c["spot"], "CE", "BUY"),
                              _leg(c["df"], c["spot"] + 2 * c["sig"], "CE", "SELL"))},
    {"key": "bull_put_spread", "name": "Bull Put Spread", "stance": "bullish", "style": "sell",
     "build": lambda c: _legs(_leg(c["df"], c["spot"] - c["sig"], "PE", "SELL"),
                              _leg(c["df"], c["spot"] - 2 * c["sig"], "PE", "BUY"))},
    {"key": "synthetic_call", "name": "Synthetic Call (Futures + Put)", "stance": "bullish",
     "style": "buy", "needs_fut": True,
     "build": lambda c: _legs(_leg(c["df"], None, "FUT", "BUY"),
                              _leg(c["df"], c["spot"], "PE", "BUY"))},
    {"key": "covered_call_fut", "name": "Covered Call w/ Futures", "stance": "bullish",
     "style": "sell", "needs_fut": True,
     "build": lambda c: _legs(_leg(c["df"], None, "FUT", "BUY"),
                              _leg(c["df"], c["spot"] + c["sig"], "CE", "SELL"))},
    {"key": "collar", "name": "Collar", "stance": "bullish", "style": "sell", "needs_fut": True,
     "build": lambda c: _legs(_leg(c["df"], None, "FUT", "BUY"),
                              _leg(c["df"], c["spot"] - c["sig"], "PE", "BUY"),
                              _leg(c["df"], c["spot"] + c["sig"], "CE", "SELL"))},
    {"key": "long_combo", "name": "Long Combo (Synthetic Long)", "stance": "bullish", "style": "buy",
     "build": lambda c: _legs(_leg(c["df"], c["spot"] - c["sig"], "PE", "SELL"),
                              _leg(c["df"], c["spot"] + c["sig"], "CE", "BUY"))},

    # ---- Bearish ----
    {"key": "long_put", "name": "Long Put", "stance": "bearish", "style": "buy",
     "build": lambda c: _legs(_leg(c["df"], c["spot"], "PE", "BUY"))},
    {"key": "short_call", "name": "Short Call", "stance": "bearish", "style": "sell",
     "build": lambda c: _legs(_leg(c["df"], c["spot"] + c["sig"], "CE", "SELL"))},
    {"key": "bear_call_spread", "name": "Bear Call Spread", "stance": "bearish", "style": "sell",
     "build": lambda c: _legs(_leg(c["df"], c["spot"] + c["sig"], "CE", "SELL"),
                              _leg(c["df"], c["spot"] + 2 * c["sig"], "CE", "BUY"))},
    {"key": "bear_put_spread", "name": "Bear Put Spread", "stance": "bearish", "style": "buy",
     "build": lambda c: _legs(_leg(c["df"], c["spot"], "PE", "BUY"),
                              _leg(c["df"], c["spot"] - 2 * c["sig"], "PE", "SELL"))},
    {"key": "protective_call", "name": "Protective Call (Synthetic Long Put)", "stance": "bearish",
     "style": "buy", "needs_fut": True,
     "build": lambda c: _legs(_leg(c["df"], None, "FUT", "SELL"),
                              _leg(c["df"], c["spot"], "CE", "BUY"))},
    {"key": "covered_put", "name": "Covered Put w/ Futures", "stance": "bearish", "style": "sell",
     "needs_fut": True,
     "build": lambda c: _legs(_leg(c["df"], None, "FUT", "SELL"),
                              _leg(c["df"], c["spot"] - c["sig"], "PE", "SELL"))},

    # ---- Neutral ----
    {"key": "long_straddle", "name": "Long Straddle", "stance": "neutral", "style": "buy",
     "vol_bias": "long_vol",
     "build": lambda c: _legs(_leg(c["df"], c["spot"], "CE", "BUY"),
                              _leg(c["df"], c["spot"], "PE", "BUY"))},
    {"key": "short_straddle", "name": "Short Straddle", "stance": "neutral", "style": "sell",
     "vol_bias": "short_vol",
     "build": lambda c: _legs(_leg(c["df"], c["spot"], "CE", "SELL"),
                              _leg(c["df"], c["spot"], "PE", "SELL"))},
    {"key": "long_strangle", "name": "Long Strangle", "stance": "neutral", "style": "buy",
     "vol_bias": "long_vol",
     "build": lambda c: _legs(_leg(c["df"], c["spot"] + c["sig"], "CE", "BUY"),
                              _leg(c["df"], c["spot"] - c["sig"], "PE", "BUY"))},
    {"key": "short_strangle", "name": "Short Strangle", "stance": "neutral", "style": "sell",
     "vol_bias": "short_vol",
     "build": lambda c: _legs(_leg(c["df"], c["spot"] + c["sig"], "CE", "SELL"),
                              _leg(c["df"], c["spot"] - c["sig"], "PE", "SELL"))},
    {"key": "long_call_butterfly", "name": "Long Call Butterfly", "stance": "neutral", "style": "buy",
     "vol_bias": "short_vol",
     "build": lambda c: _legs(_leg(c["df"], c["spot"] - c["sig"], "CE", "BUY"),
                              _leg(c["df"], c["spot"], "CE", "SELL", qty=2),
                              _leg(c["df"], c["spot"] + c["sig"], "CE", "BUY"))},
    {"key": "short_call_butterfly", "name": "Short Call Butterfly", "stance": "neutral",
     "style": "sell", "vol_bias": "long_vol",
     "build": lambda c: _legs(_leg(c["df"], c["spot"] - c["sig"], "CE", "SELL"),
                              _leg(c["df"], c["spot"], "CE", "BUY", qty=2),
                              _leg(c["df"], c["spot"] + c["sig"], "CE", "SELL"))},
    {"key": "long_call_condor", "name": "Long Call Condor", "stance": "neutral", "style": "buy",
     "vol_bias": "short_vol",
     "build": lambda c: _legs(_leg(c["df"], c["spot"] - 2 * c["sig"], "CE", "BUY"),
                              _leg(c["df"], c["spot"] - c["sig"], "CE", "SELL"),
                              _leg(c["df"], c["spot"] + c["sig"], "CE", "SELL"),
                              _leg(c["df"], c["spot"] + 2 * c["sig"], "CE", "BUY"))},
    {"key": "short_call_condor", "name": "Short Call Condor", "stance": "neutral", "style": "sell",
     "vol_bias": "long_vol",
     "build": lambda c: _legs(_leg(c["df"], c["spot"] - 2 * c["sig"], "CE", "SELL"),
                              _leg(c["df"], c["spot"] - c["sig"], "CE", "BUY"),
                              _leg(c["df"], c["spot"] + c["sig"], "CE", "BUY"),
                              _leg(c["df"], c["spot"] + 2 * c["sig"], "CE", "SELL"))},
    {"key": "long_box", "name": "Long Box / Conversion (arbitrage)", "stance": "neutral",
     "style": "arb",
     "build": lambda c: _legs(_leg(c["df"], c["spot"] - c["sig"], "CE", "BUY"),
                              _leg(c["df"], c["spot"] + c["sig"], "CE", "SELL"),
                              _leg(c["df"], c["spot"] + c["sig"], "PE", "BUY"),
                              _leg(c["df"], c["spot"] - c["sig"], "PE", "SELL"))},
    {"key": "short_box", "name": "Short Box (arbitrage)", "stance": "neutral", "style": "arb",
     "build": lambda c: _legs(_leg(c["df"], c["spot"] - c["sig"], "CE", "SELL"),
                              _leg(c["df"], c["spot"] + c["sig"], "CE", "BUY"),
                              _leg(c["df"], c["spot"] + c["sig"], "PE", "SELL"),
                              _leg(c["df"], c["spot"] - c["sig"], "PE", "BUY"))},
]


# ---------------------------------------------------------------------
# market view
# ---------------------------------------------------------------------
def _views(underlying: str, m: dict) -> dict:
    regime = latest_agent_output("regime") or {}
    options_agent = latest_agent_output("options") or {}
    flow = latest_agent_output("flow") or {}

    score = 0.0
    pcr = m.get("pcr_oi") or 1.0
    if pcr > 1.3:
        score += 0.30
    elif pcr < 0.7:
        score -= 0.30
    if m.get("max_pain") and m.get("spot"):
        if m["spot"] < m["max_pain"] * 0.99:
            score += 0.15
        elif m["spot"] > m["max_pain"] * 1.01:
            score -= 0.15
    rs = regime.get("risk_score", 50) or 50
    score += (rs - 50) / 50.0 * 0.30
    fii = (flow.get("fno", {}) or {}).get("fii", {})
    lp = fii.get("idx_fut_long_pct")
    if lp is not None:
        score += (lp - 50) / 50.0 * 0.25
    ob = (options_agent.get("view", {}) or {}).get("market_bias", "")
    score += {"bullish": 0.2, "bearish": -0.2}.get(ob, 0.0)
    score = max(-1.0, min(1.0, score))

    direction = "bullish" if score > 0.22 else ("bearish" if score < -0.22 else "neutral")

    vix = _india_vix()
    iv_ref = vix if vix is not None else m.get("atm_iv")
    if iv_ref is None:
        vol_regime = "normal"
    elif iv_ref >= 16:
        vol_regime = "high"
    elif iv_ref <= 12:
        vol_regime = "low"
    else:
        vol_regime = "normal"

    return {"dir_score": round(score, 2), "direction": direction,
            "vol_regime": vol_regime, "india_vix": vix,
            "gamma_regime": m.get("gamma_regime", "positive")}


def _relevancy(spec: dict, v: dict, metrics: dict) -> float:
    """0..100 fit of a strategy to the current view. <=0 => filtered out."""
    direction, vol, gamma = v["direction"], v["vol_regime"], v["gamma_regime"]
    style = spec["style"]
    stance = spec["stance"]

    score = 50.0
    # directional alignment
    if stance == direction:
        score += 30 + abs(v["dir_score"]) * 15
    elif stance == "neutral" and direction == "neutral":
        score += 30
    elif stance == "neutral":
        score += 2          # neutral structures are mildly ok in a trend
    else:
        return -100.0       # opposite stance — never suggest

    # volatility alignment. Neutral structures are classified by vol_bias
    # (long_vol wants a big move / cheap IV; short_vol wants range / rich IV).
    # Directional structures use their debit/credit style for the IV tilt.
    vb = spec.get("vol_bias")
    if vb == "long_vol":
        score += 14 if vol == "low" else (-12 if vol == "high" else 4)
        score += 8 if gamma == "negative" else -8   # amplified moves help
    elif vb == "short_vol":
        score += 14 if vol == "high" else (-12 if vol == "low" else 4)
        score += 8 if gamma == "positive" else -8   # range-bound helps
    elif style == "sell":
        score += 14 if vol == "high" else (-12 if vol == "low" else 0)
        if gamma == "positive":
            score += 6
    elif style == "buy":
        score += 14 if vol == "low" else (-12 if vol == "high" else 0)
        if gamma == "negative":
            score += 6

    if spec.get("needs_fut"):
        score -= 6          # extra margin / leg complexity

    if style == "arb":
        # only relevant if the box actually locks a positive payoff after premia
        locked = metrics.get("max_profit")
        score = 12 + (25 if (locked is not None and locked > 0) else -100)

    # reward a healthy reward:risk on defined-risk structures
    mp, ml = metrics.get("max_profit"), metrics.get("max_loss")
    if mp and ml and ml != 0:
        rr = abs(mp) / abs(ml)
        score += min(rr, 2.0) * 6

    return round(score, 1)


def _rationale(spec, v, m, days) -> str:
    parts = [f"{spec['name']} — view {v['direction']} ({v['dir_score']:+.2f}), "
             f"IV {v['vol_regime']}, dealer gamma {v['gamma_regime']}."]
    if spec["style"] == "sell":
        parts.append(f"Net premium seller; {days}d theta + {'rich' if v['vol_regime']=='high' else 'normal'} IV help.")
    elif spec["style"] == "buy":
        parts.append("Defined-risk premium buyer for a directional/breakout move.")
    elif spec["style"] == "arb":
        parts.append("Risk-free lock if the box is mispriced vs the strike width.")
    if spec.get("needs_fut"):
        parts.append("Uses a futures leg (margin required).")
    parts.append(f"Strikes near OI walls S{m.get('support_oi_strike')}/R{m.get('resistance_oi_strike')}, "
                 f"max-pain {m.get('max_pain')}.")
    return " ".join(parts)


def suggest_option_trades(underlying: str, top_n: int = 6) -> dict:
    df = _chain(underlying)
    if df.empty:
        return {"underlying": underlying, "available": False}
    m = options_metrics(underlying)
    if not m:
        return {"underlying": underlying, "available": False}
    spot = m["spot"]
    lot = LOT_SIZE.get(underlying, 50)
    step = _strike_step(df)

    try:
        days = max((datetime.strptime(m["expiry"], "%d-%b-%Y") - datetime.now()).days, 1)
    except Exception:  # noqa: BLE001
        days = 7
    iv = (m.get("atm_iv") or 14) / 100.0
    exp_move = spot * iv * math.sqrt(days / 365.0)
    sig = max(step, round(exp_move / step) * step)  # ~1-sigma snapped to a strike

    v = _views(underlying, m)
    ctx = {"df": df, "spot": spot, "sig": sig, "step": step}

    built, catalog = [], []
    for spec in STRATEGIES:
        try:
            legs = spec["build"](ctx)
        except Exception:  # noqa: BLE001
            legs = None
        if not legs:
            catalog.append({"name": spec["name"], "stance": spec["stance"],
                            "style": spec["style"], "relevancy": None, "note": "strikes/premia unavailable"})
            continue
        met = _metrics(legs, lot, spot)
        rel = _relevancy(spec, v, met)
        catalog.append({"name": spec["name"], "stance": spec["stance"],
                        "style": spec["style"], "relevancy": rel})
        if rel <= 0:
            continue
        rr = (abs(met["max_profit"]) / abs(met["max_loss"])
              if met["max_profit"] and met["max_loss"] else None)
        built.append({
            "strategy": spec["name"], "key": spec["key"], "stance": spec["stance"],
            "style": spec["style"], "needs_futures": bool(spec.get("needs_fut")),
            "legs": legs, "risk_reward": round(rr, 2) if rr else None,
            "fit_score": rel, "rationale": _rationale(spec, v, m, days), **met,
        })

    built.sort(key=lambda x: x["fit_score"], reverse=True)
    catalog.sort(key=lambda x: (x["relevancy"] is None, -(x["relevancy"] or -999)))

    return {
        "underlying": underlying, "available": True, "as_of": m["ts"],
        "spot": spot, "expiry": m["expiry"], "days_to_expiry": days,
        "lot_size": lot, "expected_move_1sigma": round(exp_move, 0),
        "views": v, "pcr": m.get("pcr_oi"), "max_pain": m.get("max_pain"),
        "atm_iv": m.get("atm_iv"), "support": m.get("support_oi_strike"),
        "resistance": m.get("resistance_oi_strike"),
        "suggestions": built[:top_n], "catalog": catalog,
    }


def all_option_trades() -> dict:
    return {u: suggest_option_trades(u) for u in ("NIFTY", "BANKNIFTY")}
