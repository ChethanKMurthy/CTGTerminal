"""The specialist agent team (Layer 3). Quant-grounded, LLM-augmented."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..engine.quant import (deal_pressure, flow_metrics, fno_flow_metrics,
                            options_metrics, price_features)
from ..engine.regime import detect_regime
from ..storage.db import duck_df
from .base import Agent, Context, register


# ---------------------------------------------------------------------
@register
class FlowAgent(Agent):
    """The doc's single most valuable agent: institutional flow intelligence."""
    name = "flow"
    role = "Institutional Flow Intelligence Agent for Indian equities"

    def analyse(self, ctx: Context) -> dict:
        flows = flow_metrics()
        fno = fno_flow_metrics()
        deals = deal_pressure()
        uni = set(ctx.universe.symbols)
        uni_deals = [d for d in deals if d["symbol"] in uni]

        fii5 = flows.get("fii_net_5d", 0) or 0
        dii5 = flows.get("dii_net_5d", 0) or 0
        if fii5 < -2000 and dii5 > 2000:
            stance = "FII distribution absorbed by DII — domestic-supported, fragile to global risk"
            bias = "neutral"
        elif fii5 > 0 and dii5 > 0:
            stance = "Both FII and DII net buyers — strong demand"
            bias = "risk_on"
        elif fii5 < 0 and dii5 < 0:
            stance = "Both FII and DII net sellers — broad distribution"
            bias = "risk_off"
        else:
            stance = "Mixed institutional flows"
            bias = "neutral"

        facts = {"cash_flows": flows, "fno_positioning": fno, "top_deals": uni_deals[:10]}
        view = self.reason(
            facts,
            "Synthesise cash flows (FII/DII), F&O positioning (FII index-futures "
            "net long/short) and deal pressure. Identify who is forced to buy/sell "
            "and the net directional pressure on Nifty.",
            '{"market_bias":"risk_on|risk_off|neutral","summary":"..","forced_flows":".."}',
            {"market_bias": bias, "summary": stance, "forced_flows": ""},
        )
        return {"scope": "market", "metrics": flows, "fno": fno,
                "deal_pressure": uni_deals[:10], "rule_bias": bias, "view": view}


# ---------------------------------------------------------------------
@register
class OptionsAgent(Agent):
    name = "options"
    role = "Index Options / Dealer-Gamma Positioning Agent"

    def analyse(self, ctx: Context) -> dict:
        chains = {}
        for u in ("NIFTY", "BANKNIFTY"):
            m = options_metrics(u)
            if m:
                chains[u] = m
        bias = "neutral"
        notes = []
        nifty = chains.get("NIFTY")
        if nifty:
            if (nifty["pcr_oi"] or 0) > 1.3:
                bias, n = "bullish", "High PCR — put writers confident, support below"
            elif (nifty["pcr_oi"] or 0) < 0.7:
                bias, n = "bearish", "Low PCR — call writers dominant, resistance overhead"
            else:
                n = "Balanced PCR"
            notes.append(n)
            if nifty["gamma_regime"] == "negative":
                notes.append("Negative dealer gamma — moves get amplified, expect higher intraday vol")
            else:
                notes.append("Positive dealer gamma — dealers dampen moves, range-bound bias")
            notes.append(f"Max pain {nifty['max_pain']} vs spot {nifty['spot']}")
        view = self.reason(
            chains,
            "Read dealer positioning and the most likely path into expiry.",
            '{"market_bias":"bullish|bearish|neutral","key_levels":"..","vol_outlook":".."}',
            {"market_bias": bias, "key_levels": "; ".join(notes), "vol_outlook": ""},
        )
        return {"scope": "market", "chains": chains, "rule_bias": bias,
                "notes": notes, "view": view}


# ---------------------------------------------------------------------
@register
class MacroAgent(Agent):
    name = "macro"
    role = "India Macro & Liquidity Agent"

    def analyse(self, ctx: Context) -> dict:
        df = duck_df(
            "SELECT label, value, date FROM macro WHERE (label,date) IN "
            "(SELECT label, max(date) FROM macro GROUP BY label) ORDER BY label"
        )
        latest = {r["label"]: {"value": round(float(r["value"]), 3), "date": str(r["date"])}
                  for _, r in df.iterrows()} if not df.empty else {}
        available = bool(latest)
        fallback_summary = ("Macro feed empty — set FRED_API_KEY to activate"
                            if not available else "Macro snapshot captured")
        view = self.reason(
            {"latest": latest},
            "Classify the macro backdrop (growth/inflation/liquidity) and name 2 "
            "favoured and 2 challenged sectors for India.",
            '{"backdrop":"..","favoured_sectors":[".."],"challenged_sectors":[".."]}',
            {"backdrop": fallback_summary, "favoured_sectors": [], "challenged_sectors": []},
        )
        return {"scope": "market", "available": available, "latest": latest, "view": view}


# ---------------------------------------------------------------------
@register
class SentimentAgent(Agent):
    name = "sentiment"
    role = "Narrative & Retail Sentiment Agent"

    def analyse(self, ctx: Context) -> dict:
        news = duck_df("SELECT title FROM news ORDER BY ts DESC LIMIT 40")
        social = duck_df("SELECT title, score FROM social ORDER BY ts DESC LIMIT 40")
        headlines = news["title"].tolist() if not news.empty else []
        social_titles = social["title"].tolist() if not social.empty else []

        if self.llm.available and (headlines or social_titles):
            facts = {"headlines": headlines[:30], "reddit": social_titles[:20]}
            view = self.reason(
                facts,
                "Score overall market sentiment -1..1 and flag named stocks/sectors "
                "getting unusual attention with direction.",
                '{"sentiment_score":0.0,"mood":"..","hot_names":[{"name":"..","direction":"+/-"}]}',
                {"sentiment_score": 0.0, "mood": "n/a", "hot_names": []},
            )
        else:
            score = _keyword_sentiment(headlines + social_titles)
            view = {"sentiment_score": score,
                    "mood": "positive" if score > 0.1 else ("negative" if score < -0.1 else "neutral"),
                    "hot_names": [], "source": "rule_based"}
        return {"scope": "market", "n_headlines": len(headlines),
                "n_social": len(social_titles), "view": view}


