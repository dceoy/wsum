# Claude Code Routines: Google Workspace smoke test

This checklist verifies the **actual Routines environment**. Connector names and
marketing descriptions are not evidence that the required MCP operations or
binary payload sizes are supported. Do not schedule the workflow before passing
the checks below. In particular, a connector that exposes binary data only as
base64 in agent-visible text is **incompatible** and must fail before any input
download or operational workspace creation.

## 0. Mandatory file-backed transport preflight

Inspect the real connector tool signatures before fetching the input CSV or
context, restoring snapshots, or creating managed folders. Require an exact-ID
Drive upload **from a local file** and an exact-ID Drive download **to a local
file**, both bypassing the model's output. A genuine file-reference handoff or
mounted/authenticated Drive directory is acceptable if bytes are staged and
read programmatically. A tool returning a base64 string to the agent, or
accepting a base64 argument composed by the agent, does not satisfy this,
even when the payload is just 10 KiB. **Never transcribe base64**, split it
into text chunks, recreate a CSV from rendered content, or fall back to
`base64 -d` on agent-written text.

Verify that the file-backed route supports at least 1 MiB in both directions
and the actual anticipated ZIP size (local ZIPs are bounded to 128 MiB).
If either direction lacks file-backed transfer or payload support is uncertain,
**stop here** with an explicit capability error and no Drive writes. In
particular, Cowork/Claude environments exposing only `create_file`,
`download_file_content`, and model-visible base64 are not compatible.
Installing a truly file-backed connector or mounting Drive is a prerequisite,
not a workaround available through another `gws.py` flag.

## Setup

1. Open the `dceoy/wsum` repository in Claude Code Routines. Confirm the
   `web-update-monitor`, `web-update-research`, and `web-update-report-gws` skills are discoverable.
2. Supply the **two required resources**: (a) an input targets table (local CSV path, Drive CSV URL/ID,
   or Google Spreadsheet URL/ID) and (b) one existing Drive **output folder**
   URL/ID. You may omit common context entirely, or optionally include **either** `Common context:` inline or `Context source:` selecting a Google Doc or Drive Markdown file. For a Spreadsheet, enable the Sheets connector; for Drive-hosted
   CSV, require file-backed binary download plus `size` metadata.
   The output folder must permit child listing, `workspaces/` and `reports/`
   creation, and an immutable binding JSON at the parent level.
3. Configure a Python 3.11+ environment with `pypdf` installed (e.g. `uv sync`
   in the repository and run with `uv run python`).
4. Check public-web search and individual source-opening support for follow-up research. If unavailable, require an explicit fact-only report with independent verification marked unavailable.
5. Permit outbound network requests to the monitored public URLs. Trusted
   allowlists may block Python fetches even when the connector itself works.
6. When `Context source:` is provided, verify complete authenticated Google Docs body/plain-text export or exact-ID Drive Markdown download. Missing, unreadable, empty, invalid UTF-8, unsupported or oversized (>128 KiB normalized) context must stop the run **before** any monitoring mutations; never use snippets or fall back to cached context.
7. Configure at most one active Routine per logical workspace. A schedule
   should not overlap its prior invocation; timestamp ordering is not a lock.

### Required connector operations

| Resource                        | Required operations                                                                        |
| ------------------------------- | ------------------------------------------------------------------------------------------ |
| Sheets (Spreadsheet input only) | Read spreadsheet metadata (ordered tabs), and range values preserving row/column positions |
| Input Drive CSV (when used)     | Read exact-ID file size and version; download bytes by ID directly to a local file         |
| Drive output                    | Read folder by ID; list children with IDs/MIME; create folders and immutable binding file  |
| Drive workspaces                | List IDs; upload ZIP from local file; download by ID to local file; delete by ID           |
| Drive reports                   | Search exact name; create Markdown; download by ID; update existing file by ID             |
| Context Google Docs (optional)  | Exact file ID and native MIME verification; full body read or complete plain-text export   |
| Context Drive .md (optional)    | Exact file ID and stored Markdown verification; download complete UTF-8 original bytes     |

If any required action is missing from the _actual Routine tool surface_, stop
and report the missing operation **before** accepting a connector's inline
base64 as input. Do not substitute text extraction for Sheets values or Drive
binary download. The 10 KiB/100 KiB/1 MiB smoke test is performed **after**
confirming file-backed transfer, never to discover whether agent-mediated
base64 might happen to work.

For connectors requiring file-reference materialization, verify bounded (256 KiB
plus optional BOM) temporary staging outside the workspace and cleanup on both
success and failure. Do not persist context or proceed if the bound or cleanup fails.
See the GWS skill's context contract for details.

## Minimal weekly Routine instruction (single entrypoint)

```text
/web-update-report-gws Run the weekly monitor with input targets table
<CSV_PATH_OR_DRIVE_CSV_OR_SPREADSHEET_URL> and output Drive folder <OUTPUT_FOLDER_URL>.
Execute the internal web-update-monitor -> web-update-research pipeline for material changes; separate observed facts, verified context and inference, finalize once, and report failures.
```

