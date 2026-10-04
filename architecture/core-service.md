# Core Service Architecture

## Purpose

The core service is the trusted application boundary between the web UI, plugins, domain logic, and SQLite. It owns portfolio data, validates and persists changes, calculates derived portfolio views, and exposes versioned APIs to authorized plugins. Plugins never receive a database connection and cannot issue SQL.

This design assumes the single-user Home Assistant add-on described in the [high-level design](../README.md) and the SQLite model in [database architecture](database.md). Permissions separate plugin capabilities; they do not create separate users or private portfolios. Every enabled plugin operates on the same user's portfolio within its granted scopes.

## Responsibilities

- Bind each installed plugin to a distinct host identity and check its granted scopes for every host-interface operation.
- Validate requests, enforce domain invariants, and persist writes atomically through repository interfaces.
- Own transaction-ledger, account, security, quote, and provider business rules.
- Calculate positions, cost basis estimates, valuation, allocation, and historical summaries from persisted source records.
- Coordinate provider adapters and scheduled/manual price refreshes without discarding last-known-good observations.
- Apply SQLite migrations at startup, enable foreign keys on each connection, and provide backup/restore-safe access to `/data/folio.db`.
- Return stable, documented, versioned plugin APIs and consistent error responses.

The core does not execute plugin-supplied SQL, load arbitrary native modules into the database process, store provider secrets in ordinary settings, or expose the database file to plugins.

## Runtime Shape

```text
Home Assistant user / Ingress
             |
             v
        Core process
         |          |
         |          +-- Plugin host and installed plugins
         |          +-- HTTP API, validation, and rate limits
         v
   Application services
   |       |         |
   |       |         +-- Market-data adapters / refresh worker
   |       +-- Portfolio calculations
   +-- Account, security, transaction services
             |
       Repository layer
             |
      SQLite at /data/folio.db

Installed plugins -- versioned, scope-checked plugin interface --> Plugin host
```

Plugins are installed and run inside the core process. They call core capabilities through a versioned host interface rather than authenticating to the core with network bearer credentials. The HTTP API serves the browser and any explicitly supported remote clients; its paths must work behind Home Assistant Ingress. Both the plugin interface and HTTP handlers call the same application-service layer, which owns domain validation and persistence.

Installation is the trust boundary: installed plugin code has the privileges of the core process and is not isolated by an operating-system sandbox. Permission scopes restrict capabilities exposed by the host interface and communicate user-approved access; they do not contain malicious plugin code. Plugins must use the host interface rather than accessing SQLite directly. Do not present permission grants as a security boundary against a plugin the user has installed and trusted.

## Plugin Identity and Permission Grants

### Registration and approval

Each plugin has a stable, namespaced `plugin_id` and a non-executable manifest containing its display name, plugin version, required plugin API major version, and requested permission scopes. The core validates the manifest and rejects or disables plugins requiring an unsupported API major. Within an API major, changes are backward-compatible; breaking changes require a new major. Manifest permissions are requests, not grants. Installation, configuration, update, enable/disable, and removal are managed from the application UI. Applying an installation or update requires restarting the core process; plugins are not hot-reloaded.

The user reviews requested scopes and grants only the minimum required set in the UI. Every scope declared by a plugin is required for activation; if any are declined or revoked, the plugin is disabled. The effective scope set is the intersection of the plugin's current manifest requests and active user grants. Newly requested scopes are denied until approved; removed scopes stop being effective immediately. Disabled plugins are denied regardless of their grant rows. The application must authenticate the user before allowing plugin installation or permission changes.

Persist plugin metadata in `plugin_registry`, requested/granted/revoked scope state in `plugin_permission`, and namespaced plugin data in `plugin_storage` as specified in [database architecture](database.md). The plugin host binds calls to a registered plugin identity; plugins cannot choose or spoof their identity in a call.

### Permission scopes

The following initial scopes are deliberately resource-oriented:

