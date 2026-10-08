# Claude Code Routines: Google Workspace smoke test

This checklist verifies the **actual Routines environment**. Connector names and
marketing descriptions are not evidence that the required MCP operations or
binary payload sizes are supported. Do not schedule the workflow before passing
the checks below.

## Setup

1. Open the `dceoy/wsum` repository in Claude Code Routines. Confirm the
   `web-update-monitor` and `web-update-monitor-gws` skills are discoverable.
2. Configure the Google Sheets and Google Drive connectors with access only to
   the intended source spreadsheet and dedicated report/workspaces folders.
3. Configure a Python 3.11+ environment with `pypdf` installed (e.g. `uv sync`
   in the repository and run with `uv run python`).
4. Permit outbound network requests to the monitored public URLs. Trusted
   allowlists may block Python fetches even when the connector itself works.
5. Configure at most one active Routine per logical workspace. A schedule
   should not overlap its prior invocation; timestamp ordering is not a lock.

### Required connector operations

| Resource         | Required operations                                                             |
| ---------------- | ------------------------------------------------------------------------------- |
| Sheets           | Read a specified value range into a row/column-preserving cell array            |
| Drive workspaces | List all files with IDs; create binary ZIP; download binary by ID; delete by ID |
| Drive reports    | Search exact name; create Markdown; download by ID; update existing file by ID  |

If any required action is missing from the _actual Routine tool surface_, stop
and report the missing operation. Do not substitute text extraction for Sheets
values or Drive binary download.

## 1. Verify binary transfer before monitoring

Using the local helper, build a disposable workspace containing
`output/` and `internal/` files, then create a named snapshot:

```bash
python skills/web-update-monitor-gws/scripts/gws.py pack \
  --workspace "$TEST_WORKSPACE" \
  --generation 20261009T000000Z \
  --archive "$SCRATCH/workspace-20261009T000000Z.zip"
```

Use the connector to upload it _as bytes_, then download the same file by its
returned Drive ID. Save the downloaded binary to a local file with the
original snapshot filename. Independently compare byte length and SHA-256
against the `pack` command output, then run:

```bash
python skills/web-update-monitor-gws/scripts/gws.py verify \
  --archive "$DOWNLOADED/workspace-20261009T000000Z.zip"
```

Repeat with ZIP payloads around **10 KiB, 100 KiB and 1 MiB**. Use
incompressible test data so the _ZIP sizes_, not just source sizes, satisfy
the test. Any size, integrity, or verification failure is a blocker for
that monitor's actual workspace size. A successful connector upload response
is insufficient. Delete all throwaway files when done.

## 2. Initialize without writing operational state prematurely

- Confirm the target Google Sheet can be read by range, preserving headers,
  empty cells, duplicate URLs, multiline text and disabled rows.
- Select an **empty** dedicated workspaces folder, and an independent
  Markdown report folder.
- Persist a new initial workspace ZIP containing a valid
  `internal/gws/delivery.json` with `{"version":1,"reports":{}}`, plus the
  validated Sheet projection, **before** any core `check`.
- Verify uploaded bytes by exact ID before treating this generation as durable.

## 3. Test a complete cross-session workflow

1. Start a manual Routine run; generate an initial baseline. Expect no
   material-change report on the first observation.
2. End that session. Run again in a **fresh** session and restore the latest
   ZIP by its ID and filename, using `gws.py restore` into a new workspace.
   Unchanged content must not be reported as a first observation.
3. Change a controlled target. Verify review/finalize and one Markdown report
   per completed run; pending review must block early report publication.
4. Check byte-identical Markdown read-back before `gws.py ledger --record`.
   Repeat the run and verify that no duplicate report file is created.
5. Verify at most three committed ZIP generations remain. Force an incomplete
   upload or corrupt ZIP and ensure the workflow does not silently reset the
   baseline or fall back to an older generation.
6. Simulate duplicate snapshot filenames or a changed latest generation
   between restore and save; the workflow must stop without deleting a
   previously committed snapshot.
7. Verify that missing Sheets permission, expired connector authorization,
   and an egress-denied HTTP 403 produce explicit errors, not a successful
   empty report or a new baseline.

## Failure policy

Fail closed on missing connector capabilities, damaged binaries, ambiguous
file IDs/names, validation errors, stale Sheets projection, or a conflicting
writer. Do not store Google tokens, cookies or API secrets in workspace ZIPs.
Keep connector and monitoring errors distinct.

This checklist is a **test plan**, not a claim that Anthropic's Google
connector has already passed the transfer and cross-session tests.
