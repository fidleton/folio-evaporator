# Core Service Architecture

## Purpose

The core service is the trusted application boundary between the web UI, plugins, domain logic, and SQLite. It owns portfolio data, validates and persists changes, calculates derived portfolio views, and exposes versioned APIs to authorized plugins. Plugins never receive a database connection and cannot issue SQL.

This design assumes the single-user Home Assistant add-on described in the [high-level design](../README.md) and the SQLite model in [database architecture](database.md). Permissions separate plugin capabilities; they do not create separate users or private portfolios. Every enabled plugin operates on the same user's portfolio within its granted scopes.

## Responsibilities

- Authenticate each plugin as a distinct principal and authorize every API operation.
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
      Core HTTP API
       |          |
       |          +-- Plugin auth and scope authorization
       |          +-- Request validation and rate limits
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

Plugin clients -- versioned API + plugin credential --> Core HTTP API
```

The API is served by the add-on's core process. Plugin clients use the documented API over the add-on's internal or configured API address; API paths must work behind Home Assistant Ingress when used through that route. Keep plugin credentials out of browser storage and ordinary application logs. Core services call the same application-service layer as HTTP handlers, so authorization and domain rules cannot be bypassed by using a different transport.

Permission scopes limit API capabilities; they are not an operating-system sandbox. If plugins are not fully trusted, run them in separate restricted processes/containers with no database mount, no unnecessary network access, and only their own credential. Do not install untrusted plugin code in-process.

## Plugin Identity and Permission Grants

### Registration and approval

Each plugin has a stable, namespaced `plugin_id` and a manifest containing its display name, version, supported core API version, and requested permission scopes. The core validates manifest shape and supported API version. Manifest permissions are requests, not grants.

An administrator reviews requested scopes and grants only the minimum required set. The effective scope set is the intersection of the plugin's current manifest requests and active administrator grants. Newly requested scopes are denied until approved; removed scopes stop being effective immediately. Disabled plugins and revoked credentials are denied regardless of their grant rows.

Persist plugin metadata in `plugin_registry`, requested/granted/revoked scope state in `plugin_permission`, and credential hashes in `plugin_credential` as specified in [database architecture](database.md). Generate high-entropy opaque bearer tokens; return the raw token only once through a trusted provisioning path, store only a cryptographic hash, and support rotation, expiry, and revocation. Never accept a caller-supplied `plugin_id` as identity: resolve identity from the presented credential.

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

### Administrator control plane

Plugin approval and credential lifecycle use a separate administrator-only control plane authenticated through Home Assistant. Plugin bearer credentials must never authorize these operations. A plugin can submit or update its manifest request, but cannot grant itself scopes, enable itself, or issue credentials.

| Method and path | Administrator action |
| --- | --- |
| `GET /api/v1/admin/plugins` | Review registrations, requested scopes, current grants, and status. |
| `PATCH /api/v1/admin/plugins/{plugin_id}` | Enable or disable a registration. |
| `PUT /api/v1/admin/plugins/{plugin_id}/permissions/{scope}` | Grant a requested scope. Reject scopes not in the plugin's current manifest. |
| `DELETE /api/v1/admin/plugins/{plugin_id}/permissions/{scope}` | Revoke a grant immediately. |
| `POST /api/v1/admin/plugins/{plugin_id}/credentials` | Issue or rotate a credential and return its raw value once. |
| `DELETE /api/v1/admin/plugins/{plugin_id}/credentials/{credential_id}` | Revoke a credential. |

These routes require a verified Home Assistant administrator session and CSRF protection when invoked from the browser. If the add-on cannot reliably establish administrator identity from its configured Home Assistant integration, disable the control-plane routes and require grants through a trusted local configuration path; Ingress reachability alone is not proof of administrator authorization.

### Enforcement rules

- Deny by default. Every route declares required scope(s); missing, expired, revoked, disabled, or insufficient credentials fail before service execution.
- Enforce authorization in shared application services as well as HTTP middleware. Do not rely on hidden UI controls for access control.
- Validate resource IDs and all request fields after authorization. Enforce foreign keys, event-specific constraints, supported negative-balance rules, and fixed-point limits in the domain/repository path.
- Use TLS when credentials or data traverse an untrusted network. For local-only clients, bind to the narrowest interface possible and do not expose plugin credentials to the browser.
- Apply per-plugin request limits, payload limits, provider refresh quotas, timeouts, and bounded query ranges to limit accidental or hostile resource use.
- Return `401` for absent/invalid credentials, `403` for insufficient scope, `404` for unknown resources, `409` for domain conflicts, `422` for invalid fields, and `429` for limits. Errors must not include secrets, SQL, filesystem paths, or provider response bodies.
- Log plugin ID, operation, outcome, and request correlation ID for security-relevant writes and authorization denials. Redact bearer tokens, provider keys, and financial payload fields not needed for diagnostics.

## Plugin API

Expose a versioned JSON API under `/api/v1`. All endpoints are HTTPS when crossing an untrusted network and require a plugin bearer credential, except non-sensitive health checks used by Home Assistant. Responses use stable identifiers and UTC timestamps; monetary and share quantities follow the fixed-point representation in the database design. API documentation should include JSON schemas, scope requirements, pagination, limits, and deprecation policy.

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

The exact JSON payloads are an implementation contract to be defined in OpenAPI before a plugin API is considered stable. Paginate collection endpoints and cap time windows and result counts. Do not expose a generic database query, arbitrary SQL, filesystem, shell, credential, or schema-migration endpoint.

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
- Include plugin registry, grants, credential revocation state, ledger, provider metadata, and price history in consistent backups. Keep token hashes with the database but provision fresh raw credentials only through the trusted plugin installation flow.
- On restore, validate schema/integrity and require disabled or expired credentials to remain revoked. Provide an administrator path to revoke all plugin credentials after a suspected compromise.

## Initial Acceptance Criteria

1. A plugin with no credential, a revoked credential, or a disabled registration cannot call protected APIs.
2. A plugin can call only operations covered by both its manifest request and active administrator grant; revocation takes effect on its next request.
3. A plugin cannot spoof another plugin ID, read secret settings, issue SQL, or bypass service validation.
4. A valid transaction write persists atomically and changes derived portfolio output; invalid events leave the database unchanged.
5. A provider refresh failure leaves prior prices available, and historical API responses expose each retained sampling interval.
6. Plugin identities, grants, credential hashes, and financial data survive an add-on restart and a tested backup/restore cycle.
