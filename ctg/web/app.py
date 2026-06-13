"""FastAPI dashboard + JSON API for the CTG system."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import get_settings
from ..engine.quant import flow_metrics, fno_flow_metrics, options_metrics
from ..engine.signals import open_signals
from ..portfolio.paper import equity_curve, mark_to_market
from ..storage.db import duck_df, kv_get, latest_agent_output
from ..storage.graph_store import graph_to_dict, load_graph

HERE = Path(__file__).resolve().parent
app = FastAPI(title="CTG — Autonomous Alpha Discovery (India)")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (HERE / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> JSONResponse:
    s = get_settings()
    heartbeats = {}
    for name in ("indices", "option_chain", "fii_dii", "deals", "announcements",
                 "news", "social", "prices_intraday", "prices_daily", "fundamentals", "macro"):
        heartbeats[name] = kv_get(f"ingest:{name}")
    counts = {}
    for t in ("prices", "option_chain", "fii_dii", "bulk_block_deals", "news", "macro", "fundamentals"):
        try:
            counts[t] = int(duck_df(f"SELECT count(*) c FROM {t}").c.iloc[0])
        except Exception:  # noqa: BLE001
            counts[t] = 0
    return JSONResponse({
        "capabilities": s.capability_report(),
        "ingest_heartbeats": heartbeats,
        "row_counts": counts,
    })


@app.get("/api/signals")
def signals() -> JSONResponse:
    return JSONResponse({"signals": open_signals()})


@app.get("/api/regime")
def regime() -> JSONResponse:
    return JSONResponse(latest_agent_output("regime") or {})


@app.get("/api/flow")
def flow() -> JSONResponse:
    out = latest_agent_output("flow") or {}
    out["metrics"] = flow_metrics()
    out["fno"] = fno_flow_metrics()
    return JSONResponse(out)


@app.get("/api/options")
def options() -> JSONResponse:
    return JSONResponse({
        "NIFTY": options_metrics("NIFTY"),
        "BANKNIFTY": options_metrics("BANKNIFTY"),
    })


@app.get("/api/agents")
def agents() -> JSONResponse:
    names = ["flow", "options", "macro", "sentiment", "earnings", "valuation",
             "regime", "causal", "cio"]
    return JSONResponse({n: latest_agent_output(n) for n in names})


@app.get("/api/graph")
def graph() -> JSONResponse:
    return JSONResponse(graph_to_dict(load_graph()))


@app.get("/api/portfolio")
def portfolio() -> JSONResponse:
    return JSONResponse({"book": mark_to_market(), "equity_curve": equity_curve()})


@app.get("/api/option_trades")
def option_trades() -> JSONResponse:
    from ..engine.option_strategies import all_option_trades
    return JSONResponse(all_option_trades())


@app.get("/api/evals")
def evals() -> JSONResponse:
    from ..engine.evals import performance_summary
    return JSONResponse(performance_summary())


@app.get("/api/risk")
def risk() -> JSONResponse:
    from ..engine.risk import risk_report
    return JSONResponse(risk_report())


@app.get("/api/news")
def news() -> JSONResponse:
    df = duck_df("SELECT ts, source, title, url FROM news ORDER BY ts DESC LIMIT 40")
    if df.empty:
        return JSONResponse({"news": []})
    df["ts"] = df["ts"].astype(str)
    return JSONResponse({"news": df.to_dict(orient="records")})
