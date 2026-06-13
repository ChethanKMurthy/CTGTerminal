#!/usr/bin/env python3
"""CTG — Autonomous Alpha Discovery System (India). 24/7 local entrypoint.

Usage:
  python run.py              # boot warmup + scheduler + dashboard (the 24/7 mode)
  python run.py --no-warmup  # skip the initial data pull (use existing warehouse)
  python run.py --once       # run one full EOD cycle and exit (cron-style)
  python run.py --web-only    # dashboard only, no scheduler
"""
from __future__ import annotations

import argparse
import threading
import time

import uvicorn

from ctg.config import get_settings
from ctg.logging_conf import get_logger
from ctg.storage.db import init_all

log = get_logger("run")


def _warmup_async() -> None:
    from ctg.scheduler.pipeline import boot_cycle
    try:
        boot_cycle()
    except Exception as exc:  # noqa: BLE001
        log.exception("boot warmup failed: %s", exc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--web-only", action="store_true")
    args = ap.parse_args()

    s = get_settings()
    init_all()
    caps = s.capability_report()
    log.info("=" * 64)
    log.info("CTG starting. Capabilities: %s", {k: v for k, v in caps.items() if v} or "data-only (no keys set)")
    if not caps["llm"]:
        log.warning("No LLM key set — agents run in rule-based mode. Add GEMINI_API_KEY or GROQ_API_KEY in .env for narrative reasoning.")
    log.info("=" * 64)

    if args.once:
        from ctg.scheduler.pipeline import eod_cycle
        eod_cycle()
        return

    if not args.web_only:
        from ctg.scheduler.jobs import build_scheduler
        sched = build_scheduler()
        sched.start()
        log.info("Scheduler started.")
        if not args.no_warmup:
            threading.Thread(target=_warmup_async, daemon=True).start()
            log.info("Boot warmup running in background…")

    log.info("Dashboard → http://%s:%d", s.web_host, s.web_port)
    uvicorn.run("ctg.web.app:app", host=s.web_host, port=s.web_port, log_level="warning")


if __name__ == "__main__":
    main()
