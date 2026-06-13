"""Storage: DuckDB for time-series/tabular analytics, SQLite for app state.

DuckDB holds the raw observational data (prices, flows, option chains, macro).
SQLite holds mutable application state (signals, agent outputs, paper book,
alert log) where row-level upserts and concurrent reads matter.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

import duckdb

from ..config import DATA_DIR
from ..logging_conf import get_logger

log = get_logger("storage.db")

DUCK_PATH = DATA_DIR / "warehouse.duckdb"
SQLITE_PATH = DATA_DIR / "state.sqlite"

_duck_lock = threading.Lock()


# ---------------------------------------------------------------------
# DuckDB — observational warehouse
# ---------------------------------------------------------------------
def duck() -> duckdb.DuckDBPyConnection:
    """One process-wide DuckDB connection (guarded by a lock for writes)."""
    if not hasattr(duck, "_con"):
        duck._con = duckdb.connect(str(DUCK_PATH))  # type: ignore[attr-defined]
        _init_duck(duck._con)  # type: ignore[attr-defined]
    return duck._con  # type: ignore[attr-defined]


def _init_duck(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS prices (
            symbol VARCHAR, ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, volume BIGINT, interval VARCHAR,
            PRIMARY KEY (symbol, ts, interval)
        );
        CREATE TABLE IF NOT EXISTS index_levels (
            name VARCHAR, ts TIMESTAMP, last DOUBLE, change_pct DOUBLE,
            PRIMARY KEY (name, ts)
        );
        CREATE TABLE IF NOT EXISTS fii_dii (
            date DATE, category VARCHAR, segment VARCHAR,
            buy DOUBLE, sell DOUBLE, net DOUBLE,
            PRIMARY KEY (date, category, segment)
        );
        CREATE TABLE IF NOT EXISTS bulk_block_deals (
            date DATE, symbol VARCHAR, client VARCHAR, side VARCHAR,
            qty BIGINT, price DOUBLE, deal_type VARCHAR,
            PRIMARY KEY (date, symbol, client, side, deal_type, qty)
        );
        CREATE TABLE IF NOT EXISTS option_chain (
            underlying VARCHAR, ts TIMESTAMP, expiry VARCHAR, strike DOUBLE,
            spot DOUBLE, ce_oi BIGINT, ce_chg_oi BIGINT, ce_iv DOUBLE, ce_ltp DOUBLE,
            pe_oi BIGINT, pe_chg_oi BIGINT, pe_iv DOUBLE, pe_ltp DOUBLE,
            PRIMARY KEY (underlying, ts, expiry, strike)
        );
        CREATE TABLE IF NOT EXISTS macro (
            series VARCHAR, label VARCHAR, date DATE, value DOUBLE,
            PRIMARY KEY (series, date)
        );
        CREATE TABLE IF NOT EXISTS fundamentals (
            symbol VARCHAR, ts TIMESTAMP, pe DOUBLE, pb DOUBLE, roe DOUBLE,
            market_cap DOUBLE, dividend_yield DOUBLE, eps DOUBLE,
            revenue DOUBLE, profit_margin DOUBLE, debt_to_equity DOUBLE,
            PRIMARY KEY (symbol, ts)
        );
        CREATE TABLE IF NOT EXISTS announcements (
            ts TIMESTAMP, symbol VARCHAR, subject VARCHAR, detail VARCHAR,
            url VARCHAR, PRIMARY KEY (ts, symbol, subject)
        );
        CREATE TABLE IF NOT EXISTS news (
            ts TIMESTAMP, source VARCHAR, title VARCHAR, summary VARCHAR,
            url VARCHAR, PRIMARY KEY (url)
        );
        CREATE TABLE IF NOT EXISTS social (
            ts TIMESTAMP, sub VARCHAR, post_id VARCHAR, title VARCHAR,
            body VARCHAR, score INTEGER, url VARCHAR, PRIMARY KEY (post_id)
        );
        -- Participant-wise F&O open interest (FII/DII/Pro/Client positioning)
        CREATE TABLE IF NOT EXISTS fno_participant_oi (
            date DATE, participant VARCHAR,
            fut_idx_long BIGINT, fut_idx_short BIGINT,
            fut_stk_long BIGINT, fut_stk_short BIGINT,
            opt_idx_call_long BIGINT, opt_idx_put_long BIGINT,
            opt_idx_call_short BIGINT, opt_idx_put_short BIGINT,
            opt_stk_call_long BIGINT, opt_stk_put_long BIGINT,
            opt_stk_call_short BIGINT, opt_stk_put_short BIGINT,
            total_long BIGINT, total_short BIGINT,
            PRIMARY KEY (date, participant)
        );
        -- Karpathy eval loop: snapshot every candidate's factors + entry price
        -- each cycle, so forward returns can score factor predictiveness (IC).
        CREATE TABLE IF NOT EXISTS alpha_snapshots (
            run_ts TIMESTAMP, symbol VARCHAR, entry_close DOUBLE, score DOUBLE,
            direction VARCHAR, factors VARCHAR, PRIMARY KEY (run_ts, symbol)
        );
        """
    )


