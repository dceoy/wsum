"""Tests for Claude Cowork skill packaging."""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import TYPE_CHECKING, cast
from zipfile import ZipFile

if TYPE_CHECKING:
    from collections.abc import Callable


def test_cowork_package_has_one_manifest_and_runtime_files(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "package_cowork_skill.py"
    namespace = runpy.run_path(str(script))
    build_package = cast("Callable[[Path], Path]", namespace["build_package"])
    output = build_package(tmp_path / "web-update-monitor.zip")

    with ZipFile(output) as archive:
        names = set(archive.namelist())
        manifest = archive.read("web-update-monitor/skill.md").decode("utf-8")

    assert "web-update-monitor/skill.md" in names
    assert "web-update-monitor/SKILL.md" not in names
    assert "web-update-monitor/scripts/cowork.py" in names
    assert "web-update-monitor/scripts/monitor.py" in names
    assert "web-update-monitor/scripts/workflow.py" in names
    assert "web-update-monitor/examples/targets.csv" in names
    assert not any(name.startswith("web-update-monitor/agents/") for name in names)
    assert 'dependencies: "python>=3.11, pypdf>=6.15,<7"' in manifest
