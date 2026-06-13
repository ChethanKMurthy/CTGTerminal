"""Layer 10 — CIO aggregation + signal store.

The CIO takes ranked alpha candidates, attaches scenarios, converts the edge to
a 0-100 conviction (with expected return and tail risk), and persists the top
ideas as signals. This is the system's final ranked view of "where the largest
mispricings are right now".
"""
from __future__ import annotations

import json

import numpy as np

from ..logging_conf import get_logger
from ..storage.db import now_iso, save_agent_output, sqlite
from .alpha import discover
from .scenarios import build_scenarios
from ..storage.db import latest_agent_output

log = get_logger("engine.signals")


def conviction_from_edge(edge_abs: float, regime_score: float, agreement: float) -> float:
    """Map |edge| (0..1) + regime confidence + factor agreement -> 0..100.

    Calibrated so a strong, well-agreed edge in a clear regime lands ~55-70,
    a typical edge ~35-45, and weak/contested edges fall below the deploy floor.
    """
    base = min(edge_abs * 95, 65)
    regime_conf = abs(regime_score - 50) / 50.0  # clearer regime -> more confident
    conv = base + 18 * agreement + 12 * regime_conf
    return float(np.clip(conv, 0, 100))


def run_cio(top_n: int = 12, min_edge: float = 0.12) -> dict:
    candidates = discover()
    regime = latest_agent_output("regime") or {}
    regime_score = regime.get("risk_score", 50.0) or 50.0

    selected = [c for c in candidates if c["abs_edge"] >= min_edge][:top_n]
    signals = []
    # Token discipline (free-tier): LLM-refine scenarios only for the top names;
    # the rest get the deterministic quant scenario tree.
    llm_scenario_rank = 5
    for rank, c in enumerate(selected):
        # factor agreement = share of factors pointing same way as net score
        vals = list(c["factors"].values())
        same = sum(1 for v in vals if (v > 0) == (c["raw_score"] > 0))
        agreement = same / len(vals) if vals else 0.0

        scen = build_scenarios(c, regime, use_llm=(rank < llm_scenario_rank))
        scenarios = scen.get("scenarios", [])
        # probability-weighted expected move of the underlying
        exp_move = sum(s.get("prob", 0) * s.get("target_move_pct", 0) for s in scenarios)

        # Coherence: the CIO's direction follows the probability-weighted view.
        # If that flips the quant edge's direction, the LLM scenario disagreed —
        # keep the synthesised direction but haircut conviction for the conflict.
        dir_sign = 1 if exp_move >= 0 else -1
        final_dir = "LONG" if dir_sign > 0 else "SHORT"
        conflict = final_dir != c["direction"]

        # express return/tail as P&L in the chosen direction (favourable = +)
        exp_ret = exp_move * dir_sign
        pnl_scen = [s.get("target_move_pct", 0) * dir_sign for s in scenarios]
        tail = min(pnl_scen) if pnl_scen else 0.0

        conv = conviction_from_edge(c["abs_edge"], regime_score, agreement)
        if conflict:
            conv *= 0.6

        sig = {
            "symbol": c["symbol"],
            "company": c["company"],
            "sector": c["sector"],
            "direction": final_dir,
            "quant_direction": c["direction"],
            "llm_quant_conflict": conflict,
            "conviction": round(conv, 1),
            "expected_return": round(float(exp_ret), 2),
            "tail_risk": round(float(tail), 2),
            "horizon": scen.get("horizon", "~1 month"),
            "thesis": scen.get("thesis") or (scen.get("view") or {}).get("summary", ""),
            "key_risk": scen.get("key_risk", ""),
            "factors": c["factors"],
            "scenarios": scenarios,
            "last": c["price_features"].get("last"),
        }
        signals.append(sig)

    signals.sort(key=lambda s: s["conviction"], reverse=True)
    _persist_signals(signals)
    out = {"agent": "cio", "scope": "market", "ts": now_iso(),
           "regime": regime.get("label"), "n_candidates": len(candidates),
           "signals": signals}
    save_agent_output("cio", "market", out)
    log.info("CIO produced %d signals (regime=%s)", len(signals), regime.get("label"))
    return out


def _persist_signals(signals: list[dict]) -> None:
    con = sqlite()
    try:
        # mark previous open signals as superseded
        con.execute("UPDATE signals SET status='superseded' WHERE status='open'")
        for s in signals:
            con.execute(
                """INSERT INTO signals
                   (ts,symbol,direction,conviction,expected_return,tail_risk,
                    horizon,thesis,drivers,scenarios,status)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'open')""",
                (now_iso(), s["symbol"], s["direction"], s["conviction"],
                 s["expected_return"], s["tail_risk"], s["horizon"], s["thesis"],
                 json.dumps(s["factors"]), json.dumps(s["scenarios"])),
            )
        con.commit()
    finally:
        con.close()


def open_signals() -> list[dict]:
    con = sqlite()
    try:
        rows = con.execute(
            "SELECT * FROM signals WHERE status='open' ORDER BY conviction DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["drivers"] = json.loads(d.get("drivers") or "{}")
            d["scenarios"] = json.loads(d.get("scenarios") or "[]")
            out.append(d)
        return out
    finally:
        con.close()
