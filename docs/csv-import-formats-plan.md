# Plan: configurable CSV import formats

## Goal

Let a user import CSV exports from different brokers, define how source columns and source transaction labels map to the app's canonical ledger, and save that configuration as a reusable import template. Keep the existing preview, row validation, duplicate detection, and explicit commit flow.

The transaction semantics in this plan follow the repository's documented rules in [transactions.md](./transactions.md). That document is the source of truth for ledger behavior; update it first if those semantics change.

## Original behavior and constraints

This section records the import flow before this plan was implemented.

- `POST /api/imports/csv/preview` in `backend/app/main.py` accepts a file and optional `field_map`, parses a canonical transaction CSV, validates rows, and persists a preview batch.
- `POST /api/imports/{batch_id}/commit` commits only valid preview rows. Fingerprints prevent identical transactions from being imported again.
- The frontend import dialog in `frontend/src/App.tsx` currently accepts a CSV and presents the preview, but does not offer format selection or template management.
- `backend/app/database.py` already creates/migrates the SQLite schema. Templates should persist there so they survive app restarts and work across browser devices.
- Keep the upload size limit, avoid logging CSV contents or transaction values, and keep the import dialog usable on narrow Home Assistant views.

## Implementation status

Implemented in the application: versioned SQLite templates and CRUD APIs, configurable CSV parsing and canonical field/type mapping, locale-aware numeric and date normalization, explicit gross trade-value interpretation, type-specific cash/fee/tax sign rules, reusable responsive UI, account override, preview batches that retain their template definition, and manual symbol selection for rows identified by ISIN or description. Backend coverage and the frontend production build pass.

## Product behavior

1. **Choose a format:** the import dialog offers the built-in canonical format and saved user templates. A user can also start with “Configure format”.
2. **Configure:** upload a sample CSV, select its delimiter and optional header row, map source columns to canonical fields, and specify supported normalization rules. Preview a few parsed rows while configuring.
3. **Set transaction rules:** map source type labels to canonical types and configure how signed source amounts are interpreted. Do not infer transaction types from amounts unless the user explicitly defines a deterministic rule in the template.
4. **Preview and commit:** use the selected template to parse the full file. Show canonical type, date, account, symbol, quantity, unit price, fees, taxes, cash amount, and row errors before commit. Invalid rows remain skipped; valid rows can be committed as today.
5. **Save/reuse:** allow saving a configured format with a user-chosen name; allow editing, duplicating, and deleting templates. Selecting a saved template should require only the CSV upload and any per-import choices such as account override.

## Canonical transaction semantics

Normalize every source row to the existing canonical transaction model before validation or fingerprinting. Templates describe mappings and deterministic conversions; they must not change ledger behavior.

| Canonical type | Required inputs and interpretation | Ledger effect |
| --- | --- | --- |
| `buy` | Symbol, positive quantity, positive unit price; fees and taxes may be signed | Adds shares; cash decreases by quantity × price + fees + taxes. Negative fees/taxes represent refunds. Fees affect cost basis; taxes do not affect P/L. |
| `sell` | Symbol, positive quantity, positive unit price; fees and taxes may be signed | Removes shares; cash increases by quantity × price − fees − taxes. Negative fees/taxes represent refunds. Fees affect realized P/L; taxes do not. |
| `split` | Symbol and positive split ratio in quantity; price/cash fields are ignored or zero | Multiplies the current share quantity by the ratio. |
| `transfer_in` | Symbol, positive quantity, and positive carrying cost basis per share | Adds shares and basis; no cash movement or realized P/L. |
| `transfer_out` | Symbol, positive quantity, and positive carrying cost basis per share | Removes shares and basis; no cash movement or realized P/L. Matching in/out rows preserve portfolio-wide quantity and basis. |
| `dividend` | Nonzero signed cash amount; fees/taxes may be signed | Cash increases by cash amount − fees − taxes. A negative amount corrects a previous distribution. Negative fees/taxes represent refunds. Dividend fees affect P/L; taxes do not. |
| `interest` | Nonzero signed cash amount; fees/taxes may be signed | Cash increases by cash amount − fees − taxes. A negative amount corrects interest income. Interest is kept separate from security dividends; fees affect portfolio return, taxes do not. |
| `fee` | Positive fee cash amount; taxes may be signed | Cash decreases by the fee amount and taxes; standalone fees do not affect security P/L. |
| `deposit` | Positive cash amount | Cash increases by the amount. |
| `withdrawal` | Positive cash amount | Cash decreases by the amount. |

Rules for import normalization:

- Convert source labels to a canonical type using an explicit, case-insensitive mapping saved with the template. Unknown labels invalidate that row.
- Canonical quantities are positive magnitudes. Deposits, withdrawals, and standalone fee amounts are positive magnitudes; dividend and interest cash amounts may be signed so a negative amount is an income correction. Source sign handling supports field defaults plus per-type overrides for cash amount, fees, and taxes. Convert a source negative withdrawal to a positive magnitude only when the withdrawal rule says absolute value; preserve signed dividend and interest corrections when configured. Never infer or flip transaction types from signs.
- Map source price to positive unit price, not total trade value. If the broker exports a trade value, require a template setting that says what the value represents and whether it includes fees and/or taxes. Support at least: trade principal before fees/taxes; buy cash debit including selected fees/taxes; and sell cash proceeds after selected fees/taxes. Derive unit price from the principal amount and quantity: for a buy debit, subtract included costs; for sell proceeds, add included costs back. Then divide by positive quantity. Reject a result that is zero or negative, and flag missing included fee/tax values rather than assuming them to be zero.
- Map fees and taxes independently; absent optional fields default to zero. Negative fees and taxes are valid signed adjustments when the selected per-type rule preserves them; the ledger applies them as refunds without treating tax refunds as P/L.
- Map account by exact account name by default. An optional account override can target a selected existing account for a single import; do not create accounts implicitly.
- Normalize currencies and symbols to uppercase, ISIN to uppercase, and transaction timestamps to UTC under the existing date rules. Preserve date-only values as date-only values.
- Apply mappings and conversions before duplicate fingerprinting so equivalent source formats normalize to the same duplicate identity. Preserve the existing duplicate behavior unless the fingerprint contract is separately revised.
- A semantic conversion that cannot be applied unambiguously must produce a row-level error in preview, never a guessed ledger event.

