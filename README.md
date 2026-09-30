# wsum

A local-first Agent Skill for detecting meaningful updates on public websites and documents.

The canonical skill lives in `skills/web-update-monitor/`. Its bundled Python helpers handle deterministic fetching, normalization, hashing, diffing, report persistence, and snapshot promotion. The agent edits the target list, judges whether a detected change matters, and composes reports for material changes.

## Agent Skill

The repository's canonical distribution is the `skills/web-update-monitor/` directory, with its standard `SKILL.md` manifest and bundled scripts, requirements, and example CSV.

To install it in an Agent Skills-compatible runtime, download `web-update-monitor.zip` from a published GitHub release or from the `agent-skills` artifact of a successful [Package agent skills workflow run](https://github.com/dceoy/wsum/actions/workflows/agent-skills-package.yml?query=branch%3Amain), extract it, and place the `web-update-monitor/` directory in the runtime's skill discovery directory. Developers can build an equivalent archive from a checkout:

```bash
python scripts/package_skill.py
```

This creates `dist/web-update-monitor.zip`. The archive contains the complete skill directory, and its `SKILL.md` is byte-identical to the canonical manifest in this repository.

## Workspace

Use a local folder with a `targets.csv` file. A template is available at `skills/web-update-monitor/examples/targets.csv`.

```csv
name,url,watch_focus,enabled
OpenAI Pricing,https://openai.com/api/pricing/,Pricing and plan changes,true
Anthropic News,https://www.anthropic.com/news,Important product announcements,true
```

Columns:

- `name`: required display name.
- `url`: required public HTTP(S) URL.
- `watch_focus`: optional natural-language description of meaningful changes.
- `enabled`: optional `true` or `false`; blank defaults to `true`.

`target_id` is intentionally not user-facing. It is derived deterministically from the URL.

The workspace evolves into:

```text
workspace/
├── targets.csv
├── reports/
└── .wsum/
    ├── candidates/
    ├── pending/
    └── snapshots/
```

Users may edit `targets.csv`. `.wsum/` is internal state and should not be edited manually.

## Agent workflow

Read `skills/web-update-monitor/SKILL.md` for the complete procedure. At a high level, the agent edits `targets.csv` when requested, checks enabled targets, reviews bounded diffs for materiality, and writes a Markdown report only for material changes. The helper handles deterministic state transitions and per-target errors.

## Deterministic workspace facade

For development or direct invocation, run:

```bash
python skills/web-update-monitor/scripts/workspace.py \
  --workspace /path/to/workspace check
```

The facade validates the complete CSV before fetching any target. It automatically handles first baselines, unchanged snapshots, disabled rows, and per-target failures. Changed targets are returned to the agent for semantic review.

After the agent decides whether a change is material, it passes an internal decision to:

```bash
python skills/web-update-monitor/scripts/workspace.py \
  --workspace /path/to/workspace finalize < decision.json
```

The facade verifies the review revision, promotes the candidate snapshot, writes a report only after successful promotion, and clears pending state. If report persistence fails after promotion, retain the pending state and retry. A truncated diff cannot be finalized as non-material; it stops for manual review instead.

## Development and validation

Set up the repository with:

```bash
uv sync
```

Then run tests, build the skill archive, and validate the canonical skill with the [Agent Skills reference validator](https://github.com/agentskills/agentskills/tree/main/skills-ref):

```bash
uv run pytest
python scripts/package_skill.py
skills-ref validate skills/web-update-monitor
```

`monitor.py` can fetch a public HTTP(S) URL or normalize a supplied local/rendered document. `workflow.py` provides target validation, safe report writing, and atomic snapshot promotion.

Browser-rendered targets are outside the CSV workspace workflow. Do not auto-escalate a static failure to browser rendering. Use browser input only when the browser tool can enforce public-unicast egress, bounded redirects and subresources, a total timeout, and a maximum artifact size. Never provide cookies or credentials.

## Repository boundary

Do not commit fetched production content, `targets.csv`, snapshots, reports, `.wsum/`, credentials, browser profiles, or other deployment data.
