"""Test path configuration."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / "skills" / "web-update-monitor" / "scripts"
sys.path.insert(0, str(SCRIPTS))
GWS_SCRIPTS = Path(__file__).parents[1] / "skills/web-update-report-gws/scripts"
sys.path.insert(0, str(GWS_SCRIPTS))
