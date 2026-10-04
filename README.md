# Folio Evaporator

## High-Level Design

### Goal

A self-hosted stock portfolio tracker that runs as a Home Assistant add-on. Users manage investment transactions in a browser, review portfolio value and performance, and keep data on their Home Assistant host in SQLite.

### Product Scope

The product supports manual accounts, securities, and buy/sell transactions; current holdings and cost basis derived from those transactions; portfolio value using fetched market prices; a dashboard with allocation and performance summaries; and user-managed plugins running in the core process. Plugins declare a required plugin API version and requested permission scopes, which the user can grant or revoke in the UI. It is a tracking and reporting tool, not a brokerage, tax-preparation, or trading system. No brokerage credentials are required or stored.

### Runtime Architecture

```text
Browser
  | Home Assistant Ingress (default access path)
  v
Add-on container
  +-- Web UI: portfolio dashboard, transactions, settings
  +-- HTTP API: validation, portfolio queries, price refresh
  +-- Plugin host: installed plugins running in the core process
  +-- Domain services: ledger, positions, valuation, performance
  +-- Market-data adapters: provider-specific quote/history requests
  +-- SQLite database: /data/folio.db
```

The add-on is a single deployable container. It serves the browser application and its API from the same origin, avoiding a separate frontend host or database service. Installed plugins run in the core process and use the versioned plugin interface; installation is the trust boundary, not an operating-system sandbox. The backend owns all persistence and portfolio calculations; neither the browser nor plugins access SQLite directly. Home Assistant Ingress provides the normal authenticated entry point. Direct host port exposure is disabled by default and can remain an optional advanced configuration.

### Main Components

- **Web UI:** responsive dashboard, transaction entry and history, account/security management, and data-provider settings. All requests use relative URLs so the app works behind the Ingress path prefix.
- **HTTP API:** versioned JSON endpoints for accounts, securities, transactions, portfolio summaries, prices, and settings. Validate input at the API boundary and return consistent client-safe errors.
- **Plugin host:** install, configure, update, enable, disable, and remove plugins from the UI. A plugin declares its required plugin API version and requested permission scopes; the user grants or revokes those scopes. The core checks compatibility and grants before exposing scoped capabilities. Installing or updating a plugin requires restarting the core process.
- **Portfolio domain:** calculate holdings from the transaction ledger rather than treating editable share totals as the source of truth. Keep valuation and performance calculations in backend services so UI and exports agree.
- **Market-data adapters:** isolate provider-specific symbols, request limits, errors, and response formats. The initial implementation should select one provider and make its limitations visible; provider credentials, if needed, are user-configured and stored locally.
- **SQLite storage:** one database at `/data/folio.db`, with schema migrations, foreign keys, and transactions for ledger changes. Store timestamps in UTC and monetary/share quantities at adequate decimal precision; avoid binary floating-point for persisted financial values.
- **Refresh worker:** refresh prices on demand and on a configurable schedule. A failed refresh must retain the last known price and show its timestamp rather than making the portfolio appear empty.

### Data Model

- **Account:** user-defined grouping such as taxable, retirement, or watchlist.
- **Security:** ticker/symbol, exchange or market identifier, display name, and currency.
- **Transaction:** account, security, type, trade date, quantity, unit price, fees, and optional note. Buys, sells, dividends, and splits should be represented explicitly as transaction types; corrections should be auditable rather than silently changing derived holdings.
- **Price:** security, observation time, currency, price, and provider. Keep a small history sufficient for charts and as-of reporting; define retention in settings or a documented default.
- **App setting:** non-secret preferences and provider configuration. Secrets must not be logged or exposed by read endpoints.

Holdings are derived by summing ledger effects through the requested date. Portfolio value is quantity multiplied by the latest eligible price, with the price timestamp and currency shown. Initial performance reporting should state its method and assumptions; do not label simple value change as investment return when deposits, withdrawals, or cash flows are not accounted for.

### Key Flows

1. On startup, the add-on opens `/data/folio.db`, applies pending migrations, and starts the API, UI, and refresh scheduler.
2. A user records a transaction; the API validates it and writes it atomically. Portfolio summaries are then recalculated from the ledger.
3. A scheduled or manual refresh fetches market data through the adapter and upserts observations. Provider failures are reported without discarding stored prices.
4. The dashboard requests a summary and presents holdings, value, allocation, and data freshness. Currency conversion is out of initial scope unless explicitly added with a reliable exchange-rate source.

### Home Assistant Add-on Deployment

- Package the application as a Home Assistant add-on with a pinned base image and explicit supported architectures.
- Mount `/data` as the persistent add-on data directory; never put the database in the container image or temporary filesystem.
- Expose the UI through Ingress, support the Ingress base path, and avoid requiring users to configure a separate reverse proxy.
- Provide add-on options for refresh interval, market-data provider, and optional provider token. Mark secrets appropriately in the add-on configuration schema.
- Include health checks, readable startup/runtime logs, and a clear migration/backup policy. SQLite backup should use the Home Assistant add-on backup mechanism or a consistent SQLite backup operation, not a raw copy during writes.
- Do not require privileged mode, host networking, or access to Home Assistant internals for core tracking functionality.

### Security and Reliability

- Treat Ingress as the default access boundary; do not assume the app itself is internet-safe when direct port access is enabled.
- Bind only to the container interface required by Ingress. Apply same-origin protections and validate all API input.
- Redact credentials and sensitive provider responses from logs. Keep financial data local and explain that market data is sent to the selected provider.
- Use database migrations and atomic ledger writes. On startup or database errors, fail visibly and preserve the database rather than attempting destructive recovery.
- Display quote age, stale-price status, and provider errors in the UI. Never imply real-time pricing unless the configured provider actually supplies it.

### Initial Delivery and Acceptance

The MVP is complete when the add-on can be installed and opened through Ingress; data persists across container restarts; users can create accounts and securities, enter and edit transactions, and see correctly derived holdings; price refresh works with one documented provider and preserves the last successful quote on failure; and a backup/restore cycle retains the database. Automated tests should cover ledger calculations, transaction validation, migrations, and API behavior, with a smoke test for Ingress path handling.

### Out of Scope for MVP

Brokerage synchronization, order placement, tax-lot optimization, tax advice, multi-user permissions, automatic currency conversion, and guarantees of real-time or investment-grade performance reporting.