| Scope | Permitted operations |
| --- | --- |
| `portfolio:read` | Read derived positions, allocation, valuation, and portfolio summaries. |
| `accounts:read` | List and inspect accounts. |
| `accounts:write` | Create, update, and archive accounts. Cannot delete accounts with ledger history. |
| `securities:read` | List and inspect securities and provider-symbol mappings. |
| `securities:write` | Create, update, and archive securities and mappings. |
| `transactions:read` | Read transaction history. |
| `transactions:write` | Record buy, sell, dividend, and split events. |
| `transactions:correct` | Reverse and replace a previously recorded event. Corrections are atomic and auditable; no hard delete or silent overwrite. |
| `prices:read` | Read quote observations, history intervals, freshness, and provider metadata. |
| `prices:refresh` | Request a bounded refresh for allowed securities/providers. Cannot change provider credentials or refresh policy. |
| `settings:read` | Read explicitly allowlisted, non-secret settings. |
| `settings:write` | Change explicitly allowlisted non-secret settings only. |

No wildcard scope is granted. A scope does not imply another scope: for example, `portfolio:read` does not allow reading transaction notes, while `transactions:read` does not allow changing transactions. Secret provider credentials are never returned by any scope. The core owns an allowlist of writable setting keys; plugins cannot write arbitrary `app_setting` rows.

### Plugin management UI

The authenticated application UI is the control plane for plugin lifecycle and permission management. A plugin can declare or update its manifest request, but cannot grant itself scopes or enable itself. Permission changes take effect on the next host-interface call. The UI must validate a Home Assistant-authenticated session server-side and use CSRF protection for state-changing browser requests; Ingress reachability or caller-supplied headers alone are not authentication. Credential issuance and rotation are not part of the in-process plugin model.

The UI must show the plugin's identity, publisher/source, version, required API version, requested scopes, granted scopes, and current status before enabling it. Installation is an explicit trust decision. The plugin manager validates the package structure and manifest before activation, stages package changes, and does not execute newly installed or updated code before the user confirms installation and grants required scopes.

### Installation, Updates, and Failures

- Install and update actions stage a package, validate its manifest and API compatibility, and present changed permission requests before activation. Newly requested scopes remain denied until approved. A plugin with an unsupported API major or unapproved required scopes remains disabled.
- The user explicitly enables an installed plugin. At startup, the host loads only enabled plugins whose API version and granted scopes are valid. Disabling or revoking a scope blocks new host-interface calls immediately and cancels plugin-owned scheduled work where possible.
- Applying an installation or update requires a core-process restart. Keep the last known-good package until the new version has initialized successfully. If an update fails initialization, restore and load the prior version; if a first install fails, leave the plugin disabled. Report either failure in the UI and sanitized logs.
- Catch plugin exceptions at lifecycle and host-call boundaries so ordinary plugin failures are reported and do not corrupt core state. Disable a plugin after a failed startup; isolate a failed call and return a sanitized error. Require plugin work to be asynchronous and cooperative, with bounded host calls and cancellation for scheduled tasks.
- In-process execution cannot safely interrupt a plugin that blocks the event loop, exhausts process memory, or triggers a fatal runtime failure. Plugins are trusted code, not isolated workloads; such failures can take down the core and rely on the Home Assistant supervisor to restart it. The core must preserve database integrity across restart.
- Removing a plugin disables it and removes its package, but retains its namespaced data. The UI offers a separate, explicit purge action; reinstalling the same `plugin_id` can reuse retained data.

### Enforcement rules

- Deny by default. Every plugin host capability declares required scope(s); uninstalled, disabled, API-incompatible, or insufficiently granted plugins cannot invoke it.
- Check plugin grants at the host-interface boundary and enforce domain invariants in shared application services. Do not rely on hidden UI controls for authorization. These checks are capability controls for trusted code, not a sandbox.
- Validate resource IDs and all request fields after authorization. Enforce foreign keys, event-specific constraints, supported negative-balance rules, and fixed-point limits in the domain/repository path.
- Use TLS when API credentials or data traverse an untrusted network. Do not expose Home Assistant or provider credentials to plugins through the host interface.
- Apply per-plugin request limits, payload limits, provider refresh quotas, timeouts, and bounded query ranges to limit accidental or hostile resource use.
- For HTTP APIs, return `401` for absent/invalid credentials, `403` for insufficient scope, `404` for unknown resources, `409` for domain conflicts, `422` for invalid fields, and `429` for limits. Errors must not include secrets, SQL, filesystem paths, or provider response bodies.
- Log plugin ID, operation, outcome, and request correlation ID for security-relevant writes and denied capability calls. Redact API/provider credentials and financial payload fields not needed for diagnostics.

## Plugin API

