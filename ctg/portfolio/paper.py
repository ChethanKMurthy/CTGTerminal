"""Execution Layer (paper) — a virtual book driven by CIO signals.

Marks to real prices from the warehouse. Conviction-weighted target portfolio,
capped per-name and by gross exposure. Records trades + an equity curve so the
dashboard can show real, evolving P&L. Structured so a real broker adapter
(Zerodha/Dhan) could replace `_execute` later.
"""
from __future__ import annotations

from ..config import get_settings
from ..logging_conf import get_logger
from ..storage.db import duck_df, now_iso, sqlite

log = get_logger("portfolio.paper")


def _last_price(symbol: str) -> float | None:
    df = duck_df(
        "SELECT close FROM prices WHERE symbol=? ORDER BY ts DESC LIMIT 1", [symbol]
    )
    if df.empty:
        return None
    try:
        return float(df["close"].iloc[0])
    except (ValueError, TypeError):
        return None


def _state() -> tuple[float, dict]:
    con = sqlite()
    try:
        cash_row = con.execute("SELECT v FROM kv WHERE k='paper_cash'").fetchone()
        s = get_settings()
        cash = float(__import__("json").loads(cash_row["v"])) if cash_row else \
            float(s.get("risk", "paper_starting_capital", default=1_000_000))
        pos = {r["symbol"]: dict(r) for r in
               con.execute("SELECT * FROM paper_positions").fetchall()}
        return cash, pos
    finally:
        con.close()


def _set_cash(cash: float) -> None:
    con = sqlite()
    try:
        con.execute(
            "INSERT INTO kv(k,v) VALUES('paper_cash',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (__import__("json").dumps(cash),),
        )
        con.commit()
    finally:
        con.close()


def rebalance(signals: list[dict], min_conviction: float = 30.0, top_n: int = 8) -> dict:
    """Move the book toward the risk engine's disciplined target weights.

    Sizing, sector caps and drawdown de-grossing are delegated to engine.risk so
    the book is conviction-weighted, vol-targeted and concentration-limited.
    """
    from ..engine.risk import target_weights
    from ..storage.db import latest_agent_output
    s = get_settings()

    cash, positions = _state()
    # current equity (mark to market)
    pos_value = 0.0
    for sym, p in positions.items():
        px = _last_price(sym) or p["avg_price"]
        pos_value += p["qty"] * px
    equity = cash + pos_value
    if equity <= 0:
        equity = float(s.get("risk", "paper_starting_capital", default=1_000_000))

    regime = latest_agent_output("regime") or {}
    plan = target_weights(signals, equity, regime, top_n=top_n, min_conviction=min_conviction)
    targets: dict[str, float] = plan["weights"]
    picks = [x for x in signals if x["symbol"] in targets]

    con = sqlite()
    trades = []
    try:
        # close names no longer targeted
        for sym in list(positions.keys()):
            if sym not in targets:
                px = _last_price(sym) or positions[sym]["avg_price"]
                qty = positions[sym]["qty"]
                cash += qty * px
                trades.append(("SELL" if qty > 0 else "COVER", sym, abs(qty), px, "exit: not in top signals"))
                con.execute("DELETE FROM paper_positions WHERE symbol=?", (sym,))

        # set/adjust targeted names
        for sym, w in targets.items():
            px = _last_price(sym)
            if not px:
                continue
            target_value = w * equity
            target_qty = round(target_value / px, 2)
            cur_qty = positions.get(sym, {}).get("qty", 0.0)
            delta = round(target_qty - cur_qty, 2)
            if abs(delta * px) < equity * 0.005:  # ignore tiny adjustments
                continue
            cash -= delta * px
            side = "BUY" if delta > 0 else "SELL"
            sig = next((p for p in picks if p["symbol"] == sym), {})
            trades.append((side, sym, abs(delta), px, f"target {w:+.2%} conv {sig.get('conviction')}"))
            con.execute(
                """INSERT INTO paper_positions(symbol,qty,avg_price,opened_ts,last_price,thesis)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty,
                     last_price=excluded.last_price""",
                (sym, target_qty, px, now_iso(), px, sig.get("thesis", "")[:300]),
            )

        for side, sym, qty, px, reason in trades:
            con.execute(
                "INSERT INTO paper_trades(ts,symbol,side,qty,price,reason) VALUES(?,?,?,?,?,?)",
                (now_iso(), sym, side, qty, px, reason),
            )
        con.commit()
    finally:
        con.close()

    _set_cash(cash)
    snap = mark_to_market()
    log.info("Paper rebalance: %d trades, equity ₹%.0f, gross %.0f%%",
             len(trades), snap["equity"], plan["gross"] * 100)
    return {"trades": len(trades), "risk_plan": plan, **snap}


def mark_to_market() -> dict:
    cash, positions = _state()
    pos_value = 0.0
    holdings = []
    for sym, p in positions.items():
        px = _last_price(sym) or p["avg_price"]
        val = p["qty"] * px
        pos_value += val
        pnl = (px - p["avg_price"]) * p["qty"]
        holdings.append({
            "symbol": sym, "qty": p["qty"], "avg_price": round(p["avg_price"], 2),
            "last": round(px, 2), "value": round(val, 0), "pnl": round(pnl, 0),
            "thesis": p.get("thesis", ""),
        })
    equity = cash + pos_value
    con = sqlite()
    try:
        con.execute(
            "INSERT OR REPLACE INTO paper_equity(ts,cash,positions_value,equity) VALUES(?,?,?,?)",
            (now_iso(), cash, pos_value, equity),
        )
        con.commit()
    finally:
        con.close()
    start = float(get_settings().get("risk", "paper_starting_capital", default=1_000_000))
    return {
        "cash": round(cash, 0), "positions_value": round(pos_value, 0),
        "equity": round(equity, 0), "total_return_pct": round((equity / start - 1) * 100, 2),
        "holdings": sorted(holdings, key=lambda h: abs(h["value"]), reverse=True),
    }


def equity_curve(limit: int = 200) -> list[dict]:
    con = sqlite()
    try:
        rows = con.execute(
            "SELECT ts, equity FROM paper_equity ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        con.close()
