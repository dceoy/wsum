"""Workspace-GWS deterministic boundary regression tests."""

import base64
import hashlib
import json
import sys
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
    (ws / "internal/gws").mkdir()
    (ws / "internal/gws/delivery.json").write_text('{"version":1,"reports":{}}')
    (ws / "output/report/demo.md").write_bytes("é\r\n報告".encode())
    (ws / "internal/state/snapshot.txt").write_bytes(b"\xef\xbb\xbfbaseline\n")
    return ws


class FakeDrive:
    """Small exact-ID text store; no production connector or credentials."""

    def __init__(self, root: Path) -> None:
        """Initialize an empty store."""
        self.root = root
        root.mkdir()
        self.files: dict[str, tuple[str, bytes]] = {}
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
        value = json.loads(manifest.read_bytes())
        result = {item["id"]: str(self.read(item["id"])) for item in value["files"]}
        path = self.root / "readbacks.json"
        path.write_text(json.dumps(result))
        return path

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
        draft = gws.pack(ws, snapshot, generation)
        receipts: dict[str, dict[str, str]] = {}
        for item in draft["files"]:
            digest = item["sha256"]
            if digest in receipts:
                continue
            content = (snapshot / "payloads" / f"{digest}.txt").read_bytes()
            file_id = self.create(f"payload-{generation}-{digest}.txt", content)
            if failure == "missing":
                del self.files[file_id]
            if failure == "truncated":
                self.files[file_id] = (self.files[file_id][0], content[:-1])
            if failure == "corrupt":
                self.files[file_id] = (self.files[file_id][0], b"x" * len(content))
            receipts[digest] = {"id": file_id, "file": str(self.read(file_id))}
            if failure == "incomplete":
                break
        receipt_path = snapshot / "receipts.json"
        receipt_path.write_text(json.dumps(receipts))
        manifest = snapshot / "manifest.json"
        gws.prepare(snapshot, receipt_path, manifest)
        if failure == "stale":
            self.create("workspace-20261010T000000Z.json", b"{}")
        # The second guard is immediately before the last create.
        gws.next_generation(self.listing(), datetime(2026, 10, 9, tzinfo=UTC), expected)
        name = f"workspace-{generation}.json"
        file_id = self.create(name, manifest.read_bytes())
        if failure == "manifest":
            self.files[file_id] = (name, b"{}")
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


