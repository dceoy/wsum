# Security model

## Assets and trust boundaries

Protect connector credentials, Slack destinations, Sheets configuration/state,
local operational databases, Drive/local snapshots, audit records, operator
identity, and network reachability.

Trust runtime-owned connector configuration and reviewed deterministic code. Treat
target fields, DNS, HTTP headers/bodies, redirects, rendered DOM, PDF/feed parsers,
normalized text, diffs, and every model response as lower-trust.

The critical boundaries are:

1. Target configuration to outbound network.
2. Fetched bytes to deterministic parsers.
3. Bounded normalized diff to Claude.
4. Validated structured summary to Slack/Outbox.
5. Persistence responses to operational state.

## Attack paths and controls

| Attack                         | Controls                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| SSRF/private access            | HTTP(S) only; reject credentials and credential-like query parameters; resolve twice; require all addresses public; pin static sockets; revalidate redirects and response peers                                                                                                                                                                                                                                                                                                       |
| DNS rebinding                  | Compare consecutive answers; connect only to validated addresses; verify peer; fail closed on answer drift                                                                                                                                                                                                                                                                                                                                                                            |
| Redirect abuse                 | Manual redirect count and full policy validation at each hop; do not forward validators cross-origin                                                                                                                                                                                                                                                                                                                                                                                  |
| Browser exfiltration           | Explicit mode and allowed hosts; route every request; block service workers, downloads, pop-ups, media/fonts, private hosts, excess requests, excess declared bytes, and excess DOM output; destroy context                                                                                                                                                                                                                                                                           |
| Prompt injection               | Never send raw HTML or unrelated page content; fixed system prompt treats page instructions as data; page content cannot select tools, connectors, destination, or data; validate output evidence and instruction-like text                                                                                                                                                                                                                                                           |
| Oversized/parser attacks       | Bound compressed input, decompressed PDF streams, objects, feed entries, response bytes, rendered bytes, diff sections, and output lengths; reject XML DTD/entities and encrypted/image-only PDFs                                                                                                                                                                                                                                                                                     |
| Slow-trickle / algorithmic DoS | A total wall-clock deadline across DNS, connect, redirects, and body reads (`max_total_seconds`) bounds one fetch regardless of per-op timeouts; a fixed-capacity daemon resolver pool prevents timed-out `getaddrinfo` calls from creating unbounded threads or queued jobs; a conservative sequence-length-product budget (`max_diff_complexity`) short-circuits `SequenceMatcher` before repeated-line or unique-line-permutation worst cases, independently of the line-count cap |
| Duplicate/ambiguous delivery   | Stable event ID; persistent state before and after send; atomically persist every event in one delivered Slack chunk; require the durable Outbox `sending` transition before invoking its sender; never auto-retry `pending`/`sending`; direct and Outbox paths mutually exclusive                                                                                                                                                                                                    |
| Secret leakage                 | No credentials in code, Sheets, URLs, model context, logs, errors, fixtures, snapshots, or audit metadata; native secret stores only                                                                                                                                                                                                                                                                                                                                                  |
| Connector misuse               | Least-privilege scopes; deployment-owned destination mapping; deterministic code chooses connector calls, never page/model content                                                                                                                                                                                                                                                                                                                                                    |

Static HTTP sockets are pinned to validated IPs. Browser engines cannot use the same
application-level socket pinning for every internal request; route checks, explicit
host allowlists, peer checks where exposed by the runtime, ephemeral isolation, and
resource limits reduce but do not eliminate browser/DNS engine risk. Keep browser
mode exceptional and prefer network-level egress enforcement in its sandbox.

Known gap: `BrowserNetworkGuard` re-resolves and validates each request's hostname
in Python before allowing `route.continue_()`, but Chromium performs its own,
independent DNS resolution when it actually opens the connection. A DNS-rebinding
host that answers with a public address for the guard's lookups and a private
address for Chromium's own lookup can still reach the private address; the
post-response peer check (`validate_response_peer`) detects this only after the
request has already been delivered, so it stops the run but cannot undo the
delivery. Closing this gap requires pinning Chromium's connection to the
guard-validated address set (for example via an external egress
proxy/`--host-resolver-rules`-style mechanism that preserves Host/SNI) rather than
relying on Python-side re-resolution; this is not yet implemented, so browser mode
should not be presented as fully SSRF-safe until it is.

Because this gap has no verified mitigation, `fetch_rendered` fails closed by
default: it raises `browser_egress_not_verified` unless the operator explicitly
sets `BrowserFetchConfig.verified_egress_pinning=True`, which should only be done
after a real network-level pinning mechanism has been configured and verified.

