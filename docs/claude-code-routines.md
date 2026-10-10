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
   CSV, require binary download plus `size` and `md5Checksum` metadata.
   The output folder must permit child listing, `workspaces/` and `reports/`
   creation, and an immutable binding JSON at the parent level.
3. Configure a Python 3.11+ environment with `pypdf` installed (e.g. `uv sync`
   in the repository and run with `uv run python`).
4. Permit outbound network requests to the monitored public URLs. Trusted
   allowlists may block Python fetches even when the connector itself works.
5. Configure at most one active Routine per logical workspace. A schedule
   should not overlap its prior invocation; timestamp ordering is not a lock.

### Required connector operations

| Resource                        | Required operations                                                                                    |
| ------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Sheets (Spreadsheet input only) | Read spreadsheet metadata (ordered tabs), and range values preserving row/column positions             |
| Input Drive CSV (when used)     | Read exact-ID file size, MD5 checksum and version; download complete CSV bytes by ID                    |
| Drive output                    | Read folder by ID; list children with IDs/MIME; create folders and immutable binding file              |
| Drive workspaces                | List all files with IDs; create binary ZIP; download binary by ID; delete by ID                        |
| Drive reports                   | Search exact name; create Markdown; download by ID; update existing file by ID                         |

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
  For a local CSV, validate its exact bytes with the core loader. For a Drive
  CSV, require `size` and `md5Checksum`, download by exact ID, and validate
  both fields against the downloaded bytes **before** projecting the CSV.
- List the exact output folder's complete direct children. For a new monitor
  require **no** existing `workspaces`, `reports` or binding file. Create both
  child folders by parent ID and re-list to verify their IDs. An existing
  binding must point to the exact child IDs, and an existing unbound layout
  must **not** be automatically adopted or reset.
- For a new monitor, persist/read-back-verify the initial workspace ZIP with
  a valid `internal/gws/delivery.json` (`{"version":1,"reports":{}}`) and
  validated targets before any `check`.
- Only **after** verifying that ZIP, create
  `web-update-monitor-gws.binding.json` in the parent with the exact
  parent/workspaces/reports IDs, then download and validate its bytes and IDs.
  Never recreate a missing binding over existing child folders.

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
8. Verify that missing or duplicated binding files, replaced/renamed child
   folders, empty bound `workspaces/`, inaccessible output folder, incomplete
   listings and ambiguous creates **stop**, with no baseline reset. Verify that
   a healthy later Routine resumes by the originally bound folder IDs.
9. Verify both CSV and Spreadsheet workflows. Reject malformed CSV, oversized
   CSV, unavailable input, wrong spreadsheet tab, and invalid headers without
   changing the cached projection or resuming pending reviews.
10. Download a syntactically valid **row-truncated Drive CSV** and require the
    expected-size/MD5 verification to reject it before reconciling pending
    state. Also reject equal-size altered CSV and missing checksum metadata.

## Failure policy

Fail closed on missing connector capabilities, missing/mismatched folder bindings,
ambiguous parent/child IDs, damaged binaries, missing or mismatched CSV checksum,
validation errors, stale input projection, or a conflicting writer. Do not store
Google tokens, cookies, or API secrets in workspace ZIPs.
Keep connector and monitoring errors distinct.

This checklist is a **test plan**, not a claim that Anthropic's Google
connector has already passed the transfer and cross-session tests.
