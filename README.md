# Web Update Monitor

A small Agent Skill for detecting meaningful updates on websites and documents.

The implementation separates deterministic mechanics from model judgment:

- `monitor.py` performs bounded fetch/read, normalization, hashing, diff generation,
  and atomic candidate-snapshot writes.
- `workflow.py` validates targets and owns deterministic workflow decisions,
  notification state transitions, idempotency keys, and snapshot-promotion rules.
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

Create a baseline from a public URL:

```bash
uv run python skills/web-update-monitor/scripts/monitor.py \
  --url https://example.com/ \
  --output .runtime/example.txt
```

Compare a later fetch with the baseline:

```bash
uv run python skills/web-update-monitor/scripts/monitor.py \
  --url https://example.com/ \
  --previous .runtime/example.txt \
  --output .runtime/example.next.txt
```

Validate one run's target set before monitoring:

```bash
cat targets-request.json | \
  uv run python skills/web-update-monitor/scripts/workflow.py validate-targets
```

The workflow helper also exposes `change-action` and `notification-step`. These
commands keep baseline promotion and the durable `pending` → `sending` →
`delivered` notification protocol out of model instructions. The agent persists
records returned by the helper and reports only confirmed connector outcomes back
to it.

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
