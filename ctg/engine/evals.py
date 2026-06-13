"""Evaluation & learning loop (the Karpathy layer).

Every alpha cycle we snapshot each candidate's factor vector + entry price.
Once forward prices exist, we score:
  * forward return per name over a horizon,
  * Information Coefficient (IC) = rank/linear corr of each factor with fwd return,
  * directional hit-rate of the composite score,
and convert factor ICs into ADAPTIVE WEIGHTS that the alpha engine consumes.
This turns a static heuristic into a system that measures itself and reweights
toward whatever is actually predictive in the current market.

No look-ahead: a snapshot is only evaluated once a strictly-later daily close
exists at least `horizon` trading days out.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from ..logging_conf import get_logger
from ..storage.db import duck_df, duck_upsert, kv_get, kv_set, now_iso
from ..data.universe import load_universe

log = get_logger("engine.evals")

BASE_WEIGHTS = {
    "flow_pressure": 1.3, "positioning": 1.0, "valuation": 1.0,
    "mean_reversion": 1.0, "momentum": 1.0, "sentiment": 0.7, "graph_contagion": 0.6,
}


def snapshot_candidates(candidates: list[dict]) -> int:
    """Persist the current factor panel for later forward-return scoring."""
    ts = pd.Timestamp(now_iso()).to_pydatetime()
    rows = []
    for c in candidates:
        rows.append({
            "run_ts": ts,
            "symbol": c["symbol"],
            "entry_close": c["price_features"].get("last"),
            "score": c["raw_score"],
            "direction": c["direction"],
            "factors": json.dumps(c["factors"]),
        })
    n = duck_upsert("alpha_snapshots", rows)
    return n


def _forward_returns(horizon_days: int) -> pd.DataFrame:
    """Join snapshots to the first daily close >= horizon trading days later."""
    snaps = duck_df("SELECT run_ts, symbol, entry_close, score, factors FROM alpha_snapshots")
    if snaps.empty:
        return pd.DataFrame()
    prices = duck_df("SELECT symbol, ts, close FROM prices WHERE interval='1d' ORDER BY symbol, ts")
    if prices.empty:
        return pd.DataFrame()
    prices["ts"] = pd.to_datetime(prices["ts"])
    snaps["run_ts"] = pd.to_datetime(snaps["run_ts"])

    out = []
    for sym, grp in prices.groupby("symbol"):
        g = grp.sort_values("ts").reset_index(drop=True)
        sn = snaps[snaps["symbol"] == sym]
        for _, s in sn.iterrows():
            entry = s["entry_close"]
            if not entry or entry != entry:
                continue
            # bars strictly after the snapshot time
            future = g[g["ts"] > s["run_ts"]]
            if len(future) < horizon_days:
                continue
            fwd_close = float(future.iloc[horizon_days - 1]["close"])
            fwd_ret = fwd_close / float(entry) - 1.0
            try:
                fac = json.loads(s["factors"])
            except Exception:  # noqa: BLE001
                fac = {}
            out.append({"symbol": sym, "score": s["score"], "fwd_ret": fwd_ret, **fac})
    return pd.DataFrame(out)


def evaluate(horizon_days: int = 5, learn: bool = True) -> dict:
    df = _forward_returns(horizon_days)
    if df.empty or len(df) < 12:
        return {"status": "insufficient_history", "n_evaluated": int(len(df)),
                "note": f"Need forward prices for >=12 snapshots at {horizon_days}d horizon"}

    # composite score IC + hit rate
    score_ic = _safe_corr(df["score"], df["fwd_ret"])
    hits = ((df["score"] > 0) == (df["fwd_ret"] > 0)).mean()

    # per-factor IC
    factor_ic = {}
    for f in BASE_WEIGHTS:
        if f in df.columns:
            sub = df[[f, "fwd_ret"]].dropna()
            sub = sub[sub[f] != 0]
            if len(sub) >= 10:
                factor_ic[f] = round(_safe_corr(sub[f], sub["fwd_ret"]), 4)

    learned = None
    if learn and factor_ic:
        learned = _update_weights(factor_ic)

    result = {
        "status": "ok",
        "ts": now_iso(),
        "horizon_days": horizon_days,
        "n_evaluated": int(len(df)),
        "composite_ic": round(score_ic, 4),
        "hit_rate": round(float(hits), 3),
        "factor_ic": factor_ic,
        "learned_weights": learned,
        "mean_fwd_ret_pct": round(float(df["fwd_ret"].mean() * 100), 3),
    }
    kv_set("eval:latest", result)
    log.info("Eval: IC=%.3f hit=%.2f n=%d", score_ic, hits, len(df))
    return result


def _update_weights(factor_ic: dict, lam: float = 1.5, blend: float = 0.5) -> dict:
    """Blend base weights with IC-derived multipliers; clip and persist.

    A factor with positive IC (predictive in the right direction) gets up-
    weighted; a consistently anti-predictive factor gets down-weighted.
    `blend` keeps half the prior so weights move smoothly, not violently.
    """
    prior = get_learned_weights() or dict(BASE_WEIGHTS)
    ic_vals = np.array(list(factor_ic.values()))
    scale = np.std(ic_vals) or 0.05
    new = {}
    for f, base in BASE_WEIGHTS.items():
        ic = factor_ic.get(f)
        if ic is None:
            new[f] = prior.get(f, base)
            continue
        mult = 1.0 + lam * (ic / (scale * 3))
        target = float(np.clip(base * mult, 0.2, 2.5))
        new[f] = round(blend * prior.get(f, base) + (1 - blend) * target, 3)
    kv_set("learned_factor_weights", new)
    return new


def get_learned_weights() -> dict | None:
    return kv_get("learned_factor_weights", None)


def performance_summary() -> dict:
    latest = kv_get("eval:latest")
    n_snaps = int(duck_df("SELECT count(*) c FROM alpha_snapshots").c.iloc[0])
    return {"latest_eval": latest, "n_snapshots": n_snaps,
            "learned_weights": get_learned_weights()}


def _safe_corr(a, b) -> float:
    try:
        c = pd.Series(a).corr(pd.Series(b))
        return float(c) if c == c else 0.0
    except Exception:  # noqa: BLE001
        return 0.0
