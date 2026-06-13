"""News collection via RSS (free, no key) from major Indian financial media."""
from __future__ import annotations

from datetime import datetime, timezone
from time import mktime

import feedparser

from ..config import get_settings
from ..logging_conf import get_logger

log = get_logger("data.news")


def fetch_news() -> list[dict]:
    s = get_settings()
    feeds: list[str] = s.get("data_sources", "news_rss", default=[]) or []
    rows: list[dict] = []
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
            source = parsed.feed.get("title", url.split("/")[2] if "//" in url else url)
            for e in parsed.entries[:40]:
                ts = _entry_ts(e)
                title = (e.get("title") or "").strip()
                summary = _clean(e.get("summary", ""))[:600]
                link = e.get("link", "")
                if not title or not link:
                    continue
                rows.append(
                    {
                        "ts": ts,
                        "source": source[:80],
                        "title": title[:300],
                        "summary": summary,
                        "url": link,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("RSS %s failed: %s", url, exc)
    log.info("Fetched %d news items from %d feeds", len(rows), len(feeds))
    return rows


def _entry_ts(e) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            try:
                return datetime.fromtimestamp(mktime(t), tz=timezone.utc)
            except Exception:  # noqa: BLE001
                pass
    return datetime.now(timezone.utc)


def _clean(html: str) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()
