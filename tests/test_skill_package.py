"""Tests for standard Agent Skill packaging."""

from __future__ import annotations

import re
import runpy
from pathlib import Path
from typing import TYPE_CHECKING, cast
from zipfile import ZipFile

if TYPE_CHECKING:
    from collections.abc import Callable


def test_skill_package_preserves_manifest_and_includes_skill_files(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).parents[1]
    skill_root = repository_root / "skills" / "web-update-monitor"
    script = repository_root / "scripts" / "package_skill.py"
    namespace = runpy.run_path(str(script))
    build_package = cast("Callable[[Path], Path]", namespace["build_package"])
    output = build_package(tmp_path / "web-update-monitor.zip")

    with ZipFile(output) as archive:
        names = set(archive.namelist())
        manifest = archive.read("web-update-monitor/SKILL.md")
        requirements = archive.read("web-update-monitor/requirements.txt")

    source_manifest = (skill_root / "SKILL.md").read_bytes()
    manifest_text = manifest.decode("utf-8")
    skill_name = re.search(r"(?m)^name: ([a-z0-9-]+)$", manifest_text)

    assert "web-update-monitor/SKILL.md" in names
    assert "web-update-monitor/skill.md" not in names
    assert skill_name is not None
    assert skill_name.group(1) == "web-update-monitor"
    assert manifest == source_manifest
    assert b"dependencies:" not in manifest
    assert requirements == (skill_root / "requirements.txt").read_bytes()
    assert "web-update-monitor/scripts/workspace.py" in names
    assert "web-update-monitor/scripts/monitor.py" in names
    assert "web-update-monitor/scripts/workflow.py" in names
    assert "web-update-monitor/examples/targets.csv" in names
    assert not any(name.startswith("web-update-monitor/agents/") for name in names)
