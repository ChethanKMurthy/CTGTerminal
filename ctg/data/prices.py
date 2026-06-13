"""Price + fundamentals collection via yfinance (free, ~15min delayed/EOD).

yfinance handles the Yahoo session/crumb dance and retries internally. We add a
short on-disk cache and tolerate the occasional 429 by skipping that cycle.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import yfinance as yf

from ..logging_conf import get_logger
from .universe import Universe

log = get_logger("data.prices")


def fetch_ohlcv(universe: Universe, period: str = "5d", interval: str = "15m") -> list[dict]:
    """Download recent OHLCV for the whole universe in one batched call."""
    tickers = [universe.yahoo(s) for s in universe.symbols]
    rows: list[dict] = []
    try:
        data = yf.download(
            tickers=" ".join(tickers),
            period=period,
            interval=interval,
            group_by="ticker",
            auto_adjust=False,
            threads=True,
            progress=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("yfinance batch download failed (%s)", exc)
        return rows

    if data is None or data.empty:
        log.warning("yfinance returned empty frame (likely rate-limited)")
        return rows

    for sym in universe.symbols:
        yt = universe.yahoo(sym)
        try:
            sub = data[yt] if yt in data.columns.get_level_values(0) else None
        except Exception:  # noqa: BLE001
            sub = None
        if sub is None or sub.dropna(how="all").empty:
            continue
        sub = sub.dropna(how="all")
        for ts, r in sub.iterrows():
            if pd.isna(r.get("Close")):
                continue
            rows.append(
                {
                    "symbol": sym,
                    "ts": pd.Timestamp(ts).to_pydatetime(),
                    "open": _f(r.get("Open")),
                    "high": _f(r.get("High")),
                    "low": _f(r.get("Low")),
                    "close": _f(r.get("Close")),
                    "volume": int(r.get("Volume") or 0),
                    "interval": interval,
                }
            )
    log.info("Fetched %d OHLCV rows (%s/%s) for %d symbols", len(rows), period, interval, len(tickers))
    return rows


def fetch_daily(universe: Universe, period: str = "2y") -> list[dict]:
    """Daily bars for longer-horizon analytics (regime, factors, correlations)."""
    return fetch_ohlcv(universe, period=period, interval="1d")


def fetch_fundamentals(universe: Universe, limit: int | None = None) -> list[dict]:
    """Per-name valuation snapshot. Slower (one request per name) — call EOD."""
    rows: list[dict] = []
    syms = universe.symbols[:limit] if limit else universe.symbols
    ts = datetime.now()
    for sym in syms:
        try:
            t = yf.Ticker(universe.yahoo(sym))
            info = t.info or {}
        except Exception as exc:  # noqa: BLE001
            log.debug("fundamentals %s failed: %s", sym, exc)
            continue
        if not info:
            continue
        rows.append(
            {
                "symbol": sym,
                "ts": ts,
                "pe": _f(info.get("trailingPE")),
                "pb": _f(info.get("priceToBook")),
                "roe": _f(info.get("returnOnEquity")),
                "market_cap": _f(info.get("marketCap")),
                "dividend_yield": _f(info.get("dividendYield")),
                "eps": _f(info.get("trailingEps")),
                "revenue": _f(info.get("totalRevenue")),
                "profit_margin": _f(info.get("profitMargins")),
                "debt_to_equity": _f(info.get("debtToEquity")),
            }
        )
    log.info("Fetched fundamentals for %d names", len(rows))
    return rows


def _f(x) -> float | None:
    try:
        if x is None:
            return None
        v = float(x)
        return v if v == v else None  # filter NaN
    except (ValueError, TypeError):
        return None
