"""Logging configuration: console + rotating file."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR, get_settings

_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    s = get_settings()
    level = getattr(logging, s.log_level, logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(level)
    root.addHandler(console)

    fileh = RotatingFileHandler(
        LOG_DIR / "ctg.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    fileh.setFormatter(fmt)
    fileh.setLevel(level)
    root.addHandler(fileh)

    # Quiet noisy libraries
    for noisy in ("urllib3", "yfinance", "peewee", "apscheduler.executors", "httpx", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
