# Folio Portfolio Tracker

A Home Assistant add-on for tracking stock portfolios across multiple broker accounts. The first release stores the transaction ledger in SQLite, imports transactions from CSV, and retrieves quotes, historical prices, and FX data through a replaceable provider adapter.

Supported installations are Home Assistant OS and Home Assistant Supervised. The add-on UI is served through Home Assistant Ingress.

## Development

Open the repository in VS Code and choose **Reopen in Container**. The devcontainer uses Home Assistant's `6-apps` development image, starts its systemd-managed nested Docker service, and mounts this repository into `/mnt/supervisor/apps/local/folio-evaporator` for local Supervisor testing. It adds Python 3.13 and Node 22 and installs backend/frontend dependencies.

Use **Terminal: Run Task → Start Home Assistant** or run `supervisor_run` in a devcontainer terminal. The local Supervisor downloads and starts Home Assistant and exposes the app store. Add this repository as a local add-on, install Folio, and open it through Ingress at `http://localhost:7123`.

This is a development Supervisor environment, not a production Home Assistant Supervised installation. Supervisor may report the devcontainer's Debian host as unsupported and disable host-specific functions; production support remains Home Assistant OS and Supervised on their supported hosts.

Forwarded ports:

- `7123`: Home Assistant (`8123` in the devcontainer).
- `7180`: Home Assistant HTTP proxy (`80` in the devcontainer).
- `7357`: Supervisor diagnostics (`4357` in the devcontainer).

Run backend tests, frontend lint/build, and the add-on image build in the devcontainer:

```sh
python -m pytest
npm --prefix frontend run lint
npm --prefix frontend run build
docker build --build-arg BUILD_ARCH=amd64 -t folio-tracker:local .
```

For local development, run the API and UI in separate devcontainer terminals:

```sh
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8099
npm --prefix frontend run dev -- --host 0.0.0.0
```

Vite proxies `/api` to the API service. Devcontainer ports 5173 and 8099 are forwarded.

## Home Assistant installation

For a local add-on, place this repository in Home Assistant's `/addons/folio-evaporator` directory, refresh the local add-on store, then install and start **Folio Portfolio Tracker**. The Supervisor keeps the SQLite database under the add-on's persistent `/data` volume; normal Home Assistant add-on backups include that data.

The add-on does not require a Home Assistant API token. Access is provided through Home Assistant Ingress, and no host port is exposed by default.

## CSV format

CSV headers are case-sensitive by default. Dates use ISO format `YYYY-MM-DD`. Each row must identify an existing account by its exact account name.

| Column | Required | Meaning |
| --- | --- | --- |
| `date` | Yes | Transaction date, `YYYY-MM-DD` |
| `account` | Yes | Existing broker account name |
| `type` | Yes | `buy`, `sell`, `dividend`, `fee`, `split`, `deposit`, or `withdrawal` |
| `currency` | Yes | Three-letter transaction currency |
| `symbol` | Trades and splits | Ticker or provider symbol |
| `quantity` | Trades and splits | Shares traded, or split ratio |
| `price` | Trades and sells | Unit price |
| `fees` | No | Trade fees, default `0` |
| `cash_amount` | Cash events | Positive cash movement for dividends, fees, deposits, and withdrawals |
| `notes` | No | Free-form reference |

Imports are previewed and validated before commit. Identical transactions are rejected on later imports. The first release expects this canonical header format; column mapping and broker-specific parsers are future extensions.

## API and market data

FastAPI exposes OpenAPI documentation at `/docs` and JSON endpoints for accounts, securities, transactions, portfolio holdings and summaries, CSV imports, quotes, historical prices, and current/historical FX rates. SQLite is the source of truth; holdings are derived from transactions.

The first market-data adapter uses Yahoo Finance via `yfinance`. It is an unofficial community integration with no service guarantee; quote availability, symbol coverage, freshness, and provider terms may vary. Fetched quotes and historical observations are cached locally. Direct broker connections are deferred, and the app does not place trades.

Reporting currency defaults to USD and can be changed through `PUT /api/settings?reporting_currency=EUR`. Valuations require quote and FX observations; unavailable data is reported rather than estimated.

## Initial transaction semantics

- `buy`: adds shares and subtracts `quantity * price + fees` from account cash.
- `sell`: subtracts shares and adds `quantity * price - fees` to account cash.
- `split`: multiplies the current share quantity by the supplied ratio.
- `deposit`: adds `cash_amount` to account cash.
- `withdrawal`: subtracts `cash_amount` from account cash.
- `dividend`: adds `cash_amount` less any fees to account cash.
- `fee`: subtracts the absolute `cash_amount` from account cash.
