# Web Update Monitor

A small Agent Skill for detecting meaningful updates on websites and documents.

The implementation separates deterministic mechanics from model judgment:

- `monitor.py` performs bounded fetch/read, normalization, hashing, diff generation,
  and atomic candidate-snapshot writes.
- `workflow.py` validates targets and owns deterministic workflow decisions,
  notification state transitions, idempotency keys, expected-baseline snapshot
  promotion, and crash-safe local persistence operations.
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
`local-notification-cas`, `local-notification-release`, and
`local-snapshot-promote`. These commands keep baseline promotion, local
crash-safe persistence, target-scoped notification claims, and the durable
`pending` → `sending` → `delivered` notification protocol out of model
instructions. For local mode, pass each `compare_and_swap` response to
`local-notification-cas --runtime-dir <runtime-dir>`, route every
`promote_snapshot` result through `local-snapshot-promote --runtime-dir
<runtime-dir> --candidate <candidate>`, and release the target claim only after
the notification's terminal action and snapshot promotion; the agent reports
only confirmed connector outcomes back to the helper.

Carry the monitor result's `previous_sha256` (using `null` when it is empty) as
`expected_snapshot_sha256` through the local notification request so a stale
comparison cannot create a `sending` claim.

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