## Template model

Persist versioned template definitions, not uploaded files or sample transaction rows. A template should contain:

- `id`, `name`, `created_at`, `updated_at`, and `version`.
- CSV parsing settings: delimiter, quote character if configurable, encoding (initially UTF-8/UTF-8 BOM), header-row handling, and optional skipped leading rows.
- Field mappings from canonical fields to source headers. Required canonical fields depend on the mapped transaction type; validate that the template covers the fields needed by its mappings.
- Normalization settings: date and time formats (combined in one field or separately mapped), decimal separator, thousands separator, currency/symbol cleanup, optional blank-value defaults, field-level sign handling for quantity/price/gross value plus per-type overrides for cash amount/fees/taxes, and trade-value interpretation. The trade-value rule must identify whether the source field is principal, a buy debit, or net sell proceeds, plus whether fees and taxes are included. Only derive unit price when quantity and each included cost are available and valid.
- Type-label map from source values to canonical transaction types.
- Optional row filters only if narrowly defined and testable (for example, ignore rows where a selected source column exactly matches a configured value). Do not support executable expressions or arbitrary code.

Use JSON for the versioned mapping/rules payload and ordinary relational columns for template identity and timestamps. Validate payloads on create/update and again when loading templates. Keep a schema version so future changes can migrate or reject unsupported templates safely. Deleting a template must not delete import batches or transactions already created from it.

## Backend work

1. **Separate parsing from the route.** Add a CSV import module with typed template/rule models, template validation, CSV dialect parsing, row normalization, and row-level errors. Keep `main.py` routes thin.
2. **Add the template table and migration.** Add an `import_templates` table with a unique name, JSON definition, schema version, and UTC timestamps. Advance `PRAGMA user_version` through the established migration path. The built-in canonical format need not be stored as a user template.
3. **Template API.** Add list/create/read/update/delete endpoints. Return the validated definition, never sample file contents. Enforce name length and uniqueness, reject unsupported fields/rules, and use clear 4xx errors for invalid templates.
4. **Preview API.** Extend preview to accept either a saved template ID or an inline unsaved definition, but not both. Retain current behavior when neither is provided. Store the template ID and a snapshot of the effective versioned definition with the batch, so edits/deletion after preview cannot alter the rows awaiting commit. Recheck duplicates at commit as defense against concurrent imports.
5. **Audit and privacy.** Continue logging request method, route, status, and duration through the API middleware. Do not log CSV data, mapped transaction values, template payloads, or account names. Template API requests should be covered by the same metadata-only logging.

## Frontend work

1. Add a template chooser and “Configure format” entry to the import dialog.
2. Build a guided, responsive configuration view: CSV settings, column mapping, type mapping, numeric/date/sign rules, and a small parse preview. Use controls and examples rather than asking users to enter code.
3. Allow saving the current settings as a named template, and expose edit/duplicate/delete actions for saved templates with a clear confirmation before deletion.
4. Upload the source file and selected template reference (or unsaved definition) to preview. Keep the existing review table and commit step, enhancing it to surface the canonical cash amount and validation detail for cash events.
5. Keep controls reachable on mobile: stack configuration sections, horizontally scroll only the row preview table, and keep modal actions visible without hiding validation results.

## Delivery sequence

1. **Lock rules:** review this semantic table against [transactions.md](./transactions.md) and resolve any product decisions that are not currently defined there, including refunds, withholding tax, broker cash sign conventions, total trade value versus unit price, and date/time interpretation. Record agreed behavior in `transactions.md` before implementation.
2. **Backend primitives:** add template schema/migration and pure template validation/normalization functions. Preserve the existing canonical format via a built-in default definition.
3. **Backend APIs:** implement template CRUD and preview integration, including batch snapshotting and commit-time duplicate checks.
4. **Frontend configuration:** build template selection and editing, save/reuse, then connect preview and commit.
5. **Docs and examples:** update [transactions.md](./transactions.md) with template semantics, supported options, and a minimal mapping example; add a canonical sample CSV.
6. **Validation:** extend import API tests for templates, mappings, type rules, signs, locale numbers, dates, invalid/ambiguous rows, duplicates, and migration behavior. Add frontend checks for save/reuse and narrow layouts. Run backend tests through the VS Code **Run backend tests** task as required by [AGENTS.md](../AGENTS.md).

## Acceptance criteria

- Existing canonical CSVs still preview and commit without requiring a template.
- A user can configure, save, select, edit, duplicate, and delete a format template.
- Preview output always reflects canonical ledger semantics and clearly explains invalid rows before commit, including the selected trade-value interpretation and derived unit price.
- A gross trade-value mapping cannot be saved without an explicit interpretation of fee/tax inclusion, and derived unit prices use that interpretation consistently for buys and sells.
- Unknown transaction labels and ambiguous sign/value conversions never silently produce transactions.
- Re-importing equivalent normalized transactions is caught by duplicate detection.
- A preview remains tied to the template definition used to create it, even if that template is later edited or deleted.
- Templates persist across restart, do not store uploaded files or sample rows, and the workflow remains usable on mobile and desktop.
- API logs contain request metadata and duration without CSV, transaction, account, or template content.
