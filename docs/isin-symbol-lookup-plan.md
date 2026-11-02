# ISIN-to-symbol lookup plan

## Goal

Improve the chance of finding the correct exchange-listed symbol for an ISIN without treating Yahoo Finance search as a complete ISIN directory. Preserve the existing manual candidate selection flow. OpenFIGI is always used for valid ISINs; users may configure an API key to increase the rate limit.

## Implementation status

Implemented. Every valid ISIN query uses OpenFIGI first, then validates its mapped tickers through Yahoo Finance. Yahoo is never queried with the ISIN itself. The API key is optional; without one, requests use OpenFIGI's lower unauthenticated rate limit. Settings expose only whether a key is configured. If provided, the OpenFIGI key is encrypted in SQLite with a Fernet key kept in Home Assistant add-on options; if that option is empty, the key is generated at startup and persisted using the Supervisor API.

Only Yahoo Finance search results are offered as selectable candidates, since Yahoo Finance is currently the only available market-data provider. OpenFIGI enriches a Yahoo candidate only when its mapped listing can be matched; unmatched OpenFIGI mappings are hidden. A Yahoo search match is not a guarantee of quote or history availability.

## Behavior

1. Normalize the search query. If it is a valid, checksum-verified 12-character ISIN, use the ISIN lookup path; otherwise retain the current Yahoo Finance text search behavior.
2. Always query OpenFIGI for valid ISIN mappings and convert each mapping into an exchange-qualified symbol candidate where possible. An API key is optional and only raises the rate limit.
3. Use Yahoo Finance for ordinary names and ticker fragments. For a valid ISIN, first query OpenFIGI; then query Yahoo separately for each mapped ticker and accept only exact ticker/base-symbol matches. Never send the ISIN itself to Yahoo. Merge and deduplicate results without discarding distinct Yahoo listings.
4. Only offer symbols returned by Yahoo Finance search. Use OpenFIGI mappings to enrich a Yahoo candidate where the mapped ticker and listing can be matched; do not expose OpenFIGI-only mappings.
5. Return candidates for the user to choose from. Do not silently select the first mapping: one ISIN may have multiple listings, and provider coverage or symbol formats may differ.

## OpenFIGI configuration and unavailable cases

- Add an OpenFIGI section in Settings with a masked API-key input. Explain that OpenFIGI is always used for valid ISIN searches and that adding an API key provides a higher rate limit.
- If no API key is configured, send unauthenticated requests and clearly explain that they are subject to a lower rate limit.
- Store the key encrypted in the backend database only. Never return it to the frontend, include it in logs, or send it to Yahoo Finance. Keep the encryption key in the Home Assistant add-on options and preserve it across restarts/backups.
- Do not make a failed or empty OpenFIGI response look like a total search failure.
- If an OpenFIGI request fails, log provider, operation, status/error category, and duration without logging the key, request body, or ISIN. Continue with Yahoo results when available and surface a non-blocking notice that OpenFIGI results could not be loaded.
- If neither source finds candidates, keep the current no-results state. Do not create or cache unusable symbol candidates.

## Implementation steps

1. **Provider and result model**
   - Add an OpenFIGI adapter for ISIN mapping with explicit timeout, response validation, and external-call logging.
   - Preserve the OpenFIGI ticker and exchange as mapping metadata when they can be associated with a Yahoo Finance search result; never substitute a different listing just to obtain a quote.
   - Merge provider candidates deterministically, prefer exact ISIN matches when metadata is present, and tolerate missing ISIN metadata without treating it as a mismatch.
2. **Configuration and settings**
   - Add backend-only API-key configuration.
   - Extend settings read/write behavior and the Settings UI for optional key setup/update/removal and configured status. Never return the saved key; show only whether one is configured.
   - Ensure existing settings and searches continue to work when the feature has never been configured.
3. **Search, fallback, and caching**
   - Route valid ISIN queries through OpenFIGI first regardless of key configuration, then verify its mapped tickers through Yahoo; leave regular text searches unchanged.
   - Without a key, send an unauthenticated request. When OpenFIGI errors, preserve any Yahoo results and report the partial failure without exposing provider details or secrets.
   - Cache normalized ISIN results with a documented freshness policy. Invalidate or namespace relevant cached results when OpenFIGI enablement or credentials change, and avoid storing failed/incomplete results as successful empty results.
4. **Candidate selection UX**
   - Show symbol, name, exchange, and currency for each Yahoo Finance search result. Do not show OpenFIGI-only mappings.
   - Keep user confirmation mandatory and allow choosing among multiple listings. Preserve the import workflow's ISIN mismatch protection while accepting provider results that omit ISIN metadata.
   - Provide clear loading, no-results, disabled-provider, and partial-provider-failure states on both security search surfaces.
5. **Tests and documentation**
   - Test valid/invalid ISIN detection, mapping translation, multiple listings, deduplication, missing metadata, and exact-match preference.
   - Test valid ISIN lookup calls OpenFIGI without a key; test keyed requests and failures without leaking secrets.
   - Test settings persistence, enablement validation, key non-disclosure, cache freshness/invalidation, and manual candidate selection.
   - Update market-data and API documentation with provider behavior, opt-in requirements, fallback behavior, caching, and credential handling.

## Acceptance criteria

- Users can leave OpenFIGI disabled and continue searching through Yahoo Finance with no API key.
- Every valid ISIN lookup uses OpenFIGI. Users can add or remove an API key; keyless requests use the lower unauthenticated rate limit.
- A valid ISIN can yield multiple selectable, exchange-distinct candidates returned by the ISIN search or discovered by searching Yahoo Finance for mapped FIGI tickers.
- OpenFIGI-only mappings without a Yahoo Finance search match are not selectable.
- OpenFIGI outages do not prevent Yahoo results from being returned; errors are visible without exposing secrets or sensitive request data.
- Existing name/ticker search, result caching, and import symbol selection behavior remain intact.