def test_text_snapshot_repeated_commits_restore_and_retention(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    drive = FakeDrive(tmp_path / "drive")
    expected = None
    for run in range(5):
        (ws / "internal/state/snapshot.txt").write_text(f"baseline {run}\n")
        expected = drive.commit(ws, expected)
        drive.cleanup()
        assert len(drive.durable) == min(run + 1, 3)
        manifest = drive.read(expected["id"])
        dest = tmp_path / f"restored-{run}"
        gws.restore(manifest, drive.readbacks(manifest), dest)
        for path in ws.rglob("*"):
            if path.is_file():
                assert path.read_bytes() == (dest / path.relative_to(ws)).read_bytes()
        with pytest.raises(gws.GwsError, match="must not exist"):
            gws.restore(manifest, drive.readbacks(manifest), dest)
    referenced = {row["id"] for row in drive.durable}
    for row in drive.durable:
        referenced.update(
            item["id"] for item in json.loads(drive.files[row["id"]][1])["files"]
        )
    assert set(drive.files) == referenced


def test_pack_deterministic_and_exact_bytes(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    gws.pack(ws, first, GENERATION)
    gws.pack(ws, second, GENERATION)
    for path in first.rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (second / path.relative_to(first)).read_bytes()
    with pytest.raises(gws.GwsError, match="overwrite"):
        gws.pack(ws, first, GENERATION)


@pytest.mark.parametrize(
    "failure", ["truncated", "corrupt", "missing", "incomplete", "stale", "manifest"]
)
def test_failed_commit_preserves_committed_generation(
    tmp_path: Path, failure: str
) -> None:
    ws = _workspace(tmp_path)
    drive = FakeDrive(tmp_path / "drive")
    expected = drive.commit(ws, None)
    before = dict(drive.files)
    with pytest.raises((gws.GwsError, KeyError)):
        drive.commit(ws, expected, failure)
    assert drive.durable == [expected]
    assert all(drive.files[key] == value for key, value in before.items())
    manifest = drive.read(expected["id"])
    gws.restore(manifest, drive.readbacks(manifest), tmp_path / "restored")


@pytest.mark.parametrize("content", [b"\xff", b"\x00", b"hello\x01"])
def test_pack_rejects_binary(tmp_path: Path, content: bytes) -> None:
    ws = _workspace(tmp_path)
    (ws / "internal/state/binary").write_bytes(content)
    dest = tmp_path / "packed"
    with pytest.raises((gws.GwsError, UnicodeError)):
        gws.pack(ws, dest, GENERATION)
    assert not dest.exists()


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
def test_reject_unsafe_paths(tmp_path: Path, entry: str) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({
            "format": "wsum-text-1",
            "generation": GENERATION,
            "files": [
                {
                    "path": entry,
                    "size": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "id": "file-id",
                }
            ],
        })
    )
    reads = tmp_path / "reads.json"
    reads.write_text("{}")
    with pytest.raises(gws.GwsError, match="unsafe"):
        gws.restore(manifest, reads, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("directory", [False, True])
def test_reject_symlink_input(tmp_path: Path, directory: bool) -> None:
    ws = _workspace(tmp_path)
    (ws / "internal/link").symlink_to(
        ws / ("output" if directory else "output/report/demo.md")
    )
    with pytest.raises(gws.GwsError, match="non-regular"):
        gws.pack(ws, tmp_path / "packed", GENERATION)


def test_reject_oversized_input(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    with (ws / "output/large").open("wb") as stream:
        stream.truncate(gws.MAX_ENTRY + 1)
    with pytest.raises(gws.GwsError, match="size limit"):
        gws.pack(ws, tmp_path / "packed", GENERATION)


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
    drive = FakeDrive(tmp_path / "drive")
    committed = drive.commit(ws, None)
    manifest = drive.read(committed["id"])
    dest = tmp_path / "restored"
    gws.restore(manifest, drive.readbacks(manifest), dest)
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
    ws = _workspace(tmp_path)
    drive = FakeDrive(tmp_path / "drive")
    committed = drive.commit(ws, None)
    manifest = drive.read(committed["id"])
    readbacks = drive.readbacks(manifest)
    content = json.loads(manifest.read_bytes())
    content["files"][0][field] = value
    manifest.write_text(json.dumps(content))
    destination = tmp_path / "restored"
    with pytest.raises(gws.GwsError):
        gws.restore(manifest, readbacks, destination)
    assert not destination.exists()


@pytest.mark.parametrize("suffix", ["", "/child"])
def test_restore_rejects_conflicting_paths(tmp_path: Path, suffix: str) -> None:
    ws = _workspace(tmp_path)
    drive = FakeDrive(tmp_path / "drive")
    committed = drive.commit(ws, None)
    manifest = drive.read(committed["id"])
    readbacks = drive.readbacks(manifest)
    content = json.loads(manifest.read_bytes())
    item = content["files"][0]
    content["files"].append({**item, "path": item["path"] + suffix})
    manifest.write_text(json.dumps(content))
    with pytest.raises(gws.GwsError):
        gws.restore(manifest, readbacks, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "replacement", [b"{}", b'{"format":"bad","format":"wsum-text-1"}', b"{"]
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
    ws = _workspace(tmp_path)
    drive = FakeDrive(tmp_path / "drive")
    committed = drive.commit(ws, None)
    manifest = drive.read(committed["id"])
    readbacks = drive.readbacks(manifest)
    paths = json.loads(readbacks.read_bytes())
    Path(next(iter(paths.values()))).write_bytes(replacement)
    with pytest.raises(gws.GwsError):
        gws.restore(manifest, readbacks, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_cleanup_failure_leaves_verified_commits(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
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
    ws = _workspace(tmp_path)
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


def test_text_commands_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _workspace(tmp_path)
    snapshot = tmp_path / "stage"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gws.py",
            "pack",
            "--workspace",
            str(ws),
            "--snapshot",
            str(snapshot),
            "--generation",
            GENERATION,
        ],
    )
    gws.main()
    draft = json.loads(capsys.readouterr().out)
    receipts = {
        item["sha256"]: {
            "id": item["sha256"],
            "file": str(snapshot / "payloads" / (item["sha256"] + ".txt")),
        }
        for item in draft["files"]
    }
    receipt_path = tmp_path / "receipts.json"
    receipt_path.write_text(json.dumps(receipts))
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gws.py",
            "prepare",
            "--snapshot",
            str(snapshot),
            "--receipts",
            str(receipt_path),
            "--manifest",
            str(manifest),
        ],
    )
    gws.main()
    assert (
        json.loads(capsys.readouterr().out)["sha256"]
        == hashlib.sha256(manifest.read_bytes()).hexdigest()
    )
    reads = tmp_path / "reads.json"
    reads.write_text(json.dumps({row["id"]: row["file"] for row in receipts.values()}))
    monkeypatch.setattr(
        sys,
        "argv",
        ["gws.py", "verify", "--manifest", str(manifest), "--readbacks", str(reads)],
    )
    gws.main()
    assert json.loads(capsys.readouterr().out)["generation"] == GENERATION
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gws.py",
            "restore",
            "--manifest",
            str(manifest),
            "--readbacks",
            str(reads),
            "--workspace",
            str(tmp_path / "restored"),
        ],
    )
    gws.main()
    assert json.loads(capsys.readouterr().out)["file_count"] == len(draft["files"])


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
    drive = FakeDrive(tmp_path / "drive")
    committed = drive.commit(ws, None)
    manifest = drive.read(committed["id"])
    restored = tmp_path / "restored"
    gws.restore(manifest, drive.readbacks(manifest), restored)
    assert workspace.observation_status(restored, target_id=target_id) == expected
    snapshot = Path("internal/state/snapshots") / f"{target_id}.txt"
    assert (restored / snapshot).stat().st_mtime_ns == (
        ws / snapshot
    ).stat().st_mtime_ns


