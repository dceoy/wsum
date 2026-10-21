"""Workspace-GWS deterministic boundary regression tests."""

import base64
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TypedDict, cast

import gws
import pytest

GENERATION = "20261009T000000Z"


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "output/report").mkdir(parents=True)
    (ws / "internal/state").mkdir(parents=True)
    (ws / "internal/gws").mkdir()
    (ws / "internal/gws/delivery.json").write_text('{"version":1,"reports":{}}')
    (ws / "output/report/demo.md").write_bytes("é\r\n報告".encode())
    (ws / "internal/state/snapshot.txt").write_bytes(b"\xef\xbb\xbfbaseline\n")
    return ws


def test_json_pack_deterministic_and_rejects_overwrite(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    gws.json_pack(ws, first, GENERATION)
    gws.json_pack(ws, second, GENERATION)
    for file in first.rglob("*"):
        if file.is_file():
            assert file.read_bytes() == (second / file.relative_to(first)).read_bytes()
    with pytest.raises(gws.GwsError, match="overwrite"):
        gws.json_pack(ws, first, GENERATION)


@pytest.mark.parametrize("content", [b"\xff", b"\x00", b"hello\x01"])
def test_json_pack_rejects_binary(tmp_path: Path, content: bytes) -> None:
    ws = _workspace(tmp_path)
    (ws / "internal/state/binary").write_bytes(content)
    destination = tmp_path / "packed"
    with pytest.raises((gws.GwsError, UnicodeError)):
        gws.json_pack(ws, destination, GENERATION)
    assert not destination.exists()


@pytest.mark.parametrize(
    "entry",
    [
        "../escape",
        "internal/../escape",
        "/internal/escape",
        "output//x",
        "foo/bar",
        "output/a\\b",
        "output/a\x00b",
    ],
)
def test_json_restore_rejects_unsafe_paths(tmp_path: Path, entry: str) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({
            "format": gws.JSON_FORMAT,
            "generation": GENERATION,
            "files": [
                {
                    "path": entry,
                    "size": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "text": "",
                }
            ],
        })
    )
    readbacks = tmp_path / "readbacks.json"
    readbacks.write_text("{}")
    with pytest.raises(gws.GwsError, match="unsafe"):
        gws.restore(manifest, readbacks, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("directory", [False, True])
def test_json_pack_rejects_symlink(tmp_path: Path, directory: bool) -> None:
    ws = _workspace(tmp_path)
    (ws / "internal/link").symlink_to(
        ws / ("output" if directory else "output/report/demo.md")
    )
    with pytest.raises(gws.GwsError, match="non-regular"):
        gws.json_pack(ws, tmp_path / "packed", GENERATION)


def test_json_pack_rejects_oversized_input(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    with (ws / "output/large").open("wb") as stream:
        stream.truncate(gws.MAX_ENTRY + 1)
    with pytest.raises(gws.GwsError, match="size limit"):
        gws.json_pack(ws, tmp_path / "packed", GENERATION)


def test_generation_guard() -> None:
    now = datetime(2026, 10, 9, tzinfo=UTC)
    snapshot = {"name": f"workspace-{GENERATION}.json", "id": "restored-id"}
    assert gws.next_generation([], now, None) == GENERATION
    assert gws.next_generation([snapshot], now, snapshot) == "20261009T000001Z"
    with pytest.raises(gws.GwsError, match="duplicate"):
        gws.next_generation([snapshot] * 2, now, snapshot)
    with pytest.raises(gws.GwsError, match="active snapshot changed"):
        gws.next_generation([snapshot], now, None)


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
    assert dest.read_text().splitlines()[0] == (
        "name,url,publisher,category,keywords,priority,enabled"
    )
    source.write_text(
        json.dumps({
            "values": [
                ["name", "url", "criteria", "keywords"],
                ["A", "https://example.com", "legacy", "API"],
            ]
        })
    )
    assert gws.project(source, dest, skill) == 1
    assert "legacy" not in dest.read_text()
    assert "API" in dest.read_text()
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
    ws = _workspace(tmp_path)
    path = ws / "internal/gws/delivery.json"
    if ledger_content is None:
        path.unlink()
    else:
        path.write_text(ledger_content)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gws.py",
            "ledger-validate",
            "--ledger",
            str(path),
            "--reports-dir",
            str(ws / "output/report"),
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
    assert (
        gws.binding_file(
            marker, "parent", "workspace-original", "reports-original", create=False
        )
        == first
    )
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
            marker,
            "wrong-parent",
            "workspace-original",
            "reports-original",
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
        "gws.py",
        "folder-binding",
        "--file",
        str(marker),
        "--parent-id",
        "p",
        "--workspaces-id",
        "w",
        "--reports-id",
        "r",
    ]
    monkeypatch.setattr(sys, "argv", [*base, "--create"])
    gws.main()
    created = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(sys, "argv", base)
    gws.main()
    assert json.loads(capsys.readouterr().out) == created


def test_drive_csv_size_rejects_row_truncation(tmp_path: Path) -> None:
    core = _stub_core(tmp_path)
    source = tmp_path / "downloaded.csv"
    destination = tmp_path / "workspace/targets.csv"
    complete = b"name,url\nA,https://example.com\nB,https://example.org\n"
    source.write_bytes(complete)
    assert gws.project_csv(source, destination, core, drive_size=len(complete)) == 1
    assert destination.read_bytes() == complete
    source.write_bytes(b"name,url\nA,https://example.com\n")  # valid CSV, missing row
    with pytest.raises(gws.GwsError, match="size mismatch"):
        gws.project_csv(source, destination, core, drive_size=len(complete))
    assert destination.read_bytes() == complete


def test_drive_csv_size_metadata_validation(tmp_path: Path) -> None:
    core = _stub_core(tmp_path)
    source = tmp_path / "downloaded.csv"
    destination = tmp_path / "targets.csv"
    source.write_bytes(b"name,url\nA,https://example.com\n")
    for invalid in (0, -1, True, gws.MAX_TARGET_CSV + 1):
        with pytest.raises(gws.GwsError, match="invalid Drive CSV size"):
            gws.project_csv(source, destination, core, drive_size=invalid)
    assert not destination.exists()


def test_drive_csv_cli_requires_size_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    core = _stub_core(tmp_path)
    source = tmp_path / "downloaded.csv"
    source.write_text("name,url\nA,https://example.com\n")
    dest = tmp_path / "targets.csv"
    base = [
        "gws.py",
        "project",
        "--drive-csv",
        str(source),
        "--targets",
        str(dest),
        "--core-skill-dir",
        str(core),
    ]
    monkeypatch.setattr(sys, "argv", base)
    with pytest.raises(SystemExit) as exc:
        gws.main()
    assert exc.value.code == 1
    assert "requires --drive-size" in capsys.readouterr().err
    assert not dest.exists()
    monkeypatch.setattr(
        sys,
        "argv",
        [*base, "--drive-size", str(source.stat().st_size)],
    )
    gws.main()
    assert json.loads(capsys.readouterr().out) == {"target_groups": 1}
    assert dest.read_bytes() == source.read_bytes()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("size", True),
        ("sha256", "invalid"),
        ("id", "../file"),
        ("path", "internal/../escape"),
        ("extra", "unsupported"),
    ],
)
def test_restore_rejects_invalid_manifest_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    ws = _json_workspace(tmp_path)
    manifest, readbacks, _, _ = _json_commit(ws, tmp_path, GENERATION)
    data = json.loads(manifest.read_text())
    data["files"][0][field] = value
    manifest.write_text(json.dumps(data))
    destination = tmp_path / "restored"
    with pytest.raises(gws.GwsError):
        gws.restore(manifest, readbacks, destination)
    assert not destination.exists()


