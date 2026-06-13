"""Social sentiment source: Reddit (free API) Indian investing subreddits."""
from __future__ import annotations

from datetime import datetime, timezone

from ..config import get_settings
from ..logging_conf import get_logger

log = get_logger("data.social")


def fetch_reddit(limit_per_sub: int = 40) -> list[dict]:
    s = get_settings()
    if not s.has_reddit:
        log.info("Reddit creds absent; skipping social (set REDDIT_CLIENT_ID/SECRET)")
        return []
    try:
        import praw
    except ImportError:
        log.warning("praw not installed")
        return []

    try:
        reddit = praw.Reddit(
            client_id=s.reddit_client_id,
            client_secret=s.reddit_client_secret,
            user_agent=s.reddit_user_agent,
            check_for_async=False,
        )
        reddit.read_only = True
    except Exception as exc:  # noqa: BLE001
        log.warning("Reddit init failed: %s", exc)
        return []

    subs: list[str] = s.get("data_sources", "reddit_subs", default=[]) or []
    rows: list[dict] = []
    for sub in subs:
        try:
            for post in reddit.subreddit(sub).hot(limit=limit_per_sub):
                rows.append(
                    {
                        "ts": datetime.fromtimestamp(post.created_utc, tz=timezone.utc),
                        "sub": sub,
                        "post_id": post.id,
                        "title": (post.title or "")[:300],
                        "body": (post.selftext or "")[:1000],
                        "score": int(post.score or 0),
                        "url": f"https://reddit.com{post.permalink}",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("Reddit r/%s failed: %s", sub, exc)
    log.info("Fetched %d Reddit posts from %d subs", len(rows), len(subs))
    return rows
