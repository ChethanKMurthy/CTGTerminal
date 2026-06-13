"""24/7 scheduler — APScheduler with IST market-hours awareness.

Intraday jobs only fire Mon-Fri within the NSE session and skip NSE holidays
(checked live via the market-status endpoint). Heavy jobs run after close.
"""
from __future__ import annotations

from datetime import datetime, time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import pytz

from ..config import get_settings
from ..data import collectors
from ..data.nse import nse
from ..logging_conf import get_logger
from . import pipeline

log = get_logger("scheduler")


def _ist_now() -> datetime:
    return datetime.now(pytz.timezone(get_settings().timezone))


def _within_session() -> bool:
    s = get_settings()
    now = _ist_now()
    if now.weekday() not in set(s.get("market", "weekdays", default=[0, 1, 2, 3, 4])):
        return False
    o = _parse_hm(s.get("market", "open", default="09:15"))
    c = _parse_hm(s.get("market", "close", default="15:30"))
    return o <= now.time() <= c


def _parse_hm(hm: str) -> time:
    h, m = hm.split(":")
    return time(int(h), int(m))


def _market_open_live() -> bool:
    """Combine clock check with NSE's own status (catches holidays)."""
    if not _within_session():
        return False
    try:
        return nse().is_market_open()
    except Exception:  # noqa: BLE001
        return True  # clock says session; assume open if status call fails


# --- guarded job wrappers ---------------------------------------------
def job_intraday() -> None:
    if not _market_open_live():
        return
    pipeline.intraday_cycle()


def job_news() -> None:
    collectors.ingest_news()


def job_social() -> None:
    collectors.ingest_social()


def job_announcements() -> None:
    if _within_session():
        collectors.ingest_announcements()


def job_eod() -> None:
    pipeline.eod_cycle()


def job_macro() -> None:
    collectors.ingest_macro()


def job_graph() -> None:
    from ..engine.knowledge_graph import build_graph
    build_graph(with_llm=True)


def build_scheduler() -> BackgroundScheduler:
    s = get_settings()
    tz = pytz.timezone(s.timezone)
    sched = BackgroundScheduler(timezone=tz)
    cad = s.yaml.get("cadences", {})

    sched.add_job(job_intraday, IntervalTrigger(minutes=cad.get("agents_cycle_minutes", 30)),
                  id="intraday", max_instances=1, coalesce=True, misfire_grace_time=120)
    sched.add_job(job_news, IntervalTrigger(minutes=cad.get("news_minutes", 20)),
                  id="news", max_instances=1, coalesce=True)
    sched.add_job(job_social, IntervalTrigger(minutes=cad.get("social_minutes", 60)),
                  id="social", max_instances=1, coalesce=True)
    sched.add_job(job_announcements, IntervalTrigger(minutes=cad.get("announcements_minutes", 30)),
                  id="announcements", max_instances=1, coalesce=True)

    # EOD heavy cycle at 18:15 IST (after FII/DII publish), weekdays
    sched.add_job(job_eod, CronTrigger(day_of_week="mon-fri", hour=18, minute=15),
                  id="eod", max_instances=1, coalesce=True, misfire_grace_time=3600)
    # macro refresh daily
    sched.add_job(job_macro, CronTrigger(hour=cad.get("macro_daily_hour", 7), minute=0),
                  id="macro", max_instances=1, coalesce=True)
    # graph rebuild daily
    sched.add_job(job_graph, CronTrigger(hour=cad.get("graph_rebuild_hour", 6), minute=30),
                  id="graph", max_instances=1, coalesce=True)

    log.info("Scheduler configured with %d jobs (tz=%s)", len(sched.get_jobs()), s.timezone)
    return sched
