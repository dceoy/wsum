# Local persistence setup

Local persistence mode replaces Google Sheets and Google Drive with one trusted
runtime directory. The fetch, normalization, diff, summary, and Slack delivery
semantics are identical to Google Drive mode.

## Runtime layout

Choose a runtime root outside the repository for production. For development,
`.runtime/` may be used because the repository already ignores it.

```text
<runtime-root>/
├── targets.json
├── operations.sqlite3
└── snapshots/
    └── <target_id>/
        └── <normalized_sha256>/
            ├── normalized.txt
            ├── metadata.json
            └── diff-<previous_sha256>.json
```

Only `targets.json` is required before the first run. `LocalOperationalStore`
creates `operations.sqlite3`, and `LocalSnapshotStore` creates `snapshots/` as
needed. The SQLite database stores current state, append-only run history, and
notification delivery state in separate tables.

## targets.json

`targets.json` is a JSON array. Each element uses the same fields and validation as
a row in the Google Sheets `Targets` table:

```json
[
  {
    "target_id": "example",
    "enabled": true,
    "name": "Example",
    "url": "https://example.com/",
    "fetch_mode": "static",
    "include_selector": "",
    "exclude_selectors": [],
    "watch_focus": "",
    "notification_group": "default"
  }
]
```

Target IDs must be unique. Browser mode must be explicitly approved; never switch a
target from static to browser automatically.

## Wiring

Construct `scripts/local_storage.py::LocalOperationalStore` and
`LocalSnapshotStore` with the same runtime root, then inject them into the
orchestration in `scripts/routine.py`. The caller supplies the summary client,
delivery adapter, `run_id`, and runtime root. Do not store credentials or destination
configuration in the repository.

The local backend changes persistence only; it does not impose a scheduling cadence.
The same runtime root may be used for ad hoc or externally scheduled invocations,
but overlapping invocations against the same target set are not supported.

## Persistence guarantees

SQLite primary keys preserve run-id idempotency and notification-event identity
without rewriting an ever-growing JSON history file. State replacement and grouped
notification updates use transactions, and SQLite serializes database writers across
processes. Individual stored JSON records remain size-bounded; aggregate run and
notification history is not subject to the former single-file 10 MB metadata cap.

SQLite does not make the complete monitor pipeline an atomic transaction. The
routine still performs claim checks before external fetch, snapshot, model, and
Slack side effects. Two overlapping routine invocations can therefore both pass a
claim check before either commits it. Keep invocations against the same target set
non-overlapping; see [security.md](security.md).

Snapshots use the same content-addressed layout as Drive. The stored
`snapshot_ref` is a relative path of the form
`snapshots/<target_id>/<sha256>/normalized.txt`. Loading rejects absolute paths,
path traversal, malformed hashes, symlinked files, and root escapes. Existing
snapshot artifacts are never overwritten with different bytes.

## Backup and retention

Back up `targets.json`, `operations.sqlite3`, and `snapshots/` as one unit if local
history must survive host loss. Do not copy only the database without the snapshots
referenced by current state. Apply snapshot retention only after confirming that the
current baseline remains present. Retain or archive operational database history
according to the deployment's audit policy.
