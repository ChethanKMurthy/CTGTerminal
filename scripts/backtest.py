#!/usr/bin/env python3
"""Factor backtest CLI — score alpha snapshots on forward returns across horizons.

Usage:  python scripts/backtest.py [horizons...]   (default: 5 10 21)
Reports composite Information Coefficient, hit-rate and per-factor IC.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctg.engine.evals import evaluate  # noqa: E402


def main() -> None:
    horizons = [int(a) for a in sys.argv[1:]] or [5, 10, 21]
    print(f"{'horizon':>8} | {'n':>5} | {'IC':>7} | {'hit':>5} | top factors")
    print("-" * 70)
    for h in horizons:
        r = evaluate(horizon_days=h, learn=False)
        if r.get("status") != "ok":
            print(f"{h:>8} | {r.get('n_evaluated', 0):>5} | {'--':>7} | {'--':>5} | {r.get('note', r.get('status'))}")
            continue
        fic = r.get("factor_ic", {})
        top = ", ".join(f"{k}={v:+.2f}" for k, v in
                        sorted(fic.items(), key=lambda kv: -abs(kv[1]))[:3])
        print(f"{h:>8} | {r['n_evaluated']:>5} | {r['composite_ic']:>+7.3f} | "
              f"{r['hit_rate']:>5.0%} | {top}")


if __name__ == "__main__":
    main()
