# Data model

Google Sheets is authoritative for configuration and operational state. Google
Drive is authoritative for normalized snapshots and bounded diff artifacts. GitHub
contains code, schemas, documentation, and non-sensitive test fixtures only.

## Sheets

Keep the first row as a unique header row. Required columns may have additional
deployment-owned columns after them.

### Targets

| Column | Meaning |
| --- | --- |
| `target_id` | Stable key; letters, digits, dot, underscore, and hyphen only |
| `enabled` | Boolean gate |
| `name` | Human-readable label |
| `url` | Public HTTP(S) URL without credentials |
| `fetch_mode` | `static` or explicitly approved `browser` |
| `include_selector` | Optional strict, supported CSS subset |
| `exclude_selectors` | Comma-separated strict selector list |
| `watch_focus` | Bounded guidance for materiality |
| `notification_group` | Logical destination key, never a channel secret |

### State

`target_id`, `last_checked_at`, `etag`, `last_modified`, `normalized_hash`,
`snapshot_ref`, `consecutive_failures`

Replace one target row as a unit. Preserve `normalized_hash` and `snapshot_ref` on
any fetch, parse, summary, Drive, or notification failure. Reset
`consecutive_failures` only after a successful terminal check.
Use Sheets `RAW` value input semantics for every write so untrusted validators or
summary text can never be interpreted as a formula.

### Runs

`run_id`, `target_id`, `result`, `change_score`, `summary`, `error_code`,
`started_at`, `finished_at`

Use `<routine_run_id>:<target_id>` as the idempotency key. The bounded `summary`
cell may encode attempt outcomes as compact JSON. A connector must not append a
second row for an existing run ID.

### Notifications

`event_id`, `target_id`, `status`, `notified_at`

`status` is `pending`, `sent`, `failed`, or `suppressed`. Derive a change event ID
as `SHA256(target_id + normalized_hash)`. Check the row before delivery and update
it after delivery. Never automatically retry `pending`, because delivery may have
succeeded before the final state write.

### Optional Outbox

`event_id`, `target_id`, `payload`, `status`, `attempt_count`, `created_at`,
`updated_at`, `next_attempt_at`, `last_error`

Allowed states are `pending`, `sending`, `sent`, `retry`, and `poison`. Persist
`sending` before delivery. Do not auto-retry an interrupted `sending` row.
Use `OutboxSheetsStore` to query/upsert rows with RAW value semantics. A queued
event is durable enough for monitoring state to advance, but it is not counted as
`notified` until the dispatcher records `sent`.

## Drive

Use deterministic paths:

```text
snapshots/<target_id>/<normalized_hash>/normalized.txt
snapshots/<target_id>/<normalized_hash>/metadata.json
snapshots/<target_id>/<normalized_hash>/diff.json
```

Look up each path before upload. Update `State.snapshot_ref` only when all required
writes succeed. Keep at least 12 snapshots by default, never delete the currently
referenced baseline, and generate a cleanup plan before deleting anything.

Drive files contain normalized text, format metadata, and bounded diffs only. They
must not contain connector configuration, credentials, unrelated fetched content,
or raw HTML.

## Prohibited GitHub data

Never commit:

- Raw or rendered HTML, fetched responses, PDFs, feeds, screenshots, or page dumps.
- Normalized production snapshots, production diffs, run logs, audit exports, or
  Sheets/Drive exports.
- Credentials, cookies, browser profiles, `.env` files, service-account keys,
  OAuth tokens, API keys, webhook URLs, signed URLs, or spreadsheet/Drive IDs.
- Local workspaces, replay manifests containing operational content, caches, or
  delivery payloads.
