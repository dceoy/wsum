# Web Update Monitor

A local-first Agent Skill for detecting meaningful updates on websites and documents.

The implementation keeps deterministic mechanics in Python and semantic judgment in the agent:

- `monitor.py` fetches or reads content, normalizes it, hashes it, and emits a bounded diff plus a candidate snapshot.
- `workflow.py` validates targets, routes monitor results, and atomically promotes local snapshots with an expected-baseline check.
- the agent judges materiality, writes a concise summary, and sends Slack notifications when a change matters.

Runtime state stays under a caller-selected local directory.

## Setup

```bash
uv sync
mkdir -p .runtime/candidates .runtime/snapshots
```

Keep target configuration in a local JSON file, for example:

```json
{
  "targets": [
    {
      "target_id": "example",
      "name": "Example",
      "url": "https://example.com/",
      "watch_focus": "Product or pricing changes"
    }
  ]
}
```

Validate it before a run:

```bash
uv run python skills/web-update-monitor/scripts/workflow.py \
  validate-targets < targets.json
```

## Local workflow

For each enabled target, use the canonical baseline at:

```text
.runtime/snapshots/<target_id>.txt
```

Write the next normalized snapshot under `.runtime/candidates/`. On the first run, omit `--previous`:

```bash
uv run python skills/web-update-monitor/scripts/monitor.py \
  --url https://example.com/ \
  --output .runtime/candidates/example.txt
```

On later runs, compare with the local baseline:

```bash
uv run python skills/web-update-monitor/scripts/monitor.py \
  --url https://example.com/ \
  --previous .runtime/snapshots/example.txt \
  --output .runtime/candidates/example.txt
```

Pass `status`, `diff_truncated`, and initially `materiality: null` from the monitor result to:

```bash
uv run python skills/web-update-monitor/scripts/workflow.py \
  change-action < action.json
```

If the helper returns `assess_materiality`, judge only whether the bounded diff matters to `watch_focus`, then call `change-action` again with `materiality: true` or `false`.

When the returned action is `promote_snapshot`, promote the candidate using the monitor result's `previous_sha256` as `expected_sha256` (`null` for the first baseline) and its `sha256` as `candidate_sha256`:

```bash
uv run python skills/web-update-monitor/scripts/workflow.py \
  promote-snapshot \
  --runtime-dir .runtime \
  --candidate .runtime/candidates/example.txt < promotion.json
```

The helper rejects a stale baseline instead of overwriting it. Remove the candidate after successful promotion or when the result is unchanged.

For a material change, attempt one Slack delivery in the current run and promote the snapshot only after confirmed delivery. Notifications have at-least-once semantics across retries and restarts: if Slack accepts a message but the process crashes before promotion, or delivery is ambiguous, a later run may send the same change again, so duplicates are possible. If delivery fails or is ambiguous, leave the baseline unchanged and stop that target.

## Execution model

This local-only design intentionally does not implement a distributed notification ledger or cross-run leases. Do not run overlapping invocations against the same runtime directory. Use a scheduler or process supervisor that serializes runs.

Browser-rendered targets are supported only when the browser/web tool can enforce public-unicast egress, bounded redirects and subresources, a total timeout, and a maximum artifact size. Do not provide cookies or credentials.

## Validation

```bash
uv run pytest
```

See `skills/web-update-monitor/SKILL.md` for the agent procedure.

## Repository boundary

Do not commit fetched production content, snapshots, runtime state, credentials, webhook URLs, connector identifiers, browser profiles, or other deployment data.
