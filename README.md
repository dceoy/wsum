# Web Update Monitor

A local-first Agent Skill for detecting meaningful updates on websites and documents.

The implementation keeps deterministic mechanics in Python and semantic judgment in the agent:

- `monitor.py` fetches or reads content, normalizes it, hashes it, and emits a bounded diff plus a candidate snapshot.
- `workflow.py` validates targets and atomically promotes local snapshots with an expected-baseline check.
- the agent judges materiality, writes a concise local Markdown report when a change matters, and manages candidates according to the monitor result.

Runtime state and reports stay under a caller-selected local directory.

## Setup

```bash
uv sync
mkdir -p .runtime/candidates .runtime/snapshots .runtime/reports
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

Handle the monitor result directly:

- `baseline`: promote the candidate.
- `unchanged`: delete the candidate.
- `changed`: judge whether the bounded diff matters to `watch_focus`. For a material change, write `.runtime/reports/<target_id>.md`; for a non-material change, skip the report. If the diff is truncated and would otherwise be classified non-material, stop for manual review instead of promoting it.

For a material change, write the report before promotion. If the report cannot be written, leave the baseline unchanged and stop that target.

Promote the candidate using the monitor result's `previous_sha256` as `expected_sha256` (`null` for the first baseline) and its `sha256` as `candidate_sha256`:

```bash
uv run python skills/web-update-monitor/scripts/workflow.py \
  promote-snapshot \
  --runtime-dir .runtime \
  --candidate .runtime/candidates/example.txt < promotion.json
```

The helper rejects a stale baseline instead of overwriting it. Remove the candidate after successful promotion.

## Execution model

Do not run overlapping invocations against the same runtime directory. Use a scheduler or process supervisor that serializes runs.

Browser-rendered targets are supported only when the browser/web tool can enforce public-unicast egress, bounded redirects and subresources, a total timeout, and a maximum artifact size. Do not provide cookies or credentials.

## Validation

```bash
uv run pytest
```

See `skills/web-update-monitor/SKILL.md` for the agent procedure.

## Repository boundary

Do not commit fetched production content, snapshots, reports, runtime state, credentials, browser profiles, or other deployment data.
