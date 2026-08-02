# Website Update Monitor

A deterministic toolkit and Agent Skill for weekly website change monitoring. It reads configuration/state through Google Sheets connectors, fetches
public HTTP(S) resources under a strict SSRF policy, normalizes HTML/PDF/RSS/Atom
content, scores bounded diffs, validates evidence-grounded Japanese summaries,
stores normalized artifacts through Google Drive, and sends idempotent grouped
Slack notifications.

The implementation lives in
`.claude/skills/weekly-web-monitor/`. Start with its `SKILL.md` and references.

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
python3 .claude/skills/weekly-web-monitor/scripts/dry_run.py \
  tests/fixtures/dry-run.json
```

## Repository boundary

GitHub stores code, schemas, documentation, and synthetic fixtures only. Never
commit fetched or rendered pages, PDFs/feeds from monitored sites, normalized
production snapshots, production diffs, logs, Sheets/Drive exports, credentials,
cookies, browser profiles, webhook URLs, connector configuration, signed URLs,
spreadsheet/Drive identifiers, or local runtime/replay artifacts.

The weekly schedule and all connector identifiers, destination mappings, and
credentials are configured outside this repository.
