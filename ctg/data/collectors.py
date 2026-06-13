"""Ingestion orchestrator: pull from each real source -> warehouse (DuckDB).

Each ingest_* function is idempotent (INSERT OR REPLACE on the table PK) and
returns a short summary dict for logging / dashboard health.
"""
from __future__ import annotations

from datetime import datetime

from ..logging_conf import get_logger
from ..storage.db import duck_upsert, kv_set, now_iso
from . import macro as macro_src
from . import news as news_src
from . import prices as price_src
from . import social as social_src
from .nse import (fetch_fno_participant_oi_history, nse, parse_fii_dii,
                  parse_fno_participant_oi, parse_large_deals, parse_option_chain)
from .universe import Universe, load_universe

log = get_logger("data.collectors")


def _heartbeat(name: str, count: int) -> None:
    kv_set(f"ingest:{name}", {"ts": now_iso(), "count": count})


# --- NSE ----------------------------------------------------------------
def ingest_indices() -> dict:
    c = nse()
    idx = c.all_indices()
    ts = datetime.now()
    rows = []
    for d in idx:
        name = d.get("index") or d.get("indexSymbol")
        last = d.get("last")
        chg = d.get("percentChange")
        if name and last is not None:
            rows.append(
                {"name": name, "ts": ts, "last": float(last),
                 "change_pct": float(chg) if chg is not None else None}
            )
    n = duck_upsert("index_levels", rows)
    _heartbeat("indices", n)
    return {"indices": n}


def ingest_option_chains(universe: Universe | None = None) -> dict:
    cfg_syms = ["NIFTY", "BANKNIFTY"]
    c = nse()
    ts = datetime.now()
    total = 0
    for sym in cfg_syms:
        raw = c.option_chain(sym)
        rows = parse_option_chain(sym, raw, ts) if raw else []
        total += duck_upsert("option_chain", rows)
    _heartbeat("option_chain", total)
    return {"option_chain_rows": total}


def ingest_fii_dii() -> dict:
    rows = parse_fii_dii(nse().fii_dii())
    n = duck_upsert("fii_dii", rows)
    _heartbeat("fii_dii", n)
    return {"fii_dii": n}


def ingest_large_deals() -> dict:
    rows = parse_large_deals(nse().large_deals())
    n = duck_upsert("bulk_block_deals", rows)
    _heartbeat("deals", n)
    return {"deals": n}


def ingest_fno_flows() -> dict:
    rows = []
    for d, text in fetch_fno_participant_oi_history(limit=6):
        rows.extend(parse_fno_participant_oi(d, text))
    n = duck_upsert("fno_participant_oi", rows)
    _heartbeat("fno_flows", n)
    return {"fno_flows": n}


def ingest_announcements() -> dict:
    raw = nse().corporate_announcements()
    rows = []
    for rec in raw[:200]:
        sym = (rec.get("symbol") or "").strip()
        subject = (rec.get("desc") or rec.get("subject") or "").strip()[:200]
        detail = (rec.get("attchmntText") or rec.get("smIndustry") or "").strip()[:500]
        dt_str = rec.get("an_dt") or rec.get("sort_date") or ""
        try:
            ts = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            ts = datetime.now()
        url = rec.get("attchmntFile") or ""
        if sym and subject:
            rows.append({"ts": ts, "symbol": sym, "subject": subject,
                         "detail": detail, "url": url})
    n = duck_upsert("announcements", rows)
    _heartbeat("announcements", n)
    return {"announcements": n}


# --- Prices / fundamentals ---------------------------------------------
def ingest_prices_intraday(universe: Universe | None = None) -> dict:
    u = universe or load_universe()
    rows = price_src.fetch_ohlcv(u, period="5d", interval="15m")
    n = duck_upsert("prices", rows)
    _heartbeat("prices_intraday", n)
    return {"prices_intraday": n}


def ingest_prices_daily(universe: Universe | None = None) -> dict:
    u = universe or load_universe()
    rows = price_src.fetch_daily(u, period="2y")
    n = duck_upsert("prices", rows)
    _heartbeat("prices_daily", n)
    return {"prices_daily": n}


def ingest_fundamentals(universe: Universe | None = None) -> dict:
    u = universe or load_universe()
    rows = price_src.fetch_fundamentals(u)
    n = duck_upsert("fundamentals", rows)
    _heartbeat("fundamentals", n)
    return {"fundamentals": n}


# --- Macro / news / social ---------------------------------------------
def ingest_macro() -> dict:
    rows = macro_src.fetch_fred() + macro_src.fetch_worldbank()
    n = duck_upsert("macro", rows)
    _heartbeat("macro", n)
    return {"macro": n}


def ingest_news() -> dict:
    rows = news_src.fetch_news()
    n = duck_upsert("news", rows)
    _heartbeat("news", n)
    return {"news": n}


def ingest_social() -> dict:
    rows = social_src.fetch_reddit()
    n = duck_upsert("social", rows)
    _heartbeat("social", n)
    return {"social": n}


# --- Bundles -----------------------------------------------------------
def ingest_intraday_bundle(universe: Universe | None = None) -> dict:
    out: dict = {}
    for fn in (ingest_indices, ingest_option_chains, ingest_prices_intraday):
        try:
            out.update(fn())
        except Exception as exc:  # noqa: BLE001
            log.exception("intraday ingest %s failed: %s", fn.__name__, exc)
    return out


def ingest_eod_bundle(universe: Universe | None = None) -> dict:
    u = universe or load_universe()
    out: dict = {}
    for fn in (
        lambda: ingest_prices_daily(u),
        ingest_fii_dii,
        ingest_fno_flows,
        ingest_large_deals,
        lambda: ingest_fundamentals(u),
        ingest_announcements,
    ):
        try:
            out.update(fn())
        except Exception as exc:  # noqa: BLE001
            log.exception("eod ingest failed: %s", exc)
    return out
