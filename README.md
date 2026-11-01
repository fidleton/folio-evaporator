# Folio Evaporator

A Home Assistant add-on for tracking stock portfolios across multiple broker accounts. Folio stores a transaction ledger in SQLite, imports transactions from CSV, and retrieves quotes, historical prices, and FX data through a replaceable provider adapter.

Supported installations are Home Assistant OS and Home Assistant Supervised. The add-on UI is served through Home Assistant Ingress.

## Installation

For a local add-on, place this repository in Home Assistant's `/addons/folio-evaporator` directory, refresh the local add-on store, then install and start **Folio Evaporator**. The Supervisor keeps the SQLite database under the add-on's persistent `/data` volume; normal Home Assistant add-on backups include that data.

The add-on does not require a Home Assistant API token. Access is provided through Home Assistant Ingress, and no host port is exposed by default.

## Documentation

- [CSV imports and transaction behavior](docs/transactions.md)
- [API reference](docs/api.md)
- [Market data and graph updates](docs/market-data.md)
- [Development setup](docs/development.md)

## License

This project is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0). See [LICENSE](LICENSE) for the full terms.
