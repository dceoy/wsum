# Data model

The monitor has one logical data model with two persistence mappings. Select exactly
one mode for a run.

- `google-drive`: Google Sheets is authoritative for targets and operational state;
  Google Drive is authoritative for normalized snapshots and bounded diff artifacts.
- `local`: `targets.json` is authoritative for target configuration; local JSON
  files are authoritative for operational state; the local `snapshots/` tree stores
  normalized snapshots and bounded diffs.

GitHub contains code, schemas, documentation, and non-sensitive test fixtures only.
Production monitoring data never belongs in the repository.

## Shared records

Both modes use the schemas under `schemas/` and the same logical fields.

### Targets

| Field                | Meaning                                                       |
| -------------------- | ------------------------------------------------------------- |
| `target_id`          | Stable key; letters, digits, dot, underscore, and hyphen only |
| `enabled`            | Boolean gate                                                  |
| `name`               | Human-readable label                                          |
| `url`                | Public HTTP(S) URL without credentials                        |
| `fetch_mode`         | `static` or explicitly approved `browser`                     |
| `include_selector`   | Optional strict, supported CSS subset                         |
| `exclude_selectors`  | Strict selector list                                          |
| `watch_focus`        | Bounded guidance for materiality                              |
| `notification_group` | Logical destination key, never a channel secret               |

### State

`target_id`, `last_checked_at`, `etag`, `last_modified`, `validated_url`,
`normalized_hash`, `snapshot_ref`, `consecutive_failures`

Replace one target state as a unit. Preserve `normalized_hash` and `snapshot_ref` on
any fetch, parse, summary, snapshot, or notification failure. Reset
`consecutive_failures` only after a successful terminal check.

### Runs

`run_id`, `target_id`, `result`, `change_score`, `summary`, `error_code`,
`started_at`, `finished_at`

Use `<routine_run_id>:<target_id>` as the idempotency key. The bounded `summary`
field may encode attempt outcomes as compact JSON. The operational store must not
create a second record for an existing run ID, and the routine checks for an
existing terminal record before fetching, writing state, or notifying.

### Notifications

`event_id`, `target_id`, `status`, `notified_at`, `kind`, `last_error`

`status` is `pending`, `sent`, `failed`, or `suppressed`. `kind` is `change` or
`failure`. Derive a change event ID as `SHA256(target_id + normalized_hash)`.
Never automatically retry `pending`, because delivery may have succeeded before the
final state write.

### Optional Outbox

`event_id`, `target_id`, `payload`, `status`, `attempt_count`, `created_at`,
`updated_at`, `next_attempt_at`, `last_error`

Allowed states are `pending`, `sending`, `sent`, `retry`, and `poison`. Persist
`sending` before delivery through the dispatcher's required persistence callback;
abort without calling the sender if that write fails. Do not auto-retry an
interrupted `sending` record.

## Google Drive mode

Keep the first Google Sheets row as a unique header row. Required columns may have
additional deployment-owned columns after them.

- `Targets`: target fields above. `exclude_selectors` is stored as a comma-separated
  string.
- `State`: one current row per target.
- `Runs`: append-only terminal run records.
- `Notifications`: one row per event; read and write all six columns so `kind` and
  `last_error` are preserved.
- Optional `Outbox`: use `OutboxSheetsStore` with RAW value semantics.

Use Sheets `RAW` value input semantics for every write so untrusted validators or
summary text cannot be interpreted as formulas. Grouped notification delivery must
persist every event in one Slack chunk through one all-or-nothing connector batch.

Use deterministic Drive paths:

```text
snapshots/<target_id>/<normalized_hash>/normalized.txt
snapshots/<target_id>/<normalized_hash>/metadata.json
snapshots/<target_id>/<normalized_hash>/diff-<previous_hash>.json
```

Look up each path before upload. Update `State.snapshot_ref` only when all required
writes succeed. Never delete the currently referenced baseline.

## Local mode

Use one trusted runtime root outside the repository:

```text
<runtime-root>/
├── targets.json
├── state.json
├── runs.json
├── notifications.json
└── snapshots/
```

`targets.json` is the only file required before the first run. The local adapters
create the other JSON files and snapshot directories as needed using bounded reads,
validated relative paths, and atomic replacement. See [local-setup.md](local-setup.md)
for the exact layout and wiring.

## Prohibited GitHub data

Never commit:

- Raw or rendered HTML, fetched responses, PDFs, feeds, screenshots, or page dumps.
- Normalized production snapshots, production diffs, run logs, audit exports,
  Sheets/Drive exports, or local runtime data.
- Credentials, cookies, browser profiles, `.env` files, service-account keys,
  OAuth tokens, API keys, webhook URLs, signed URLs, or spreadsheet/Drive IDs.
- Local workspaces, replay manifests containing operational content, caches, or
  delivery payloads.
