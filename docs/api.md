# API

FastAPI exposes OpenAPI documentation at `/docs` and JSON endpoints for accounts, securities, transactions, portfolio holdings and summaries, CSV imports, quotes, historical prices, and current/historical FX rates. SQLite is the source of truth; holdings are derived from transactions.

CSV import templates are managed with `GET/POST /api/imports/templates`, `GET/PUT/DELETE /api/imports/templates/{template_id}`. Uploads use `POST /api/imports/csv/preview` with a saved `template_id`, an inline `template_definition`, or no template for the built-in canonical format. A preview batch stores normalized rows and a snapshot of its template settings. `PUT /api/imports/{batch_id}/rows/{line_number}/symbol` stores a user-selected symbol before `POST /api/imports/{batch_id}/commit` commits valid rows.

Reporting currency defaults to USD and can be changed through `PUT /api/settings?reporting_currency=EUR`. Displayed dates and times use the web client's locale and timezone. Valuations require quote and FX observations; unavailable data is reported rather than estimated.

The portfolio summary reports signed net `interest_income` separately and includes it in `total_return`. Interest transactions are cash events and do not require a security symbol.
