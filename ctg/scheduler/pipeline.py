"""Composite pipelines: the repeatable 'cycles' the scheduler fires.

Each is safe to call manually (e.g. on first boot) and logs a summary.
"""
from __future__ import annotations

import ctg.agents.market_agents  # noqa: F401  (registers agents)
from ..agents.base import Context, all_agents
from ..alerts import dispatcher
from ..data import collectors
from ..data.universe import load_universe
from ..engine.knowledge_graph import build_graph
from ..engine.quant import flow_metrics
from ..engine.signals import run_cio
from ..logging_conf import get_logger
from ..portfolio.paper import rebalance
from ..storage.db import kv_get, kv_set, latest_agent_output

log = get_logger("pipeline")


def run_agents(ctx: Context | None = None) -> dict:
    ctx = ctx or Context()
    results = {}
    for agent in all_agents():
        out = agent.run(ctx)
        results[agent.name] = "error" if "error" in out else "ok"
    return results


def cio_and_alerts(do_paper: bool = True) -> dict:
    prev_regime = kv_get("last_regime_label")
    regime = latest_agent_output("regime") or {}

    cio = run_cio()
    signals = cio.get("signals", [])

    # alerts
    dispatcher.alert_signals(signals)
    dispatcher.alert_regime_change(regime, prev_regime)
    dispatcher.alert_unusual_flow(flow_metrics())
    if regime.get("label"):
        kv_set("last_regime_label", regime.get("label"))

    out = {"signals": len(signals), "regime": cio.get("regime")}
    if do_paper:
        rb = rebalance(signals)
        out["paper_trades"] = rb.get("trades")
        out["paper_equity"] = rb.get("equity")
    return out


# --- scheduler entry points -------------------------------------------
def intraday_cycle() -> dict:
    """Light cycle during market hours: refresh fast data + flow/options agents."""
    log.info("▶ intraday cycle")
    ing = collectors.ingest_intraday_bundle()
    ctx = Context()
    # only the fast, intraday-relevant agents
    from ..agents.market_agents import FlowAgent, OptionsAgent, RegimeAgent, SentimentAgent
    for cls in (FlowAgent, OptionsAgent, SentimentAgent, RegimeAgent):
        cls().run(ctx)
    out = cio_and_alerts(do_paper=False)
    # live option-trade ideas + alert on high-fit setups
    try:
        from ..engine.option_strategies import all_option_trades
        trades = all_option_trades()
        dispatcher.alert_option_trades(trades)
        out["option_trades"] = sum(len(t.get("suggestions", [])) for t in trades.values())
    except Exception as exc:  # noqa: BLE001
        log.warning("option trades failed: %s", exc)
    log.info("✓ intraday cycle: %s | %s", ing, out)
    return {**ing, **out}


def eod_cycle() -> dict:
    """Heavy end-of-day cycle: full ingest, graph rebuild, all agents, CIO, paper."""
    log.info("▶ EOD cycle")
    u = load_universe()
    ing = collectors.ingest_eod_bundle(u)
    build_graph(u, with_llm=True)
    run_agents(Context(universe=u))
    out = cio_and_alerts(do_paper=True)
    # learning loop: score prior snapshots' forward returns -> adaptive weights
    try:
        from ..engine.evals import evaluate
        ev = evaluate(horizon_days=5, learn=True)
        out["eval"] = {k: ev.get(k) for k in ("status", "composite_ic", "hit_rate", "n_evaluated")}
    except Exception as exc:  # noqa: BLE001
        log.warning("eval failed: %s", exc)
    log.info("✓ EOD cycle: %s | %s", ing, out)
    return {**ing, **out}


def boot_cycle() -> dict:
    """One-shot warmup on startup so the dashboard has data immediately."""
    log.info("▶ boot warmup")
    u = load_universe()
    collectors.ingest_indices()
    collectors.ingest_option_chains()
    collectors.ingest_fii_dii()
    collectors.ingest_fno_flows()
    collectors.ingest_large_deals()
    collectors.ingest_announcements()
    collectors.ingest_news()
    collectors.ingest_prices_intraday(u)
    # daily prices + fundamentals only if we have none yet (expensive)
    from ..storage.db import duck_df
    if int(duck_df("SELECT count(*) c FROM prices WHERE interval='1d'").c.iloc[0]) < 1000:
        collectors.ingest_prices_daily(u)
        collectors.ingest_fundamentals(u)
    collectors.ingest_macro()
    collectors.ingest_social()
    build_graph(u, with_llm=True)
    run_agents(Context(universe=u))
    out = cio_and_alerts(do_paper=True)
    log.info("✓ boot warmup complete: %s", out)
    return out
