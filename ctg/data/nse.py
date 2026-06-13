"""NSE India data client — real, free, official public endpoints.

NSE blocks naked requests (403). The trick is to first hit the homepage with a
browser-like header set to obtain cookies, then reuse that session for the JSON
API endpoints. We cache the session, rotate it on failure, and back off politely
to respect the (unpublished) rate limits.

Endpoints used (all public, no key):
  - /api/marketStatus
  - /api/option-chain-indices?symbol=NIFTY|BANKNIFTY
  - /api/fiidiiTradeReact                      (FII/DII cash provisional)
  - /api/historical/bulk-deals , block-deals
  - /api/corporate-announcements?index=equities
  - /api/allIndices
  - /api/marketStatus / live index
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any

import requests

from ..logging_conf import get_logger

log = get_logger("data.nse")

BASE = "https://www.nseindia.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    # NOTE: do NOT advertise 'br' (brotli) — requests can't decode it without the
    # brotli package and NSE will return a body that fails JSON parsing.
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nseindia.com/option-chain",
    "Connection": "keep-alive",
}


class NSEClient:
    def __init__(self) -> None:
        self._session: requests.Session | None = None
        self._stamp = 0.0
        self._lock = threading.Lock()
        self._min_interval = 0.6  # seconds between requests
        self._last_call = 0.0

    # -- session management ------------------------------------------
    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(HEADERS)
        try:
            s.get(BASE, timeout=12)
            s.get(BASE + "/option-chain", timeout=12)  # warms more cookies
        except Exception as exc:  # noqa: BLE001
            log.warning("NSE session warmup failed: %s", exc)
        return s

    def _ensure_session(self) -> requests.Session:
        with self._lock:
            if self._session is None or (time.time() - self._stamp) > 300:
                self._session = self._new_session()
                self._stamp = time.time()
            return self._session

    def _throttle(self) -> None:
        dt = time.time() - self._last_call
        if dt < self._min_interval:
            time.sleep(self._min_interval - dt)
        self._last_call = time.time()

    def get_json(self, path: str, params: dict | None = None, retries: int = 3) -> Any:
        last_err: Exception | None = None
        for attempt in range(retries):
            self._throttle()
            sess = self._ensure_session()
            try:
                r = sess.get(BASE + path, params=params, timeout=15)
                if r.status_code in (401, 403):
                    log.debug("NSE %s -> %s; refreshing session", path, r.status_code)
                    with self._lock:
                        self._session = None
                    time.sleep(1.0 + attempt)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(1.0 + attempt)
        log.warning("NSE GET %s failed after %d tries: %s", path, retries, last_err)
        return None

    # -- endpoints ---------------------------------------------------
    def market_status(self) -> Any:
        return self.get_json("/api/marketStatus")

    def all_indices(self) -> list[dict]:
        data = self.get_json("/api/allIndices")
        return (data or {}).get("data", []) if data else []

    def option_expiries(self, symbol: str) -> list[str]:
        data = self.get_json("/api/option-chain-contract-info", {"symbol": symbol})
        return (data or {}).get("expiryDates", []) if data else []

    def option_chain(self, symbol: str, expiry: str | None = None) -> dict | None:
        """Index option chain via the current v3 endpoint.

        symbol in {NIFTY, BANKNIFTY, FINNIFTY}. If no expiry is given, the
        nearest (front-month, most gamma-relevant) expiry is used.
        """
        if expiry is None:
            expiries = self.option_expiries(symbol)
            if not expiries:
                return None
            expiry = expiries[0]
        data = self.get_json(
            "/api/option-chain-v3",
            {"type": "Indices", "symbol": symbol, "expiry": expiry},
        )
        if data is not None and isinstance(data, dict):
            data.setdefault("_expiry", expiry)
        return data

    def fii_dii(self) -> list[dict]:
        data = self.get_json("/api/fiidiiTradeReact")
        return data if isinstance(data, list) else []

    def large_deals(self) -> dict:
        """Returns the live large-deal snapshot: bulk + block + short deals."""
        data = self.get_json("/api/snapshot-capital-market-largedeal")
        return data if isinstance(data, dict) else {}

    def corporate_announcements(self) -> list[dict]:
        data = self.get_json("/api/corporate-announcements", {"index": "equities"})
        return data if isinstance(data, list) else []

    def is_market_open(self) -> bool:
        ms = self.market_status()
        if not ms:
            return False
        for seg in ms.get("marketState", []):
            if seg.get("market") == "Capital Market":
                return str(seg.get("marketStatus", "")).lower() == "open"
        return False


_client: NSEClient | None = None


def nse() -> NSEClient:
    global _client
    if _client is None:
        _client = NSEClient()
    return _client


# ---------------------------------------------------------------------
# Parsers -> normalised rows for the warehouse
# ---------------------------------------------------------------------
def parse_option_chain(symbol: str, raw: dict, ts: datetime) -> list[dict]:
    """Flatten NSE option-chain JSON into per-strike rows."""
    rows: list[dict] = []
    if not raw:
        return rows
    records = raw.get("records", {})
    spot = records.get("underlyingValue")
    chain_expiry = raw.get("_expiry") or (records.get("expiryDates") or [None])[0]
    for item in records.get("data", []):
        strike = item.get("strikePrice")
        expiry = item.get("expiryDate") or chain_expiry
        ce = item.get("CE", {}) or {}
        pe = item.get("PE", {}) or {}
        rows.append(
            {
                "underlying": symbol,
                "ts": ts,
                "expiry": expiry,
                "strike": float(strike) if strike is not None else None,
                "spot": float(spot) if spot is not None else None,
                "ce_oi": int(ce.get("openInterest", 0) or 0),
                "ce_chg_oi": int(ce.get("changeinOpenInterest", 0) or 0),
                "ce_iv": float(ce.get("impliedVolatility", 0) or 0),
                "ce_ltp": float(ce.get("lastPrice", 0) or 0),
                "pe_oi": int(pe.get("openInterest", 0) or 0),
                "pe_chg_oi": int(pe.get("changeinOpenInterest", 0) or 0),
                "pe_iv": float(pe.get("impliedVolatility", 0) or 0),
                "pe_ltp": float(pe.get("lastPrice", 0) or 0),
            }
        )
    return rows


def _to_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(str(x).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_fii_dii(raw: list[dict]) -> list[dict]:
    """NSE returns FII & DII cash provisional buy/sell/net for the latest day."""
    out: list[dict] = []
    for rec in raw:
        cat = rec.get("category", "")
        date_str = rec.get("date", "")
        try:
            d = datetime.strptime(date_str, "%d-%b-%Y").date()
        except Exception:  # noqa: BLE001
            try:
                d = datetime.strptime(date_str, "%d-%m-%Y").date()
            except Exception:  # noqa: BLE001
                continue
        category = "FII" if "FII" in cat.upper() or "FPI" in cat.upper() else "DII"
        out.append(
            {
                "date": d,
                "category": category,
                "segment": "Cash",
                "buy": _to_float(rec.get("buyValue")),
                "sell": _to_float(rec.get("sellValue")),
                "net": _to_float(rec.get("netValue")),
            }
        )
    return out


def fetch_fno_participant_oi(max_lookback: int = 8) -> tuple[Any, str | None]:
    """Fetch the most recent participant-wise F&O open interest CSV."""
    hist = fetch_fno_participant_oi_history(days_back=max_lookback, limit=1)
    return hist[0] if hist else (None, None)


def fetch_fno_participant_oi_history(days_back: int = 10, limit: int = 6) -> list[tuple]:
    """Fetch up to `limit` recent trading-day participant-OI files (newest first).

    Returns a list of (date, csv_text). Lets the day-on-day change metric
    populate on first run instead of waiting for tomorrow.
    """
    from datetime import date, timedelta

    hdr = {"User-Agent": HEADERS["User-Agent"], "Accept": "text/csv,*/*"}
    today = date.today()
    out: list[tuple] = []
    for i in range(days_back):
        if len(out) >= limit:
            break
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        ddmmyyyy = d.strftime("%d%m%Y")
        url = f"https://archives.nseindia.com/content/nsccl/fao_participant_oi_{ddmmyyyy}.csv"
        try:
            r = requests.get(url, headers=hdr, timeout=12)
            if r.status_code == 200 and "Participant" in r.text:
                out.append((d, r.text))
        except Exception:  # noqa: BLE001
            continue
    if not out:
        log.warning("F&O participant OI: no files found in last %d days", days_back)
    return out


def parse_fno_participant_oi(d, csv_text: str) -> list[dict]:
    """Parse the participant OI CSV (skips the title line; positional columns)."""
    if not csv_text:
        return []
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    # line 0 = title, line 1 = header, rest = participants
    rows: list[dict] = []
    for ln in lines[2:]:
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) < 15:
            continue
        participant = parts[0]
        if participant.upper() not in ("CLIENT", "DII", "FII", "PRO", "TOTAL"):
            continue
        nums = [_to_int(x) for x in parts[1:15]]
        rows.append({
            "date": d, "participant": participant,
            "fut_idx_long": nums[0], "fut_idx_short": nums[1],
            "fut_stk_long": nums[2], "fut_stk_short": nums[3],
            "opt_idx_call_long": nums[4], "opt_idx_put_long": nums[5],
            "opt_idx_call_short": nums[6], "opt_idx_put_short": nums[7],
            "opt_stk_call_long": nums[8], "opt_stk_put_long": nums[9],
            "opt_stk_call_short": nums[10], "opt_stk_put_short": nums[11],
            "total_long": nums[12], "total_short": nums[13],
        })
    return rows


def _to_int(x: Any) -> int:
    try:
        return int(float(str(x).replace(",", "").strip() or 0))
    except (ValueError, TypeError):
        return 0


def parse_large_deals(snapshot: dict) -> list[dict]:
    """Parse the /snapshot-capital-market-largedeal payload.

    Contains BULK_DEALS_DATA, BLOCK_DEALS_DATA, SHORT_DEALS_DATA arrays.
    """
    if not snapshot:
        return []
    as_on = snapshot.get("as_on_date", "")
    try:
        default_date = datetime.strptime(as_on[:11].strip(), "%d-%b-%Y").date()
    except Exception:  # noqa: BLE001
        default_date = datetime.now().date()

    blocks = {
        "BULK_DEALS_DATA": "bulk",
        "BLOCK_DEALS_DATA": "block",
        "SHORT_DEALS_DATA": "short",
    }
    out: list[dict] = []
    for key, deal_type in blocks.items():
        for rec in snapshot.get(key, []) or []:
            sym = (rec.get("symbol") or rec.get("name") or "").strip()
            client = (rec.get("clientName") or "").strip()[:120]
            side_raw = (rec.get("buySell") or "").upper()
            side = "BUY" if side_raw.startswith("B") else "SELL"
            qty = _to_float(rec.get("qty") or rec.get("quantity")) or 0
            price = _to_float(rec.get("watp") or rec.get("tradePrice") or rec.get("price")) or 0
            if not sym:
                continue
            out.append(
                {
                    "date": default_date,
                    "symbol": sym,
                    "client": client,
                    "side": side,
                    "qty": int(qty),
                    "price": price,
                    "deal_type": deal_type,
                }
            )
    return out
