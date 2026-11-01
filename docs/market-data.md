# Market data

The first market-data adapter uses Yahoo Finance via `yfinance`. It is an unofficial community integration with no service guarantee; quote availability, symbol coverage, freshness, and provider terms may vary. Fetched quotes and historical observations are cached locally. Direct broker connections are deferred, and the app does not place trades.

## Graph stock data updates

The portfolio graph and a selected security's price graph use historical price observations from Yahoo Finance. Observations are cached in the add-on's SQLite database and reused between requests. Graph data is requested when the app loads, when the selected account or graph time range changes, and when a transaction is added, edited, deleted, or imported. The security graph is requested when a security is selected or its time range changes.

Historical data is fetched from Yahoo Finance when the local cache does not cover the requested range, its newest observation is more than one day old, or, for the `max` range, lacks full-history coverage. Daily and longer-range history is checked on each graph request and refreshed when one of those conditions is met. While a `1d` graph is open, the app requests it again every five minutes, so the backend can refresh the intraday series after its five-minute cache interval. Other ranges refresh on the next graph request when their cache needs updating.

Intraday price observations are stored separately from daily history. When a newer exchange session is fetched, observations from older sessions are removed from the intraday cache. Existing databases are migrated automatically.

The **Update prices** action refreshes current portfolio quotes and currency conversion rates for the summary and positions. It does not itself request new historical series or force either graph to reload. Historical graph data updates on the next graph request when the cache rules above require a fetch. All cached observations persist in the add-on database.
