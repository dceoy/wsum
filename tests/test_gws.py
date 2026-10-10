"""Workspace-GWS deterministic boundary regression tests."""

import hashlib
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
    snapshot = {"name": f"workspace-{GENERATION}.zip", "id": "restored-id"}
    assert gws.next_generation([], now, None) == GENERATION
    assert gws.next_generation([snapshot], now, snapshot) == "20261009T000001Z"
    with pytest.raises(gws.GwsError, match="duplicate"):
        gws.next_generation([snapshot] * 2, now, snapshot)


@pytest.mark.parametrize(
    "current",
    [
        [],
        [{"name": f"workspace-{GENERATION}.zip", "id": "replacement-id"}],
        [{"name": "workspace-20261009T000001Z.zip", "id": "intervening-id"}],
    ],
)
def test_generation_rejects_changed_active_snapshot(
    current: list[dict[str, str]],
) -> None:
    expected = {"name": f"workspace-{GENERATION}.zip", "id": "restored-id"}
    with pytest.raises(gws.GwsError, match="active snapshot changed"):
        gws.next_generation(current, datetime(2026, 10, 9, tzinfo=UTC), expected)


def test_generation_refreshes_expectation_after_self_commit() -> None:
    now = datetime(2026, 10, 9, tzinfo=UTC)
    first = {"name": f"workspace-{GENERATION}.zip", "id": "first-id"}
    second = {"name": "workspace-20261009T000001Z.zip", "id": "second-id"}
    with pytest.raises(gws.GwsError, match="active snapshot changed"):
        gws.next_generation([second, first], now, first)
    assert gws.next_generation([second, first], now, second) == "20261009T000002Z"
    with pytest.raises(gws.GwsError, match="active snapshot changed"):
        gws.next_generation([first], now, None)


@pytest.mark.parametrize(
    ("snapshots", "expected"),
    [({}, None), (["filename"], None), ([{"name": "x", "id": ""}], None), ([], {})],
)
def test_generation_rejects_invalid_connector_records(
    snapshots: object, expected: object
) -> None:
    with pytest.raises(gws.GwsError, match=r"JSON|snapshot must contain"):
        gws.next_generation(snapshots, datetime(2026, 10, 9, tzinfo=UTC), expected)


@pytest.mark.parametrize("changed", [False, True])
def test_generation_cli_conflict_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    changed: bool,
) -> None:
    snapshot = {"name": f"workspace-{GENERATION}.zip", "id": "restored-id"}
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(snapshot))
    current = tmp_path / "current.json"
    current.write_text(
        json.dumps([{**snapshot, "id": "replacement-id"} if changed else snapshot])
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gws.py",
            "next-generation",
            "--snapshots-json",
            str(current),
            "--expected-snapshot-json",
            str(expected),
        ],
    )
    if changed:
        with pytest.raises(SystemExit) as exc:
            gws.main()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert not captured.out
        assert "active snapshot changed" in captured.err
    else:
        gws.main()
        result = json.loads(capsys.readouterr().out)
        assert result["generation"] > GENERATION


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


def _stub_core(tmp_path: Path) -> Path:
    skill = tmp_path / "core"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts/workspace.py").write_text(
        "from pathlib import Path\n"
        "def load_targets(path):\n"
        "    content = Path(path).read_text(encoding='utf-8')\n"
        "    if 'invalid' in content:\n"
        "        raise ValueError('invalid CSV')\n"
        "    return [{'id': 1}]\n"
    )
    return skill


def test_project_csv_preserves_bytes_and_atomic_validation(tmp_path: Path) -> None:
    core = _stub_core(tmp_path)
    source = tmp_path / "input.csv"
    dest = tmp_path / "workspace/internal/gws/targets.csv"
    content = b'name,url\r\n"A, B",https://example.com\r\n'
    source.write_bytes(content)
    assert gws.project_csv(source, dest, core) == 1
    assert dest.read_bytes() == content
    source.write_text("name,url\ninvalid,https://example.com\n")
    with pytest.raises(ValueError, match="invalid CSV"):
        gws.project_csv(source, dest, core)
    assert dest.read_bytes() == content


def test_project_csv_rejects_missing_symlink_or_large_source(tmp_path: Path) -> None:
    core = _stub_core(tmp_path)
    source = tmp_path / "input.csv"
    dest = tmp_path / "workspace/targets.csv"
    with pytest.raises(gws.GwsError, match="regular file"):
        gws.project_csv(source, dest, core)
    source.write_text("name,url\nA,https://example.com\n")
    symlink = tmp_path / "link.csv"
    symlink.symlink_to(source)
    with pytest.raises(gws.GwsError, match="regular file"):
        gws.project_csv(symlink, dest, core)
    source.write_bytes(b"x" * (gws.MAX_TARGET_CSV + 1))
    with pytest.raises(gws.GwsError, match="1 MiB"):
        gws.project_csv(source, dest, core)
    assert not dest.exists()


def test_project_csv_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    core = _stub_core(tmp_path)
    source = tmp_path / "targets.csv"
    source.write_text("name,url\nA,https://example.com\n")
    dest = tmp_path / "output.csv"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gws.py",
            "project",
            "--csv",
            str(source),
            "--targets",
            str(dest),
            "--core-skill-dir",
            str(core),
        ],
    )
    gws.main()
    assert json.loads(capsys.readouterr().out) == {"target_groups": 1}
    assert dest.read_bytes() == source.read_bytes()


