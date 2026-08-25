# Website Update Monitor

A deterministic toolkit with two Agent Skills for website change monitoring. Both
variants use the same SSRF-safe fetch, normalization, diff, summary-validation,
retry, replay, and notification pipeline; only persistence differs.

- `weekly-web-monitor-google-drive` reads operational records through Google Sheets
  connectors and stores normalized snapshots in Google Drive.
- `weekly-web-monitor-local` stores targets, operational state, run history,
  notification state, and snapshots in a caller-selected local directory without
  Google Sheets or Google Drive.

The two registered skills live under `skills/` and share the implementation in
`skills/_weekly-web-monitor-shared/`. `.claude/skills` exposes the root skill tree,
and `.agents/skills` links the two registered monitor skills explicitly.

HTML, plain-text, and feed monitoring use only the standard library. PDF
normalization requires [`pypdf`](https://pypi.org/project/pypdf/); when running from
a source checkout, install it before monitoring PDFs or running the complete test
suite:

```bash
python3 -m pip install 'pypdf>=6,<7'
```

Run the local test suite:

```bash
python3 -m unittest discover -s tests -v
```

Run the end-to-end connector-free fixture through either registered skill:

```bash
python3 .claude/skills/weekly-web-monitor-local/scripts/dry_run.py \
  tests/fixtures/dry-run.json
```

## Repository boundary

GitHub stores code, schemas, documentation, and synthetic fixtures only. Never
commit fetched or rendered pages, PDFs/feeds from monitored sites, normalized
production snapshots, production diffs, logs, Sheets/Drive exports, credentials,
cookies, browser profiles, webhook URLs, connector configuration, signed URLs,
spreadsheet/Drive identifiers, or local runtime/replay artifacts.

The weekly schedule, connector identifiers, destination mappings, credentials, and
production local runtime root are configured outside this repository.
