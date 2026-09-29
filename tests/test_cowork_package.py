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
    output = build_package(tmp_path / "wsum.zip")

    with ZipFile(output) as archive:
        names = set(archive.namelist())
        manifest = archive.read("wsum/skill.md").decode("utf-8")

    assert "wsum/skill.md" in names
    assert "wsum/SKILL.md" not in names
    assert "wsum/scripts/cowork.py" in names
    assert "wsum/scripts/monitor.py" in names
    assert "wsum/scripts/workflow.py" in names
    assert "wsum/examples/targets.csv" in names
    assert not any(name.startswith("wsum/agents/") for name in names)
    assert 'dependencies: "python>=3.11, pypdf>=6.15,<7"' in manifest
