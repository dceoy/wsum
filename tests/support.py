from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / ".claude" / "skills" / "weekly-web-monitor" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def public_resolver(host: str, port: int, **_: object) -> list[tuple]:
    del host
    return [(2, 1, 6, "", ("93.184.216.34", port))]
