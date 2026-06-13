# Contributing to CTG Terminal

Thanks for your interest! This project values **correctness, clarity, and real data**.

## Getting started
```bash
make install      # venv + dependencies
make once         # run one full cycle to populate the warehouse
make test         # run the test suite
make run          # launch the dashboard at http://localhost:8799
```

## Guidelines
- **No seeded/fake data.** Every signal must trace back to a real, free source.
- **Quant must be deterministic and tested.** Payoff/Greeks/risk math lives in
  `ctg/engine/` and should have unit tests in `tests/`.
- **LLM is optional.** Any LLM-backed feature must have a deterministic fallback.
- **Keep it embedded.** Prefer zero-ops stores (DuckDB/SQLite/NetworkX).
- Run `make lint` and `make test` before opening a PR.

## Commit style
Conventional commits: `feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `build:`, `ci:`, `chore:`.

## Disclaimer
Research/educational software. Not investment advice.
