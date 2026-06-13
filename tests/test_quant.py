"""Unit tests for the deterministic quant analytics (no DB / network needed)."""
import math

from ctg.engine.quant import _bs_gamma, _rsi, _streak
import pandas as pd


def test_bs_gamma_peaks_atm():
    # gamma is highest at-the-money and falls off in the wings
    atm = _bs_gamma(100, 100, 30 / 365, 0.2)
    otm = _bs_gamma(100, 130, 30 / 365, 0.2)
    assert atm > otm > 0


def test_bs_gamma_degenerate_inputs():
    assert _bs_gamma(0, 100, 0.1, 0.2) == 0.0
    assert _bs_gamma(100, 100, 0, 0.2) == 0.0
    assert _bs_gamma(100, 100, 0.1, 0) == 0.0


def test_rsi_bounds_and_trend():
    up = pd.Series([i for i in range(1, 40)])  # monotonically rising
    assert _rsi(up) > 70
    down = pd.Series([i for i in range(40, 1, -1)])
    assert _rsi(down) < 30


def test_streak_sign_and_length():
    assert _streak([1, 2, 3]) == 3            # three positive in a row
    assert _streak([1, -1, -2]) == -2          # two negative ending
    assert _streak([]) == 0
