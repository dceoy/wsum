# Web Update Monitor

A small Agent Skill for detecting meaningful updates on websites and documents.

The implementation separates deterministic mechanics from model judgment:

- `monitor.py` performs bounded fetch/read, normalization, hashing, diff generation,
  and atomic candidate-snapshot writes.
- `workflow.py` validates targets and owns deterministic workflow decisions,
  notification state transitions, occurrence-scoped idempotency keys,
  expected-baseline snapshot promotion, and crash-safe local persistence
  operations.
- the agent performs connector/browser I/O, judges whether a bounded diff is
  material to `watch_focus`, writes the summary, and sends Slack notifications only
  when directed by the workflow helper.

The skill supports two persistence modes selected per run:

- `local`: keep target configuration, normalized snapshots, and notification state
  in a caller-selected local runtime directory.
- `google-drive`: keep the equivalent records in Google Sheets/Drive through the
  connected app.

## Setup

Use [uv](https://docs.astral.sh/uv/) for Python dependency and command execution:

```bash
uv sync
```

## Usage

Create a candidate baseline from a public URL:

```bash
uv run python skills/web-update-monitor/scripts/monitor.py \
  --url https://example.com/ \
  --output .runtime/candidates/example-baseline.txt
```

Promote it through the local expected-baseline CAS, using the candidate's
`sha256` from the monitor result:

```bash
uv run python skills/web-update-monitor/scripts/workflow.py \
  local-snapshot-promote --runtime-dir .runtime \
  --candidate .runtime/candidates/example-baseline.txt < promotion-request.json
```

Relative `--candidate` paths are resolved from the caller's working directory
and then checked for containment, regular-file type, and symlink safety under
`runtime_dir/candidates/`. Thus the example above is valid when run from the
repository containing `.runtime`.

Notification event IDs scope a notification to one occurrence of a baseline
transition. The helper hashes `target_id`, the previous event ID (or an empty
segment), the nullable `expected_snapshot_sha256`, and the candidate `sha256`
with NUL separators. The per-target cursor advances only after delivery and
snapshot promotion, so retries reuse an unfinished event while a later
recurrence receives a new event ID. A delivered record remains the active
outbox until its candidate snapshot is durably promoted; it is then retired
before the cursor and target claim are finalized.

The request uses `expected_sha256: null` and `claim_event_id: null` for this
initial baseline. Compare a later fetch with the canonical baseline:

```bash
uv run python skills/web-update-monitor/scripts/monitor.py \
  --url https://example.com/ \
  --previous .runtime/snapshots/example.txt \
  --output .runtime/candidates/example-next.txt
```

For later baseline or non-material promotions, pass the monitor's
`previous_sha256` as `expected_sha256`; for a delivered notification, also pass
that notification's event ID as `claim_event_id`. Existing `.runtime/example.txt`
baselines must be migrated by the operator to `.runtime/snapshots/example.txt`
before using this workflow.

Validate one run's target set before monitoring:

```bash
cat targets-request.json | \
  uv run python skills/web-update-monitor/scripts/workflow.py validate-targets
```

The workflow helper also exposes `change-action`, `notification-step`,
`local-notification-state`, `local-notification-cas`, `local-notification-release`, and
`local-snapshot-promote`. These commands keep baseline promotion, local
crash-safe persistence, target-scoped notification claims, and the durable
`pending` → `sending` → `delivered` notification protocol (version 3) out of
model instructions. Notification records use schema version 2 and include the
expected baseline hash and previous event ID; target claims use the same
occurrence identity. For local mode, read the cursor with
`local-notification-state --runtime-dir <runtime-dir>`, pass each
`compare_and_swap` response to
`local-notification-cas --runtime-dir <runtime-dir>`, route every
`promote_snapshot` result through `local-snapshot-promote --runtime-dir
<runtime-dir> --candidate <candidate>`, and release the target claim only after
the notification's terminal action and snapshot promotion. Delivered records
are removed durably during the delivered release after the cursor CAS and before
the claim is removed; the agent reports only confirmed connector outcomes back
to the helper.

When an active target claim remains, `local-notification-state` returns
`action: "notification_state_recovery"` with the claim, matching notification
record, and a `recovery_action`. For an unpromoted delivered candidate it also
returns a runtime-relative `candidate_path` selected by matching regular,
non-symlinked files under `candidates/` to the claim's candidate hash; when
multiple files match, it selects the lexicographically first runtime-relative
path. Pass `$runtime-dir/$candidate_path` (or an equivalent absolute path) to
`local-snapshot-promote`. Release a pending failure, promote an unpromoted
delivered candidate, or release an already-promoted delivery before monitoring;
stop for manual reconciliation when the notification is still `sending`, and
never start a new event while the claim exists. Missing or symlinked candidate
matches fail closed and leave the claim in place.

Carry the monitor result's `previous_sha256` (using `null` when it is empty) as
`expected_snapshot_sha256` through the local notification request. Also carry
the `previous_event_id` returned by `local-notification-state` (using `null` for
the first occurrence) so a stale cursor cannot create a new `sending` claim.

Existing notification records or target claims from protocol 2, and protocol-3
records created before occurrence cursors, lack the required lineage and cannot
be migrated safely. Stop the runners, reconcile or archive that notification
state, and start the current protocol with the canonical snapshots unchanged;
the helper rejects legacy state before Slack or snapshot promotion.

For browser-rendered content, use browser mode only with a tool that enforces
public-unicast egress, bounded redirects/subresources, a total timeout, and a
maximum artifact size. Do not provide cookies or credentials; fail closed if those
controls are unavailable. Save the rendered document to a temporary file and pass
it to `monitor.py` with `--input` and `--source-url`.

Run tests with:

```bash
uv run pytest
```

See `skills/web-update-monitor/SKILL.md` for the agent workflow.

## Repository boundary

Do not commit fetched production content, snapshots, runtime state, credentials,
webhook URLs, connector identifiers, browser profiles, or other deployment data.
