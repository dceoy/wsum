"""Shared test-only helpers: scripts-path setup and a fake DNS resolver."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "web-monitor" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def public_resolver(host: str, port: int, **_: object) -> list[tuple[Any, ...]]:
    """Resolve any host to a fixed public IPv4 address for deterministic tests.

    Returns:
        A single ``getaddrinfo``-shaped result pointing at a public address.
    """
    del host
    return [(2, 1, 6, "", ("93.184.216.34", port))]
