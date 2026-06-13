"""Deterministic daily market digest — a quick 'what matters today' summary.

Aggregates regime, flows, top movers, the highest-conviction signal and recent
headlines into one structured payload. No LLM required.
"""
from __future__ import annotations

from ..storage.db import duck_df, latest_agent_output
from .quant import flow_metrics, top_movers


def daily_digest() -> dict:
    regime = latest_agent_output("regime") or {}
    flow = flow_metrics()
    movers = top_movers(3)

    sig = (latest_agent_output("cio") or {}).get("signals", [])
    top_signal = sig[0] if sig else None

    news = duck_df("SELECT title, source FROM news ORDER BY ts DESC LIMIT 6")
    headlines = news["title"].tolist() if not news.empty else []

    return {
        "regime": regime.get("label"),
        "risk_score": regime.get("risk_score"),
        "india_vix": regime.get("india_vix"),
        "fii_net_latest": flow.get("fii_net_latest"),
        "dii_net_latest": flow.get("dii_net_latest"),
        "top_gainers": movers.get("gainers", []),
        "top_losers": movers.get("losers", []),
        "top_idea": None if not top_signal else {
            "symbol": top_signal.get("symbol"),
            "direction": top_signal.get("direction"),
            "conviction": top_signal.get("conviction"),
        },
        "headlines": headlines,
    }
