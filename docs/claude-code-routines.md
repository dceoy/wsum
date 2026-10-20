# Claude Code Routines: Google Workspace runbook

Use `web-update-report-gws` as the **only entrypoint**. It invokes installed
`web-update-report`, which uses `web-update-monitor` and includes research
for local reporting. Install all three skills. The wrapper persists each core
operation and delivers completed canonical Markdown reports to Drive.

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

Use the authorized Google Drive connector and local Python only; no additional
Drive API, service or credentials are needed. See the
[skill](../skills/web-update-report-gws/SKILL.md#google-drive-connector-text-chunk-contract).
New workspace snapshots use `wsum-archive-1` with a deterministic tar.gz
archive and linked text chunks. Older `wsum-text-1` snapshots remain readable.
Uploads use `create_file.textContent`, with
`contentMimeType: text/plain` and `disableConversionToGoogleType: true`,
not `base64Content`. Downloads still return a Base64 envelope via
`download_file_content`; decode it with `transfer-decode`.

1. Resolve optional context, bound output folder and immutable
   `wsum-text.binding.json`. Restore the newest verified generation by
   exact ID. For archive manifests, follow and authenticate the full
   linked chunk chain; for legacy manifests use the old payload map.
   Never initialize an empty baseline following a failed restore.
2. Read and validate current CSV or Sheets values; project current targets
   and validate the delivery ledger. Invoke `web-update-report`. The wrapper
   must checkpoint after every `pending`, `check`, `ingest`, `discard`
   and `finalize`, including any command that might recover state.
3. Run `next-generation`, then `archive-pack` to create deterministic
   compressed bytes. For each Base64 fragment, **from last index to
   index 0**, run `archive-link` with the verified next reference.
4. Run `transfer-text` on each linked JSON chunk. Create a plain text
   Drive file with conversion disabled; download by resulting exact ID
   and run `transfer-decode` against original local length and SHA-256.
   Store the resulting verified `{id,size,sha256}` as the next reference
   for the following lower-index chunk. Any failure aborts the checkpoint.
5. Once chunk 0 is verified, use `archive-prepare` to write the compact
   root manifest. Re-list and confirm unchanged active identity before
   creating `workspace-<generation>.json` **last**, via `textContent`.
   Download and validate its original size and SHA-256, run `verify` on
   all exact-ID chunk readbacks, then re-list to confirm the committed
   manifest is newest before acknowledging durable persistence.
6. Keep the three latest *verified* generations. Delete obsolete
   manifests first, and only then unreferenced chunks after a fresh
   listing; preserve unknown or orphan files.
7. Refresh pending state. Deliver completed canonical Markdown reports
   with the existing exact-ID Base64 readback and ledger process, then
   checkpoint the updated ledger using the new archive format.

One writer per workspace; listings detect stale sessions but are not
distributed locks. Reconcile ambiguous file creates by name and exact-ID
readback, not blind retry. Live connector transport/context limits still
apply; a large number of 24 KiB compressed fragments can require many
tool calls. Reject incomplete fields and unsupported types.

## Failure handling

Missing bound resources, Base64 relay/context limits, incomplete reads or
listings, corrupt newest manifests, unsafe paths, missing payloads, hash
mismatch, stale expected identity or unavailable transfer operations stop. A failure is not an empty
first run, and no older generation is silently selected. Cleanup failures can
leave extra files without invalidating an already verified commit.
Do not bypass environment-denied HTTP 403 with main-agent fetch. Keep resolved
context, credentials and tokens out of persisted state.

## Offline verification and live follow-up

`tests/test_gws.py` covers legacy snapshots; `tests/test_gws_archive.py` covers
compressed text chains, integrity and mtime preservation. Both use offline data.
No Google credentials are used.

**Live connector compatibility has not been tested.** Model relay is bounded
by tool/context size and cannot stream large files. Original Drive CSV bytes
have size-only provenance without source checksum. Implementation, PR review
and merge do not require live access. In an authorized Claude Code web/Routines
environment, verify actual text create/read-back with Unicode, CRLF, BOM and no
final newline, manifest-last commit, report delivery and fresh-session restore.
If any required operation is unavailable or transforms bytes, document it and
fail closed without claiming compatibility. Exercise a partial upload, corrupt
read-back and stale session; previous committed generations must remain usable.
