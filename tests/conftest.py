"""Test path configuration."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / "skills" / "wsum-monitor" / "scripts"
sys.path.insert(0, str(SCRIPTS))
GWS_SCRIPTS = Path(__file__).parents[1] / "skills/wsum-gws/scripts"
sys.path.insert(0, str(GWS_SCRIPTS))
