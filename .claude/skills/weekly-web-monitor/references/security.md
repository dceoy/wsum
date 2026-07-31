# Security model

## Assets and trust boundaries

Protect connector credentials, Slack destinations, Sheets configuration/state,
Drive snapshots, audit records, operator identity, and network reachability.

Trust runtime-owned connector configuration and reviewed deterministic code. Treat
Sheets target fields, DNS, HTTP headers/bodies, redirects, rendered DOM, PDF/feed
parsers, normalized text, diffs, and every model response as lower-trust.

The critical boundaries are:

1. Target configuration to outbound network.
2. Fetched bytes to deterministic parsers.
3. Bounded normalized diff to Claude.
4. Validated structured summary to Slack/Outbox.
5. Connector responses to operational state.

## Attack paths and controls

| Attack | Controls |
| --- | --- |
| SSRF/private access | HTTP(S) only; reject credentials and credential-like query parameters; resolve twice; require all addresses public; pin static sockets; revalidate redirects and response peers |
| DNS rebinding | Compare consecutive answers; connect only to validated addresses; verify peer; fail closed on answer drift |
| Redirect abuse | Manual redirect count and full policy validation at each hop; do not forward validators cross-origin |
| Browser exfiltration | Explicit mode and allowed hosts; route every request; block service workers, downloads, pop-ups, media/fonts, private hosts, excess requests, excess declared bytes, and excess DOM output; destroy context |
| Prompt injection | Never send raw HTML or unrelated page content; fixed system prompt treats page instructions as data; page content cannot select tools, connectors, destination, or data; validate output evidence and instruction-like text |
| Oversized/parser attacks | Bound compressed input, decompressed PDF streams, objects, feed entries, response bytes, rendered bytes, diff sections, and output lengths; reject XML DTD/entities and encrypted/image-only PDFs |
| Slow-trickle / algorithmic DoS | A total wall-clock deadline across connect, redirects, and body reads (`max_total_seconds`) bounds one fetch regardless of per-op timeouts; a conservative sequence-length-product budget (`max_diff_complexity`) short-circuits `SequenceMatcher` before repeated-line or unique-line-permutation worst cases, independently of the line-count cap |
| Duplicate/ambiguous delivery | Stable event ID; persistent state before and after send; never auto-retry `pending`/`sending`; direct and Outbox paths mutually exclusive |
| Secret leakage | No credentials in code, Sheets, URLs, model context, logs, errors, fixtures, snapshots, or audit metadata; native secret stores only |
| Connector misuse | Least-privilege scopes; deployment-owned destination mapping; deterministic code chooses connector calls, never page/model content |

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

The bundled PDF extractor intentionally supports a conservative text subset. Complex
PDFs fail closed. Add a new parser only after isolated fuzzing and checklist review.

## Connector scopes

- Sheets: ranges listed in [routine-setup.md](routine-setup.md), no whole-Drive scope.
- Drive: one snapshot root; delete omitted by default.
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
Slack, and Apps Script, preserve state/audit metadata, and quarantine affected Drive
artifacts. Do not open suspicious fetched content in a privileged browser. Review
target/configuration changes, run IDs, event IDs, error codes, and connector audit
logs without copying response bodies into tickets or GitHub.

Residual risks include compromised public origins, parser implementation defects,
browser-engine vulnerabilities, connector platform compromise, and delivery
ambiguity between an external send and state persistence. Fail closed and keep the
last valid baseline for all of them.
