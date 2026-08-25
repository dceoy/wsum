# Routine setup and rollback

## Persistence mode

Select exactly one persistence mode for each deployment and keep it fixed for a run.

### Google Drive mode

Grant only these operations:

- Sheets: read `Targets`, `State`, `Runs`, and `Notifications`; replace one `State`
  or `Notifications` row; append `State`, `Runs`, and `Notifications` rows; and
  atomically batch-replace all `Notifications` rows for one grouped Slack chunk.
  The injected connector's `batch_replace_values` operation must either apply
  every supplied range or none of them. Add Outbox access only when that delivery
  mode is enabled.
- Drive: find, upload, and download files only below the configured snapshot root.
  Grant delete only when retention cleanup is enabled.

Supply spreadsheet and Drive identifiers through runtime configuration outside the
repository.

### Local mode

No Google Sheets or Google Drive connector is required. Use one trusted runtime root
outside the repository and follow [local-setup.md](local-setup.md). Restrict access
to the account running the monitor and back up state and snapshots as one unit.

## Common connector setup

- Slack: send messages only to deployment-owned destinations mapped from
  `notification_group`. Do not expose channel IDs or credentials to model context.
  Adapt confirmed non-delivery to `ConfirmedDeliveryFailure`; leave timeouts,
  missing receipts, and other uncertain outcomes ambiguous so they cannot be
  retried automatically.
- Web: outbound HTTP(S) only. Deny local, private, loopback, link-local, multicast,
  reserved, metadata-service, and non-HTTP(S) destinations.

Keep Slack webhook URLs, OAuth tokens, and GAS properties in their native secret
stores.

## Initial setup

1. Choose `google-drive` or `local` persistence.
2. For Google Drive mode, create the required sheets and exact header rows from
   [data-model.md](data-model.md), then create a dedicated Drive snapshot root and
   least-privilege connector binding.
3. For local mode, create the trusted runtime root and `targets.json` described in
   [local-setup.md](local-setup.md).
4. Add up to about 10 static HTML pilot targets with `enabled=false`.
5. Run `dry_run.py` with local fixtures.
6. Enable one static target and verify baseline creation without a notification.
7. Verify a second unchanged run and one controlled material fixture.
8. Review the checklist in [security.md](security.md).
9. Enable remaining pilot targets gradually.
10. Configure any desired external schedule only after the pilot is stable.

Use a stable external run ID for retries of the same invocation. Do not trigger a
manual/replay run while another invocation is still in flight, and do not configure
overlapping schedules against the same target set: the store has no cross-instance
claim primitive, so overlapping invocations can duplicate notifications (see the
concurrent-invocation known gap in [security.md](security.md)).

## Scheduling

The repository does not define execution frequency. Invoke the skill manually or
configure an external scheduler for hourly, daily, weekly, or another practical
cadence based on target volatility, resource cost, and notification requirements.
Keep the scheduling control plane outside this repository and avoid overlapping
runs against the same target set.

## Browser mode

Keep `static` as the default. Enable `browser` only after documenting why static
fetching is insufficient, approving required hostnames, installing an ephemeral
Playwright runtime, running its security fixtures, and accepting the added cost and
attack surface. Never use persistent browser profiles or auto-escalation. Browser
mode has a known, currently unmitigated DNS-rebinding gap between the guard's
Python-side validation and Chromium's own connection; see
[security.md](security.md) before approving it for any target that is not fully
trusted. `fetch_rendered` fails closed with `browser_egress_not_verified` unless
`BrowserFetchConfig.verified_egress_pinning=True` is explicitly set, which should
only happen after a verified network-level egress pinning mechanism is in place.
It also fails closed with `browser_memory_bound_not_verified` unless
`BrowserFetchConfig.verified_memory_bound=True` is explicitly set, which should
only happen after the browser process has been placed under a verified external
hard memory limit. It also fails closed with
`browser_execution_bound_not_verified` unless
`BrowserFetchConfig.verified_execution_bound=True` is explicitly set, which should
only happen after the browser process has been placed under a verified external
wall-clock/liveness supervisor. See [security.md](security.md) for the full threat
model and known gaps.

## Delivery

Use exactly one path:

- Direct Slack Connector: preferred minimal architecture.
- GAS Outbox: use `scripts/gas/Code.gs`, store `SLACK_WEBHOOK_URL` and
  `ALLOWED_NOTIFICATION_GROUP` in Apps Script Properties, create a time-driven
  dispatcher trigger, and fix the destination in the webhook or dispatcher
  configuration. The dispatcher poisons any row whose `notification_group` does
  not match `ALLOWED_NOTIFICATION_GROUP` instead of delivering it to that webhook.
  Configure `RoutineConfig.delivery_mode=outbox`, inject `OutboxSheetsStore`, and
  omit the direct Slack connector.

Disable one path before enabling the other.

## Rollback and safe disable

1. Disable the external schedule or other trigger source.
2. Disable all targets in the selected backend if persistence access must remain.
3. Disable the GAS trigger or Slack connector binding.
4. Preserve operational state, notification records, and currently referenced
   snapshots for investigation.
5. Revert code only after disabling execution; never delete the last valid baseline.
6. Rotate affected credentials in their native systems if exposure is suspected.
