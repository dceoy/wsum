# Claude Code Routines: Google Workspace smoke test

This checklist verifies the **actual Routines environment**. Connector names and
marketing descriptions are not evidence that the required MCP operations or
binary payload sizes are supported. Do not schedule the workflow before passing
the checks below.

## Setup

1. Open the `dceoy/wsum` repository in Claude Code Routines. Confirm the
   `web-update-monitor` and `web-update-monitor-gws` skills are discoverable.
2. Supply **only** (a) an input targets table (local CSV path, Drive CSV URL/ID,
   or Google Spreadsheet URL/ID) and (b) one existing Drive **output folder**
   URL/ID. For a Spreadsheet, enable the Sheets connector; for Drive-hosted
   CSV, enable exact-ID binary download. The output folder must permit child
   listing, `workspaces/` and `reports/` creation, and file operations.
3. Configure a Python 3.11+ environment with `pypdf` installed (e.g. `uv sync`
   in the repository and run with `uv run python`).
4. Permit outbound network requests to the monitored public URLs. Trusted
   allowlists may block Python fetches even when the connector itself works.
5. Configure at most one active Routine per logical workspace. A schedule
   should not overlap its prior invocation; timestamp ordering is not a lock.

### Required connector operations

| Resource         | Required operations                                                             |
| ---------------- | ------------------------------------------------------------------------------- |
| Sheets (Spreadsheet input only) | Read spreadsheet metadata (ordered tabs), and range values preserving row/column positions |
| Input Drive CSV (when used) | Read file metadata and download complete CSV bytes by exact ID |
| Drive output     | Read folder by ID; list all direct children with IDs and MIME types; create child folders by parent ID |
| Drive workspaces | List all files with IDs; create binary ZIP; download binary by ID; delete by ID |
| Drive reports    | Search exact name; create Markdown; download by ID; update existing file by ID  |

If any required action is missing from the _actual Routine tool surface_, stop
and report the missing operation. Do not substitute text extraction for Sheets
values or Drive binary download.

## Minimal weekly Routine instruction

```text
/web-update-monitor-gws Run the weekly monitor with input targets table
<CSV_PATH_OR_DRIVE_CSV_OR_SPREADSHEET_URL> and output Drive folder <OUTPUT_FOLDER_URL>.
Follow the skill instructions; report material changes and failures.
```

Set the weekly schedule in Routines. The skill derives the first worksheet
and entire used range for Spreadsheet inputs by default, validates CSV inputs
with the core CSV parser, and resolves or creates `workspaces/` and `reports/`
under the output folder. No separate range or internal directory is required.

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

- For Spreadsheet input, confirm the first grid worksheet is automatically
  selected via metadata and its entire used range can be read, preserving
  headers, empty cells, duplicate URLs, multiline text and disabled rows.
  For CSV input, confirm original CSV bytes (local or exact-ID Drive download)
  validate with the core target loader; no Sheets connector is required.
- Resolve the exact output folder ID; list all its direct children. Resolve/reuse
  or create `workspaces/` and `reports/`, then re-list and verify child IDs.
  For a brand-new monitor, require an **empty** `workspaces/` folder. For an
  existing monitor, ensure its prior snapshots were moved into this folder
  before running; never silently reset the baseline.
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
8. Verify that duplicate `workspaces`/`reports` names, a same-name non-folder,
   inaccessible output folder, incomplete child listing, and an uncertain folder
   creation stop safely. A successful re-run reuses the same child folder IDs
   without creating duplicates.
9. Verify both CSV and Spreadsheet workflows. Reject malformed CSV, oversized
   CSV, unavailable input, wrong spreadsheet tab, and invalid headers without
   changing the cached projection or resuming pending reviews.

## Failure policy

Fail closed on missing connector capabilities, ambiguous parent/child folder
resolution, damaged binaries, ambiguous file IDs/names, validation errors,
stale Sheets projection, or a conflicting writer. Do not store Google tokens, cookies or API secrets in workspace ZIPs.
Keep connector and monitoring errors distinct.

This checklist is a **test plan**, not a claim that Anthropic's Google
connector has already passed the transfer and cross-session tests.
