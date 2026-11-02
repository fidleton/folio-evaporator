# CSV imports and transactions

## CSV format

A complete example covering every supported transaction type is available in [canonical-transactions.csv](./canonical-transactions.csv).

CSV headers are case-sensitive by default. Dates use ISO format `YYYY-MM-DD`. Each row must identify an existing account by its exact account name.

| Column | Required | Meaning |
| --- | --- | --- |
| `date` | Yes | Transaction date, `YYYY-MM-DD` |
| `account` | Yes | Existing broker account name |
| `type` | Yes | `buy`, `sell`, `dividend`, `interest`, `fee`, `split`, `deposit`, `withdrawal`, `transfer_in`, or `transfer_out` |
| `currency` | Yes | Three-letter transaction currency |
| `symbol` | Trades, splits, and security transfers | Ticker or provider symbol |
| `isin` | No | A 12-character ISIN or empty; security-linked distributions may include one |
| `quantity` | Trades, splits, and transfers | Shares traded or transferred, or split ratio |
| `price` | Trades and sells | Unit price; for transfers, carrying cost basis per share |
| `fees` | No | Trade or dividend fees, default `0`; negative values represent a rebate or correction |
| `taxes` | No | Transaction taxes, default `0`; reduces cash but is excluded from P/L; negative values represent a refund or correction |
| `cash_amount` | Cash events | Positive magnitude for fees, deposits, and withdrawals; signed for dividends and interest so a negative value corrects income |
| `notes` | No | Free-form reference |

Imports are previewed and validated before commit. Valid rows can be committed while invalid rows are skipped and reported. Identical normalized transactions are rejected on later imports. The built-in canonical format is available without setup; users can also map broker-specific CSV columns and save those mappings as reusable templates.

During preview, an existing open position in the targeted account is reused when the imported row has the same ISIN and that ISIN identifies exactly one ticker there. This avoids re-resolving a known position through market-data search. A missing ISIN is also filled when the row's account and symbol identify exactly one known ISIN in an open position. Known security names are shown above their tickers. Closed positions, ambiguous ticker/ISIN matches, and positions in other accounts are not used for inference.

Transaction timestamps are stored in UTC. The web client displays dates and times using the browser's locale and timezone preferences.

## Import format templates

Templates store their parsing and mapping settings in the local database. They do not retain uploaded files or sample transaction rows. A template can configure:

- delimiter, quote character, UTF-8 encoding, whether the first row is a header, and up to 100 skipped leading rows;
- mappings from source headers to canonical transaction fields and explicit source-label mappings to the canonical transaction types;
- security rows without a ticker can keep their source ISIN and description; the preview searches by either value, automatically selects a sole exact-ISIN match, and asks the user to choose when multiple symbols match;
- date and time formats using Python `strptime` directives (for example `%d/%m/%Y`, `%H:%M:%S`, or `%d/%m/%Y %H:%M`). Date and time can share one mapped date column or use separate mapped date and time columns. For separate columns, map `time` and specify `time_format`; for a combined column, include the time directives in `date_format`;
- decimal separator and optional thousands separator;
- how negative quantity, price, and gross-value fields are handled (`reject_negative`, `absolute`, or `preserve`);
- type-aware cash sign defaults: negative withdrawals and standalone fee amounts become positive magnitudes; dividend and interest amounts keep their sign, so negative amounts correct income; negative fees and taxes are preserved as rebates/refunds. Templates can override these rules for each transaction type and field;
- an optional gross trade-value interpretation. Select trade principal before costs, buy debit, or net sell proceeds. For buy debit or net sell proceeds, independently specify whether the value includes mapped fees and taxes. Buy debit conversion subtracts the included costs before dividing by quantity; net sell proceeds conversion adds the included costs back. If a configured included fee or tax is blank on a row, that row is invalid rather than assuming a zero cost.

The source type label map is explicit and case-insensitive. Unknown labels are invalid. Source values never silently change the transaction type based on their sign. Account names match existing accounts exactly unless the importer selects an account override for that import. Currency, symbol, and ISIN values are normalized to uppercase.

Example saved template definition:

```json
{
  "schema_version": 1,
  "delimiter": ";",
  "date_format": "%d/%m/%Y",
  "decimal_separator": ",",
  "thousands_separator": ".",
  "field_map": {
    "date": "Trade date",
    "account": "Account",
    "type": "Activity",
    "symbol": "Ticker",
    "quantity": "Shares",
    "gross_value": "Total",
    "currency": "Currency",
    "fees": "Commission",
    "taxes": "Tax"
  },
  "type_map": {"Purchase": "buy", "Sale": "sell"},
  "trade_value": {
    "mode": "buy_debit",
    "includes_fees": true,
    "includes_taxes": true
  }
}
```

When `trade_value` is used, map `gross_value` instead of `price`. The selected mode applies to rows of that transaction type; other trade types are invalid under that template.

## Initial transaction semantics

- `buy`: adds shares and subtracts `quantity * price + fees + taxes` from account cash. Trade fees affect cost basis; taxes do not affect P/L. A negative fee or tax is a refund and increases cash.
- `sell`: subtracts shares and adds `quantity * price - fees - taxes` to account cash. Trade fees affect realized P/L; taxes do not. A negative fee or tax is a refund and increases cash.
- `split`: multiplies the current share quantity by the supplied ratio.
- `transfer_in`: adds the transferred shares and carrying cost basis to the account. Requires a symbol, positive quantity, and positive carrying cost basis per share. Does not change cash or realized P/L.
- `transfer_out`: removes the transferred shares and their carrying cost basis from the account. Requires a symbol, positive quantity, and positive carrying cost basis per share. Does not change cash or realize a gain or loss. For an internal account transfer, record matching `transfer_out` and `transfer_in` entries with the same quantity and carrying cost basis; the portfolio-wide holding and basis remain unchanged.
- `deposit`: adds `cash_amount` to account cash.
- `withdrawal`: subtracts `cash_amount` from account cash.
- `dividend`: adds the signed `cash_amount` less any fees and taxes to account cash. A negative amount corrects or reverses a previous distribution. Dividend fees affect P/L; taxes do not. Negative fees or taxes are refunds and increase cash.
- `interest`: adds the signed `cash_amount` less any fees and taxes to account cash. A negative amount corrects or reverses interest income. Interest is tracked separately from security dividends and is included in portfolio total return; fees affect return and taxes do not.
- `fee`: subtracts the absolute `cash_amount` and any taxes from account cash; standalone fees do not affect security P/L.
