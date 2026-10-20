"""Archive text-chain transport tests without Google credentials."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import gws
import gws_archive
import pytest


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "output/report").mkdir(parents=True)
    (workspace / "internal/state/snapshots").mkdir(parents=True)
    (workspace / "internal/gws").mkdir(parents=True)
    (workspace / "internal/gws/delivery.json").write_text(
        '{"version":1,"reports":{}}', encoding="utf-8"
    )
    original = workspace / "internal/state/snapshots/abc.txt"
    original.write_bytes(b"\xef\xbb\xbfbaseline\r\n")
    os.utime(original, ns=(1_700_000_000_123_456_789,) * 2)
    # Incompressible ASCII forces multiple chunks without binary source files.
    (workspace / "internal/state/noise.txt").write_text(
        os.urandom(55_000).hex(), encoding="ascii"
    )
    return workspace


def _commit(
    workspace: Path, tmp_path: Path, generation: str, prefix: str
) -> tuple[Path, Path, dict[str, Path]]:
    snapshot = tmp_path / f"stage-{prefix}"
    draft = gws_archive.pack(workspace, snapshot, generation)
    readbacks: dict[str, Path] = {}
    nxt = tmp_path / f"next-{prefix}.json"
    nxt.write_text("null")
    assert draft["count"] > 1
    for index in reversed(range(draft["count"])):
        path = tmp_path / f"{prefix}-chunk-{index}.txt"
        result = gws_archive.link(
            snapshot / "fragments" / f"{index:04d}.txt", nxt, path, index
        )
        transfer = gws.transfer_text(path)
        assert transfer["textContent"] == path.read_text()
        assert transfer["sha256"] == result["sha256"]
        assert "base64Content" not in transfer
        file_id = f"{prefix}-id-{index}"
        readbacks[file_id] = path
        nxt.write_text(
            json.dumps({"id": file_id, "size": result["size"], "sha256": result["sha256"]})
        )
    manifest = tmp_path / f"manifest-{prefix}.json"
    gws_archive.prepare(snapshot, nxt, manifest)
    mapping = tmp_path / f"readbacks-{prefix}.json"
    mapping.write_text(json.dumps({k: str(v) for k, v in readbacks.items()}))
    return manifest, mapping, readbacks


def test_archive_round_trip_is_deterministic_and_restores_mtime(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    generation = "20261020T000000Z"
    first = tmp_path / "pack-1"
    second = tmp_path / "pack-2"
    assert gws_archive.pack(workspace, first, generation) == gws_archive.pack(
        workspace, second, generation
    )
    assert (first / "workspace.tar.gz").read_bytes() == (
        second / "workspace.tar.gz"
    ).read_bytes()
    manifest, readbacks, ids = _commit(workspace, tmp_path, generation, "one")
    assert set(gws.verify(manifest, readbacks)["ids"]) == set(ids)
    restored = tmp_path / "restored"
    result = gws.restore(manifest, readbacks, restored)
    assert result["file_count"] == len(list(workspace.rglob("*"))) - len(
        [p for p in workspace.rglob("*") if p.is_dir()]
    )
    for original in workspace.rglob("*"):
        if original.is_file():
            new = restored / original.relative_to(workspace)
            assert new.read_bytes() == original.read_bytes()
            if "state/snapshots" in original.as_posix():
                assert new.stat().st_mtime_ns == original.stat().st_mtime_ns
    with pytest.raises(gws.GwsError, match="must not exist"):
        gws.restore(manifest, readbacks, restored)


def test_archive_corruption_and_missing_chunks_fail_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    manifest, mapping, ids = _commit(workspace, tmp_path, "20261020T000000Z", "one")
    missing = next(iter(ids))
    records = json.loads(mapping.read_text())
    del records[missing]
    mapping.write_text(json.dumps(records))
    with pytest.raises(gws.GwsError, match="missing"):
        gws.verify(manifest, mapping)
    mapping.write_text(json.dumps({k: str(v) for k, v in ids.items()}))
    ids[missing].write_text("corrupted")
    with pytest.raises(gws.GwsError, match="hash mismatch"):
        gws.restore(manifest, mapping, tmp_path / "restore-after-tamper")
    assert not (tmp_path / "restore-after-tamper").exists()


def test_archive_retention_handles_legacy_and_new_format(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    manifest, readbacks, ids = _commit(
        workspace, tmp_path, "20261020T000000Z", "one"
    )
    rows = [{
        "name": "workspace-20261020T000000Z.json",
        "id": "archive-manifest-id",
        "manifest": str(manifest),
        "readbacks": str(readbacks),
    }]
    generations = tmp_path / "generations.json"
    generations.write_text(json.dumps(rows))
    plan = gws.retention(generations)
    assert plan["retain"] == ["archive-manifest-id"]
    assert plan["delete"] == []
    assert set(gws.verify(manifest, readbacks)["ids"]) == set(ids)


def test_archive_invalid_chain_index_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    manifest, readbacks, ids = _commit(
        workspace, tmp_path, "20261020T000000Z", "one"
    )
    head = json.loads(manifest.read_text())["head"]["id"]
    body = json.loads(ids[head].read_text())
    body["index"] = 1
    content = gws._json_bytes(body)
    ids[head].write_bytes(content)
    value = json.loads(manifest.read_text())
    value["head"]["sha256"] = gws._digest(content)
    value["head"]["size"] = len(content)
    manifest.write_bytes(gws._json_bytes(value))
    with pytest.raises(gws.GwsError, match="structure"):
        gws.verify(manifest, readbacks)