Known gap: the rendered-size guard measures
`document.documentElement.outerHTML` via `page.evaluate()` before
`fetch_rendered` reads `page.content()` into the Routine (Python) process, so an
oversized DOM never crosses into Python. It does not bound Chromium/Blink's own
memory while that string is materialized in the renderer: a page whose script
balloons the DOM before the guard runs can still exhaust the browser process.
Closing this gap requires either an in-engine node/character budget enforced
during execution (not exposed by Playwright's public API) or running the
browser process under an external hard memory limit (a container/cgroup memory
cap that kills the process before host memory is exhausted). Because neither is
implemented by this code, `fetch_rendered` fails closed by default: it raises
`browser_memory_bound_not_verified` unless the operator explicitly sets
`BrowserFetchConfig.verified_memory_bound=True`, which should only be done
after the browser process has been placed under a verified external memory
cap.

Known gap: `config.timeout_seconds` is only passed to `page.goto()`.
`page.evaluate()` and `page.content()` — used afterward to measure and read
the rendered DOM — are plain Playwright sync-API calls with no `timeout`
parameter of their own, so an unresponsive or CPU-saturated renderer can
occupy a Routine worker indefinitely after navigation succeeds. Interrupting
a blocked Playwright sync call from a watchdog thread is not a
documented/thread-safe operation, so this code does not attempt it. Closing
this gap requires running the browser process under an external wall-clock
or liveness supervisor (a process-group timeout or container liveness probe
that kills the process tree past the configured deadline) rather than an
in-process timeout. Because that is not implemented by this code,
`fetch_rendered` fails closed by default: it raises
`browser_execution_bound_not_verified` unless the operator explicitly sets
`BrowserFetchConfig.verified_execution_bound=True`, which should only be
done after the browser process has been placed under a verified external
wall-clock/liveness supervisor.

Known gap: neither persistence backend exposes a store-level lease that atomically
covers a complete target run or notification delivery. `SheetsStore` cannot express
an atomic create-if-absent/conditional write through the Sheets Values API. Local
mode uses SQLite transactions and primary keys, so separate processes no longer
lose unrelated state/run/notification writes through whole-file read-modify-write,
but the routine-level sequence is still `get_run` or `get_notification` followed by
external fetch/snapshot/model/Slack work and a later persistence write. Two Routine
invocations that overlap can therefore both observe "not claimed" before either
commits and both perform external side effects. The in-process `_store_lock` only
serializes one Routine instance; SQLite serializes database writes, not the entire
external side-effect sequence. Closing this gap requires a store-level atomic claim
or lease integrated into orchestration. Until then, do not schedule or manually
trigger overlapping Routine invocations against the same target set, and treat the
"zero duplicate notifications" SLO in [operations.md](operations.md) as conditioned
on that operational constraint, not as a guarantee the code enforces.

The bundled PDF extractor intentionally supports a conservative text subset. Complex
PDFs fail closed. Add a new parser only after isolated fuzzing and checklist review.

## Connector scopes

- Sheets: ranges listed in [routine-setup.md](routine-setup.md), no whole-Drive scope.
- Drive: one snapshot root; delete omitted by default.
- Local: one trusted runtime root containing `targets.json`, `operations.sqlite3`,
  and content-addressed snapshots; no broader filesystem access.
- Slack: send-only to an approved destination mapping; no history/user access.
- GAS: Spreadsheet-bound Outbox plus Apps Script Properties and the one Slack URL.
- Web/browser: public HTTP(S) egress only, no inbound service or persistent profile.

## New target/fetch mode/connector checklist

- Confirm business owner, public URL, and absence of secrets or signed parameters.
- Confirm static mode is insufficient before browser approval.
- Enumerate every required hostname and subresource.
- Run SSRF, redirect, DNS drift, size-limit, timeout, malformed-content, selector
  drift, and prompt-injection fixtures.
- Confirm only normalized bounded diffs reach Claude.
- Confirm page/model data cannot set a tool, connector, spreadsheet, Drive root,
  notification group, channel, or credential.
- Review exact connector scopes and disable unused write/delete operations.
- Verify first-run baseline, duplicate suppression, partial failure, and rollback.
- Record reviewer and approval outside fetched content/audit payloads.

## Rotation and incident response

If exposure or malicious behavior is suspected, disable the external schedule and
targets, disable delivery, revoke connector bindings, rotate credentials in Google,
Slack, and Apps Script, preserve state/audit metadata, and quarantine affected
snapshot artifacts. Do not open suspicious fetched content in a privileged browser.
Review target/configuration changes, run IDs, event IDs, error codes, and connector
or local persistence audit evidence without copying response bodies into tickets or
GitHub.

Residual risks include compromised public origins, parser implementation defects,
browser-engine vulnerabilities, connector platform compromise, local host
compromise, and delivery ambiguity between an external send and state persistence.
Fail closed and keep the last valid baseline for all of them.
