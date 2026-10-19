# Evaporator Add-on Architecture

## Goal and scope

Build a Home Assistant add-on for tracking stock portfolios across multiple broker accounts. The first release provides a browser UI through Home Assistant Ingress, a JSON API, SQLite persistence, transaction-ledger holdings, CSV transaction imports, and current/historical market prices with currency conversion.

Supported installations are Home Assistant OS and Home Assistant Supervised. Container/Core-only installations are out of scope.

The first release does not connect to broker APIs and does not place trades. It provides a broker adapter boundary for future integrations. Options, crypto, tax advice, and tax-lot optimization are out of scope.

## Technology

- **API and domain:** Python with FastAPI.
- **UI:** TypeScript with React, built with Vite and served by the API service.
- **Persistence:** SQLite in the add-on's persistent `/data` directory.
- **Packaging:** Home Assistant add-on using a multi-stage Docker build; the UI is served by the same service as the API.
- **Market data:** An adapter interface with Yahoo Finance via `yfinance` as the initial community data source. It is unofficial, has no service-level guarantee, and may have usage or availability constraints; keep the adapter replaceable and display data timestamps/source.

## Components and responsibilities

1. **Home Assistant add-on wrapper**
   - `config.yaml` declares add-on metadata, Ingress support, and supported architectures.
   - The container starts the API/UI service and writes all durable state to `/data`.
   - Ingress requests may be mounted under a generated prefix. The app must honor forwarded prefix information and use relative UI/API URLs.

2. **API service**
   - Owns validation, portfolio calculations, import workflow, market data synchronization, and persistence.
   - Serves JSON endpoints and the built frontend from one origin.
   - Does not let UI code access SQLite directly.

3. **Portfolio domain**
   - Transactions are authoritative; positions and cash balances are derived from the ledger.
   - Each transaction belongs to a broker account and security/currency context.
   - Initial transaction types include buy, sell, dividend, fee, split, deposit, and withdrawal.
   - Decimal-safe values are used for quantities and monetary amounts.
   - Original transaction and instrument currencies are retained. A configured reporting currency is used for portfolio totals.

4. **SQLite repository**
   - Stores broker accounts, securities, transactions, import batches, price observations, FX observations, and settings.
   - Uses schema migrations and foreign-key constraints.
   - Market-data rows include provider and observation/fetch timestamps so stale data can be recognized.

5. **CSV import service**
   - Supports upload, preview, column mapping, row validation, duplicate detection, and explicit commit.
   - A canonical CSV format is documented; provider-specific formats can be mapped to it.
   - Import batches and source-row identity support auditability and repeat-import protection.

6. **Market-data adapters**
   - Separate current quote, historical price, and FX-rate interfaces from portfolio calculations.
   - Cache normalized results in SQLite and expose data source and freshness.
   - Missing/stale data is represented explicitly; it must not silently change ledger transactions.

7. **Ingress UI**
   - Provides account management, holdings, transaction history, CSV import preview/commit, and quote/valuation freshness.
   - Uses only the API and is usable under an Ingress URL prefix.

## Data flow

```text
Home Assistant Ingress
        |
        v
React UI -- same-origin JSON --> FastAPI routes
                                      |       \
                                      v        v
                              Domain services  Provider adapters
                                      |        (quotes / history / FX)
                                      v                 |
                              SQLite under /data <---- cache observations
```

CSV uploads pass through the API to a preview/validation step. Only an explicit commit writes accepted transactions and the import batch to SQLite. Holdings are calculated from committed ledger entries; quote and FX observations enrich valuations but never rewrite transactions.

## Initial API surface

- `GET/POST /api/accounts`, `PATCH/DELETE /api/accounts/{id}`
- `GET/POST /api/securities`, `GET /api/securities/{id}`
- `GET/POST /api/transactions`, `PATCH/DELETE /api/transactions/{id}`
- `GET /api/portfolio/summary`, `GET /api/portfolio/holdings`
- `POST /api/imports/csv/preview`, `POST /api/imports/csv/{id}/commit`, `GET /api/imports`
- `GET /api/prices/{symbol}`, `GET /api/prices/{symbol}/history`
- `GET /api/fx/{base}/{quote}/history`
- `GET/PUT /api/settings`
- `GET /api/health`

Exact route shapes may evolve with implementation, but API contracts remain the UI boundary.

## Security and operations

- Store the database and import metadata under `/data`; never depend on the container's ephemeral filesystem for user data.
- Do not store broker credentials in the first release because live broker connections are deferred.
- Validate uploaded file size/type and CSV row contents; never execute uploaded content.
- Avoid logging transaction details or uploaded CSV contents.
- Document Home Assistant backup/restore behavior for the add-on data directory.

## Verification

- Validate add-on metadata and build the multi-stage image.
- Test schema creation/migrations, foreign keys, decimal-safe ledger calculations, multiple accounts, dividends/fees/splits, and reporting-currency conversion.
- Test CSV preview/commit, invalid rows, duplicates, and idempotent repeat imports.
- Test provider adapters using deterministic fixtures, including missing and stale quote/FX observations.
- Smoke-test API and UI under a prefixed Ingress path and verify SQLite persists across container restarts.

## Delivery sequence

1. Add this architecture document and the add-on/service skeleton.
2. Implement SQLite schema, transaction ledger, and API endpoints.
3. Implement CSV preview and commit.
4. Add market-data/FX adapter and cached observations.
5. Build the Ingress UI, then verify add-on packaging and persistence.
