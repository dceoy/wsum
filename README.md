# Web Update Monitor

A local-first Agent Skill for detecting meaningful updates on public websites and documents.

The implementation keeps deterministic mechanics in Python and semantic judgment in the agent:

- `monitor.py` fetches or reads content, normalizes it, hashes it, and emits a bounded diff plus a candidate snapshot.
- `workflow.py` safely validates targets, writes reports, and promotes snapshots.
- `cowork.py` turns a simple CSV workspace into a multi-target Cowork workflow and hides internal IDs, hashes, and promotion details from the user.
- the agent judges whether a bounded diff matters and composes a concise Markdown report only for material changes.

## Claude Cowork

The Cowork workflow is designed so a non-engineer only needs to manage a folder and describe what to monitor.

### 1. Install the skill

Build the Cowork package from the repository:

```bash
python scripts/package_cowork_skill.py
```

Upload `dist/web-update-monitor.zip` from Claude's custom Skills UI.

The repository keeps the portable Agent Skill manifest as `SKILL.md`. The packager emits it as lowercase `skill.md` inside the Cowork ZIP and adds the Python and `pypdf` dependency metadata required by Cowork. This avoids keeping case-colliding `SKILL.md` and `skill.md` files in the source tree.

### 2. Connect a workspace folder

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

### 3. Ask Cowork in natural language

Examples:

```text
Check every target in targets.csv and summarize meaningful updates.
```

```text
Add https://example.com/pricing to the monitor and watch for pricing changes.
```

```text
Disable the Anthropic News target.
```

Cowork edits the CSV when needed, checks all enabled rows, and writes Markdown files under `reports/` only when a change is material.

## Deterministic Cowork facade

For development or direct invocation, run:

```bash
python skills/web-update-monitor/scripts/cowork.py \
  --workspace /path/to/workspace check
```

The facade validates the complete CSV before fetching any target. It automatically handles first baselines, unchanged snapshots, disabled rows, and per-target failures. Changed targets are returned to the agent for semantic review.

After the agent decides whether a change is material, it passes an internal decision to:

```bash
python skills/web-update-monitor/scripts/cowork.py \
  --workspace /path/to/workspace finalize < decision.json
```

The facade then writes a report when needed, verifies the expected baseline, promotes the candidate snapshot, and clears pending state. A truncated diff cannot be finalized as non-material; it stops for manual review instead.

## Direct low-level workflow

The existing deterministic helpers remain available for development and non-Cowork integrations.

Set up the repository with:

```bash
uv sync
```

`monitor.py` can fetch a public HTTP(S) URL or normalize a supplied local/rendered document. `workflow.py` provides target validation, safe report writing, and atomic snapshot promotion. See `skills/web-update-monitor/SKILL.md` for the canonical agent procedure.

Browser-rendered targets are intentionally outside the Cowork CSV facade. Do not auto-escalate a static failure to browser rendering. Use browser input only when the browser tool can enforce public-unicast egress, bounded redirects and subresources, a total timeout, and a maximum artifact size. Never provide cookies or credentials.

## Validation

```bash
uv run pytest
python scripts/package_cowork_skill.py
```

The local QA skill also runs Ruff, Pyright, Markdown formatting, and GitHub Actions checks.

## Repository boundary

Do not commit fetched production content, `targets.csv`, snapshots, reports, `.wsum/`, credentials, browser profiles, or other deployment data.
