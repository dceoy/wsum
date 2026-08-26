"""Test path configuration."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / "skills" / "web-update-monitor" / "scripts"
sys.path.insert(0, str(SCRIPTS))
