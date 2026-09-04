"""Build a Claude Cowork ZIP from the canonical Agent Skill."""

from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SKILL_ROOT = _REPOSITORY_ROOT / "skills" / "web-update-monitor"
_DEFAULT_OUTPUT = _REPOSITORY_ROOT / "dist" / "web-update-monitor.zip"
_COWORK_DEPENDENCIES = "python>=3.11, pypdf>=6.15,<7"
_ARCHIVE_ROOT = Path("web-update-monitor")


def _cowork_manifest(source: str) -> str:
    """Add Cowork dependency metadata to the canonical Skill manifest."""
    if not source.startswith("---\n"):
        raise RuntimeError("SKILL.md must start with YAML frontmatter")
    closing = source.find("\n---\n", 4)
    if closing < 0:
        raise RuntimeError("SKILL.md frontmatter is not terminated")
    frontmatter = source[:closing]
    body = source[closing + len("\n---\n") :]
    return f'{frontmatter}\ndependencies: "{_COWORK_DEPENDENCIES}"\n---\n{body}'


def build_package(output: Path = _DEFAULT_OUTPUT) -> Path:
    """Create a Cowork-compatible ZIP and return its path."""
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = _cowork_manifest((_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"))
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(str(_ARCHIVE_ROOT / "skill.md"), manifest)
        for path in sorted(_SKILL_ROOT.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(_SKILL_ROOT)
            if relative.name == "SKILL.md" or relative.parts[0] == "agents":
                continue
            if "__pycache__" in relative.parts:
                continue
            archive.write(path, str(_ARCHIVE_ROOT / relative))
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    return parser


def main() -> int:
    """Build the package from command-line arguments."""
    output = build_package(_parser().parse_args().output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
