# Security Policy

## Secrets
- All credentials live in `.env`, which is git-ignored. **Never commit `.env`.**
- The system runs fully without any keys (degraded, deterministic mode).
- Rotate any key that may have been exposed via your provider's console.

## Data
- The local DuckDB/SQLite warehouse and logs are git-ignored.
- No order-routing or brokerage credentials are used — execution is simulated.

## Reporting
Found a vulnerability? Please open a private security advisory on GitHub rather
than a public issue.

## Scope
Research/educational software. Not investment advice. No warranty.
