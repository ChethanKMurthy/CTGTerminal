# 🗺️ CTG Terminal — Roadmap

A living backlog of improvements, shipped incrementally. Grouped by theme.

### Engineering & Quality
1. Project roadmap & contribution docs
2. Unit tests for quant analytics (Greeks, RSI, momentum)
3. Unit tests for the options payoff engine (max P/L, breakevens)
4. Unit tests for the risk engine (VaR, stress tests)
5. GitHub Actions CI (lint + import + tests)
6. Makefile (run / test / lint / format)
7. Dockerfile + docker-compose for one-command spin-up
8. CONTRIBUTING guide
9. Ruff + pre-commit configuration
10. mypy type-checking configuration

### Quant Indicators & Analytics
11. MACD in price features
12. Bollinger Bands + %B
13. VWAP (intraday)
14. ATR (volatility / stop sizing)
15. Stochastic oscillator
16. ADX (trend strength)
17. Supertrend
18. Pivot points & CPR for indices
19. Advance/Decline breadth collector
20. Sector-performance heatmap dataset

### Data Sources
21. Additional financial RSS feeds
22. Additional FRED macro series (M3, forex reserves)
23. Precious metals (gold/silver) via free proxies
24. Global risk indices (S&P 500, Nasdaq, Nikkei) for context
25. NIFTY sectoral indices tracking (IT, Pharma, Auto, FMCG)
26. India 10Y G-Sec yield series
27. WTI crude (alongside Brent)
28. Currency basket (EUR/INR, GBP/INR)
29. Put–Call ratio time series
30. Option OI build-up classification (long/short build-up)

### LLM / Agents
31. Daily news-digest agent
32. Risk-narrative agent (plain-English risk explainer)
33. Gemini ⇄ Groq failover hardening + docs
34. Versioned prompt-template module
35. LLM usage / token tracker
36. Devil's-advocate critic agent for the top signal
37. TF-IDF news clustering (theme detection)
38. Sector-rotation agent

### Dashboard / UX
39. Signal detail drill-down
40. Signal search & filter
41. Light / dark theme toggle
42. CSV / JSON export buttons
43. Sector heatmap visualization
44. Custom option payoff builder
45. Equity-curve drawdown overlay
46. News sentiment timeline

### Ops / MLOps
47. `/api/metrics` (Prometheus-style) endpoint
48. Factor backtest CLI
49. Rolling Information-Coefficient history chart
50. Strategy hit-rate tracked & charted over time

> Items ship one per day via `scripts/daily_release.sh`. Status is tracked in `docs/CHANGELOG.md`.