Set the weekly schedule in Routines. The skill derives the first worksheet
and entire used range for Spreadsheet inputs by default, validates CSV inputs
with the core CSV parser, and resolves or creates `workspaces/` and `reports/`
under the output folder. No extra range, internal directory, or common context is required. To add context, append `Context source: <GOOGLE_DOCS_OR_DRIVE_MD_URL_OR_FILE_ID>` or an inline `Common context:` block, but **not both**. When supplied, the orchestrator resolves the selected source once before workspace mutations and forwards its full text to both materiality review and follow-up research, preserving target-specific `criteria`. If omitted, retain previous behavior. Do not invoke either child skill separately: this command owns orchestration, report finalization and delivery.

### Common-context acceptance cases

Verify the agent's decisions and reports across these scenarios:

1. No context: ordinary target-specific `criteria`/`keywords` behavior, with no extra input.
2. Inline, Google Doc, and Drive Markdown context: same text yields equivalent decisions; context informs multiple interests and research without overriding explicit criteria. A modified Doc affects the next run or resumed pending review without rewriting saved evidence.
3. Invalid sources: reject local `.md` paths, missing permissions, incomplete exports, unsupported types/URLs, invalid or empty UTF-8, oversized content, and simultaneous input modes before workspace mutation.
4. Security: no extra fetching or invented findings from context-only topics; transient staged source files are bounded and cleaned up, with no raw context text copied into logs, snapshots, pending reviews, or reports.

## 1. Verify binary transfer before monitoring

Using the local helper, build a disposable workspace containing
`output/` and `internal/` files, then create a named snapshot:

```bash
python skills/web-update-report-gws/scripts/gws.py pack \
  --workspace "$TEST_WORKSPACE" \
  --generation 20261009T000000Z \
  --archive "$SCRATCH/workspace-20261009T000000Z.zip"
```

Upload the ZIP directly **from its local file**, download by returned exact
Drive ID directly **into another local file**, and compare via the helper
(without showing or writing any binary/base64 payload in agent text):

```bash
python skills/web-update-report-gws/scripts/gws.py compare-transfer \
  --source "$SCRATCH/workspace-20261009T000000Z.zip" \
  --downloaded "$DOWNLOADED/workspace-20261009T000000Z.zip"
python skills/web-update-report-gws/scripts/gws.py verify \
  --archive "$DOWNLOADED/workspace-20261009T000000Z.zip"
```

Require the size and SHA-256 returned by `compare-transfer` to agree with
`pack` as well.

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
  CSV, require `size`, download by exact ID directly to a local file, and
  compare the downloaded byte length with that metadata **before** projecting
  the CSV. Pass `--drive-sha256` too only when a trusted checksum for the
  exact source revision is available; do not fabricate or require an MD5.
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
3. Change a controlled public target with independently inspectable primary
   corroboration and confirm `web-update-report-gws` first judges materiality,
   invokes `web-update-research` exactly once for that material URL, and
   invokes the monitor's `finalize` exactly once with the returned Markdown.
   Verify distinct **Observed changes**, **Verified context**, and **Analysis
   (inference)** sections and inspected source URLs. Verify one delivered
   Markdown report per completed run, and that pending reviews block delivery.
   Also test a **non-material** change: no research invocation, no report.
   Disable public-web search in a test run: a material change must still yield
   a fact-only report with an explicit independent-verification limitation.
   Simulate a transient search/page-open failure: allow at most one retry,
   then finalize a qualified fact-only report if the retry fails. Confirm
   research is not silently retried for an already finalized revision.
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
   empty report or a new baseline. Separately use a controlled public target
   whose static fetch returns a website HTTP 403 and the main agent can read:
   verify `agent_fetch_required` triggers Web fetch of the returned final URL,
   monitor `ingest` with original `run_id`/`target_id` and the exact
   `link_depth`/`max_links` (and `--links` when available), workspace
   persistence, and subsequent pending/materiality/research/finalize stages.
   Verify baseline-only ingestion does not research. When Web fetch also
   fails, report that target and preserve the previous baseline; never bypass
   an egress-denied 403 or replace the source with a search snippet.
8. Verify that missing or duplicated binding files, replaced/renamed child
   folders, empty bound `workspaces/`, inaccessible output folder, incomplete
   listings and ambiguous creates **stop**, with no baseline reset. Verify that
   a healthy later Routine resumes by the originally bound folder IDs.
9. Verify both CSV and Spreadsheet workflows. Reject malformed CSV, oversized
   CSV, unavailable input, wrong spreadsheet tab, and invalid headers without
   changing the cached projection or resuming pending reviews.
10. Download a syntactically valid **row-truncated Drive CSV** and require the
    expected-size verification to reject it before reconciling pending
    state. Also reject missing or invalid size metadata. Equal-size altered
    CSV cannot be detected by the size check alone; when trusted SHA-256 is
    available, a same-size altered CSV must be rejected before projection.

## Failure policy

Fail closed on missing **file-backed** binary upload/download capability (even
for small files), missing/mismatched folder bindings, ambiguous parent/child
IDs, damaged binaries, missing or mismatched CSV size or supplied SHA-256,
validation errors, stale input projection, or a conflicting writer. Do not store
Google tokens, cookies, or API secrets in workspace ZIPs.
Keep connector and monitoring errors distinct.

This checklist is a **test plan**, not a claim that Anthropic's Google
connector has already passed the transfer and cross-session tests.
