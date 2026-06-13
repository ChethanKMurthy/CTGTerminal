"""Macro data: FRED (free key) for India + global series, World Bank fallback.

FRED carries India CPI, policy rate, IIP plus global drivers (Brent, USD/INR,
US VIX, US 10Y). World Bank is a keyless fallback for India GDP/CPI annual.
"""
from __future__ import annotations

from datetime import datetime

import requests

from ..config import get_settings
from ..logging_conf import get_logger

log = get_logger("data.macro")

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
WB_URL = "https://api.worldbank.org/v2/country/IND/indicator/{ind}?format=json&per_page=60"


def fetch_fred() -> list[dict]:
    s = get_settings()
    if not s.has_fred:
        log.info("FRED key absent; skipping macro (set FRED_API_KEY to enable)")
        return []
    series_map: dict[str, str] = s.get("data_sources", "fred_series", default={}) or {}
    rows: list[dict] = []
    for series_id, label in series_map.items():
        try:
            r = requests.get(
                FRED_URL,
                params={
                    "series_id": series_id,
                    "api_key": s.fred_api_key,
                    "file_type": "json",
                    "observation_start": "2015-01-01",
                },
                timeout=15,
            )
            r.raise_for_status()
            for obs in r.json().get("observations", []):
                val = obs.get("value")
                if val in (".", "", None):
                    continue
                try:
                    d = datetime.strptime(obs["date"], "%Y-%m-%d").date()
                    rows.append(
                        {"series": series_id, "label": label, "date": d, "value": float(val)}
                    )
                except (ValueError, TypeError):
                    continue
        except Exception as exc:  # noqa: BLE001
            log.warning("FRED %s failed: %s", series_id, exc)
    log.info("Fetched %d FRED observations across %d series", len(rows), len(series_map))
    return rows


# World Bank keyless fallback (annual) ---------------------------------
WB_INDICATORS = {
    "NY.GDP.MKTP.KD.ZG": "India GDP growth",
    "FP.CPI.TOTL.ZG": "India CPI inflation",
}


def fetch_worldbank() -> list[dict]:
    rows: list[dict] = []
    for ind, label in WB_INDICATORS.items():
        try:
            r = requests.get(WB_URL.format(ind=ind), timeout=15)
            r.raise_for_status()
            payload = r.json()
            if len(payload) < 2:
                continue
            for obs in payload[1]:
                val = obs.get("value")
                if val is None:
                    continue
                try:
                    d = datetime(int(obs["date"]), 12, 31).date()
                    rows.append({"series": ind, "label": label, "date": d, "value": float(val)})
                except (ValueError, TypeError):
                    continue
        except Exception as exc:  # noqa: BLE001
            log.warning("WorldBank %s failed: %s", ind, exc)
    log.info("Fetched %d World Bank observations", len(rows))
    return rows
