# Market data

The first market-data adapter uses Yahoo Finance via `yfinance`. It is an unofficial community integration with no service guarantee; quote availability, symbol coverage, freshness, and provider terms may vary. Fetched quotes and historical observations are cached locally. Direct broker connections are deferred, and the app does not place trades.

A Yahoo Finance search result does not guarantee historical-price availability. If Yahoo reports no history for a symbol, the graph shows an empty-state message instead of an error, and that no-data result is cached using the selected range's normal history refresh interval. Other provider failures remain visible as errors.

## ISIN lookup

Security search uses Yahoo Finance for ordinary names and ticker queries. For a checksum-valid ISIN, the backend queries OpenFIGI first, then searches Yahoo Finance separately for each returned FIGI ticker. It includes only exact ticker/listing matches returned by Yahoo; Yahoo is never queried with the ISIN itself. OpenFIGI does not require an API key; without one, requests use its lower rate limit. Users can optionally add an API key in **Settings** for a higher rate limit. OpenFIGI mapping errors are shown as warnings and do not produce unvalidated Yahoo ticker suggestions.

Only symbols returned by Yahoo Finance search are offered as candidates, because Yahoo Finance is currently the only available market-data provider. OpenFIGI mappings enrich Yahoo candidates where the ticker and listing can be matched; mappings without a Yahoo match are omitted rather than offering an unpriceable symbol. A Yahoo search match still does not guarantee that quote or history data will be available for that symbol.

The API key is encrypted in the add-on's SQLite database using Fernet authenticated encryption. The separate encryption key is an add-on option in Home Assistant. If that option is empty at startup, the add-on generates a key and saves it through the Home Assistant Supervisor API. Keep the encryption key stable: changing it without first migrating the stored OpenFIGI key prevents decryption and stops the add-on from starting, with an explicit error. Home Assistant backups include the add-on options and database, so restoring both preserves decryption. The API key is never returned to the browser or written to application logs. For standalone development without a Supervisor token, the add-on uses a separate permission-restricted key file in its data directory.

OpenFIGI is called for valid ISIN queries regardless of whether an API key is configured; invalid ISINs are never sent. Lookup results use the existing one-day search cache under a versioned ISIN-specific cache key. Changing or removing the API key invalidates the search cache. Partial results from a provider failure are not cached.

## Graph stock data updates

The portfolio graph and a selected security's price graph use historical price observations from Yahoo Finance. Observations are cached in the add-on's SQLite database and reused between requests. Graph data is requested when the app loads, when the selected account or graph time range changes, and when a transaction is added, edited, deleted, or imported. The security graph is requested when a security is selected or its time range changes.

Historical data is fetched from Yahoo Finance when the local cache does not cover the requested range, its newest observation is more than one day old, or, for the `max` range, lacks full-history coverage. Daily and longer-range history is checked on each graph request and refreshed when one of those conditions is met. While a `1d` graph is open, the app requests it again every five minutes, so the backend can refresh the intraday series after its five-minute cache interval. Other ranges refresh on the next graph request when their cache needs updating.

Intraday price observations are stored separately from daily history. When a newer exchange session is fetched, observations from older sessions are removed from the intraday cache. Existing databases are migrated automatically.

The **Update prices** action refreshes current portfolio quotes and currency conversion rates for the summary and positions. It does not itself request new historical series or force either graph to reload. Historical graph data updates on the next graph request when the cache rules above require a fetch. All cached observations persist in the add-on database.
