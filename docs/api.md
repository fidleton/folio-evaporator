# API

FastAPI exposes OpenAPI documentation at `/docs` and JSON endpoints for accounts, securities, transactions, portfolio holdings and summaries, CSV imports, quotes, historical prices, and current/historical FX rates. SQLite is the source of truth; holdings are derived from transactions.

`GET /api/securities/search?q=...` returns `{ "results": [...], "warnings": [...] }`. For every checksum-valid ISIN, the backend queries OpenFIGI first, then searches Yahoo Finance separately for each mapped FIGI ticker. Yahoo is not queried with the ISIN. Only exact ticker/listing matches returned by Yahoo are offered; mappings without a Yahoo match are omitted because Yahoo Finance is currently the only available market-data provider. A Yahoo search match does not guarantee quote or history availability. An API key is optional; keyless requests use OpenFIGI's lower unauthenticated rate limit. Provider failures are reported in `warnings`.

`GET /api/settings` reports the reporting currency and whether an OpenFIGI API key is configured, but never returns the key. `PUT /api/settings` accepts `openfigi_api_key` in a JSON body. The API key must be provided in the body, not a URL parameter. Sending an empty key removes the saved key; valid ISIN lookups continue without it at OpenFIGI's lower unauthenticated rate limit. The key, if configured, is encrypted at rest using the `encryption_key` add-on option from `config.yaml`; when the option is blank, startup generates a key and persists it in Home Assistant through the Supervisor API.

CSV import templates are managed with `GET/POST /api/imports/templates`, `GET/PUT/DELETE /api/imports/templates/{template_id}`. Uploads use `POST /api/imports/csv/preview` with a saved `template_id`, an inline `template_definition`, or no template for the built-in canonical format. A preview batch stores normalized rows and a snapshot of its template settings. `PUT /api/imports/{batch_id}/rows/{line_number}/symbol` stores a user-selected symbol before `POST /api/imports/{batch_id}/commit` commits valid rows.

Reporting currency defaults to USD and can be changed through `PUT /api/settings?reporting_currency=EUR`. Displayed dates and times use the web client's locale and timezone. Valuations require quote and FX observations; unavailable data is reported rather than estimated.

The portfolio summary reports signed net `interest_income` separately and includes it in `total_return`. Interest transactions are cash events and do not require a security symbol.
