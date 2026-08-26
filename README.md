# Web Update Monitor

A deterministic toolkit and Agent Skill for website update monitoring. The monitor
uses the same SSRF-safe fetch, normalization, diff, summary-validation, retry,
replay, and notification pipeline with one of two persistence modes selected at
runtime:

- `google-drive`: read targets and operational records through Google Sheets and
  store normalized snapshots in Google Drive.
- `local`: read targets from `targets.json`, store operational state and history in
  SQLite, and store content-addressed snapshots in a caller-selected local runtime
  directory.

The registered skill is `web-update-monitor` and lives in
`.claude/skills/web-update-monitor/`. Start with its `SKILL.md` and references.

HTML, plain-text, and feed monitoring use only the standard library. PDF
normalization requires [`pypdf`](https://pypi.org/project/pypdf/); when running
from a source checkout, install it before monitoring PDFs or running the complete
test suite:

```bash
python3 -m pip install 'pypdf>=6,<7'
```

Run the local test suite:

```bash
python3 -m unittest discover -s tests -v
```

Run the end-to-end connector-free fixture:

```bash
python3 .claude/skills/web-update-monitor/scripts/dry_run.py \
  tests/fixtures/dry-run.json
```

## Repository boundary

GitHub stores code, schemas, documentation, and synthetic fixtures only. Never
commit fetched or rendered pages, PDFs/feeds from monitored sites, normalized
production snapshots, production diffs, logs, Sheets/Drive exports, SQLite
databases, credentials, cookies, browser profiles, webhook URLs, connector
configuration, signed URLs, spreadsheet/Drive identifiers, or local runtime/replay
artifacts.

Execution cadence is configured outside this repository. The skill may be invoked
ad hoc or by any non-overlapping external schedule appropriate to the monitored
targets; connector identifiers, destination mappings, credentials, and production
local runtime roots also remain deployment configuration.