def duck_upsert(table: str, rows: list[dict[str, Any]]) -> int:
    """Insert-or-replace rows into a DuckDB table via a temp register."""
    if not rows:
        return 0
    import pandas as pd

    df = pd.DataFrame(rows)
    with _duck_lock:
        con = duck()
        con.register("_staging", df)
        cols = ", ".join(df.columns)
        # DuckDB: INSERT OR REPLACE honours the PRIMARY KEY
        con.execute(f"INSERT OR REPLACE INTO {table} ({cols}) SELECT {cols} FROM _staging")
        con.unregister("_staging")
    return len(df)


def duck_df(query: str, params: list | None = None):
    import pandas as pd  # noqa: F401

    with _duck_lock:
        con = duck()
        return con.execute(query, params or []).df()


# ---------------------------------------------------------------------
# SQLite — application state
# ---------------------------------------------------------------------
def sqlite() -> sqlite3.Connection:
    con = sqlite3.connect(SQLITE_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    return con


def init_sqlite() -> None:
    con = sqlite()
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_output (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, agent TEXT, scope TEXT, payload TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_agent_output_agent ON agent_output(agent, ts);

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, symbol TEXT, direction TEXT, conviction REAL,
                expected_return REAL, tail_risk REAL, horizon TEXT,
                thesis TEXT, drivers TEXT, scenarios TEXT, status TEXT DEFAULT 'open'
            );
            CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts);

            CREATE TABLE IF NOT EXISTS regime (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, label TEXT, vol_regime TEXT, trend TEXT,
                risk_score REAL, detail TEXT
            );

            CREATE TABLE IF NOT EXISTS paper_positions (
                symbol TEXT PRIMARY KEY, qty REAL, avg_price REAL,
                opened_ts TEXT, last_price REAL, thesis TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, symbol TEXT, side TEXT, qty REAL, price REAL, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_equity (
                ts TEXT PRIMARY KEY, cash REAL, positions_value REAL, equity REAL
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, kind TEXT, dedup_key TEXT, channel TEXT, message TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_alerts_dedup ON alerts(dedup_key);

            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            """
        )
        con.commit()
    finally:
        con.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def kv_set(k: str, v: Any) -> None:
    con = sqlite()
    try:
        con.execute(
            "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json.dumps(v)),
        )
        con.commit()
    finally:
        con.close()


def kv_get(k: str, default: Any = None) -> Any:
    con = sqlite()
    try:
        row = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default
    finally:
        con.close()


def save_agent_output(agent: str, scope: str, payload: dict) -> None:
    con = sqlite()
    try:
        con.execute(
            "INSERT INTO agent_output(ts,agent,scope,payload) VALUES(?,?,?,?)",
            (now_iso(), agent, scope, json.dumps(payload, default=str)),
        )
        con.commit()
    finally:
        con.close()


def latest_agent_output(agent: str) -> dict | None:
    con = sqlite()
    try:
        row = con.execute(
            "SELECT payload FROM agent_output WHERE agent=? ORDER BY ts DESC LIMIT 1",
            (agent,),
        ).fetchone()
        return json.loads(row["payload"]) if row else None
    finally:
        con.close()


def agent_output_history(agent: str, limit: int = 60) -> list[dict]:
    """Recent outputs for an agent (newest first) as {ts, payload} records."""
    con = sqlite()
    try:
        rows = con.execute(
            "SELECT ts, payload FROM agent_output WHERE agent=? ORDER BY ts DESC LIMIT ?",
            (agent, limit),
        ).fetchall()
        out = []
        for r in rows:
            try:
                out.append({"ts": r["ts"], "payload": json.loads(r["payload"])})
            except Exception:  # noqa: BLE001
                continue
        return out
    finally:
        con.close()


def init_all() -> None:
    duck()  # triggers _init_duck
    init_sqlite()
    log.info("Storage initialised: %s , %s", DUCK_PATH.name, SQLITE_PATH.name)