def test_folder_binding_roundtrip_and_stable_ids(tmp_path: Path) -> None:
    marker = tmp_path / "binding.json"
    first = gws.binding_file(
        marker, "parent", "workspace-original", "reports-original", create=True
    )
    assert first["size"] == marker.stat().st_size
    assert first["sha256"] == hashlib.sha256(marker.read_bytes()).hexdigest()
    assert gws.binding_file(
        marker, "parent", "workspace-original", "reports-original", create=False
    ) == first
    with pytest.raises(FileExistsError):
        gws.binding_file(
            marker, "parent", "workspace-new", "reports-original", create=True
        )
    with pytest.raises(gws.GwsError, match="IDs or schema changed"):
        gws.binding_file(
            marker, "parent", "workspace-replacement", "reports-original", create=False
        )
    with pytest.raises(gws.GwsError, match="IDs or schema changed"):
        gws.binding_file(
            marker, "wrong-parent", "workspace-original", "reports-original",
            create=False,
        )


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        '{"version":2,"parent_id":"p","workspaces_id":"w","reports_id":"r"}',
        '{"version":1,"parent_id":"p","workspaces_id":"w","reports_id":"r","extra":0}',
        "not JSON",
    ],
)
def test_folder_binding_rejects_modified_metadata(tmp_path: Path, content: str) -> None:
    marker = tmp_path / "binding.json"
    marker.write_text(content)
    with pytest.raises((gws.GwsError, ValueError)):
        gws.binding_file(marker, "p", "w", "r", create=False)


def test_folder_binding_rejects_missing_symlink_or_duplicate_ids(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "binding.json"
    with pytest.raises(gws.GwsError, match="regular file"):
        gws.binding_file(marker, "p", "w", "r", create=False)
    marker.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(marker)
    with pytest.raises(gws.GwsError, match="regular file"):
        gws.binding_file(link, "p", "w", "r", create=False)
    with pytest.raises(gws.GwsError, match="duplicate Drive folder IDs"):
        gws.binding_file(tmp_path / "invalid.json", "p", "p", "r", create=True)


def test_folder_binding_cli_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = tmp_path / "binding.json"
    base = [
        "gws.py", "folder-binding", "--file", str(marker),
        "--parent-id", "p", "--workspaces-id", "w", "--reports-id", "r",
    ]
    monkeypatch.setattr(sys, "argv", [*base, "--create"])
    gws.main()
    created = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(sys, "argv", base)
    gws.main()
    assert json.loads(capsys.readouterr().out) == created


def test_drive_csv_integrity_rejects_row_truncation(tmp_path: Path) -> None:
    core = _stub_core(tmp_path)
    source = tmp_path / "downloaded.csv"
    destination = tmp_path / "workspace/targets.csv"
    complete = b"name,url\nA,https://example.com\nB,https://example.org\n"
    source.write_bytes(complete)
    digest = hashlib.md5(complete, usedforsecurity=False).hexdigest()
    assert gws.project_csv(
        source, destination, core, drive_size=len(complete), drive_md5=digest
    ) == 1
    assert destination.read_bytes() == complete
    source.write_bytes(b"name,url\nA,https://example.com\n")  # valid CSV, missing row
    with pytest.raises(gws.GwsError, match="size or MD5 mismatch"):
        gws.project_csv(
            source, destination, core, drive_size=len(complete), drive_md5=digest
        )
    assert destination.read_bytes() == complete


def test_drive_csv_integrity_rejects_same_length_corruption(tmp_path: Path) -> None:
    core = _stub_core(tmp_path)
    source = tmp_path / "downloaded.csv"
    destination = tmp_path / "targets.csv"
    original = b"name,url\nA,https://example.com\n"
    source.write_bytes(original)
    digest = hashlib.md5(original, usedforsecurity=False).hexdigest()
    source.write_bytes(original.replace(b"A,", b"B,"))
    with pytest.raises(gws.GwsError, match="size or MD5 mismatch"):
        gws.project_csv(
            source, destination, core, drive_size=len(original), drive_md5=digest
        )
    assert not destination.exists()
    with pytest.raises(gws.GwsError, match="both size and MD5"):
        gws.project_csv(source, destination, core, drive_size=len(original))
    with pytest.raises(gws.GwsError, match="invalid Drive CSV"):
        gws.project_csv(
            source, destination, core, drive_size=len(original), drive_md5="invalid"
        )


def test_drive_csv_cli_requires_integrity_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    core = _stub_core(tmp_path)
    source = tmp_path / "downloaded.csv"
    source.write_text("name,url\nA,https://example.com\n")
    dest = tmp_path / "targets.csv"
    base = [
        "gws.py", "project", "--drive-csv", str(source), "--targets", str(dest),
        "--core-skill-dir", str(core),
    ]
    monkeypatch.setattr(sys, "argv", base)
    with pytest.raises(SystemExit) as exc:
        gws.main()
    assert exc.value.code == 1
    assert "requires --drive-size and --drive-md5" in capsys.readouterr().err
    assert not dest.exists()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            *base, "--drive-size", str(source.stat().st_size),
            "--drive-md5", hashlib.md5(
                source.read_bytes(), usedforsecurity=False
            ).hexdigest(),
        ],
    )
    gws.main()
    assert json.loads(capsys.readouterr().out) == {"target_groups": 1}
    assert dest.read_bytes() == source.read_bytes()