# ---------------------------------------------------------------------
@register
class EarningsAgent(Agent):
    name = "earnings"
    role = "Corporate Events & Earnings Agent"

    def analyse(self, ctx: Context) -> dict:
        uni = set(ctx.universe.symbols)
        df = duck_df("SELECT ts, symbol, subject FROM announcements ORDER BY ts DESC LIMIT 200")
        events = []
        keywords = ("result", "dividend", "board meeting", "buyback", "bonus",
                    "split", "acquisition", "order", "resignation", "fund rais")
        rows_iter = df.iterrows() if not df.empty else iter(())
        for _, r in rows_iter:
            sub = (r["subject"] or "").lower()
            flag = next((k for k in keywords if k in sub), None)
            if r["symbol"] in uni and flag:
                events.append({"ts": str(r["ts"]), "symbol": r["symbol"],
                               "subject": r["subject"], "type": flag})
        return {"scope": "per_symbol", "events": events[:30], "n_events": len(events)}


# ---------------------------------------------------------------------
@register
class ValuationAgent(Agent):
    name = "valuation"
    role = "Relative Valuation Agent"

    def analyse(self, ctx: Context) -> dict:
        df = duck_df(
            "SELECT symbol, pe, pb, roe, profit_margin FROM fundamentals f WHERE ts=("
            "SELECT max(ts) FROM fundamentals)"
        )
        if df.empty:
            return {"scope": "per_symbol", "available": False, "rankings": []}
        u = ctx.universe
        df["sector"] = df["symbol"].map(lambda s: u.sector(s))
        rankings = []
        for sector, grp in df.groupby("sector"):
            med_pe = grp["pe"].median(skipna=True)
            for _, r in grp.iterrows():
                pe = r["pe"]
                if pe is None or pd.isna(pe) or pe <= 0 or med_pe is None or pd.isna(med_pe):
                    continue
                rel = pe / med_pe
                rankings.append({
                    "symbol": r["symbol"], "sector": sector,
                    "pe": round(float(pe), 1), "sector_median_pe": round(float(med_pe), 1),
                    "rel_pe": round(float(rel), 2),
                    "label": "cheap" if rel < 0.8 else ("expensive" if rel > 1.25 else "fair"),
                    "roe": round(float(r["roe"]), 3) if r["roe"] and not pd.isna(r["roe"]) else None,
                })
        rankings.sort(key=lambda x: x["rel_pe"])
        return {"scope": "per_symbol", "available": True, "rankings": rankings}


# ---------------------------------------------------------------------
@register
class RegimeAgent(Agent):
    name = "regime"
    role = "Market Regime Detection Agent"

    def analyse(self, ctx: Context) -> dict:
        r = detect_regime()
        return {"scope": "market", **r}


# ---------------------------------------------------------------------
@register
class CausalAgent(Agent):
    name = "causal"
    role = "Causal / Lead-Lag Inference Agent"

    def analyse(self, ctx: Context) -> dict:
        """Lightweight lead-lag: which macro/commodity move precedes sector moves."""
        links = _lead_lag()
        return {"scope": "market", "lead_lag": links,
                "note": "Lagged-correlation hints, not proven causality"}


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------
_POS = {"surge", "jump", "gain", "rally", "record", "beat", "upgrade", "profit",
        "rise", "soar", "high", "boost", "strong", "buy"}
_NEG = {"fall", "drop", "loss", "plunge", "slump", "downgrade", "miss", "weak",
        "crash", "decline", "cut", "fraud", "probe", "sell", "fear", "slowdown"}


def _keyword_sentiment(titles: list[str]) -> float:
    if not titles:
        return 0.0
    pos = neg = 0
    for t in titles:
        words = set((t or "").lower().split())
        pos += len(words & _POS)
        neg += len(words & _NEG)
    total = pos + neg
    return round((pos - neg) / total, 3) if total else 0.0


def _lead_lag() -> list[dict]:
    macro = duck_df(
        "SELECT label, date, value FROM macro WHERE label IN "
        "('Brent crude','USD/INR','US 10Y yield','US VIX','Brent Crude')"
    )
    if macro.empty:
        return []
    # Build a market proxy daily return
    proxy = duck_df(
        "SELECT ts::DATE d, avg(close) c FROM prices WHERE interval='1d' GROUP BY d ORDER BY d"
    )
    if proxy.empty or len(proxy) < 60:
        return []
    proxy["r"] = proxy["c"].pct_change()
    out = []
    for label, grp in macro.groupby("label"):
        m = grp.sort_values("date").copy()
        m["mr"] = m["value"].pct_change()
        merged = pd.merge(proxy, m[["date", "mr"]], left_on="d", right_on="date", how="inner").dropna()
        if len(merged) < 30:
            continue
        # macro change today vs market return next day
        corr = merged["mr"].shift(1).corr(merged["r"])
        if corr is not None and not pd.isna(corr) and abs(corr) >= 0.1:
            out.append({"driver": label, "lag_days": 1, "corr_with_market": round(float(corr), 3)})
    return sorted(out, key=lambda x: abs(x["corr_with_market"]), reverse=True)
