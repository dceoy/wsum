# Operations and auditability

## Practical weekly SLOs

For the initial approximately 10-target pilot:

- Finish a weekly static run within 15 minutes.
- Achieve at least 95% successful target checks over four weeks.
- Produce zero duplicate successful change notifications under normal
  single-instance operation; see the concurrent-invocation gap in
  [security.md](security.md) for the bound that applies if invocations
  overlap.
- Alert after three consecutive target failures.
- Retain at least 12 normalized snapshots per target.
- Investigate any failed target within one business day and any security-policy
  failure before re-enabling it.

## Health checks

After each run, inspect counts for checked, unchanged, baseline, minor, material,
notified, and failed. The checked count must equal enabled targets. A run result
of `suppressed` means a material change was detected and the baseline advanced,
but an operator-suppressed Notifications record blocked delivery; it is excluded
from both `material` and `notified` so a backlog of undelivered material changes
does not silently register as delivered. Verify every target has one terminal
run ID, temporary workspaces no longer exist, no `pending`/`sending` delivery is
automatically retried, and state references a loadable snapshot after successful
non-304 processing.

Weekly checks should also verify connector authorization, available Drive capacity,
stale selectors, retention backlog, poison Outbox rows, and browser-mode target
approval.

## Failure triage

1. Use only target ID, run ID, stable error code, attempt count, and timestamps.
2. Do not paste fetched bodies, connector errors, credentials, or signed URLs into
   logs or tickets.
3. Classify retryable transport/platform failures separately from permanent policy,
   selector, parser, or validation failures. `diff_budget_exceeded` and
   `truncated_diff_non_material` are neither: they mean the deterministic diff or
   the model's evidence could not rule out a material change, so the baseline was
   intentionally not advanced. Investigate manually; do not just retry.
4. Confirm the previous state hash/reference remains intact.
5. Fix configuration or parser policy, run local fixtures, then retry with a stable
   external run ID.
6. Inspect ambiguous `pending`/`sending` delivery manually in Slack before changing
   its status.

## Recovery

After a successful check, reset `consecutive_failures` to zero. If the current Drive
snapshot is missing, restore a known valid snapshot reference or create a reviewed
baseline; never silently replace it with empty or unrelated extraction.

For an interrupted material notification, query `Notifications`/Outbox by event ID.
If sent, update state without resending. If definitively failed, mark `failed`/`retry`
before another attempt. If ambiguous, retain `pending`/`sending` and require operator
resolution.

## Replay

Use `scripts/replay.py` with stored normalized snapshots, their version/hash
metadata, optional diff expectations, and optional summary. Replay performs no
network access. It verifies both hashes, reruns deterministic diff/scoring, checks
expected results, and revalidates summary evidence.

Keep replay manifests outside GitHub when they contain operational normalized
content.

## Audit records

Record configuration load, target terminal outcome, notification outcome, failure
alert outcome, and routine terminal outcome. Store identifiers, configuration
digest, counts, outcomes, and stable error codes only. Audit validation rejects keys
that imply body, content, HTML, payload, credential, secret, token, webhook, or
free-text data. A temporary audit-sink failure does not change fetch, state, or
delivery decisions; surface and repair it through deployment-level connector
health monitoring.

## Retention

Keep Sheets operational records according to the deployment's audit policy. Keep 12
Drive snapshots per target by default. Generate and review the cleanup plan; never
delete the current baseline. Disable Drive delete permission when cleanup is not
scheduled. Poison/ambiguous delivery rows remain until an operator resolves them.
