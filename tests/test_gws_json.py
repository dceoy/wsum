"""JSON-state + content-addressed Drive object transport regression tests."""

import json
import os
from pathlib import Path
from typing import TypedDict, cast

import gws
import gws_json
import pytest


class _ChunkRef(TypedDict):
    id: str
    size: int
    sha256: str


class _ObjectReceipt(TypedDict):
    head: _ChunkRef
    readbacks: dict[str, str]


def _workspace(tmp: Path) -> Path:
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


def _commit(
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
    draft = gws_json.pack(ws, staged, generation)
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
            meta = gws_json.link(
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
    gws_json.prepare(staged, receipts, manifest)
    assert gws_json.verify(manifest, receipts)["generation"] == generation
    return manifest, receipts, current, staged


def test_small_json_state_with_external_snapshots_restores_exact_bytes(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    generation = "20261020T000000Z"
    first, maps, _, stage = _commit(ws, tmp_path, generation)
    state = json.loads(first.read_text())
    assert first.stat().st_size < gws_json.MAX_STATE_BYTES
    assert "text" in next(x for x in state["files"] if x["path"].endswith("a.md"))
    assert "object" in next(
        x for x in state["files"] if x["path"].endswith("source.txt")
    )
    other = tmp_path / "second-stage"
    gws_json.pack(ws, other, generation)
    assert (stage / "draft.json").read_bytes() == (other / "draft.json").read_bytes()
    restored = tmp_path / "restored"
    gws.restore(first, maps, restored)
    for path in ws.rglob("*"):
        if path.is_file():
            copied = restored / path.relative_to(ws)
            assert copied.read_bytes() == path.read_bytes()
            if "snapshots" in path.parts:
                assert copied.stat().st_mtime_ns == path.stat().st_mtime_ns


def test_reuse_unchanged_objects_between_generations(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    a, _, old, _ = _commit(ws, tmp_path, "20261020T000000Z")
    # Only the inline current-state file changed, object head IDs remain identical.
    (ws / "internal/gws/delivery.json").write_text(
        '{"reports":{"run":"abc"},"version":1}'
    )
    b, b_map, new, _ = _commit(ws, tmp_path, "20261020T000001Z", old)
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
    ws = _workspace(tmp_path)
    manifest, maps, receipts, _ = _commit(ws, tmp_path, "20261020T000000Z")
    first = next(iter(receipts.values()))
    path = Path(next(iter(first["readbacks"].values())))
    expected = path.read_bytes()
    path.write_bytes(expected[:-1])
    with pytest.raises(gws.GwsError, match="readback mismatch"):
        gws.restore(manifest, maps, tmp_path / "should-not-exist")
    assert not (tmp_path / "should-not-exist").exists()


def test_oversized_state_fails_closed(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    folder = ws / "internal/state"
    for index in range(1800):
        (folder / f"{index:04d}.json").write_text('{"x":"y"}')
    with pytest.raises(gws.GwsError, match="JSON exceeds connector"):
        gws_json.pack(ws, tmp_path / "too-large", "20261020T000000Z")
    assert not (tmp_path / "too-large").exists()


def test_retention_preserves_shared_object_ids_and_legacy_restore(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    legacy_stage = tmp_path / "legacy-stage"
    legacy = gws.pack(ws, legacy_stage, "20261019T000000Z")
    legacy_receipts: dict[str, dict[str, str]] = {}
    old_reads: dict[str, str] = {}
    for item in legacy["files"]:
        digest = item["sha256"]
        if digest in legacy_receipts:
            continue
        path = legacy_stage / "payloads" / f"{digest}.txt"
        identifier = f"old-{digest}"
        legacy_receipts[digest] = {"id": identifier, "file": str(path)}
        old_reads[identifier] = str(path)
    legacy_receipts_file = tmp_path / "legacy-receipts.json"
    legacy_receipts_file.write_text(json.dumps(legacy_receipts))
    legacy_manifest = tmp_path / "legacy-manifest.json"
    gws.prepare(legacy_stage, legacy_receipts_file, legacy_manifest)
    legacy_maps = tmp_path / "legacy-maps.json"
    legacy_maps.write_text(json.dumps(old_reads))
    assert gws.verify(legacy_manifest, legacy_maps)["file_count"] > 0

    rows: list[dict[str, str]] = [{
        "name": "workspace-20261019T000000Z.json",
        "id": "legacy-manifest",
        "manifest": str(legacy_manifest),
        "readbacks": str(legacy_maps),
    }]
    previous: dict[str, _ObjectReceipt] | None = None
    protected: set[str] = set()
    for second in range(4):
        generation = f"20261020T00000{second}Z"
        manifest, maps, previous, _ = _commit(ws, tmp_path, generation, previous)
        for receipt in previous.values():
            protected.update(receipt["readbacks"])
        rows.append({
            "name": f"workspace-{generation}.json",
            "id": f"new-manifest-{second}",
            "manifest": str(manifest),
            "readbacks": str(maps),
        })
    manifest_list = tmp_path / "mixed-generations.json"
    manifest_list.write_text(json.dumps(rows))
    plan = gws.retention(manifest_list)
    assert set(plan["retain"]) == {
        "new-manifest-1", "new-manifest-2", "new-manifest-3"
    }
    assert {"legacy-manifest", "new-manifest-0"} <= set(plan["delete"])
    assert not protected.intersection(plan["delete"])
