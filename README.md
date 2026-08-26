# Web Update Monitor

A small Agent Skill for detecting meaningful updates on websites and documents.
Python is limited to deterministic fetch/read, normalization, hashing, and bounded
diff generation. The agent handles persistence, summarization, and notification.
The helper strictly decodes text and bounds HTTP, PDF, XML, and diff resources.

The skill supports two persistence modes selected per run:

- `local`: keep target configuration and normalized snapshots in a caller-selected
  local directory.
- `google-drive`: keep the same target configuration and snapshots in Google
  Sheets/Drive through the connected app.

## Setup

Use [uv](https://docs.astral.sh/uv/) for all Python dependency and command
execution:

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

For browser-rendered or connector-fetched content, save the document to a temporary
file and use `--input` instead of `--url`:

```bash
uv run python skills/web-update-monitor/scripts/monitor.py \
  --input /tmp/rendered.html \
  --source-url https://example.com/app \
  --previous .runtime/example.txt \
  --output .runtime/example.next.txt
```

The command prints one JSON object with `baseline`, `unchanged`, or `changed`
status and a bounded unified diff. Promote the new snapshot only after the whole
monitoring workflow succeeds.

The external workflow keeps a durable notification ledger keyed by target and
normalized hash. It records `pending`, `sending`, and `delivered` transitions
before promoting a replacement snapshot; an indeterminate `sending` event is
left for manual reconciliation rather than retried automatically.

Run tests with:

```bash
uv run pytest
```

See `skills/web-update-monitor/SKILL.md` for the agent workflow.

## Repository boundary

Do not commit fetched production content, snapshots, runtime state, credentials,
webhook URLs, connector identifiers, browser profiles, or other deployment data.
