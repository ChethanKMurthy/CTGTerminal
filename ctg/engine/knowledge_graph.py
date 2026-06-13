"""Layer 2 — the Market Knowledge Graph.

A continuously-updated directed multigraph of the Indian market:
  nodes:  stock, sector, index, macro (incl. commodity/fx), theme
  edges:  belongs_to (stock->sector), sensitive_to (sector->macro, signed),
          correlated (stock<->stock, from realised returns),
          contains (index->stock), news_link (LLM-extracted entity relations)

The graph is the substrate the agents reason over and the alpha engine traverses
for contagion / second-order effects.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data.universe import MACRO_NODES, SECTOR_MACRO_LINKS, Universe, load_universe
from ..logging_conf import get_logger
from ..storage.db import duck_df
from ..storage.graph_store import load_graph, save_graph
from ..llm.client import llm

log = get_logger("engine.kg")


def _returns_matrix(symbols: list[str], min_obs: int = 30) -> pd.DataFrame:
    """Aligned daily-ish returns matrix from the warehouse (daily if present)."""
    df = duck_df(
        """
        SELECT symbol, ts, close,
               row_number() OVER (PARTITION BY symbol, interval ORDER BY ts) rn,
               interval
        FROM prices
        WHERE interval = '1d'
        """
    )
    if df.empty:
        # fall back to intraday closes resampled to last-per-day
        df = duck_df("SELECT symbol, ts, close FROM prices")
        if df.empty:
            return pd.DataFrame()
        df["day"] = pd.to_datetime(df["ts"]).dt.date
        df = df.sort_values("ts").groupby(["symbol", "day"], as_index=False).last()
        df["ts"] = pd.to_datetime(df["day"])
    piv = df.pivot_table(index="ts", columns="symbol", values="close", aggfunc="last")
    piv = piv[[s for s in symbols if s in piv.columns]].sort_index()
    rets = piv.pct_change().dropna(how="all")
    rets = rets.dropna(axis=1, thresh=min_obs)
    return rets


def build_graph(universe: Universe | None = None, with_llm: bool = True) -> dict:
    u = universe or load_universe()
    g = load_graph()
    g.clear()

    # --- macro nodes ---
    for m in MACRO_NODES:
        kind = "fx" if "/" in m else ("commodity" if m in ("Brent Crude", "Gold") else "macro")
        g.add_node(m, kind=kind, label=m)

    # --- latest macro values (annotate nodes) ---
    macro_df = duck_df(
        "SELECT label, value FROM macro WHERE (label, date) IN "
        "(SELECT label, max(date) FROM macro GROUP BY label)"
    )
    macro_latest = dict(zip(macro_df.get("label", []), macro_df.get("value", []))) if not macro_df.empty else {}

    # --- index nodes + latest level ---
    idx_df = duck_df(
        "SELECT name, last, change_pct FROM index_levels WHERE (name, ts) IN "
        "(SELECT name, max(ts) FROM index_levels GROUP BY name)"
    )
    tracked_idx = {"NIFTY 50", "NIFTY BANK", "NIFTY FIN SERVICE", "INDIA VIX"}
    for _, r in idx_df.iterrows():
        if r["name"] in tracked_idx:
            g.add_node(r["name"], kind="index", label=r["name"],
                       last=_num(r["last"]), change_pct=_num(r["change_pct"]))

    # --- sector nodes ---
    for sector in u.sectors():
        g.add_node(sector, kind="sector", label=sector)

    # --- latest stock price + short-window return ---
    px = duck_df(
        """
        WITH latest AS (
          SELECT symbol, max(ts) mx FROM prices GROUP BY symbol
        ), first5 AS (
          SELECT symbol, close FROM prices p
          WHERE ts = (SELECT min(ts) FROM prices q WHERE q.symbol=p.symbol)
        )
        SELECT p.symbol, p.close AS last,
               (SELECT close FROM prices x WHERE x.symbol=p.symbol ORDER BY ts ASC LIMIT 1) AS first
        FROM prices p JOIN latest l ON p.symbol=l.symbol AND p.ts=l.mx
        """
    )
    ret_map = {}
    if not px.empty:
        for _, r in px.iterrows():
            last, first = _num(r["last"]), _num(r["first"])
            ret = (last / first - 1) * 100 if last and first else None
            ret_map[r["symbol"]] = (last, ret)

    # --- stock nodes + belongs_to edges ---
    for sym in u.symbols:
        sector = u.sector(sym)
        last, ret = ret_map.get(sym, (None, None))
        g.add_node(sym, kind="stock", label=u.company(sym), sector=sector,
                   last=last, window_ret_pct=ret)
        g.add_edge(sym, sector, relation="belongs_to", weight=1.0)

    # --- index contains stock (Nifty 50 contains all) ---
    if "NIFTY 50" in g:
        for sym in u.symbols:
            g.add_edge("NIFTY 50", sym, relation="contains", weight=1.0)

    # --- sector -> macro sensitivities (signed) ---
    for sector, links in SECTOR_MACRO_LINKS.items():
        if sector not in g:
            continue
        for macro_node, sign in links:
            if macro_node in g:
                g.add_edge(sector, macro_node, relation="sensitive_to", weight=float(sign))

    # --- stock<->stock correlations (top-k per name) ---
    n_corr = _add_correlations(g, u, top_k=4)

    # --- LLM-extracted news relations (optional) ---
    n_news = 0
    if with_llm and llm().available:
        n_news = _add_news_relations(g, u)

    save_graph(g)
    summary = {
        "nodes": g.number_of_nodes(),
        "edges": g.number_of_edges(),
        "correlation_edges": n_corr,
        "news_edges": n_news,
        "macro_annotated": len(macro_latest),
    }
    log.info("Knowledge graph built: %s", summary)
    return summary


def _add_correlations(g, u: Universe, top_k: int = 4) -> int:
    rets = _returns_matrix(u.symbols)
    if rets.empty or rets.shape[1] < 3:
        return 0
    corr = rets.corr()
    added = 0
    for sym in corr.columns:
        series = corr[sym].drop(labels=[sym], errors="ignore").dropna()
        top = series.reindex(series.abs().sort_values(ascending=False).index)[:top_k]
        for other, val in top.items():
            if abs(val) >= 0.35:
                g.add_edge(sym, other, relation="correlated", weight=round(float(val), 3))
                added += 1
    return added


def _add_news_relations(g, u: Universe, max_items: int = 25) -> int:
    """Use the LLM to extract entity->entity relations from recent headlines."""
    news = duck_df("SELECT title, summary FROM news ORDER BY ts DESC LIMIT ?", [max_items])
    if news.empty:
        return 0
    headlines = "\n".join(f"- {t}" for t in news["title"].tolist())
    known = ", ".join(list(g.nodes)[:120])
    prompt = (
        "From these Indian market headlines, extract cause->effect or "
        "company/sector relationships. Prefer entities from this known list when "
        f"they match: {known}.\n\nHeadlines:\n{headlines}\n\n"
        'Return JSON: {"relations":[{"source":"..","target":"..",'
        '"relation":"impacts|supplies|competes|owns|regulates","weight":0.0_to_1.0}]}'
    )
    data = llm().complete_json(prompt, default={"relations": []})
    rels = (data or {}).get("relations", [])
    added = 0
    for rel in rels[:60]:
        src, tgt = str(rel.get("source", "")).strip(), str(rel.get("target", "")).strip()
        if not src or not tgt or src == tgt:
            continue
        if src not in g:
            g.add_node(src, kind="theme", label=src)
        if tgt not in g:
            g.add_node(tgt, kind="theme", label=tgt)
        g.add_edge(src, tgt, relation=str(rel.get("relation", "impacts")),
                   weight=float(rel.get("weight", 0.5) or 0.5))
        added += 1
    return added


def neighbors(symbol: str, depth: int = 1) -> dict:
    """Return the local subgraph around a node (for explainability)."""
    g = load_graph()
    if symbol not in g:
        return {"node": symbol, "found": False, "edges": []}
    out = []
    seen = {symbol}
    frontier = [symbol]
    for _ in range(depth):
        nxt = []
        for n in frontier:
            for _, v, d in g.out_edges(n, data=True):
                out.append({"source": n, "target": v, "relation": d.get("relation"),
                            "weight": d.get("weight")})
                if v not in seen:
                    seen.add(v); nxt.append(v)
            for u_, _, d in g.in_edges(n, data=True):
                out.append({"source": u_, "target": n, "relation": d.get("relation"),
                            "weight": d.get("weight")})
        frontier = nxt
    return {"node": symbol, "found": True, "edges": out}


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (ValueError, TypeError):
        return None
