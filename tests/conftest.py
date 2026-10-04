"""Test path configuration."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / "skills" / "web-update-monitor" / "scripts"
WIKI_SCRIPTS = (
    Path(__file__).parents[1] / "skills" / "web-update-monitor-llm-wiki" / "scripts"
)
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(WIKI_SCRIPTS))
