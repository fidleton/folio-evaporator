# CSV imports and transactions

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
| `taxes` | No | Transaction taxes, default `0`; reduces cash but is excluded from P/L |
| `cash_amount` | Cash events | Positive cash movement for dividends, fees, deposits, and withdrawals |
| `notes` | No | Free-form reference |

Imports are previewed and validated before commit. Identical transactions are rejected on later imports. The first release expects this canonical header format; column mapping and broker-specific parsers are future extensions.

## Initial transaction semantics

- `buy`: adds shares and subtracts `quantity * price + fees + taxes` from account cash. Trade fees affect cost basis; taxes do not affect P/L.
- `sell`: subtracts shares and adds `quantity * price - fees - taxes` to account cash. Trade fees affect realized P/L; taxes do not.
- `split`: multiplies the current share quantity by the supplied ratio.
- `deposit`: adds `cash_amount` to account cash.
- `withdrawal`: subtracts `cash_amount` from account cash.
- `dividend`: adds `cash_amount` less any fees and taxes to account cash. Dividend fees affect P/L; taxes do not.
- `fee`: subtracts the absolute `cash_amount` and any taxes from account cash; standalone fees do not affect security P/L.
