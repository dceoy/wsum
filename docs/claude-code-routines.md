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

## Text persistence operations

Use the existing authorized Google connector and local Python only. No extra
Drive API/CLI, credentials, Apps Script, service or infrastructure is needed.
Tool names vary: follow the agent-facing create/read/list/delete contract in
[the skill](../skills/web-update-report-gws/SKILL.md#google-drive-text-contract).
Use complete exact-ID original text, materialized losslessly as UTF-8. Previews,
truncation, Docs conversion and manual Base64 transcription cannot verify storage.

1. Resolve optional context, the parent and its immutable
   `wsum-text.binding.json`. Start initial setup with a fresh dedicated folder;
   verify both child IDs. Never recreate missing managed resources.
2. Read the newest `workspace-*.json` manifest and every payload by exact ID.
   Validate filename/generation, safe paths, sizes and SHA-256, then restore to
   a new local directory. Validate the required delivery ledger.
3. Project current targets before resuming pending reviews. For Drive CSV,
   compare original byte length with `size` metadata; size alone cannot detect
   equal-length corruption. Sheets inputs require range values, not previews.
4. Resume pending reviews, monitor, research material changes and finalize via
   the core API. Persist after every state change. `pack` stages exact UTF-8
   payloads; unsupported binary/control content fails explicitly.
5. Create text payloads and read back every returned ID. `prepare` checks their
   sizes/hashes before producing the final manifest. Re-list and check the
   expected active identity **immediately before** creating the manifest last.
6. Read back the manifest by exact ID and compare size/SHA-256 with `prepare`;
   run `verify` for all payloads. Re-list to confirm the self-commit is newest
   and unique before updating expected identity or reporting durability.
7. Retain three verified generations using `retention`. Revalidate the listing,
   delete obsolete manifests first, confirm their absence, then delete only
   payloads unreferenced by remaining manifests. Stop cleanup on uncertainty;
   leave unknown/orphan files alone.
8. Deliver completed canonical Markdown only after pending reviews clear.
   Verify exact-ID report bytes, then record the ledger and persist it. If
   ledger persistence fails, restore committed state and reuse/verify the same
   exact report filename on retry. Never report that failed ledger as durable.

Only one writer may run per workspace. Listing guards detect stale sessions
but provide no atomic lock. Ambiguous creates require exact-name reconciliation
with exact-ID byte checks; never blindly retry or delete a previous generation.

## Failure handling

Missing bound resources, incomplete reads/listings, corrupt newest manifests,
unsafe paths, missing payloads, hash mismatch, stale expected identity or an
unavailable required text operation stop execution. A failure is not an empty
first run, and no older generation is silently selected. Cleanup failures can
leave extra files without invalidating an already verified commit.
Do not bypass environment-denied HTTP 403 with main-agent fetch. Keep resolved
context, credentials and tokens out of persisted state.

## Offline verification and live follow-up

`tests/test_gws.py` uses a small fake Drive text store for deterministic packing,
exact hash checks, fresh-workspace restore, repeated commits, three-generation
retention and injected failures. No Google credentials are used.

**Live connector compatibility has not been tested.** Implementation, PR review
and merge do not require live access. In an authorized Claude Code web/Routines
environment, verify actual text create/read-back with Unicode, CRLF, BOM and no
final newline, manifest-last commit, report delivery and fresh-session restore.
If any required operation is unavailable or transforms bytes, document it and
fail closed without claiming compatibility. Exercise a partial upload, corrupt
read-back and stale session; previous committed generations must remain usable.
