# wsum

Local-first Agent Skills for monitoring public web pages, PDFs, and feeds, researching meaningful changes, and producing cited Markdown reports. **Python handles fetching and durable state; the AI agent handles materiality decisions and research.** Google Workspace delivery is optional.

## Choose an entrypoint

| Goal                                               | Invoke                                                           | Required skills |
| -------------------------------------------------- | ---------------------------------------------------------------- | --------------- |
| Monitor targets with the core API                  | [`web-update-monitor`](skills/web-update-monitor/SKILL.md)       | Monitor         |
| Create reports in a local workspace                | [`web-update-report`](skills/web-update-report/SKILL.md)         | Report, monitor |
| Restore state and deliver reports via Google Drive | [`web-update-report-gws`](skills/web-update-report-gws/SKILL.md) | All three       |

`web-update-report` includes research and drafting for material changes. Install each required skill separately from the [packaging workflow](https://github.com/dceoy/wsum/actions/workflows/agent-skills-package.yml?query=branch%3Amain), or use this checkout's `.agents/skills/` discovery links (`.claude/skills/` points to them). Invoke only the chosen entrypoint.

## Quick start

### Local reporting

Use Python 3.11–3.14 and a host agent with Skill discovery and shell execution. From a checkout:

```bash
git clone https://github.com/dceoy/wsum.git
cd wsum
uv sync --no-dev
source .venv/bin/activate
```

Create `targets.csv` (see below) in the agent's working directory, then invoke:

```text
/web-update-report
```

By default, the skill reads `./targets.csv`, uses `.` as its persistent workspace, and reads `./context.md` **if present**. Missing `context.md` means no common context; missing/invalid `targets.csv` or an invalid existing context file is an error. Relative paths are resolved from the agent's working directory, not from the installed skill directory.

Override any of these paths when needed:

```text
/web-update-report
Targets: /absolute/path/to/targets.csv
Workspace: /absolute/path/to/workspace
Context source: /absolute/path/to/context.md
```

Only one external `Context source:` is supported (a local Markdown file); inline `Common context:` is no longer an input. An explicit source overrides the default `context.md`. The CSV and workspace remain independent paths. The skill validates input, creates a missing workspace root safely, resumes pending reviews, and writes reports under `output/report/` inside the selected workspace. **The first successful check establishes baselines; it does not produce a change report.** Subsequent material changes can produce one report per check run. No Google tools are required.

### Google Workspace reporting (optional)

Install all three skills and invoke:

```text
/web-update-report-gws
Targets: <LOCAL_CSV_OR_DRIVE_CSV_OR_GOOGLE_SHEET>
Output: <EXISTING_DEDICATED_DRIVE_FOLDER>
```

The wrapper restores small JSON checkpoints and content-addressed page objects, validates the current target table, invokes the local workflow, checkpoints core operations, and delivers **completed** Markdown reports through Drive. Unchanged large objects are reused across checkpoints, and only new objects are chunked for textContent transport. Optional context is one explicit Google Doc/Drive Markdown `Context source:`; inline context and local Markdown paths are **not** supported by this wrapper.

See the [GWS Skill](skills/web-update-report-gws/SKILL.md) and [Claude Code Routines runbook](docs/claude-code-routines.md) for folder binding, JSON state/Drive textContent transport, exact-byte verification, recovery, and limits. Legacy `wsum-text-1` folders require external migration or a fresh workspace. **Live Cowork/Routines connector round trips remain unverified**; offline connector tests are not an operational guarantee.

## How it works

```mermaid
flowchart LR
    CSV["Local targets CSV"] --> LOCAL["web-update-report"]
    GCSV["Drive CSV / Google Sheet"] --> GWS["web-update-report-gws"]
    GWS -->|"Restore + validate"| LOCAL
    LOCAL --> MON["web-update-monitor"]
    MON -->|"Changed / resumed review"| REVIEW{"Material?"}
    REVIEW -->|Yes| RESEARCH["Research within web-update-report"]
    REVIEW -->|No| FINAL["Core finalize"]
    RESEARCH -->|"Cited section"| FINAL
    FINAL -->|"Material only"| MD["output/report/<run-id>.md"]
    MD -->|"Completed GWS run"| GWS
    GWS -->|"Verified delivery"| DRIVE["Drive Markdown report"]
```

A new URL establishes its baseline without research. Each subsequent changed URL has one pending review; repeated CSV interests for the same URL share a fetch. The monitor persists candidate evidence and recovery state, while the agent evaluates semantic keywords, the optional single run-wide context, and newly linked documents. Material updates receive research; non-material changes advance the baseline without a report. The GWS wrapper persists each core transition and withholds delivery while reviews remain pending.

## Targets and workspace

Provide a UTF-8 CSV. Only `name` and `url` are required; optional columns are `publisher`, `category`, `keywords`, `priority`, and `enabled`. For example:

```csv
name,url,keywords,enabled
API updates,https://example.com/releases,breaking API changes,true
Pricing updates,https://example.com/releases,Report pricing changes,true
```

These are **placeholder URLs**, not live monitoring targets. Replace them before running. Repeated exact URLs share one fetch, and changes are material if they match any enabled interest. See the [CSV contract](skills/web-update-monitor/SKILL.md#workspace) for validation and traversal limits.

Local state is separate from the targets CSV:

```text
workspace/
├── output/report/             # canonical run-level Markdown reports
└── internal/
    ├── state/snapshots/        # accepted baselines
    ├── state/observations/     # last checked and baseline updated (UTC)
    ├── state/pending/          # resumable reviews
    ├── state/recovery/         # interrupted transactions
    └── evidence/               # optional captured source evidence
```

Only `output/` is user-facing. Do not edit `internal/` by hand. The GWS wrapper additionally stores input projection and delivery metadata under `internal/gws/` and verified snapshots in Drive.

## Core CLI (optional)

For debugging or manual orchestration, the monitor provides a deterministic CLI. Its workspace root must already exist:

```bash
mkdir -p /path/to/workspace
python skills/web-update-monitor/scripts/workspace.py \
  --workspace /path/to/workspace --targets /path/to/targets.csv check --compact
```

`check` alone does **not** perform an AI materiality review or finalize reports. Use `web-update-report` for end-to-end local reporting. See [monitor commands](skills/web-update-monitor/SKILL.md#interface) for `pending`, `finalize`, `ingest`, and recovery behavior.

## Development

```bash
uv sync
uv run pytest
```

[CI](.github/workflows/ci.yml) runs tests, linting, security checks, and validation of all three canonical Agent Skills with [`skills-ref`](https://github.com/agentskills/agentskills/tree/main/skills-ref).

Keep operational targets CSVs, downloaded content, `output/`, `internal/`, and credentials out of Git. Browser-rendered sources require separately controlled public-network egress; static-fetch failures must not silently trigger a browser.
