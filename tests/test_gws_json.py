"""JSON-state + content-addressed Drive object transport regression tests."""

import json
import os
from pathlib import Path

import gws
import gws_json
import pytest


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
    previous: dict[str, dict[str, object]] | None = None,
) -> tuple[Path, Path, dict[str, dict[str, object]], Path]:
    """Simulate exact-ID textContent uploads and verified readbacks."""
    staged = tmp / ("stage-" + generation)
    draft = gws_json.pack(ws, staged, generation)
    assert draft["format"] == "wsum-json-1"
    objects: dict[str, dict[str, object]] = dict(previous or {})
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
                staged / "objects" / digest / f"{index:04d}.txt",
                nxt, index, chunk_path
            )
            relay = gws.transfer_text(chunk_path)
            assert relay["textContent"] == chunk_path.read_text()
            assert "base64Content" not in relay
            file_id = f"id-{generation}-{digest[:8]}-{index}"
            readbacks[file_id] = str(chunk_path)
            nxt.write_text(json.dumps({
                "id": file_id, "size": meta["size"], "sha256": meta["sha256"]
            }))
        objects[digest] = {
            "head": json.loads(nxt.read_text()), "readbacks": readbacks
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
    assert "object" in next(x for x in state["files"] if x["path"].endswith("source.txt"))
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
    rows = []
    for i, (manifest, maps) in enumerate(((a, tmp_path / "receipts-20261020T000000Z.json"), (b, b_map))):
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
