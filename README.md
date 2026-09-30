# wsum

A local-first Agent Skill for detecting meaningful updates on public websites and documents.

The canonical skill lives in `skills/web-update-monitor/`. It intentionally has two runtime helpers: `workspace.py` owns CSV/state/report orchestration, while `monitor.py` owns safe fetching, normalization, hashing, and bounded diffing. The agent edits the target list, judges whether a detected change matters, and composes report sections for material changes. All material changes finalized from one check run are aggregated into a single Markdown report.

## Agent Skill

The repository's canonical distribution is the `skills/web-update-monitor/` directory, with its standard `SKILL.md` manifest and bundled scripts, requirements, and example CSV.

To install it in an Agent Skills-compatible runtime, download `web-update-monitor.zip` from a published GitHub release or from the `agent-skills` artifact of a successful [Package agent skills workflow run](https://github.com/dceoy/wsum/actions/workflows/agent-skills-package.yml?query=branch%3Amain), extract it, and place the `web-update-monitor/` directory in the runtime's skill discovery directory.

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

### Generated files

The workspace contains one user-facing input, user-facing reports, and internal state:

- `targets.csv`: the user-facing source of truth for monitored targets. The agent may create or edit it when the user changes monitoring configuration.
- `reports/<run-id>.md`: the user-facing output. One report is created per `check` run only when at least one material change is finalized. The run ID has the form `YYYYMMDDTHHMMSSZ-xxxxxxxx`. Material targets from the same run are merged into this file.
- `.wsum/snapshots/<target-id>.txt`: the accepted normalized baseline for each target. A first observation creates it; later finalized observations replace it atomically, including non-material changes.
- `.wsum/candidates/<target-id>.txt`: the normalized candidate produced by the latest changed observation. It is retained while the agent reviews the diff and removed after successful finalization.
- `.wsum/pending/<target-id>.json`: internal review state linking a candidate to its run, revision, expected baseline hash, candidate hash, and diff-truncation status. It prevents stale decisions from being applied and is removed after successful finalization.

The helper may briefly create hidden `*.tmp` files while atomically replacing reports, snapshots, or pending state. These are implementation details and are cleaned up during normal operation.

## Agent workflow

Read `skills/web-update-monitor/SKILL.md` for the complete procedure. At a high level, the agent edits `targets.csv` when requested, checks enabled targets, reviews bounded diffs for materiality, and contributes each material target to one run-level Markdown report. The helper handles deterministic state transitions and per-target errors.

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

The facade verifies the review revision, promotes the candidate snapshot, merges material target sections into `reports/<run-id>.md` only after successful promotion, and clears pending state. Multiple material targets from the same check run therefore produce one report file. If report persistence fails after promotion, retain the pending state and retry. A truncated diff cannot be finalized as non-material; it stops for manual review instead.

## Development and validation

Set up the repository with:

```bash
uv sync
```

Then run tests and validate the canonical skill with the [Agent Skills reference validator](https://github.com/agentskills/agentskills/tree/main/skills-ref):

```bash
uv run pytest
skills-ref validate skills/web-update-monitor
```

`monitor.py` can fetch a public HTTP(S) URL or normalize a supplied local/rendered document. `workspace.py` validates targets and owns candidate/pending state, safe report writing, and atomic snapshot promotion.

Browser-rendered targets are outside the CSV workspace workflow. Do not auto-escalate a static failure to browser rendering. Use browser input only when the browser tool can enforce public-unicast egress, bounded redirects and subresources, a total timeout, and a maximum artifact size. Never provide cookies or credentials.

## Repository boundary

Do not commit fetched production content, `targets.csv`, snapshots, reports, `.wsum/`, credentials, browser profiles, or other deployment data.
