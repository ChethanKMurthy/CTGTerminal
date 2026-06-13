"""The tradable universe (Nifty 50) + sector/theme/macro entity map.

Constituents are fetched live from NSE when possible, with a curated
fallback baked in so the system is always functional offline.
The sector map and macro/commodity nodes seed the knowledge graph.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

import requests

from ..logging_conf import get_logger

log = get_logger("data.universe")

# --- Curated Nifty 50 fallback (symbol -> (company, sector)) ----------
# Yahoo ticker = symbol + ".NS"  (NSE)
NIFTY50_FALLBACK: dict[str, tuple[str, str]] = {
    "RELIANCE": ("Reliance Industries", "Energy"),
    "TCS": ("Tata Consultancy Services", "IT"),
    "HDFCBANK": ("HDFC Bank", "Banking"),
    "ICICIBANK": ("ICICI Bank", "Banking"),
    "INFY": ("Infosys", "IT"),
    "BHARTIARTL": ("Bharti Airtel", "Telecom"),
    "ITC": ("ITC", "FMCG"),
    "SBIN": ("State Bank of India", "Banking"),
    "LT": ("Larsen & Toubro", "Capital Goods"),
    "HINDUNILVR": ("Hindustan Unilever", "FMCG"),
    "AXISBANK": ("Axis Bank", "Banking"),
    "KOTAKBANK": ("Kotak Mahindra Bank", "Banking"),
    "BAJFINANCE": ("Bajaj Finance", "NBFC"),
    "MARUTI": ("Maruti Suzuki", "Auto"),
    "SUNPHARMA": ("Sun Pharmaceutical", "Pharma"),
    "M&M": ("Mahindra & Mahindra", "Auto"),
    "HCLTECH": ("HCL Technologies", "IT"),
    "NTPC": ("NTPC", "Power"),
    "TATAMOTORS": ("Tata Motors", "Auto"),
    "TITAN": ("Titan Company", "Consumer Durables"),
    "POWERGRID": ("Power Grid Corp", "Power"),
    "ULTRACEMCO": ("UltraTech Cement", "Cement"),
    "ASIANPAINT": ("Asian Paints", "Consumer Durables"),
    "ADANIENT": ("Adani Enterprises", "Diversified"),
    "WIPRO": ("Wipro", "IT"),
    "ONGC": ("Oil & Natural Gas Corp", "Energy"),
    "NESTLEIND": ("Nestle India", "FMCG"),
    "JSWSTEEL": ("JSW Steel", "Metals"),
    "TATASTEEL": ("Tata Steel", "Metals"),
    "COALINDIA": ("Coal India", "Energy"),
    "BAJAJFINSV": ("Bajaj Finserv", "NBFC"),
    "ADANIPORTS": ("Adani Ports & SEZ", "Infrastructure"),
    "HDFCLIFE": ("HDFC Life Insurance", "Insurance"),
    "SBILIFE": ("SBI Life Insurance", "Insurance"),
    "GRASIM": ("Grasim Industries", "Cement"),
    "TECHM": ("Tech Mahindra", "IT"),
    "HINDALCO": ("Hindalco Industries", "Metals"),
    "DRREDDY": ("Dr Reddy's Laboratories", "Pharma"),
    "CIPLA": ("Cipla", "Pharma"),
    "EICHERMOT": ("Eicher Motors", "Auto"),
    "BAJAJ-AUTO": ("Bajaj Auto", "Auto"),
    "BRITANNIA": ("Britannia Industries", "FMCG"),
    "APOLLOHOSP": ("Apollo Hospitals", "Healthcare"),
    "TATACONSUM": ("Tata Consumer Products", "FMCG"),
    "HEROMOTOCO": ("Hero MotoCorp", "Auto"),
    "INDUSINDBK": ("IndusInd Bank", "Banking"),
    "SHRIRAMFIN": ("Shriram Finance", "NBFC"),
    "BPCL": ("Bharat Petroleum", "Energy"),
    "TRENT": ("Trent", "Retail"),
    "JIOFIN": ("Jio Financial Services", "NBFC"),
}

# Macro / commodity / FX nodes for the knowledge graph (non-tradable here)
MACRO_NODES = [
    "India Repo Rate", "India CPI", "India IIP", "India GDP",
    "India 10Y Yield", "USD/INR", "Brent Crude", "Gold",
    "US 10Y Yield", "US VIX", "FII Flows", "DII Flows", "Global Risk Sentiment",
]

# Normalise NSE's broad "Industry" labels to our canonical sector keys so the
# sector->macro links below resolve for any new index entrants.
NSE_INDUSTRY_TO_SECTOR: dict[str, str] = {
    "Financial Services": "Banking",
    "Information Technology": "IT",
    "Oil Gas & Consumable Fuels": "Energy",
    "Automobile and Auto Components": "Auto",
    "Fast Moving Consumer Goods": "FMCG",
    "Metals & Mining": "Metals",
    "Construction Materials": "Cement",
    "Consumer Durables": "Consumer Durables",
    "Healthcare": "Pharma",
    "Telecommunication": "Telecom",
    "Power": "Power",
    "Construction": "Capital Goods",
    "Capital Goods": "Capital Goods",
    "Services": "Services",
    "Consumer Services": "Retail",
}

# Sector -> macro factor sensitivities (sign: +1 helps, -1 hurts) seed edges
SECTOR_MACRO_LINKS: dict[str, list[tuple[str, int]]] = {
    "Banking": [("India Repo Rate", +1), ("India 10Y Yield", +1), ("DII Flows", +1)],
    "NBFC": [("India Repo Rate", -1), ("India 10Y Yield", -1)],
    "IT": [("USD/INR", +1), ("US 10Y Yield", -1), ("Global Risk Sentiment", +1)],
    "Auto": [("India CPI", -1), ("Brent Crude", -1)],
    "Energy": [("Brent Crude", +1)],
    "Metals": [("Global Risk Sentiment", +1), ("USD/INR", -1)],
    "FMCG": [("India CPI", -1)],
    "Pharma": [("USD/INR", +1)],
    "Power": [("Brent Crude", -1)],
    "Cement": [("India GDP", +1)],
    "Capital Goods": [("India GDP", +1)],
    "Telecom": [("India CPI", -1)],
}


@dataclass
class Universe:
    constituents: dict[str, tuple[str, str]] = field(default_factory=dict)
    source: str = "fallback"

    @property
    def symbols(self) -> list[str]:
        return list(self.constituents.keys())

    def yahoo(self, symbol: str) -> str:
        # Yahoo uses different separators for a few names
        return symbol.replace("&", "%26") + ".NS"

    def company(self, symbol: str) -> str:
        return self.constituents.get(symbol, (symbol, "Unknown"))[0]

    def sector(self, symbol: str) -> str:
        return self.constituents.get(symbol, (symbol, "Unknown"))[1]

    def sectors(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for sym, (_, sec) in self.constituents.items():
            out.setdefault(sec, []).append(sym)
        return out


_NIFTY50_CSV = "https://archives.nseindia.com/content/indices/ind_nifty50list.csv"


def _fetch_live_nifty50() -> dict[str, tuple[str, str]] | None:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "text/csv,*/*",
        }
        r = requests.get(_NIFTY50_CSV, headers=headers, timeout=15)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        live: dict[str, tuple[str, str]] = {}
        for row in reader:
            sym = (row.get("Symbol") or "").strip()
            name = (row.get("Company Name") or sym).strip()
            sector = (row.get("Industry") or "Unknown").strip()
            if sym:
                live[sym] = (name, sector)
        if len(live) >= 40:
            log.info("Fetched %d live Nifty 50 constituents from NSE", len(live))
            return live
    except Exception as exc:  # noqa: BLE001
        log.warning("Live Nifty 50 fetch failed (%s); using curated fallback", exc)
    return None


_CACHE: dict = {"u": None, "ts": 0.0}
_CACHE_TTL = 6 * 3600  # constituents change rarely; refresh a few times a day


def load_universe(prefer_live: bool = True) -> Universe:
    # process-level cache so API calls / agents don't re-hit NSE every request
    import time as _t
    if _CACHE["u"] is not None and (_t.time() - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["u"]
    u = _load_universe_uncached(prefer_live)
    _CACHE["u"], _CACHE["ts"] = u, _t.time()
    return u


def _load_universe_uncached(prefer_live: bool = True) -> Universe:
    if prefer_live:
        live = _fetch_live_nifty50()
        if live:
            # Use live membership + names, but canonicalise sectors:
            #  - curated fallback sector wins for known names (matches macro links)
            #  - otherwise map NSE's broad industry label to a canonical sector
            merged: dict[str, tuple[str, str]] = {}
            for sym, (name, nse_sector) in live.items():
                if sym in NIFTY50_FALLBACK:
                    sector = NIFTY50_FALLBACK[sym][1]
                else:
                    sector = NSE_INDUSTRY_TO_SECTOR.get(nse_sector, nse_sector or "Unknown")
                merged[sym] = (name, sector)
            return Universe(constituents=merged, source="nse_live")
    return Universe(constituents=dict(NIFTY50_FALLBACK), source="fallback")
