# Claude Code Routines: Google Workspace runbook

Use `web-update-report-gws` as the **only entrypoint**. It invokes installed
`web-update-report`, which uses `web-update-monitor` and includes research
for local reporting. Install all three skills. The wrapper persists each core
operation and delivers completed canonical Markdown reports to Drive.

## Configure the Routine

Provide two resources:

- **Targets:** a local CSV, Drive CSV URL/ID, or Google Spreadsheet URL/ID.
- **Output:** one existing Google Drive folder URL/ID dedicated to the workspace.

Optionally provide exactly one `Context source:` pointing to a Google Doc or
Drive Markdown file URL/ID. Inline `Common context:` and multiple sources
are unsupported; context is not required.

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

Use the existing authorized Google connector plus local Python only. No
Cloud Run, extra Drive API credentials, Apps Script or server is needed.
See the [skill](../skills/web-update-report-gws/SKILL.md#google-drive-connector-text-transport-cowork-compatible).

New checkpoints use **small immutable JSON state** (`wsum-json-1`)
containing inline short text and content-addressed references to
large/snapshot objects. Objects are gzip-compressed and Base64-encoded
**as text**, split into bounded hash-linked JSON text chunks. Unlike
the old approach, **unchanged objects are reused across generations**.
The entire Workspace is not tarred or re-uploaded on every operation.

The upload path for the JSON checkpoint and all new chunks is
`transfer-text` → `create_file.textContent` with MIME type
`text/plain` and Google-native conversion disabled. It does **not**
upload using `base64Content`. The connector's download response remains
Base64-wrapped, so use `transfer-decode` against the independent source
file size and SHA-256 to validate every exact-ID readback. Current snapshots require the `wsum-json-1` format; other formats are rejected.

1. Resolve the bound output folder and immutable
   `wsum-text.binding.json`. Require full, paginated listing and verify
   the newest checkpoint, all external chunk chains, and the local
   delivery ledger before any new monitoring work.
2. Project current Google Sheets or CSV input and run
   `web-update-report` with the synchronous checkpoint handoff after
   every `pending`, `check`, `ingest`, `discard` and `finalize`
   transition. Include failure and recovery paths.
3. Run `next-generation` and `json-pack`. New small file contents go
   inline into the small JSON state document. Large page snapshots
   become separate SHA-256-addressed objects.
4. For an unchanged object hash, inherit the head reference from a
   **previously exact-ID-verified committed state manifest** without
   redownloading all old chunks at every checkpoint. Build an object
   receipt `{"head":...,"previous_manifest":"/scratch/verified-old.json"}`.
   For newly uploaded objects use `json-link` **from last chunk to first**,
   `textContent` and exact-ID readbacks of every new chunk; their receipts
   use `{"head":...,"readbacks":{id:path,...}}`.
5. Build receipts keyed by object content SHA-256. Run `json-prepare`
   to verify fresh object chunks and to bind unchanged objects to the
   previously committed manifest and the staged source digest.
   Revalidate the expected active manifest immediately before commit.
6. Upload the final `workspace-<generation>.json` by `textContent`
   **last**. Download and validate its exact bytes, then run
   `json-verify-commit --manifest ... --receipts ...` and re-list to
   confirm it is uniquely newest. Use full `verify --readbacks ...`
   for restore, not for ordinary checkpoints.
7. Run full three-generation retention in a **separate maintenance
   operation**, not after every core mutation. After downloading and
   validating complete references for all generations, delete discarded
   manifests first, then only unreferenced object chunks. Shared
   objects referenced by retained generations must remain untouched.
   If full verification exceeds connector limits, safely skip cleanup.
8. Publish completed Markdown report files only after pending reviews
   clear, with the original exact-ID readback/ledger procedure.
   Persist ledger changes in another small JSON checkpoint.

A single state document is bounded at 64 KiB; chunk size is 12 KiB
of encoded object data. These are not connector guarantees. The transfer
remains model-mediated; incomplete connector output or inability to read
a chunk fails closed. Do not copy full state into the final assistant
response. One writer per workspace; timestamp/listing checks are not locks.

## Failure handling

Missing bound resources, Base64 relay/context limits, incomplete reads or
listings, corrupt newest manifests, unsafe paths, missing payloads, hash
mismatch, stale expected identity or unavailable transfer operations stop. A failure is not an empty
first run, and no older generation is silently selected. Cleanup failures can
leave extra files without invalidating an already verified commit.
Do not bypass environment-denied HTTP 403 with main-agent fetch. Keep resolved
context, credentials and tokens out of persisted state.

## Redundant checkpoint elimination

For each persistence handoff, re-list the Drive workspace and call `next-generation` with `--workspace` and the most recent verified `--manifest` (omit the manifest on first creation). This single preflight checks the Drive active identity and returns `changed`. A false result skips staging and upload; stale identity and invalid state always fail closed. Core pending reviews, reports, evidence, and durable file paths remain unchanged.

## Reusing verified content objects during staging

On a changed workspace, pass the last exact-ID-verified committed manifest to `json-pack --previous-manifest`; omit it for a fresh workspace. Previously committed large objects are content-hashed but are not recompressed into redundant local fragments. New objects still follow the original chunk upload and verification flow; the committed JSON checkpoint schema is unchanged.

## Offline verification and live follow-up

`tests/test_gws.py` covers current JSON checkpoints,
deduplicated objects and corrupt readbacks. The tests use
local synthetic files for deterministic packing,
exact hash checks, fresh-workspace restore, repeated commits, three-generation
retention and injected failures. No Google credentials are used.

**Live connector compatibility has not been tested.** Model relay is bounded
by tool/context size and cannot stream large files. Original Drive CSV bytes
have size-only provenance without source checksum. Implementation, PR review
and merge do not require live access. In an authorized Claude Code web/Routines
environment, verify actual text create/read-back with Unicode, CRLF, BOM and no
final newline, manifest-last commit, report delivery and fresh-session restore.
If any required operation is unavailable or transforms bytes, document it and
fail closed without claiming compatibility. Exercise a partial upload, corrupt
read-back and stale session; previous committed generations must remain usable.
