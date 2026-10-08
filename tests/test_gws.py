"""Workspace-GWS deterministic boundary regression tests."""

import importlib.util
import json
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "skills/web-update-monitor-gws/scripts/gws.py"
spec = importlib.util.spec_from_file_location("gws", MODULE)
gws = importlib.util.module_from_spec(spec)
sys.modules["gws"] = gws
spec.loader.exec_module(gws)
GENERATION = "20261009T000000Z"


def _workspace(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "output/report").mkdir(parents=True)
    (ws / "internal/state").mkdir(parents=True)
    (ws / "output/report/demo.md").write_text("report\n")
    (ws / "internal/state/snapshot.bin").write_bytes(b"\0\xffabc")
    return ws


def _archive(tmp_path):
    ws = _workspace(tmp_path)
    archive = tmp_path / f"workspace-{GENERATION}.zip"
    gws.pack(ws, archive, GENERATION)
    return ws, archive


def test_snapshot_roundtrip(tmp_path):
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


@pytest.mark.parametrize("entry", ["../escape", "internal/../escape", "/internal/escape", "output//x", "foo/bar"])
def test_reject_unsafe_paths(tmp_path, entry):
    archive = tmp_path / f"workspace-{GENERATION}.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("manifest.json", json.dumps({"version": 1, "generation": GENERATION, "files": []}))
        writer.writestr(entry, "bad")
    with pytest.raises(gws.GwsError, match="unsafe"):
        gws.verify(archive)
    assert not (tmp_path / "restored").exists()


def test_reject_duplicate_archive_member(tmp_path):
    archive = tmp_path / f"workspace-{GENERATION}.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("manifest.json", "{}")
        writer.writestr("internal/a", "old")
        writer.writestr("internal/a", "new")
    with pytest.raises(gws.GwsError, match="duplicate"):
        gws.verify(archive)


def test_reject_manifest_digest_mismatch(tmp_path):
    _, archive = _archive(tmp_path)
    with zipfile.ZipFile(archive, "a") as writer:
        writer.writestr("output/report/extra.md", "unrecorded")
    with pytest.raises(gws.GwsError, match="manifest"):
        gws.verify(archive)


def test_reject_symlink_input(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "internal/link").symlink_to(ws / "output/report/demo.md")
    with pytest.raises(gws.GwsError, match="non-regular"):
        gws.pack(ws, tmp_path / f"workspace-{GENERATION}.zip", GENERATION)


def test_generation_monotonicity_and_duplicates():
    now = datetime(2026, 10, 9, tzinfo=UTC)
    assert gws.next_generation([], now) == GENERATION
    assert gws.next_generation([f"workspace-{GENERATION}.zip"], now) == "20261009T000001Z"
    with pytest.raises(gws.GwsError, match="duplicate"):
        gws.next_generation([f"workspace-{GENERATION}.zip"] * 2, now)


def test_delivery_ledger_fail_closed(tmp_path):
    path = tmp_path / "delivery.json"
    path.write_text('{"version":1,"reports":{}}')
    report = tmp_path / "20261009T000000Z-abcdef12.md"
    report.write_text("report")
    assert gws.ledger(path, report, False)["delivered"] is False
    gws.ledger(path, report, True)
    assert gws.ledger(path, report, False)["delivered"] is True
    report.write_text("mutated")
    with pytest.raises(gws.GwsError, match="changed"):
        gws.ledger(path, report, False)
    path.write_text('{"version":1,"reports":{},"extra":0}')
    with pytest.raises(gws.GwsError, match="invalid"):
        gws.ledger(path, report, False)


def test_project_atomic_with_core_validation(tmp_path):
    skill = tmp_path / "core"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts/workspace.py").write_text("def load_targets(path):\n    text = open(path).read()\n    if 'invalid' in text:\n        raise ValueError('invalid row')\n    return [{'id': 1}]\n")
    source = tmp_path / "sheet.json"
    dest = tmp_path / "targets.csv"
    source.write_text(json.dumps({"values": [["url", "name", "ignored"], ["https://example.com", "A, B", "x"]]}))
    assert gws.project(source, dest, skill) == 1
    assert '"A, B"' in dest.read_text()
    original = dest.read_bytes()
    source.write_text(json.dumps({"values": [["url", "name"], ["https://example.com", "invalid"]]}))
    with pytest.raises(ValueError, match="invalid row"):
        gws.project(source, dest, skill)
    assert dest.read_bytes() == original
    source.write_text(json.dumps({"values": [["url", "name", "name"], ["x", "y", "z"]]}))
    with pytest.raises(gws.GwsError, match="duplicate"):
        gws.project(source, dest, skill)