@pytest.mark.parametrize("suffix", ["", "/child"])
def test_restore_rejects_conflicting_paths(tmp_path: Path, suffix: str) -> None:
    manifest, readbacks, _, _ = _json_commit(
        _json_workspace(tmp_path), tmp_path, GENERATION
    )
    data = json.loads(manifest.read_text())
    item = data["files"][0]
    data["files"].append({**item, "path": item["path"] + suffix})
    manifest.write_text(json.dumps(data))
    with pytest.raises(gws.GwsError):
        gws.restore(manifest, readbacks, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "replacement", [b"{}", b'{"format":"bad","format":"wsum-json-1"}', b"{"]
)
def test_restore_rejects_bad_manifest_json(tmp_path: Path, replacement: bytes) -> None:
    manifest, readbacks = tmp_path / "manifest.json", tmp_path / "readbacks.json"
    manifest.write_bytes(replacement)
    readbacks.write_text("{}")
    with pytest.raises(ValueError, match=r"manifest|JSON|duplicate|property name"):
        gws.restore(manifest, readbacks, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("replacement", [b"", b"x" * 26])
def test_restore_rejects_damaged_payload(tmp_path: Path, replacement: bytes) -> None:
    manifest, maps, receipts, _ = _json_commit(
        _json_workspace(tmp_path), tmp_path, GENERATION
    )
    path = Path(next(iter(next(iter(receipts.values()))["readbacks"].values())))
    path.write_bytes(replacement)
    with pytest.raises(gws.GwsError):
        gws.restore(manifest, maps, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_report_delivery_retry_after_failed_checkpoint(tmp_path: Path) -> None:
    ws = _json_workspace(tmp_path)
    report = ws / "output/report/20261009T000000Z-abcdef12.md"
    report.write_text("canonical report\n")
    first, first_maps, _, _ = _json_commit(ws, tmp_path, GENERATION)
    gws.ledger(ws / "internal/gws/delivery.json", report, record=True)
    # The modified ledger was not durably checkpointed.
    restored = tmp_path / "retry"
    gws.restore(first, first_maps, restored)
    restored_report = restored / report.relative_to(ws)
    restored_ledger = restored / "internal/gws/delivery.json"
    assert not gws.ledger(restored_ledger, restored_report, record=False)["delivered"]
    gws.ledger(restored_ledger, restored_report, record=True)
    second, maps, _, _ = _json_commit(restored, tmp_path, "20261009T000001Z")
    gws.restore(second, maps, tmp_path / "final")
    assert gws.ledger(
        tmp_path / "final/internal/gws/delivery.json",
        tmp_path / "final" / report.relative_to(ws),
        record=False,
    )["delivered"]


@pytest.mark.parametrize(
    "current",
    [
        [],
        [{"name": f"workspace-{GENERATION}.json", "id": "replacement"}],
        [{"name": "workspace-20261009T000001Z.json", "id": "new"}],
    ],
)
def test_generation_rejects_stale_identity(current: list[dict[str, str]]) -> None:
    expected = {"name": f"workspace-{GENERATION}.json", "id": "original"}
    with pytest.raises(gws.GwsError, match="active snapshot changed"):
        gws.next_generation(current, datetime(2026, 10, 9, tzinfo=UTC), expected)


def test_json_commands_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _json_workspace(tmp_path)
    staged = tmp_path / "cli-staged"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gws.py",
            "json-pack",
            "--workspace",
            str(ws),
            "--snapshot",
            str(staged),
            "--generation",
            GENERATION,
        ],
    )
    gws.main()
    assert json.loads(capsys.readouterr().out)["format"] == gws.JSON_FORMAT

    manifest, maps, _, _ = _json_commit(ws, tmp_path, "20261009T000001Z")
    # The CLI accepts verified prior state without recreating large object chunks.
    reused_stage = tmp_path / "cli-reused-stage"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gws.py",
            "json-pack",
            "--workspace",
            str(ws),
            "--snapshot",
            str(reused_stage),
            "--generation",
            "20261009T000002Z",
            "--previous-manifest",
            str(manifest),
        ],
    )
    gws.main()
    assert json.loads(capsys.readouterr().out)["generation"] == "20261009T000002Z"
    assert not list((reused_stage / "objects").iterdir())

    monkeypatch.setattr(
        sys,
        "argv",
        ["gws.py", "verify", "--manifest", str(manifest), "--readbacks", str(maps)],
    )
    gws.main()
    assert json.loads(capsys.readouterr().out)["file_count"] > 0
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gws.py",
            "restore",
            "--manifest",
            str(manifest),
            "--readbacks",
            str(maps),
            "--workspace",
            str(tmp_path / "restored"),
        ],
    )
    gws.main()
    assert json.loads(capsys.readouterr().out)["generation"] == "20261009T000001Z"