Expose the plugin contract as a versioned host interface. The table below gives the corresponding operation names and optional HTTP routes; in-process plugins call matching host-interface methods, not HTTP bearer endpoints. Any HTTP adapter for remote clients is a separate API surface and must define its own authentication. Responses use stable identifiers and UTC timestamps; monetary and share quantities follow the fixed-point representation in the database design. Document host-interface types, scope requirements, pagination, limits, and deprecation policy.

| Method and path | Required scope | Behavior |
| --- | --- | --- |
| `GET /api/v1/portfolio/summary` | `portfolio:read` | Derived value, allocation, quote freshness, and as-of timestamp. |
| `GET /api/v1/portfolio/positions` | `portfolio:read` | Derived positions, optionally filtered by account or security. |
| `GET /api/v1/accounts` | `accounts:read` | List active accounts; optional include-inactive filter. |
| `POST /api/v1/accounts` | `accounts:write` | Create an account. |
| `PATCH /api/v1/accounts/{id}` | `accounts:write` | Update account metadata or archive it. |
| `GET /api/v1/securities` | `securities:read` | List securities and provider mappings. |
| `POST /api/v1/securities` | `securities:write` | Create a security and optional provider mappings. |
| `PATCH /api/v1/securities/{id}` | `securities:write` | Update metadata or archive it. |
| `GET /api/v1/transactions` | `transactions:read` | Paginated transaction history, with bounded filters. |
| `POST /api/v1/transactions` | `transactions:write` | Record one ledger event. Validate and commit atomically. |
| `POST /api/v1/transactions/{id}/corrections` | `transactions:correct` | Atomically reverse an event and record its replacement. |
| `GET /api/v1/prices/history` | `prices:read` | Query observations by security, provider, and time range; return `history_interval` and quote age. |
| `POST /api/v1/prices/refresh` | `prices:refresh` | Request a bounded refresh; return per-security/provider outcome and preserve prior successful quotes on failure. |
| `GET /api/v1/providers` | `prices:read` | List enabled provider metadata, attribution, and refresh limits; never return credentials. |
| `GET /api/v1/settings` | `settings:read` | Read only allowlisted non-secret settings. |
| `PATCH /api/v1/settings` | `settings:write` | Update only allowlisted non-secret settings. |

The exact host-interface types and, if provided, JSON payloads are an implementation contract to define before a plugin API is considered stable. Paginate collection operations and cap time windows and result counts. Do not expose a generic database query, arbitrary SQL, filesystem, shell, credential, or schema-migration operation.

## Persistence and Consistency

- Application services own write transactions. Validate first, then persist the event and related correction records in one SQLite transaction; commit all or none.
- Reads of derived views use a consistent database snapshot. Calculate positions from the immutable ledger and market value from the latest eligible stored price. Any future cache must be rebuildable and disposable.
- Provider responses are normalized by adapters before persistence. Associate every observation with a registered `provider_id`, security mapping, quote currency, `observed_at`, `fetched_at`, and `history_interval`.
- Price refreshes are idempotent on `(security_id, provider_id, observed_at)`. A failed refresh updates sanitized refresh state but does not remove the last good observation.
- Historical compaction follows the tiered policy in [database architecture](database.md), runs transactionally, and retains the newest observation. APIs disclose the retained interval; they never imply that a monthly/yearly point is daily data.
- Apply ordered schema migrations before serving APIs. On migration/database failure, fail visibly and preserve `/data/folio.db`; never silently recreate or truncate it.

## Operational Requirements

- Expose readiness only after migrations succeed; health responses must not disclose database contents or secrets.
- Make API version, plugin identity, operation, latency, and error class observable. Avoid logging access tokens and portfolio values by default.
- Include plugin registry, grants, plugin storage, ledger, provider metadata, and price history in consistent backups.
- On restore, validate schema/integrity and preserve plugin disabled and permission-revocation state.

## Initial Acceptance Criteria

1. An uninstalled, disabled, or API-incompatible plugin cannot invoke host capabilities.
2. A plugin can call only operations covered by both its manifest request and active user grant; revocation takes effect on its next host-interface call.
3. A plugin cannot spoof another plugin ID, read secret settings through the host interface, issue SQL through supported interfaces, or bypass service validation.
4. A valid transaction write persists atomically and changes derived portfolio output; invalid events leave the database unchanged.
5. A provider refresh failure leaves prior prices available, and historical API responses expose each retained sampling interval.
6. Plugin identities, grants, and financial data survive an add-on restart and a tested backup/restore cycle.
