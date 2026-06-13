# Data Sources

All sources are **free** and **real**. No paid feeds, no seeded data.

| Source | What | Cadence | Notes |
|---|---|---|---|
| NSE (`nseindia.com`) | Option chains, FII/DII cash, F&O participant OI, bulk/block deals, indices, India VIX, announcements | intraday / EOD | Browser-session emulation required; residential IP recommended |
| yfinance (Yahoo) | OHLCV + fundamentals for the universe; index OHLC for pivots | intraday / EOD | ~15-min delayed |
| FRED | India CPI/IIP + global drivers (Brent, WTI, USD/INR, US yields, VIX, DXY) | daily | Free API key |
| World Bank | India GDP & CPI (annual) | daily | Keyless fallback |
| RSS | Moneycontrol, ET, Mint, BS, Hindu BL, Financial Express | ~20 min | Headlines + summaries |
| Reddit (PRAW) | Indian investing subreddits | hourly | Free script app |

## Not used (deliberately)
Satellite/shipping/flight/credit-card/mobile-location data, real-time tick data,
X/Twitter firehose, full earnings-call transcripts — none have a free India feed.

## Reliability
Endpoints are unofficial and can change shape or rate-limit. Collectors retry,
rotate sessions, and degrade gracefully; the **System** tab shows per-source
freshness heartbeats.
