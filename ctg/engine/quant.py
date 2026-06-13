"""Quantitative analytics shared by the agents.

Pure functions over the warehouse: options positioning (PCR, max-pain, dealer
gamma/GEX, IV skew, OI walls), institutional flow pressure, and price/return
features for regime + signals. No LLM here — this is the deterministic spine.
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd
from scipy.stats import norm

from ..logging_conf import get_logger
from ..storage.db import duck_df

log = get_logger("engine.quant")

# Index option contract multipliers (lot size affects absolute GEX scale only)
LOT_SIZE = {"NIFTY": 75, "BANKNIFTY": 35, "FINNIFTY": 65}


# ---------------------------------------------------------------------
# Options analytics
# ---------------------------------------------------------------------
def _bs_gamma(spot, strike, t, iv, r=0.065):
    if spot <= 0 or strike <= 0 or t <= 0 or iv <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    return norm.pdf(d1) / (spot * iv * math.sqrt(t))


def options_metrics(underlying: str) -> dict | None:
    """Compute positioning metrics from the latest option-chain snapshot."""
    snap = duck_df(
        "SELECT * FROM option_chain WHERE underlying=? AND ts=("
        "SELECT max(ts) FROM option_chain WHERE underlying=?)",
        [underlying, underlying],
    )
    if snap.empty:
        return None
    spot = float(snap["spot"].iloc[0]) if not pd.isna(snap["spot"].iloc[0]) else None
    expiry = snap["expiry"].iloc[0]
    ts = snap["ts"].iloc[0]
    if not spot:
        return None

    # days to expiry
    try:
        exp_dt = datetime.strptime(expiry, "%d-%b-%Y")
        t_years = max((exp_dt - datetime.now()).days, 0) / 365.0
        t_years = max(t_years, 1 / 365)
    except Exception:  # noqa: BLE001
        t_years = 7 / 365

    ce_oi = snap["ce_oi"].sum()
    pe_oi = snap["pe_oi"].sum()
    pcr = round(pe_oi / ce_oi, 3) if ce_oi else None

    # Max pain: strike minimising total writer payout
    strikes = snap["strike"].values
    max_pain, min_pay = None, float("inf")
    for k in strikes:
        ce_pay = np.sum(np.maximum(strikes - k, 0) * snap["ce_oi"].values)
        pe_pay = np.sum(np.maximum(k - strikes, 0) * snap["pe_oi"].values)
        total = ce_pay + pe_pay
        if total < min_pay:
            min_pay, max_pain = total, float(k)

    # OI walls (support/resistance)
    resistance = float(snap.loc[snap["ce_oi"].idxmax(), "strike"])
    support = float(snap.loc[snap["pe_oi"].idxmax(), "strike"])

    # Dealer gamma exposure (GEX). Convention: dealers long calls / short puts.
    lot = LOT_SIZE.get(underlying, 50)
    gex = 0.0
    for _, row in snap.iterrows():
        k = row["strike"]
        cg = _bs_gamma(spot, k, t_years, (row["ce_iv"] or 0) / 100.0)
        pg = _bs_gamma(spot, k, t_years, (row["pe_iv"] or 0) / 100.0)
        gex += cg * row["ce_oi"] * lot
        gex -= pg * row["pe_oi"] * lot
    gex_notional = gex * spot * spot * 0.01  # $ gamma per 1% move (scaled)

    # IV skew: avg OTM put IV - avg OTM call IV
    otm_puts = snap[(snap["strike"] < spot) & (snap["pe_iv"] > 0)]["pe_iv"]
    otm_calls = snap[(snap["strike"] > spot) & (snap["ce_iv"] > 0)]["ce_iv"]
    skew = None
    if len(otm_puts) and len(otm_calls):
        skew = round(float(otm_puts.mean() - otm_calls.mean()), 2)

    atm_iv = None
    atm_row = snap.iloc[(snap["strike"] - spot).abs().argsort()[:1]]
    if not atm_row.empty:
        ce_iv, pe_iv = atm_row["ce_iv"].iloc[0], atm_row["pe_iv"].iloc[0]
        ivs = [v for v in (ce_iv, pe_iv) if v and v > 0]
        atm_iv = round(float(np.mean(ivs)), 2) if ivs else None

    return {
        "underlying": underlying,
        "ts": str(ts),
        "expiry": expiry,
        "spot": round(spot, 2),
        "pcr_oi": pcr,
        "max_pain": max_pain,
        "max_pain_dist_pct": round((spot / max_pain - 1) * 100, 2) if max_pain else None,
        "support_oi_strike": support,
        "resistance_oi_strike": resistance,
        "gex": round(gex_notional, 2),
        "gamma_regime": "positive" if gex_notional > 0 else "negative",
        "iv_skew_put_minus_call": skew,
        "atm_iv": atm_iv,
        "total_ce_oi": int(ce_oi),
        "total_pe_oi": int(pe_oi),
    }


def pcr_interpretation(pcr: float | None) -> str:
    """Human-readable read of a Put-Call Ratio (OI)."""
    if pcr is None:
        return "n/a"
    if pcr >= 1.5:
        return "Heavily put-heavy — strong support / possible oversold"
    if pcr >= 1.1:
        return "Put-heavy — bullish-leaning, writers expect support"
    if pcr >= 0.9:
        return "Balanced positioning"
    if pcr >= 0.6:
        return "Call-heavy — bearish-leaning, overhead resistance"
    return "Heavily call-heavy — possible overbought / capped upside"


def top_movers(n: int = 5) -> dict:
    """Top gainers/losers across the universe by latest 1-session return."""
    df = duck_df(
        "SELECT symbol, ts, close FROM prices WHERE interval='1d' ORDER BY symbol, ts"
    )
    if df.empty:
        return {"gainers": [], "losers": []}
    rows = []
    for sym, g in df.groupby("symbol"):
        c = g["close"].astype(float)
        if len(c) < 2 or c.iloc[-2] == 0:
            continue
        ret = (c.iloc[-1] / c.iloc[-2] - 1) * 100
        rows.append({"symbol": sym, "ret_pct": round(float(ret), 2),
                     "last": round(float(c.iloc[-1]), 2)})
    rows.sort(key=lambda r: r["ret_pct"], reverse=True)
    return {"gainers": rows[:n], "losers": rows[-n:][::-1]}


def sector_performance() -> list[dict]:
    """Average latest 1-session return grouped by sector (for a heatmap)."""
    from ..data.universe import load_universe
    u = load_universe()
    df = duck_df("SELECT symbol, ts, close FROM prices WHERE interval='1d' ORDER BY symbol, ts")
    if df.empty:
        return []
    by_sector: dict[str, list[float]] = {}
    for sym, g in df.groupby("symbol"):
        c = g["close"].astype(float)
        if len(c) < 2 or c.iloc[-2] == 0:
            continue
        ret = (c.iloc[-1] / c.iloc[-2] - 1) * 100
        by_sector.setdefault(u.sector(sym), []).append(float(ret))
    out = [{"sector": s, "avg_ret_pct": round(sum(v) / len(v), 2), "n": len(v)}
           for s, v in by_sector.items() if v]
    out.sort(key=lambda r: r["avg_ret_pct"], reverse=True)
    return out


def oi_change_summary(underlying: str) -> dict | None:
    """Classify the day's option OI build-up from CE/PE change-in-OI.

    Put writing (PE OI up) is supportive/bullish; call writing (CE OI up) is
    resistive/bearish. The dominant flow gives a quick positioning read.
    """
    snap = duck_df(
        "SELECT ce_chg_oi, pe_chg_oi FROM option_chain WHERE underlying=? AND ts=("
        "SELECT max(ts) FROM option_chain WHERE underlying=?)",
        [underlying, underlying],
    )
    if snap.empty:
        return None
    ce = int(snap["ce_chg_oi"].sum())
    pe = int(snap["pe_chg_oi"].sum())
    if pe > 0 and ce < 0:
        verdict = "Put writing + call unwinding — bullish"
    elif ce > 0 and pe < 0:
        verdict = "Call writing + put unwinding — bearish"
    elif pe > ce:
        verdict = "Net put writing — supportive"
    elif ce > pe:
        verdict = "Net call writing — capped upside"
    else:
        verdict = "Balanced OI change"
    return {"underlying": underlying, "ce_chg_oi": ce, "pe_chg_oi": pe, "verdict": verdict}


# ---------------------------------------------------------------------
# Institutional flow analytics
# ---------------------------------------------------------------------
def flow_metrics(lookback_days: int = 10) -> dict:
    fd = duck_df(
        "SELECT date, category, net FROM fii_dii ORDER BY date DESC LIMIT 60"
    )
    out: dict = {"fii_dii_available": not fd.empty}
    if not fd.empty:
        piv = fd.pivot_table(index="date", columns="category", values="net", aggfunc="sum").sort_index()
        for cat in ("FII", "DII"):
            if cat in piv.columns:
                series = piv[cat].dropna()
                out[f"{cat.lower()}_net_latest"] = round(float(series.iloc[-1]), 1)
                out[f"{cat.lower()}_net_5d"] = round(float(series.tail(5).sum()), 1)
                out[f"{cat.lower()}_net_streak"] = _streak(series.tail(lookback_days).values)
    return out


def fno_flow_metrics() -> dict:
    """Participant F&O positioning — the FII index-futures long/short gauge.

    FII net index futures (long-short) and its day-on-day change is one of the
    most-watched directional flow signals for Nifty.
    """
    df = duck_df(
        "SELECT * FROM fno_participant_oi WHERE date >= "
        "(SELECT max(date) FROM fno_participant_oi) - 1 ORDER BY date"
    )
    if df.empty:
        return {"available": False}
    dates = sorted(df["date"].unique())
    latest = df[df["date"] == dates[-1]]
    prev = df[df["date"] == dates[-2]] if len(dates) > 1 else None

    out: dict = {"available": True, "date": str(dates[-1])}
    for who in ("FII", "DII", "Pro", "Client"):
        row = latest[latest["participant"].str.upper() == who.upper()]
        if row.empty:
            continue
        r = row.iloc[0]
        long_, short_ = int(r["fut_idx_long"]), int(r["fut_idx_short"])
        net = long_ - short_
        denom = long_ + short_
        long_pct = round(long_ / denom * 100, 1) if denom else None
        # option index directional proxy: call-long + put-short  vs  put-long + call-short
        opt_bull = int(r["opt_idx_call_long"]) + int(r["opt_idx_put_short"])
        opt_bear = int(r["opt_idx_put_long"]) + int(r["opt_idx_call_short"])
        delta = None
        if prev is not None:
            pr = prev[prev["participant"].str.upper() == who.upper()]
            if not pr.empty:
                prev_net = int(pr.iloc[0]["fut_idx_long"]) - int(pr.iloc[0]["fut_idx_short"])
                delta = net - prev_net
        out[who.lower()] = {
            "idx_fut_long": long_, "idx_fut_short": short_,
            "idx_fut_net": net, "idx_fut_long_pct": long_pct,
            "idx_fut_net_chg": delta,
            "idx_opt_directional": opt_bull - opt_bear,
        }
    # headline read
    fii = out.get("fii", {})
    if fii:
        net = fii.get("idx_fut_net", 0)
        if net > 20000:
            out["headline"] = f"FII net LONG {net:,} index-futures contracts — bullish positioning"
        elif net < -20000:
            out["headline"] = f"FII net SHORT {abs(net):,} index-futures contracts — bearish/hedged positioning"
        else:
            out["headline"] = f"FII roughly balanced in index futures (net {net:,})"
    return out


def deal_pressure(symbols: set[str] | None = None, days: int = 5) -> list[dict]:
    """Net institutional accumulation/distribution from bulk+block deals."""
    df = duck_df(
        "SELECT date, symbol, side, qty, price, deal_type FROM bulk_block_deals "
        "WHERE date >= (SELECT max(date) FROM bulk_block_deals) - ?",
        [days],
    )
    if df.empty:
        return []
    df["signed_val"] = df.apply(
        lambda r: r["qty"] * r["price"] * (1 if r["side"] == "BUY" else -1), axis=1
    )
    agg = df.groupby("symbol").agg(
        net_value=("signed_val", "sum"),
        n_deals=("symbol", "count"),
    ).reset_index()
    agg = agg.sort_values("net_value", key=abs, ascending=False)
    rows = []
    for _, r in agg.head(25).iterrows():
        if symbols and r["symbol"] not in symbols:
            continue
        rows.append({
            "symbol": r["symbol"],
            "net_value_cr": round(r["net_value"] / 1e7, 2),  # to ₹ crore
            "n_deals": int(r["n_deals"]),
            "direction": "accumulation" if r["net_value"] > 0 else "distribution",
        })
    return rows


# ---------------------------------------------------------------------
# Price / return features
# ---------------------------------------------------------------------
def price_features(symbol: str) -> dict | None:
    df = duck_df(
        "SELECT ts, high, low, close, volume FROM prices WHERE symbol=? AND interval='1d' "
        "ORDER BY ts", [symbol]
    )
    if df.empty or len(df) < 30:
        return None
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)
    rets = close.pct_change().dropna()
    last = float(close.iloc[-1])
    ma20 = float(close.tail(20).mean())
    ma50 = float(close.tail(50).mean()) if len(close) >= 50 else ma20
    ma200 = float(close.tail(200).mean()) if len(close) >= 200 else ma50
    vol_ann = float(rets.tail(20).std() * math.sqrt(252) * 100)
    mom_1m = float(close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) > 21 else None
    mom_3m = float(close.iloc[-1] / close.iloc[-63] - 1) * 100 if len(close) > 63 else None
    rsi = _rsi(close)
    dist_52w_high = float(last / close.tail(252).max() - 1) * 100
    macd_line, macd_signal, macd_hist = _macd(close)
    return {
        "symbol": symbol, "last": round(last, 2),
        "above_ma20": last > ma20, "above_ma50": last > ma50, "above_ma200": last > ma200,
        "trend": "up" if last > ma50 > ma200 else ("down" if last < ma50 < ma200 else "mixed"),
        "vol_annualised_pct": round(vol_ann, 1),
        "mom_1m_pct": round(mom_1m, 2) if mom_1m is not None else None,
        "mom_3m_pct": round(mom_3m, 2) if mom_3m is not None else None,
        "rsi14": round(rsi, 1) if rsi is not None else None,
        "macd": macd_line, "macd_signal": macd_signal, "macd_hist": macd_hist,
        **_bollinger(close),
        "vwap20": _vwap(close, volume),
        "atr14": _atr(high, low, close),
        "stoch_k": _stochastic(high, low, close),
        "adx14": _adx(high, low, close),
        **_supertrend(high, low, close),
        "dist_from_52w_high_pct": round(dist_52w_high, 1),
    }


def _supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
                period: int = 10, mult: float = 3.0) -> dict:
    """Supertrend value + direction (bullish/bearish) from ATR bands."""
    n = len(close)
    if n < period + 1:
        return {"supertrend": None, "supertrend_dir": None}
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    hl2 = (high + low) / 2
    upper = (hl2 + mult * atr).to_numpy()
    lower = (hl2 - mult * atr).to_numpy()
    c = close.to_numpy()
    st = [0.0] * n
    dir_up = True
    for i in range(1, n):
        if c[i] > upper[i - 1]:
            dir_up = True
        elif c[i] < lower[i - 1]:
            dir_up = False
        st[i] = lower[i] if dir_up else upper[i]
    return {"supertrend": round(float(st[-1]), 2),
            "supertrend_dir": "bullish" if dir_up else "bearish"}


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float | None:
    """Average Directional Index — trend strength (0..100; >25 = trending)."""
    if len(close) < period * 2:
        return None
    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))) * 100
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    val = adx.iloc[-1]
    return round(float(val), 1) if val == val else None


def _stochastic(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float | None:
    """Stochastic oscillator %K (0..100): where close sits in the period range."""
    if len(close) < period:
        return None
    hh = high.tail(period).max()
    ll = low.tail(period).min()
    if hh == ll:
        return None
    k = (float(close.iloc[-1]) - ll) / (hh - ll) * 100
    return round(float(k), 1)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float | None:
    """Average True Range — volatility / stop-sizing measure (absolute price)."""
    if len(close) < period + 1:
        return None
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(),
                    (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    return round(float(tr.tail(period).mean()), 2)


def _vwap(close: pd.Series, volume: pd.Series, window: int = 20) -> float | None:
    """Rolling volume-weighted average price over the last `window` sessions."""
    if len(close) < window:
        return None
    c, v = close.tail(window), volume.tail(window)
    denom = float(v.sum())
    if denom <= 0:
        return None
    return round(float((c * v).sum() / denom), 2)


def _bollinger(close: pd.Series, window: int = 20, k: float = 2.0) -> dict:
    """Bollinger Bands + %B (position within the bands, 0..1)."""
    if len(close) < window:
        return {"bb_upper": None, "bb_lower": None, "bb_pctb": None}
    ma = close.tail(window).mean()
    sd = close.tail(window).std()
    upper, lower = ma + k * sd, ma - k * sd
    last = float(close.iloc[-1])
    pctb = (last - lower) / (upper - lower) if upper != lower else None
    return {"bb_upper": round(float(upper), 2), "bb_lower": round(float(lower), 2),
            "bb_pctb": round(float(pctb), 2) if pctb is not None else None}


def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """Moving Average Convergence Divergence (line, signal, histogram)."""
    if len(close) < slow + signal:
        return None, None, None
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, adjust=False).mean()
    hist = line - sig
    return round(float(line.iloc[-1]), 2), round(float(sig.iloc[-1]), 2), round(float(hist.iloc[-1]), 2)


INDEX_YAHOO = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "^CNXFIN"}


def pivot_levels(underlying: str) -> dict | None:
    """Classic pivot points + Central Pivot Range (CPR) for an index.

    Uses the last completed daily bar (free, via yfinance index tickers).
    """
    import yfinance as yf

    yt = INDEX_YAHOO.get(underlying)
    if not yt:
        return None
    try:
        data = yf.download(yt, period="7d", interval="1d", progress=False, auto_adjust=False)
    except Exception:  # noqa: BLE001
        return None
    if data is None or data.empty:
        return None
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.droplevel(1)  # single-ticker -> flat columns
    row = data.dropna().iloc[-1]
    h, l, c = float(row["High"]), float(row["Low"]), float(row["Close"])
    p = (h + l + c) / 3
    bc = (h + l) / 2
    tc = 2 * p - bc
    rng = h - l
    return {
        "underlying": underlying,
        "pivot": round(p, 1),
        "r1": round(2 * p - l, 1), "r2": round(p + rng, 1), "r3": round(h + 2 * (p - l), 1),
        "s1": round(2 * p - h, 1), "s2": round(p - rng, 1), "s3": round(l - 2 * (h - p), 1),
        "cpr_top": round(max(tc, bc), 1), "cpr_bottom": round(min(tc, bc), 1),
        "cpr_width_pct": round(abs(tc - bc) / c * 100, 3),
    }


def _rsi(close: pd.Series, period: int = 14) -> float | None:
    if len(close) < period + 1:
        return None
    delta = close.diff().dropna()
    gain = delta.clip(lower=0).tail(period).mean()
    loss = -delta.clip(upper=0).tail(period).mean()
    if loss == 0:
        return 100.0
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def _streak(vals) -> int:
    """Consecutive same-sign run ending at the last value (signed)."""
    if len(vals) == 0:
        return 0
    sign = 1 if vals[-1] >= 0 else -1
    n = 0
    for v in reversed(vals):
        if (v >= 0 and sign > 0) or (v < 0 and sign < 0):
            n += 1
        else:
            break
    return n * sign
