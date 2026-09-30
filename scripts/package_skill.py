"""Build a standard Agent Skill ZIP from the canonical skill directory."""

from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SKILL_ROOT = _REPOSITORY_ROOT / "skills" / "web-update-monitor"
_DEFAULT_OUTPUT = _REPOSITORY_ROOT / "dist" / "web-update-monitor.zip"
_ARCHIVE_ROOT = Path("web-update-monitor")


def build_package(output: Path = _DEFAULT_OUTPUT) -> Path:
    """Create a ZIP containing the complete canonical skill directory."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(_SKILL_ROOT.rglob("*")):
            if path.is_symlink():
                message = f"skill files must not be symlinks: {path}"
                raise RuntimeError(message)
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(_SKILL_ROOT)
            archive_path = _ARCHIVE_ROOT / relative
            archive.write(path, archive_path.as_posix())
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
