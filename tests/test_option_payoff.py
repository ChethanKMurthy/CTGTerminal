"""Unit tests for the options payoff engine (exact at-expiry P&L math)."""
from ctg.engine.option_strategies import _payoff_curve, _net_premium, _metrics

LOT = 50
SPOT = 100.0


def _long_call(strike, prem):
    return [{"action": "BUY", "type": "CE", "strike": strike, "ltp": prem, "qty": 1}]


def test_long_call_breakeven_and_loss():
    legs = _long_call(100, 5)
    m = _metrics(legs, LOT, SPOT)
    # max loss = premium paid, profit unbounded, BE = strike + premium
    assert m["max_loss"] == -5 * LOT
    assert m["max_profit"] is None
    assert abs(m["breakevens"][0] - 105) <= 1


def test_bull_call_spread_is_capped_both_sides():
    legs = [
        {"action": "BUY", "type": "CE", "strike": 100, "ltp": 6, "qty": 1},
        {"action": "SELL", "type": "CE", "strike": 110, "ltp": 2, "qty": 1},
    ]
    m = _metrics(legs, LOT, SPOT)
    net = _net_premium(legs, LOT)          # debit of 4 * lot
    assert net == -4 * LOT
    assert m["max_loss"] == -4 * LOT       # debit
    assert m["max_profit"] == 6 * LOT      # (10 width - 4 debit) * lot
    assert m["max_profit"] is not None and m["max_loss"] is not None


def test_short_call_loss_is_unbounded():
    legs = [{"action": "SELL", "type": "CE", "strike": 100, "ltp": 5, "qty": 1}]
    m = _metrics(legs, LOT, SPOT)
    assert m["max_loss"] is None           # unlimited upside risk
    assert m["max_profit"] == 5 * LOT      # premium kept


def test_payoff_curve_monotonic_points():
    legs = _long_call(100, 5)
    pts = _payoff_curve(legs, LOT, SPOT)
    assert len(pts) == 121
    assert pts[0]["pnl"] <= pts[-1]["pnl"]  # rises to the upside