@pytest.mark.parametrize("stamp", [True, -1, 2**63, "123", None])
def test_restore_rejects_invalid_snapshot_mtime(tmp_path: Path, stamp: object) -> None:
    drive = FakeDrive(tmp_path / "drive")
    committed = drive.commit(_workspace(tmp_path), None)
    manifest = drive.read(committed["id"])
    readbacks = drive.readbacks(manifest)
    content = json.loads(manifest.read_bytes())
    content["files"][0]["mtime_ns"] = stamp
    manifest.write_text(json.dumps(content))
    destination = tmp_path / "restored"
    with pytest.raises(gws.GwsError, match="manifest file record"):
        gws.restore(manifest, readbacks, destination)
    assert not destination.exists()


def test_restore_rejects_inexact_snapshot_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive = FakeDrive(tmp_path / "drive")
    ws = _workspace(tmp_path)
    snapshot = ws / "internal/state/snapshots/example.txt"
    snapshot.parent.mkdir()
    snapshot.write_text("baseline\n")
    committed = drive.commit(ws, None)
    manifest = drive.read(committed["id"])
    readbacks = drive.readbacks(manifest)
    original = gws.os.utime

    def inexact(path: Path, *, ns: tuple[int, int]) -> None:
        original(path, ns=(ns[0] - 1, ns[1] - 1))

    monkeypatch.setattr(gws.os, "utime", inexact)
    destination = tmp_path / "restored"
    with pytest.raises(gws.GwsError, match="identity precision"):
        gws.restore(manifest, readbacks, destination)
    assert not destination.exists()
