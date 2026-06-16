"""Unit tests for risk-engine pure logic."""
from ctg.engine.risk import degross_for_drawdown, STRESS_SCENARIOS


def test_degross_steps_down_with_drawdown():
    assert degross_for_drawdown(0.0) == 1.0
    assert degross_for_drawdown(-0.03) == 1.0
    assert degross_for_drawdown(-0.06) == 0.85
    assert degross_for_drawdown(-0.12) == 0.7
    assert degross_for_drawdown(-0.20) == 0.5


def test_degross_monotonic_non_increasing():
    dds = [0.0, -0.05, -0.10, -0.15, -0.30]
    vals = [degross_for_drawdown(d) for d in dds]
    assert vals == sorted(vals, reverse=True)


def test_stress_scenarios_present():
    assert "Broad selloff -7%" in STRESS_SCENARIOS
    assert any("Crude" in s for s in STRESS_SCENARIOS)
    assert all(isinstance(v, dict) for v in STRESS_SCENARIOS.values())
