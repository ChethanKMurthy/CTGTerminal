# Architecture

CTG Terminal is a layered, event-driven research pipeline. Each layer is
independently testable and degrades gracefully when an input is unavailable.

## Layers
1. **Ingestion (`ctg/data`)** — fault-tolerant collectors normalise 8+ free
   sources into the warehouse. Idempotent upserts on natural keys; freshness
   heartbeats per source.
2. **Storage (`ctg/storage`)** — DuckDB (columnar OLAP) for time-series,
   SQLite (WAL) for transactional state, NetworkX for the knowledge graph.
3. **Knowledge Graph (`ctg/engine/knowledge_graph.py`)** — entities + edges from
   curated maps, realised-return correlations and LLM-extracted news relations.
4. **Agents (`ctg/agents`)** — nine specialists read warehouse facts and emit
   structured views; LLM-augmented with deterministic fallbacks.
5. **Engine (`ctg/engine`)** — alpha discovery, scenarios, regime, quant
   analytics, risk and the self-learning eval loop.
6. **Delivery** — CIO aggregation → paper book + options engine →
   FastAPI/dashboard + alerts.

## Orchestration
`ctg/scheduler` runs market-hours-aware jobs (intraday / EOD / macro / graph)
via APScheduler. `run.py` wires the scheduler + web server; a launchd unit keeps
it alive 24/7.

## Data flow
```
sources → collectors → DuckDB/SQLite → graph + agents → alpha/risk/eval
        → CIO → paper book + options engine → API → dashboard + alerts
```

## Design principles
- **Real data only** — no seeded values; every number traces to a free source.
- **Deterministic core, optional intelligence** — quant is exact and tested;
  LLM adds narrative but is never required.
- **Embedded & zero-ops** — no external services to run.
- **Explainable** — every signal decomposes into named factors.
