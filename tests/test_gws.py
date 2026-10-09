"""Workspace-GWS deterministic boundary regression tests."""

import json
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import gws
import pytest

GENERATION = "20261009T000000Z"


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "output/report").mkdir(parents=True)
    (ws / "internal/state").mkdir(parents=True)
    (ws / "output/report/demo.md").write_text("report\n")
    (ws / "internal/state/snapshot.bin").write_bytes(b"\0\xffabc")
    return ws


def _archive(tmp_path: Path) -> tuple[Path, Path]:
    ws = _workspace(tmp_path)
    archive = tmp_path / f"workspace-{GENERATION}.zip"
    gws.pack(ws, archive, GENERATION)
    return ws, archive


def test_snapshot_roundtrip(tmp_path: Path) -> None:
    ws, archive = _archive(tmp_path)
    verified = gws.verify(archive)
    assert verified["file_count"] == 2
    dest = tmp_path / "restored"
    gws.restore(archive, dest)
    for path in ws.rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (dest / path.relative_to(ws)).read_bytes()
    with pytest.raises(gws.GwsError, match="must not exist"):
        gws.restore(archive, dest)
    with pytest.raises(gws.GwsError, match="overwrite"):
        gws.pack(ws, archive, GENERATION)


@pytest.mark.parametrize(
    "entry",
    ["../escape", "internal/../escape", "/internal/escape", "output//x", "foo/bar"],
)
def test_reject_unsafe_paths(tmp_path: Path, entry: str) -> None:
    archive = tmp_path / f"workspace-{GENERATION}.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr(
            "manifest.json",
            json.dumps({"version": 1, "generation": GENERATION, "files": []}),
        )
        writer.writestr(entry, "bad")
    with pytest.raises(gws.GwsError, match="unsafe"):
        gws.verify(archive)
    assert not (tmp_path / "restored").exists()


def test_reject_duplicate_archive_member(tmp_path: Path) -> None:
    archive = tmp_path / f"workspace-{GENERATION}.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("manifest.json", "{}")
        writer.writestr("internal/a", "old")
        writer.writestr("internal/a", "new")
    with pytest.raises(gws.GwsError, match="duplicate"):
        gws.verify(archive)


def test_reject_manifest_digest_mismatch(tmp_path: Path) -> None:
    _, archive = _archive(tmp_path)
    with zipfile.ZipFile(archive, "a") as writer:
        writer.writestr("output/report/extra.md", "unrecorded")
    with pytest.raises(gws.GwsError, match="manifest"):
        gws.verify(archive)


def test_reject_symlink_input(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    (ws / "internal/link").symlink_to(ws / "output/report/demo.md")
    with pytest.raises(gws.GwsError, match="non-regular"):
        gws.pack(ws, tmp_path / f"workspace-{GENERATION}.zip", GENERATION)


def test_reject_oversized_input_before_reading(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    (ws / "output").mkdir(parents=True)
    (ws / "internal").mkdir()
    oversized = ws / "output/large.bin"
    with oversized.open("wb") as stream:
        stream.truncate(gws.MAX_ENTRY + 1)
    with pytest.raises(gws.GwsError, match="size limit"):
        gws.pack(ws, tmp_path / f"workspace-{GENERATION}.zip", GENERATION)


def test_generation_monotonicity_and_duplicates() -> None:
    now = datetime(2026, 10, 9, tzinfo=UTC)
    assert gws.next_generation([], now) == GENERATION
    assert (
        gws.next_generation([f"workspace-{GENERATION}.zip"], now) == "20261009T000001Z"
    )
    with pytest.raises(gws.GwsError, match="duplicate"):
        gws.next_generation([f"workspace-{GENERATION}.zip"] * 2, now)


def test_delivery_ledger_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "delivery.json"
    path.write_text('{"version":1,"reports":{}}')
    report = tmp_path / "20261009T000000Z-abcdef12.md"
    report.write_text("report")
    assert gws.ledger(path, report, record=False)["delivered"] is False
    assert gws.ledger(path, report, record=True)["delivered"] is True
    assert gws.ledger(path, report, record=False)["delivered"] is True
    report.unlink()
    with pytest.raises(gws.GwsError, match="missing report"):
        gws.ledger(path, report, record=False)
    report.write_text("report")
    report.write_text("mutated")
    with pytest.raises(gws.GwsError, match="changed"):
        gws.ledger(path, report, record=False)
    path.write_text('{"version":1,"reports":{},"extra":0}')
    with pytest.raises(gws.GwsError, match="invalid"):
        gws.ledger(path, report, record=False)


def test_project_atomic_with_core_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = tmp_path / "core"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts/workspace.py").write_text(
        "from pathlib import Path\n"
        "def load_targets(path):\n"
        "    if 'invalid' in Path(path).read_text(encoding='utf-8'):\n"
        "        raise ValueError('requested validator rejected invalid row')\n"
        "    return [{'id': 1}]\n"
    )

    class ConflictingWorkspace(ModuleType):
        """Fail if projection selects a cached module instead of the requested path."""

        @staticmethod
        def load_targets(_: Path) -> list[dict[str, int]]:
            pytest.fail("used cached workspace module instead of requested core skill")

    conflicting_core = ConflictingWorkspace("workspace")
    monkeypatch.setitem(sys.modules, "workspace", conflicting_core)
    source = tmp_path / "sheet.json"
    dest = tmp_path / "targets.csv"
    source.write_text(
        json.dumps({
            "values": [["url", "name", "ignored"], ["https://example.com", "A, B", "x"]]
        })
    )
    assert gws.project(source, dest, skill) == 1
    assert '"A, B"' in dest.read_text()
    original = dest.read_bytes()
    source.write_text(
        json.dumps({"values": [["url", "name"], ["https://example.com", "invalid"]]})
    )
    with pytest.raises(ValueError, match="requested validator rejected invalid row"):
        gws.project(source, dest, skill)
    assert dest.read_bytes() == original
    source.write_text(
        json.dumps({
            "values": [["url", "name"], ["https://example.com", "A", "unexpected"]]
        })
    )
    with pytest.raises(gws.GwsError, match="beyond the header"):
        gws.project(source, dest, skill)
    assert dest.read_bytes() == original
    source.write_text(
        json.dumps({"values": [["url", "name", "name"], ["x", "y", "z"]]})
    )
    with pytest.raises(gws.GwsError, match="duplicate"):
        gws.project(source, dest, skill)


@pytest.mark.parametrize(
    "ledger_content", [None, "{}", "not JSON", '{"version":1,"reports":{}}']
)
def test_validate_restored_zero_report_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ledger_content: str | None,
) -> None:
    ws = tmp_path / "workspace"
    (ws / "output/report").mkdir(parents=True)
    (ws / "internal/gws").mkdir(parents=True)
    path = ws / "internal/gws/delivery.json"
    if ledger_content is not None:
        path.write_text(ledger_content)
    archive = tmp_path / f"workspace-{GENERATION}.zip"
    gws.pack(ws, archive, GENERATION)
    dest = tmp_path / "restored"
    gws.restore(archive, dest)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gws.py",
            "ledger-validate",
            "--ledger",
            str(dest / "internal/gws/delivery.json"),
            "--reports-dir",
            str(dest / "output/report"),
        ],
    )
    if ledger_content == '{"version":1,"reports":{}}':
        gws.main()
        assert json.loads(capsys.readouterr().out) == {"valid": True, "report_count": 0}
    else:
        with pytest.raises(SystemExit) as exc:
            gws.main()
        assert exc.value.code == 1
        assert "gws:" in capsys.readouterr().err
