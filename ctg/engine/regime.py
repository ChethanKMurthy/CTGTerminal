"""Layer 8 — Regime detection.

Classifies the current market regime from breadth, trend, volatility (India VIX
if available, else realised), and flow. Markets are non-stationary; signals are
weighted by regime downstream so a mean-reversion edge isn't applied in a crash.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..logging_conf import get_logger
from ..storage.db import duck_df
from .quant import flow_metrics

log = get_logger("engine.regime")


def detect_regime() -> dict:
    # --- index trend + realised vol from NIFTY 50 daily ---
    nf = duck_df(
        "SELECT name, ts, last FROM index_levels WHERE name='NIFTY 50' ORDER BY ts"
    )
    # Prefer the longer daily series from a proxy: use prices of constituents breadth
    breadth = _breadth()
    vix = _india_vix()

    # NIFTY trend via constituents' daily index proxy (equal-weight return)
    trend, mom = _index_trend()

    vol_regime = "normal"
    if vix is not None:
        if vix >= 20:
            vol_regime = "high"
        elif vix <= 12:
            vol_regime = "low"
    else:
        # fall back to realised vol of the equal-weight proxy
        rv = _realised_vol()
        if rv is not None:
            vol_regime = "high" if rv >= 22 else ("low" if rv <= 12 else "normal")

    flows = flow_metrics()
    fii_5d = flows.get("fii_net_5d", 0) or 0
    dii_5d = flows.get("dii_net_5d", 0) or 0

    # Risk score 0..100 (higher = risk-on)
    score = 50.0
    if trend == "up":
        score += 15
    elif trend == "down":
        score -= 15
    score += np.clip((breadth - 0.5) * 60, -15, 15) if breadth is not None else 0
    if vol_regime == "high":
        score -= 15
    elif vol_regime == "low":
        score += 8
    score += np.clip((fii_5d + dii_5d) / 2000.0, -10, 10)
    score = float(np.clip(score, 0, 100))

    if score >= 65:
        label = "Risk-On / Expansion"
    elif score >= 50:
        label = "Neutral / Constructive"
    elif score >= 35:
        label = "Cautious / Distribution"
    else:
        label = "Risk-Off / Stress"

    if vol_regime == "high" and score < 40:
        label = "Liquidity Stress"

    return {
        "label": label,
        "risk_score": round(score, 1),
        "trend": trend,
        "vol_regime": vol_regime,
        "india_vix": vix,
        "breadth_above_ma50": round(breadth, 2) if breadth is not None else None,
        "nifty_mom_pct": mom,
        "fii_net_5d": fii_5d,
        "dii_net_5d": dii_5d,
    }


def _breadth() -> float | None:
    df = duck_df("SELECT symbol, ts, close FROM prices WHERE interval='1d' ORDER BY symbol, ts")
    if df.empty:
        return None
    above = []
    for _, grp in df.groupby("symbol"):
        c = grp["close"].astype(float)
        if len(c) < 20:
            continue
        ma50 = c.tail(50).mean()
        above.append(1.0 if c.iloc[-1] > ma50 else 0.0)
    return float(sum(above) / len(above)) if above else None


def _index_trend() -> tuple[str, float | None]:
    df = duck_df(
        "SELECT ts, close FROM prices WHERE symbol='RELIANCE' AND interval='1d' ORDER BY ts"
    )
    # Use an equal-weight proxy across all symbols for a market trend read
    proxy = duck_df(
        "SELECT ts, avg(close) c FROM prices WHERE interval='1d' GROUP BY ts ORDER BY ts"
    )
    if proxy.empty or len(proxy) < 60:
        return "mixed", None
    c = proxy["c"].astype(float)
    ma50 = c.tail(50).mean()
    ma200 = c.tail(200).mean() if len(c) >= 200 else ma50
    last = c.iloc[-1]
    mom = float(c.iloc[-1] / c.iloc[-21] - 1) * 100 if len(c) > 21 else None
    trend = "up" if last > ma50 >= ma200 else ("down" if last < ma50 <= ma200 else "mixed")
    return trend, round(mom, 2) if mom is not None else None


def _realised_vol() -> float | None:
    import math

    proxy = duck_df(
        "SELECT ts, avg(close) c FROM prices WHERE interval='1d' GROUP BY ts ORDER BY ts"
    )
    if proxy.empty or len(proxy) < 22:
        return None
    rets = proxy["c"].astype(float).pct_change().dropna()
    return float(rets.tail(20).std() * math.sqrt(252) * 100)


def _india_vix() -> float | None:
    df = duck_df(
        "SELECT last FROM index_levels WHERE name='INDIA VIX' ORDER BY ts DESC LIMIT 1"
    )
    if df.empty:
        return None
    try:
        return round(float(df["last"].iloc[0]), 2)
    except (ValueError, TypeError):
        return None