def test_base64_bridge_preserves_exact_utf8(tmp_path: Path) -> None:
    """Model-mediated text transport can be validated against local bytes."""
    original = tmp_path / "original.txt"
    original.write_bytes(b"\xef\xbb\xbf" + "報告\r\n".encode() + b"no newline")
    transfer = gws.transfer_encode(original)
    assert transfer["size"] == len(original.read_bytes())
    assert transfer["sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()
    encoded = tmp_path / "from-connector.b64"
    encoded.write_text(str(transfer["base64Content"]), encoding="ascii")
    dest = tmp_path / "readback.txt"
    result = gws.transfer_decode(
        encoded, dest, int(transfer["size"]), str(transfer["sha256"])
    )
    assert result["verified"] is True
    assert dest.read_bytes() == original.read_bytes()
    assert base64.b64decode(str(transfer["base64Content"])) == dest.read_bytes()


@pytest.mark.parametrize(
    ("encoded", "size", "digest"),
    [
        ("not-base64!", 1, None),
        ("YQ==", 2, None),
        ("Yg==", 1, hashlib.sha256(b"a").hexdigest()),
        ("YQ==", 1, "0" * 63),
    ],
)
def test_base64_bridge_rejects_invalid_readback(
    tmp_path: Path, encoded: str, size: int, digest: str | None
) -> None:
    """A failed read-back never creates a partial local source file."""
    source = tmp_path / "download.b64"
    source.write_text(encoded)
    dest = tmp_path / "readback.txt"
    with pytest.raises(gws.GwsError):
        gws.transfer_decode(source, dest, size, digest)
    assert not dest.exists()


def test_base64_bridge_source_has_no_independent_digest(tmp_path: Path) -> None:
    """Original Drive CSV can only prove byte size without a source checksum."""
    source = tmp_path / "download.b64"
    source.write_text("YQ==")
    dest = tmp_path / "readback.txt"
    assert gws.transfer_decode(source, dest, 1, None)["verified"] is False
    assert dest.read_bytes() == b"a"
    with pytest.raises(gws.GwsError, match="must not exist"):
        gws.transfer_decode(source, dest, 1, None)


def test_observation_identity_survives_drive_snapshot_restore(tmp_path: Path) -> None:
    import workspace  # ruff: ignore[import-outside-top-level]

    ws = _workspace(tmp_path)
    targets = tmp_path / "targets.csv"
    targets.write_text("name,url\nExample,https://example.com/\n")
    target_id = str(workspace.load_targets(targets)[0]["target_id"])
    fetched = tmp_path / "fetch.txt"
    fetched.write_text("baseline content\n")
    workspace.ingest_agent_fetch(
        ws,
        targets,
        target_id=target_id,
        run_id="20261001T000000Z-deadbeef",
        input_path=fetched,
        link_depth=0,
    )
    expected = workspace.observation_status(ws, target_id=target_id)
    assert expected["baseline_updated_at"] is not None
    manifest, maps, _, _ = _json_commit(ws, tmp_path, GENERATION)
    restored = tmp_path / "restored"
    gws.restore(manifest, maps, restored)
    assert workspace.observation_status(restored, target_id=target_id) == expected
    snapshot = Path("internal/state/snapshots") / f"{target_id}.txt"
    assert (restored / snapshot).stat().st_mtime_ns == (
        ws / snapshot
    ).stat().st_mtime_ns


@pytest.mark.parametrize("stamp", [True, -1, 2**63, "123", None])
def test_restore_rejects_invalid_snapshot_mtime(tmp_path: Path, stamp: object) -> None:
    manifest, maps, _, _ = _json_commit(_json_workspace(tmp_path), tmp_path, GENERATION)
    data = json.loads(manifest.read_text())
    snapshot = next(item for item in data["files"] if "snapshots/" in item["path"])
    snapshot["mtime_ns"] = stamp
    manifest.write_text(json.dumps(data))
    destination = tmp_path / "restored"
    with pytest.raises(gws.GwsError, match="unsafe"):
        gws.restore(manifest, maps, destination)
    assert not destination.exists()


def test_restore_rejects_inexact_snapshot_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, maps, _, _ = _json_commit(_json_workspace(tmp_path), tmp_path, GENERATION)
    original = gws.os.utime

    def inexact(path: Path, *, ns: tuple[int, int]) -> None:
        original(path, ns=(ns[0] - 1, ns[1] - 1))

    monkeypatch.setattr(gws.os, "utime", inexact)
    destination = tmp_path / "restored"
    with pytest.raises(gws.GwsError, match="mtime precision"):
        gws.restore(manifest, maps, destination)
    assert not destination.exists()


class _ChunkRef(TypedDict):
    id: str
    size: int
    sha256: str


class _ObjectReceipt(TypedDict):
    head: _ChunkRef
    readbacks: dict[str, str]


def _json_workspace(tmp: Path) -> Path:
    ws = tmp / "workspace"
    (ws / "output/report").mkdir(parents=True)
    (ws / "internal/state/snapshots").mkdir(parents=True)
    (ws / "internal/gws").mkdir(parents=True)
    (ws / "internal/gws/delivery.json").write_text(
        '{"reports":{},"version":1}', encoding="utf-8"
    )
    (ws / "output/report/a.md").write_bytes(b"# Heading\r\n")
    original = ws / "internal/state/snapshots/source.txt"
    original.write_bytes(os.urandom(72_000).hex().encode())
    stamp = 1_700_000_000_123_456_789
    os.utime(original, ns=(stamp, stamp))
    return ws


def _json_commit(
    ws: Path,
    tmp: Path,
    generation: str,
    previous: dict[str, _ObjectReceipt] | None = None,
) -> tuple[Path, Path, dict[str, _ObjectReceipt], Path]:
    """Simulate exact-ID textContent uploads and verified readbacks.

    Returns:
        Manifest path, readback map path, object receipts, and staged files.
    """
    staged = tmp / ("stage-" + generation)
    draft = gws.json_pack(ws, staged, generation)
    assert draft["format"] == "wsum-json-1"
    objects: dict[str, _ObjectReceipt] = dict(previous or {})
    for item in draft["files"]:
        if "object" not in item or item["sha256"] in objects:
            continue
        digest = item["sha256"]
        count = item["object"]["chunks"]
        nxt = tmp / ("next-" + digest + ".json")
        nxt.write_text("null")
        readbacks: dict[str, str] = {}
        for index in reversed(range(count)):
            chunk_path = tmp / f"{generation}-{digest}-{index}.txt"
            meta = gws.json_link(
                staged / "objects" / digest / f"{index:04d}.txt", nxt, index, chunk_path
            )
            relay = gws.transfer_text(chunk_path)
            assert relay["textContent"] == chunk_path.read_text()
            assert "base64Content" not in relay
            file_id = f"id-{generation}-{digest[:8]}-{index}"
            readbacks[file_id] = str(chunk_path)
            nxt.write_text(
                json.dumps({
                    "id": file_id,
                    "size": meta["size"],
                    "sha256": meta["sha256"],
                })
            )
        objects[digest] = {
            "head": cast("_ChunkRef", json.loads(nxt.read_text())),
            "readbacks": readbacks,
        }
    receipts = tmp / ("receipts-" + generation + ".json")
    # Only entries referenced by this checkpoint; old objects are still reusable.
    wanted = {item["sha256"] for item in draft["files"] if "object" in item}
    current = {digest: objects[digest] for digest in wanted}
    receipts.write_text(json.dumps(current))
    manifest = tmp / ("manifest-" + generation + ".json")
    gws.json_prepare(staged, receipts, manifest)
    assert gws.verify(manifest, receipts)["generation"] == generation
    return manifest, receipts, current, staged


class FakeDrive:
    """Small exact-ID text store; no production connector or credentials."""

    def __init__(self, root: Path) -> None:
        """Initialize an empty store."""
        self.root = root
        root.mkdir()
        self.files: dict[str, tuple[str, bytes]] = {}
        self.objects: dict[str, dict[str, _ObjectReceipt]] = {}
        self.sequence = 0
        self.durable: list[dict[str, str]] = []

    def create(self, name: str, content: bytes) -> str:
        """Create an immutable text file.

        Returns:
            The returned stable file ID.
        """
        content.decode("utf-8")
        self.sequence += 1
        file_id = f"id-{self.sequence}"
        self.files[file_id] = (name, content)
        return file_id

    def listing(self) -> list[dict[str, str]]:
        """Return a complete direct-child listing."""
        return [{"id": key, "name": value[0]} for key, value in self.files.items()]

    def read(self, file_id: str) -> Path:
        """Materialize complete exact-ID bytes.

        Returns:
            The local read-back path.
        """
        path = self.root / file_id
        path.write_bytes(self.files[file_id][1])
        return path

    def readbacks(self, manifest: Path) -> Path:
        """Materialize every referenced payload.

        Returns:
            The mapping file path.
        """
        objects = self.objects[manifest.name]
        result = {
            digest: {
                "head": receipt["head"],
                "readbacks": {key: str(self.read(key)) for key in receipt["readbacks"]},
            }
            for digest, receipt in objects.items()
        }
        path = self.root / (manifest.name + "-readbacks.json")
        path.write_text(json.dumps(result))
        return path

    def upload_objects(
        self, snapshot: Path, generation: str, failure: str
    ) -> dict[str, _ObjectReceipt]:
        """Upload hash-linked objects with optional injected transport damage.

        Returns:
            Object heads and exact-ID readbacks keyed by content digest.
        """
        receipts: dict[str, _ObjectReceipt] = {}
        for item in json.loads((snapshot / "draft.json").read_text())["files"]:
            if "object" not in item or item["sha256"] in receipts:
                continue
            digest = item["sha256"]
            nxt = snapshot / "next.json"
            nxt.write_text("null")
            readbacks: dict[str, str] = {}
            for index in reversed(range(item["object"]["chunks"])):
                chunk = snapshot / f"upload-{digest}-{index}.txt"
                meta = gws.json_link(
                    snapshot / "objects" / digest / f"{index:04d}.txt",
                    nxt,
                    index,
                    chunk,
                )
                text = gws.transfer_text(chunk)["textContent"]
                assert isinstance(text, str)
                content = text.encode("utf-8")
                file_id = self.create(
                    f"object-{generation}-{digest}-{index}.txt", content
                )
                if failure == "missing":
                    del self.files[file_id]
                if failure == "truncated":
                    self.files[file_id] = (self.files[file_id][0], content[:-1])
                if failure == "corrupt":
                    self.files[file_id] = (self.files[file_id][0], b"x" * len(content))
                readbacks[file_id] = str(self.read(file_id))
                nxt.write_text(json.dumps({"id": file_id, **meta}))
                if failure == "incomplete":
                    break
            receipts[digest] = {
                "head": cast("_ChunkRef", json.loads(nxt.read_text())),
                "readbacks": readbacks,
            }
        return receipts

    def commit(
        self, ws: Path, expected: dict[str, str] | None, failure: str = ""
    ) -> dict[str, str]:
        """Follow the contract, recording durability after verification.

        Returns:
            The committed identity.

        Raises:
            gws.GwsError: Manifest read-back mismatch.
        """
        generation = gws.next_generation(
            self.listing(), datetime(2026, 10, 9, tzinfo=UTC), expected
        )
        snapshot = self.root / f"stage-{generation}-{self.sequence}"
        gws.json_pack(ws, snapshot, generation)
        receipts = self.upload_objects(snapshot, generation, failure)
        receipt_path = snapshot / "receipts.json"
        receipt_path.write_text(json.dumps(receipts))
        manifest = snapshot / "manifest.json"
        gws.json_prepare(snapshot, receipt_path, manifest)
        if failure == "stale":
            self.create("workspace-20261010T000000Z.json", b"{}")
        # The second guard is immediately before the last create.
        gws.next_generation(self.listing(), datetime(2026, 10, 9, tzinfo=UTC), expected)
        name = f"workspace-{generation}.json"
        file_id = self.create(name, manifest.read_bytes())
        if failure == "manifest":
            self.files[file_id] = (name, b"{}")
        self.objects[file_id] = receipts
        downloaded = self.read(file_id)
        if downloaded.read_bytes() != manifest.read_bytes():
            message = "manifest read-back mismatch"
            raise gws.GwsError(message)
        gws.verify(downloaded, self.readbacks(downloaded))
        active = {"name": name, "id": file_id}
        assert gws.next_generation(
            self.listing(), datetime(2026, 10, 9, tzinfo=UTC), active
        )
        self.durable.append(active)
        return active

    def cleanup(self) -> None:
        """Apply a verified retention plan."""
        rows: list[dict[str, str]] = []
        for identity in (
            row for row in self.listing() if gws.SNAPSHOT_RE.fullmatch(row["name"])
        ):
            manifest = self.read(identity["id"])
            # Each manifest needs its own mapping file for the retained snapshot.
            readbacks = self.root / f"reads-{identity['id']}.json"
            readbacks.write_bytes(self.readbacks(manifest).read_bytes())
            rows.append({
                **identity,
                "manifest": str(manifest),
                "readbacks": str(readbacks),
            })
        generations = self.root / "generations.json"
        generations.write_text(json.dumps(rows))
        plan = gws.retention(generations)
        # Delete manifests first; a stop between these phases only leaks data.
        obsolete = {row["id"] for row in rows} - set(plan["retain"])
        for file_id in [*sorted(obsolete), *sorted(set(plan["delete"]) - obsolete)]:
            del self.files[file_id]
        self.durable = [row for row in self.durable if row["id"] in plan["retain"]]


def test_small_json_state_with_external_snapshots_restores_exact_bytes(
    tmp_path: Path,
) -> None:
    ws = _json_workspace(tmp_path)
    generation = "20261020T000000Z"
    first, maps, _, stage = _json_commit(ws, tmp_path, generation)
    state = json.loads(first.read_text())
    assert first.stat().st_size < gws.JSON_MAX_STATE_BYTES
    assert "text" in next(x for x in state["files"] if x["path"].endswith("a.md"))
    assert "object" in next(
        x for x in state["files"] if x["path"].endswith("source.txt")
    )
    other = tmp_path / "second-stage"
    gws.json_pack(ws, other, generation)
    assert (stage / "draft.json").read_bytes() == (other / "draft.json").read_bytes()
    assert not list(stage.glob("objects/*/source.txt"))
    assert not list(other.glob("objects/*/source.txt"))
    restored = tmp_path / "restored"
    gws.restore(first, maps, restored)
    for path in ws.rglob("*"):
        if path.is_file():
            copied = restored / path.relative_to(ws)
            assert copied.read_bytes() == path.read_bytes()
            if "snapshots" in path.parts:
                assert copied.stat().st_mtime_ns == path.stat().st_mtime_ns


def test_json_pack_omits_reused_large_object_fragments(tmp_path: Path) -> None:
    ws = _json_workspace(tmp_path)
    previous, previous_maps, receipts, _ = _json_commit(
        ws, tmp_path, GENERATION
    )
    stage = tmp_path / "reused-stage"
    draft = gws.json_pack(ws, stage, "20261009T000001Z", previous_manifest=previous)
    assert not list((stage / "objects").iterdir())

    receipt_map = {
        item["sha256"]: {
            "head": receipts[item["sha256"]]["head"],
            "previous_manifest": str(previous),
        }
        for item in draft["files"]
        if "object" in item
    }
    receipt_path = tmp_path / "reuse-receipts.json"
    receipt_path.write_text(json.dumps(receipt_map))
    output = tmp_path / "reused-manifest.json"
    prepared = gws.json_prepare(stage, receipt_path, output)
    assert prepared["new_objects"] == 0
    assert prepared["reused_objects"] > 0
    assert gws.json_verify_commit(output, receipt_path)["new_objects"] == 0
    assert gws.verify(output, previous_maps)["generation"] == "20261009T000001Z"
    restored = tmp_path / "reused-restored"
    gws.restore(output, previous_maps, restored)
    for source in ws.rglob("*"):
        if source.is_file():
            target = restored / source.relative_to(ws)
            assert target.read_bytes() == source.read_bytes()
            if "snapshots" in source.parts:
                assert target.stat().st_mtime_ns == source.stat().st_mtime_ns

    # A new large object still creates fragments.
    (ws / "output/report/a.md").write_bytes(b"x" * 2048)
    next_stage = tmp_path / "next-stage"
    changed = gws.json_pack(
        ws, next_stage, "20261009T000002Z", previous_manifest=previous
    )
    assert sum("object" in item for item in changed["files"]) > len(receipt_map)
    assert len(list((next_stage / "objects").iterdir())) == 1

    # Two different files with the same new digest stage one chunk chain.
    (ws / "output/report/duplicate1.md").write_bytes(b"z" * 2048)
    (ws / "output/report/duplicate2.md").write_bytes(b"z" * 2048)
    deduplicated = tmp_path / "deduplicated-stage"
    gws.json_pack(
        ws, deduplicated, "20261009T000003Z", previous_manifest=previous
    )
    assert len(list((deduplicated / "objects").iterdir())) == 2

    # An ambiguous previous digest/chunk-count mapping is rejected.
    conflicting = tmp_path / "conflicting-previous.json"
    prior = json.loads(previous.read_text())
    original = next(item for item in prior["files"] if "object" in item)
    duplicate = {**original, "path": "internal/state/snapshots/other.txt"}
    duplicate["object"] = {
        **original["object"],
        "chunks": original["object"]["chunks"] + 1,
    }
    prior["files"].append(duplicate)
    conflicting.write_text(json.dumps(prior))
    with pytest.raises(gws.GwsError, match="conflicting prior object"):
        gws.json_pack(
            ws,
            tmp_path / "conflict-stage",
            "20261009T000004Z",
            previous_manifest=conflicting,
        )

    invalid = tmp_path / "invalid-previous.json"
    invalid.write_text("{}")
    with pytest.raises(gws.GwsError, match="invalid JSON state"):
        gws.json_pack(
            ws,
            tmp_path / "invalid-stage",
            "20261009T000003Z",
            previous_manifest=invalid,
        )


def test_workspace_change_detection_preserves_bytes_and_mtime(
    tmp_path: Path,
) -> None:
    ws = _json_workspace(tmp_path)
    manifest, _, _, _ = _json_commit(ws, tmp_path, GENERATION)
    assert gws._workspace_changed(ws, manifest) is False

    # Staging files are excluded from checkpoints, just as in json_pack.
    temporary = ws / "internal/gws/.tmp-staging"
    temporary.write_text("discard me", encoding="utf-8")
    assert gws._workspace_changed(ws, manifest) is False
    temporary.unlink()

    report = ws / "output/report/a.md"
    original = report.read_bytes()
    report.write_bytes(original.replace(b"Heading", b"Changed"))
    assert gws._workspace_changed(ws, manifest) is True
    report.write_bytes(original)
    assert gws._workspace_changed(ws, manifest) is False
    renamed = report.with_name("renamed.md")
    report.rename(renamed)
    assert gws._workspace_changed(ws, manifest) is True
    renamed.rename(report)

    snapshot = ws / "internal/state/snapshots/source.txt"
    stamp = snapshot.stat().st_mtime_ns
    os.utime(snapshot, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
    assert gws._workspace_changed(ws, manifest) is True
    os.utime(snapshot, ns=(stamp, stamp))
    assert gws._workspace_changed(ws, manifest) is False

    extra = ws / "internal/gws/added.json"
    extra.write_text("{}", encoding="utf-8")
    assert gws._workspace_changed(ws, manifest) is True
    extra.unlink()
    report.unlink()
    assert gws._workspace_changed(ws, manifest) is True


def test_workspace_change_detection_rejects_invalid_state(
    tmp_path: Path,
) -> None:
    ws = _json_workspace(tmp_path)
    manifest, _, _, _ = _json_commit(ws, tmp_path, GENERATION)
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(gws.GwsError, match="invalid JSON state"):
        gws._workspace_changed(ws, manifest)


def test_next_generation_cli_enforces_drive_identity_before_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ws = _json_workspace(tmp_path)
    manifest, _, _, _ = _json_commit(ws, tmp_path, GENERATION)
    expected = {"name": f"workspace-{GENERATION}.json", "id": "committed"}
    expected_path = tmp_path / "expected.json"
    expected_path.write_text(json.dumps(expected))
    listed = tmp_path / "listed.json"
    listed.write_text(json.dumps([expected]))
    argv = [
        "gws.py",
        "next-generation",
        "--snapshots-json",
        str(listed),
        "--expected-snapshot-json",
        str(expected_path),
        "--workspace",
        str(ws),
        "--manifest",
        str(manifest),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    gws.main()
    result = json.loads(capsys.readouterr().out)
    assert result["changed"] is False
    assert result["generation"] > GENERATION

    (ws / "output/report/a.md").write_bytes(b"# Changed")
    gws.main()
    assert json.loads(capsys.readouterr().out)["changed"] is True

    # An externally replaced active generation must fail even on a no-op.
    (ws / "output/report/a.md").write_bytes(b"# Heading\r\n")
    listed.write_text(json.dumps([{**expected, "id": "replacement"}]))
    with pytest.raises(SystemExit) as failure:
        gws.main()
    assert failure.value.code == 1
    assert "active snapshot changed" in capsys.readouterr().err

    # Initial creation does not need an old manifest.
    listed.write_text("[]")
    expected_path.write_text("null")
    monkeypatch.setattr(sys, "argv", argv[:-2])
    gws.main()
    assert json.loads(capsys.readouterr().out)["changed"] is True

    # The conflict guard also works without the workspace optimization.
    monkeypatch.setattr(sys, "argv", argv[:-4])
    gws.main()
    assert "changed" not in json.loads(capsys.readouterr().out)

    # A manifest without its required workspace argument is rejected.
    monkeypatch.setattr(sys, "argv", [*argv[:-4], "--manifest", str(manifest)])
    with pytest.raises(SystemExit) as invalid:
        gws.main()
    assert invalid.value.code == 1
    assert "--manifest requires --workspace" in capsys.readouterr().err


def test_json_prepare_rejects_corrupt_staged_chunks(tmp_path: Path) -> None:
    """A corrupt compressed staging fragment cannot be committed as new or reused."""
    ws = _json_workspace(tmp_path)
    first, receipts, previous, stage = _json_commit(ws, tmp_path, "20261020T000000Z")
    chunk = next(stage.glob("objects/*/0000.txt"))
    chunk.write_text("AAAA", encoding="ascii")
    with pytest.raises(gws.GwsError, match=r"compressed object|SHA-256 mismatch"):
        gws.json_prepare(stage, receipts, tmp_path / "invalid-new.json")
    assert not (tmp_path / "invalid-new.json").exists()

    reused = tmp_path / "reused-stage"
    draft = gws.json_pack(ws, reused, "20261020T000001Z")
    reused_receipts = {
        item["sha256"]: {
            "head": previous[item["sha256"]]["head"],
            "previous_manifest": str(first),
        }
        for item in draft["files"]
        if "object" in item
    }
    reused_file = tmp_path / "reused-receipts.json"
    reused_file.write_text(json.dumps(reused_receipts))
    next(reused.glob("objects/*/0000.txt")).write_text("AAAA", encoding="ascii")
    with pytest.raises(gws.GwsError, match="reused object must not stage chunks"):
        gws.json_prepare(reused, reused_file, tmp_path / "invalid-reused.json")
    assert not (tmp_path / "invalid-reused.json").exists()


def test_reuse_unchanged_objects_between_generations(tmp_path: Path) -> None:
    ws = _json_workspace(tmp_path)
    a, _, old, _ = _json_commit(ws, tmp_path, "20261020T000000Z")
    # Only the inline current-state file changed, object head IDs remain identical.
    (ws / "internal/gws/delivery.json").write_text(
        '{"reports":{"run":"abc"},"version":1}'
    )
    b, b_map, new, _ = _json_commit(ws, tmp_path, "20261020T000001Z", old)
    assert {v["head"]["id"] for v in old.values()} == {
        v["head"]["id"] for v in new.values()
    }
    assert gws.verify(b, b_map)["generation"] == "20261020T000001Z"
    rows: list[dict[str, str]] = []
    for i, (manifest, maps) in enumerate((
        (a, tmp_path / "receipts-20261020T000000Z.json"),
        (b, b_map),
    )):
        generation = json.loads(manifest.read_text())["generation"]
        rows.append({
            "name": f"workspace-{generation}.json",
            "id": f"manifest-{i}",
            "manifest": str(manifest),
            "readbacks": str(maps),
        })
    rows_path = tmp_path / "rows.json"
    rows_path.write_text(json.dumps(rows))
    plan = gws.retention(rows_path)
    assert set(plan["retain"]) == {"manifest-0", "manifest-1"}
    assert plan["delete"] == []


def test_changed_or_missing_readback_fails_without_restore(tmp_path: Path) -> None:
    ws = _json_workspace(tmp_path)
    manifest, maps, receipts, _ = _json_commit(ws, tmp_path, "20261020T000000Z")
    first = next(iter(receipts.values()))
    path = Path(next(iter(first["readbacks"].values())))
    expected = path.read_bytes()
    path.write_bytes(expected[:-1])
    with pytest.raises(gws.GwsError, match="readback mismatch"):
        gws.restore(manifest, maps, tmp_path / "should-not-exist")
    assert not (tmp_path / "should-not-exist").exists()


def test_oversized_state_fails_closed(tmp_path: Path) -> None:
    ws = _json_workspace(tmp_path)
    folder = ws / "internal/state"
    for index in range(1800):
        (folder / f"{index:04d}.json").write_text('{"x":"y"}')
    with pytest.raises(gws.GwsError, match="JSON exceeds connector"):
        gws.json_pack(ws, tmp_path / "too-large", "20261020T000000Z")
    assert not (tmp_path / "too-large").exists()


def test_retention_preserves_shared_current_objects(tmp_path: Path) -> None:
    ws = _json_workspace(tmp_path)
    rows: list[dict[str, str]] = []
    previous: dict[str, _ObjectReceipt] | None = None
    protected: set[str] = set()
    for second in range(5):
        generation = f"20261020T00000{second}Z"
        manifest, maps, previous, _ = _json_commit(ws, tmp_path, generation, previous)
        for receipt in previous.values():
            protected.update(receipt["readbacks"])
        rows.append({
            "name": f"workspace-{generation}.json",
            "id": f"manifest-{second}",
            "manifest": str(manifest),
            "readbacks": str(maps),
        })
    manifest_list = tmp_path / "current-generations.json"
    manifest_list.write_text(json.dumps(rows))
    plan = gws.retention(manifest_list)
    assert set(plan["retain"]) == {"manifest-2", "manifest-3", "manifest-4"}
    assert set(plan["delete"]) == {"manifest-0", "manifest-1"}
    assert not protected.intersection(plan["delete"])

    # A corrupt generation aborts retention before any deletion is planned.
    first = Path(rows[0]["manifest"])
    first.write_text("{}", encoding="utf-8")
    with pytest.raises(gws.GwsError, match="invalid JSON state"):
        gws.retention(manifest_list)


def test_reuse_checkpoint_skips_unchanged_chunk_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only changed objects need exact-ID chunk reads on a checkpoint."""
    ws = _json_workspace(tmp_path)
    first, old_reads, previous, _ = _json_commit(ws, tmp_path, "20261020T000000Z")
    (ws / "internal/gws/delivery.json").write_text(
        '{"reports":{"20261020T000000Z-abcdef01":"' + "0" * 64 + '"},"version":1}'
    )
    staged = tmp_path / "stage-reused"
    draft = gws.json_pack(
        ws, staged, "20261020T000001Z", previous_manifest=first
    )
    receipts = {
        item["sha256"]: {
            "head": previous[item["sha256"]]["head"],
            "previous_manifest": str(first),
        }
        for item in draft["files"]
        if "object" in item
    }
    receipts_path = tmp_path / "reused-receipts.json"
    receipts_path.write_text(json.dumps(receipts))
    second = tmp_path / "reused-manifest.json"

    def forbid_chunk_reads(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unchanged objects must not require chunk readbacks")

    monkeypatch.setattr(gws, "_json_object_bytes", forbid_chunk_reads)
    counts = gws.json_prepare(staged, receipts_path, second)
    assert counts["new_objects"] == 0
    assert counts["reused_objects"] == len(receipts)
    check = gws.json_verify_commit(second, receipts_path)
    assert check["reused_objects"] == len(receipts)

    # Full restore still needs the previous exact-ID readbacks.
    monkeypatch.undo()
    restored = tmp_path / "reused-restored"
    assert gws.restore(second, old_reads, restored)["generation"] == "20261020T000001Z"
    assert (restored / "internal/state/snapshots/source.txt").read_bytes() == (
        ws / "internal/state/snapshots/source.txt"
    ).read_bytes()


def test_reuse_rejects_unproven_reference(tmp_path: Path) -> None:
    """Reject an object head that was not present in the previous commit."""
    ws = _json_workspace(tmp_path)
    first, _, previous, _ = _json_commit(ws, tmp_path, "20261020T000000Z")
    stage = tmp_path / "stage-untrusted"
    draft = gws.json_pack(
        ws, stage, "20261020T000001Z", previous_manifest=first
    )
    receipts = {
        item["sha256"]: {
            "head": {
                **previous[item["sha256"]]["head"],
                "id": "unrelated-id",
            },
            "previous_manifest": str(first),
        }
        for item in draft["files"]
        if "object" in item
    }
    file = tmp_path / "untrusted-receipts.json"
    file.write_text(json.dumps(receipts))
    with pytest.raises(gws.GwsError, match="not in prior verified"):
        gws.json_prepare(stage, file, tmp_path / "should-not-commit.json")


def test_inline_state_rejects_control_bytes_even_if_digest_matches(
    tmp_path: Path,
) -> None:
    """An attacker-supplied inline NUL must fail before workspace restore."""
    ws = _json_workspace(tmp_path)
    manifest, maps, _, _ = _json_commit(ws, tmp_path, "20261020T000000Z")
    state = json.loads(manifest.read_text())
    inline = next(item for item in state["files"] if "text" in item)
    bad = "x" + chr(0)
    inline["text"] = bad
    data = bad.encode()
    inline["size"] = len(data)
    inline["sha256"] = hashlib.sha256(data).hexdigest()
    altered = tmp_path / "altered-manifest.json"
    altered.write_text(json.dumps(state))
    with pytest.raises(gws.GwsError, match="inline state mismatch"):
        gws.verify(altered, maps)
    assert not (tmp_path / "untrusted-restore").exists()


def test_commit_verify_rejects_replaced_head(tmp_path: Path) -> None:
    """Lightweight post-upload validation must bind to the prepared reference."""
    ws = _json_workspace(tmp_path)
    manifest, receipts_path, receipts, _ = _json_commit(
        ws, tmp_path, "20261020T000000Z"
    )
    forged = json.loads(manifest.read_text())
    entry = next(item for item in forged["files"] if "object" in item)
    original = receipts[entry["sha256"]]["head"]["id"]
    entry["object"]["head"]["id"] = "attacker-controlled-id"
    assert original != entry["object"]["head"]["id"]
    altered = tmp_path / "altered-head-manifest.json"
    altered.write_text(json.dumps(forged))
    with pytest.raises(gws.GwsError, match="reference changed"):
        gws.json_verify_commit(altered, receipts_path)


@pytest.mark.parametrize(
    "failure", ["truncated", "corrupt", "missing", "incomplete", "stale", "manifest"]
)
def test_failed_commit_preserves_committed_generation(
    tmp_path: Path, failure: str
) -> None:
    ws = _json_workspace(tmp_path)
    drive = FakeDrive(tmp_path / "drive")
    expected = drive.commit(ws, None)
    before = dict(drive.files)
    with pytest.raises((gws.GwsError, KeyError)):
        drive.commit(ws, expected, failure)
    if failure not in {"stale", "manifest"}:
        assert [
            row for row in drive.listing() if gws.SNAPSHOT_RE.fullmatch(row["name"])
        ] == [expected]
    if failure == "stale":
        assert not any(
            row["name"] == "workspace-20261009T000001Z.json" for row in drive.listing()
        )
    assert drive.durable == [expected]
    assert all(drive.files[key] == value for key, value in before.items())
    manifest = drive.read(expected["id"])
    gws.restore(manifest, drive.readbacks(manifest), tmp_path / "restored")


def test_cleanup_failure_leaves_verified_commits(tmp_path: Path) -> None:
    ws = _json_workspace(tmp_path)
    drive = FakeDrive(tmp_path / "drive")
    expected = None
    for _ in range(4):
        expected = drive.commit(ws, expected)
    before = dict(drive.files)
    corrupt_id = drive.durable[0]["id"]
    drive.files[corrupt_id] = (drive.files[corrupt_id][0], b"{}")
    damaged = dict(drive.files)
    with pytest.raises((gws.GwsError, KeyError)):
        drive.cleanup()
    assert drive.files == damaged  # no deletion until complete validation
    drive.files[corrupt_id] = before[corrupt_id]
    # An interrupted cleanup can leak obsolete payloads without harming latest.
    del drive.files[corrupt_id]
    assert expected is not None
    manifest = drive.read(expected["id"])
    gws.restore(manifest, drive.readbacks(manifest), tmp_path / "restored")


def test_report_delivery_retry_after_failed_ledger_persistence(tmp_path: Path) -> None:
    ws = _json_workspace(tmp_path)
    report = ws / "output/report/20261009T000000Z-abcdef12.md"
    report.write_text("canonical report\n")
    drive = FakeDrive(tmp_path / "drive")
    committed = drive.commit(ws, None)
    report_id = drive.create("Web Update Report — " + report.name, report.read_bytes())
    assert drive.read(report_id).read_bytes() == report.read_bytes()
    gws.ledger(ws / "internal/gws/delivery.json", report, record=True)
    with pytest.raises(gws.GwsError):
        drive.commit(ws, committed, "incomplete")
    manifest = drive.read(committed["id"])
    restored = tmp_path / "retry"
    gws.restore(manifest, drive.readbacks(manifest), restored)
    restored_report = restored / report.relative_to(ws)
    ledger = restored / "internal/gws/delivery.json"
    assert not gws.ledger(ledger, restored_report, record=False)["delivered"]
    matching = [
        row
        for row in drive.listing()
        if row["name"] == "Web Update Report — " + report.name
    ]
    assert len(matching) == 1
    assert drive.read(matching[0]["id"]).read_bytes() == restored_report.read_bytes()
    gws.ledger(ledger, restored_report, record=True)
    final = drive.commit(restored, committed)
    assert (
        len([
            row
            for row in drive.listing()
            if row["name"] == "Web Update Report — " + report.name
        ])
        == 1
    )
    manifest = drive.read(final["id"])
    gws.restore(manifest, drive.readbacks(manifest), tmp_path / "final")
    assert gws.ledger(
        tmp_path / "final/internal/gws/delivery.json",
        tmp_path / "final" / report.relative_to(ws),
        record=False,
    )["delivered"]
