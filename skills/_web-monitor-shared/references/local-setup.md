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
├── state.json
├── runs.json
├── notifications.json
└── snapshots/
    └── <target_id>/
        └── <normalized_sha256>/
            ├── normalized.txt
            ├── metadata.json
            └── diff-<previous_sha256>.json
```

Only `targets.json` is required before the first run. `state.json`, `runs.json`,
`notifications.json`, and `snapshots/` are created as needed by
`LocalOperationalStore` and `LocalSnapshotStore`.

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

Operational JSON updates are serialized within the process and written by atomic
rename. Run IDs remain append-only/idempotent, notification event IDs remain
upserted/deduplicated, and state is replaced only after the routine has reached its
normal commit point.

Snapshots use the same content-addressed layout as Drive. The stored
`snapshot_ref` is a relative path of the form
`snapshots/<target_id>/<sha256>/normalized.txt`. Loading rejects absolute paths,
path traversal, malformed hashes, symlinked files, and root escapes. Existing
snapshot artifacts are never overwritten with different bytes.

## Backup and retention

Back up the entire runtime root as one unit if local history must survive host loss.
Do not copy only `state.json` without the referenced snapshots. Apply retention only
after confirming that the current baseline's snapshot remains present.
