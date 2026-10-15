# Claude Code Routines: Google Workspace runbook

Use `web-update-report-gws` as the **only entrypoint**. It calls the installed
`web-update-monitor` and `web-update-research` skills, maintains a local
workspace, and persists snapshots and completed Markdown reports to Drive.

## Configure the Routine

Provide two resources:

- **Targets:** a local CSV, Drive CSV URL/ID, or Google Spreadsheet URL/ID.
- **Output:** one existing Google Drive folder URL/ID dedicated to the workspace.

Optionally provide either `Common context:` (inline) or `Context source:`
(one Google Doc or Drive Markdown file URL/ID). Both together are invalid;
common context is not required.

The runtime needs Python 3.11+ with `pypdf`, local scratch space, the
installed skills, authenticated Drive access, Sheets cell reads for
Spreadsheet targets, and public-web access to monitored sites. A search
tool helps with external verification but is not required for fact-only
reports. Run at most **one non-overlapping Routine** per workspace.

### Example

```text
/web-update-report-gws
Targets: <CSV_OR_SPREADSHEET_URL>
Output: <DRIVE_FOLDER_URL>
Context source: <OPTIONAL_DOC_OR_DRIVE_MD_URL>
```

The optional last line may be omitted. The skill selects the first grid tab
and full used range for Sheets input. No manual A1 range is needed.

## Use the tools as needed

Do **not** perform a separate connector inventory, preflight capability
audit, or synthetic 10 KiB/100 KiB/1 MiB ZIP round-trip before the run.
Resolve tools and operate on the requested resources directly:

1. Load the current targets with the core CSV validator or Sheets cell
   values. For a Drive CSV, compare exact downloaded bytes against the
   Drive `size` metadata. A size match alone cannot detect same-length
   corruption.
2. Restore the newest verified `workspaces/workspace-*.zip` snapshot,
   or initialize a new workspace only if the output folder has no existing
   managed state. Follow the immutable folder binding rules in the skill.
3. Monitor targets, resume pending reviews, research material changes,
   finalize once, and persist the complete workspace ZIP after changes.
4. On every actual ZIP upload, read the new file back by **exact Drive ID**.
   Compare size and SHA-256 to `gws.py pack` and run `gws.py verify`.
   Retain at most three **verified** generations.
5. Deliver completed canonical Markdown reports with exact-ID read-back;
   record delivery only after verifying the report bytes.

The standard Drive MCP can return binary data as a **base64 string**.
Do not manually copy, split, regenerate, or reconstruct base64 in agent
messages, and do not reconstruct binary CSV/ZIP files from rendered Drive
previews. Those paths can silently alter valid-looking data. Prefer native
file/artifact transfer when the runtime provides it. If an actual binary
transfer cannot preserve the required bytes, report that operation as
unsupported and stop without claiming a persisted state.

## Failure handling

A failed download or corrupted newest ZIP is **not** an empty first-run
workspace. Do not discard an existing baseline or skip binding checks.
Do not publish a report before its pending reviews are finalized, or
mark an upload delivered before its read-back verifies. Never bypass an
environment-denied HTTP 403 with a main-agent fetch.

The skill handles Google Docs/Markdown context separately: require
complete authorized reads, valid UTF-8 and the documented size bounds.
Do not put resolved context, credentials, or tokens into persisted state.

## Recommended acceptance checks

For a new deployment, optionally test one manual first run and one
**fresh-session** restore. Confirm that the same baseline resumes without
duplicated reports, material changes produce verified citations, incomplete
pending reviews block delivery, and failed transfers leave the previous
verified snapshot untouched.

These acceptance checks are recommendations, **not a mandatory Routines
gate**. The runbook does not claim that the default Google Drive MCP can
transport ZIP bytes reliably in a given Routines environment.
