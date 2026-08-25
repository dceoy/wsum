# Local persistence setup

The local skill replaces Google Sheets and Google Drive persistence with one trusted
runtime directory. The monitoring algorithms and Slack delivery semantics remain
unchanged.

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

Use the local stores with the shared routine:

```python
from local_storage import LocalOperationalStore, LocalSnapshotStore
from routine import WeeklyMonitorRoutine

store = LocalOperationalStore(runtime_root)
snapshots = LocalSnapshotStore(runtime_root)
routine = WeeklyMonitorRoutine(
    store=store,
    snapshots=snapshots,
    summary_client=summary_client,
    slack=slack_connector,
)
result = routine.run(run_id=run_id)
```

`summary_client`, `slack_connector`, `run_id`, and `runtime_root` are supplied by the
caller. Do not store their credentials or destination configuration in the
repository.

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
