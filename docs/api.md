# API

FastAPI exposes OpenAPI documentation at `/docs` and JSON endpoints for accounts, securities, transactions, portfolio holdings and summaries, CSV imports, quotes, historical prices, and current/historical FX rates. SQLite is the source of truth; holdings are derived from transactions.

Reporting currency defaults to USD and can be changed through `PUT /api/settings?reporting_currency=EUR`. Valuations require quote and FX observations; unavailable data is reported rather than estimated.
