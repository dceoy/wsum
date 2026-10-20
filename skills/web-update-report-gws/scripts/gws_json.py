"""Content-addressed JSON workspace checkpoints for Google Drive text transport.

Only state manifests are committed each generation. Immutable external objects
are reused by SHA-256 across generations; large objects use verified text chunks.
This module performs no network operations and imports no Google credentials.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import io
import json
import os
import shutil
import tempfile
import typing
from pathlib import Path

import gws

# Shared validation primitives are intentionally reused from gws.
# pyright: reportPrivateUsage=false

FORMAT = "wsum-json-1"
MAX_STATE_BYTES = 64 * 1024
INLINE_BYTES = 1024
CHUNK_CHARS = 12 * 1024
MAX_CHUNKS = 4096
MAX_LINK_BYTES = 16 * 1024
MAX_COMPRESSED = CHUNK_CHARS * MAX_CHUNKS


def _load(path: Path, limit: int = gws.MAX_MANIFEST) -> typing.Any:
    return json.loads(
        gws._text_bytes(path, limit), object_pairs_hook=gws._unique_object
    )


def _reference(raw: object) -> dict[str, typing.Any]:
    """Check a content-addressed object's exact Drive ID, size and SHA-256."""
    if not isinstance(raw, dict):
        raise gws.GwsError("invalid Drive chunk reference")
    ref = typing.cast("dict[str, typing.Any]", raw)
    if (
        set(ref) != {"id", "size", "sha256"}
        or not gws._file_id(ref["id"])
        or type(ref["size"]) is not int
        or not 0 < ref["size"] <= MAX_LINK_BYTES
        or not isinstance(ref["sha256"], str)
        or not gws.SHA_RE.fullmatch(ref["sha256"])
    ):
        raise gws.GwsError("invalid Drive chunk reference")
    return ref


def _state(path: Path, *, draft: bool = False) -> dict[str, typing.Any]:
    """Parse the bounded, standalone current-state JSON checkpoint."""
    raw = _load(path, MAX_STATE_BYTES)
    if not isinstance(raw, dict):
        raise gws.GwsError("invalid JSON state")
    state = typing.cast("dict[str, typing.Any]", raw)
    if (
        set(state) != {"format", "generation", "files"}
        or state["format"] != FORMAT
        or not isinstance(state["generation"], str)
        or not isinstance(state["files"], list)
        or len(state["files"]) > gws.MAX_FILES
    ):
        raise gws.GwsError("invalid JSON state header")
    gws._timestamp(state["generation"])
    names: set[str] = set()
    total = 0
    for raw_item in state["files"]:
        if not isinstance(raw_item, dict):
            raise gws.GwsError("invalid JSON state file")
        item = typing.cast("dict[str, typing.Any]", raw_item)
        if (
            (
                set(item) - {"mtime_ns"} != {"path", "size", "sha256", "text"}
                and set(item) - {"mtime_ns"} != {"path", "size", "sha256", "object"}
            )
            or not isinstance(item.get("path"), str)
            or not gws._safe_path(item["path"])
            or item["path"] in names
            or type(item.get("size")) is not int
            or not 0 <= item["size"] <= gws.MAX_ENTRY
            or not isinstance(item.get("sha256"), str)
            or not gws.SHA_RE.fullmatch(item["sha256"])
            or (
                "mtime_ns" in item
                and (
                    type(item["mtime_ns"]) is not int
                    or not 0 <= item["mtime_ns"] <= 2**63 - 1
                    or not item["path"].startswith("internal/state/snapshots/")
                )
            )
        ):
            raise gws.GwsError("unsafe, duplicate or malformed JSON state file")
        names.add(item["path"])
        total += item["size"]
        if "text" in item:
            if not isinstance(item["text"], str):
                raise gws.GwsError("invalid inline state text")
            content = item["text"].encode("utf-8")
            if (
                len(content) > INLINE_BYTES
                or len(content) != item["size"]
                or gws._digest(content) != item["sha256"]
                or item["path"].startswith("internal/state/snapshots/")
            ):
                raise gws.GwsError("inline state mismatch")
        else:
            raw_object = item["object"]
            if not isinstance(raw_object, dict):
                raise gws.GwsError("invalid external object")
            obj = typing.cast("dict[str, typing.Any]", raw_object)
            if (
                set(obj) != ({"chunks"} if draft else {"chunks", "head"})
                or type(obj["chunks"]) is not int
                or not 1 <= obj["chunks"] <= MAX_CHUNKS
            ):
                raise gws.GwsError("invalid external object metadata")
            if not draft:
                _reference(obj["head"])
    if total > gws.MAX_TOTAL or any(
        "/".join(name.split("/")[:i]) in names
        for name in names
        for i in range(1, len(name.split("/")))
    ):
        raise gws.GwsError("workspace size or path conflict")
    return state


def pack(workspace: Path, snapshot: Path, generation: str) -> dict[str, typing.Any]:
    """Stage small state JSON and unique immutable compressed text objects."""
    gws._timestamp(generation)
    if snapshot.exists() or snapshot.is_symlink():
        raise gws.GwsError("refusing to overwrite staged state")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=snapshot.parent, prefix=".tmp-state-"))
    try:
        (stage / "objects").mkdir()
        files: list[dict[str, typing.Any]] = []
        total = 0
        for name, path in gws._workspace_files(workspace):
            if not gws._safe_path(name):
                raise gws.GwsError("unsafe workspace path")
            content = gws._text_bytes(path)
            total += len(content)
            if total > gws.MAX_TOTAL:
                raise gws.GwsError("workspace exceeds size limit")
            digest = gws._digest(content)
            item: dict[str, typing.Any] = {
                "path": name,
                "size": len(content),
                "sha256": digest,
            }
            if name.startswith("internal/state/snapshots/"):
                item["mtime_ns"] = path.stat().st_mtime_ns
            if len(content) <= INLINE_BYTES and not name.startswith(
                "internal/state/snapshots/"
            ):
                item["text"] = content.decode("utf-8")
            else:
                object_dir = stage / "objects" / digest
                if not object_dir.exists():
                    object_dir.mkdir()
                    (object_dir / "source.txt").write_bytes(content)
                    encoded = base64.b64encode(gzip.compress(content, mtime=0)).decode(
                        "ascii"
                    )
                    fragments = [
                        encoded[i : i + CHUNK_CHARS]
                        for i in range(0, len(encoded), CHUNK_CHARS)
                    ]
                    if not 1 <= len(fragments) <= MAX_CHUNKS:
                        raise gws.GwsError("object exceeds chunk limit")
                    for index, part in enumerate(fragments):
                        (object_dir / f"{index:04d}.txt").write_text(
                            part, encoding="ascii"
                        )
                count = len(list(object_dir.glob("[0-9][0-9][0-9][0-9].txt")))
                item["object"] = {"chunks": count}
            files.append(item)
        state = {"format": FORMAT, "generation": generation, "files": files}
        encoded = gws._json_bytes(state)
        if len(encoded) > MAX_STATE_BYTES:
            raise gws.GwsError("current-state JSON exceeds connector transport bound")
        (stage / "draft.json").write_bytes(encoded)
        _state(stage / "draft.json", draft=True)
        stage.rename(snapshot)
        return state
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def link(
    fragment: Path, next_json: Path, index: int, output: Path
) -> dict[str, typing.Any]:
    """Build one immutable JSON text fragment, tail to head."""
    if index < 0 or output.exists() or output.is_symlink():
        raise gws.GwsError("invalid or existing linked chunk destination")
    data = gws._text_bytes(fragment, CHUNK_CHARS).decode("ascii")
    if not data or not all(
        c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
        for c in data
    ):
        raise gws.GwsError("invalid encoded object fragment")
    successor = _load(next_json, MAX_LINK_BYTES)
    if successor is not None:
        _reference(successor)
    encoded = gws._json_bytes({"index": index, "data": data, "next": successor})
    if len(encoded) > MAX_LINK_BYTES:
        raise gws.GwsError("object chunk exceeds connector text bound")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        stream.write(encoded)
    return {"size": len(encoded), "sha256": gws._digest(encoded)}


def _object_bytes(
    item: dict[str, typing.Any],
    receipt: dict[str, typing.Any],
) -> tuple[bytes, set[str]]:
    """Verify an entire hash-linked object using downloaded exact-ID text files."""
    object_ref = item["object"]
    count: int = object_ref["chunks"]
    paths_raw = receipt.get("readbacks")
    if not isinstance(paths_raw, dict):
        raise gws.GwsError("missing object readback map")
    paths = typing.cast("dict[str, object]", paths_raw)
    current: object = object_ref["head"]
    seen: set[str] = set()
    data: list[str] = []
    for index in range(count):
        ref = _reference(current)
        file_id: str = ref["id"]
        file_path = paths.get(file_id)
        if file_id in seen or not isinstance(file_path, str):
            raise gws.GwsError("missing or cyclic exact-ID object chunk")
        seen.add(file_id)
        content = gws._text_bytes(Path(file_path), MAX_LINK_BYTES)
        if len(content) != ref["size"] or gws._digest(content) != ref["sha256"]:
            raise gws.GwsError("object chunk readback mismatch")
        raw = json.loads(content, object_pairs_hook=gws._unique_object)
        if not isinstance(raw, dict):
            raise gws.GwsError("malformed object chunk")
        record = typing.cast("dict[str, typing.Any]", raw)
        if (
            set(record) != {"index", "data", "next"}
            or type(record["index"]) is not int
            or record["index"] != index
            or not isinstance(record["data"], str)
            or not 0 < len(record["data"]) <= CHUNK_CHARS
        ):
            raise gws.GwsError("malformed object chunk")
        data.append(record["data"])
        current = record["next"]
        if index != count - 1:
            _reference(current)
        elif current is not None:
            raise gws.GwsError("extra object chunk after final index")
    if set(paths) != seen:
        raise gws.GwsError("unexpected object chunk readbacks")
    encoded = "".join(data)
    if len(encoded) > MAX_COMPRESSED:
        raise gws.GwsError("compressed object exceeds size bound")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as zipped:
            content = zipped.read(item["size"] + 1)
            if zipped.read(1):
                raise gws.GwsError("oversized decompressed object")
    except (OSError, EOFError, ValueError, binascii.Error) as exc:
        raise gws.GwsError("invalid compressed object") from exc
    if len(content) != item["size"] or gws._digest(content) != item["sha256"]:
        raise gws.GwsError("external object size or SHA-256 mismatch")
    content.decode("utf-8")
    if any(byte < gws.CONTROL_LIMIT and byte not in {9, 10, 13} for byte in content):
        raise gws.GwsError("unsupported control content")
    return content, seen


def _receipts(path: Path) -> dict[str, dict[str, typing.Any]]:
    raw = _load(path)
    if not isinstance(raw, dict):
        raise gws.GwsError("object receipts must be a map")
    return typing.cast("dict[str, dict[str, typing.Any]]", raw)


def prepare(snapshot: Path, receipts: Path, destination: Path) -> dict[str, typing.Any]:
    """Validate new/reused objects before publishing the immutable state JSON."""
    state = _state(snapshot / "draft.json", draft=True)
    uploads = _receipts(receipts)
    wanted = {item["sha256"] for item in state["files"] if "object" in item}
    if set(uploads) != wanted:
        raise gws.GwsError("missing or unexpected object receipts")
    for item in state["files"]:
        if "object" not in item:
            continue
        receipt = uploads[item["sha256"]]
        if not isinstance(receipt, dict) or set(receipt) != {"head", "readbacks"}:
            raise gws.GwsError("invalid object receipt")
        item["object"]["head"] = _reference(receipt["head"])
        content, _ = _object_bytes(item, receipt)
        source = snapshot / "objects" / item["sha256"] / "source.txt"
        if gws._digest(gws._text_bytes(source)) != gws._digest(content):
            raise gws.GwsError("staged object changed during transfer")
    if destination.exists() or destination.is_symlink():
        raise gws.GwsError("refusing to overwrite state manifest")
    encoded = gws._json_bytes(state)
    if len(encoded) > MAX_STATE_BYTES:
        raise gws.GwsError("final state JSON exceeds connector transport bound")
    with destination.open("xb") as output:
        output.write(encoded)
    _state(destination)
    return {"sha256": gws._digest(encoded), "size": len(encoded)}


def _verified(
    manifest: Path, readbacks: Path
) -> tuple[dict[str, typing.Any], dict[str, bytes], set[str]]:
    state = _state(manifest)
    all_maps = _receipts(readbacks)
    wanted = {item["sha256"] for item in state["files"] if "object" in item}
    if set(all_maps) != wanted:
        raise gws.GwsError("incomplete object readback map")
    contents: dict[str, bytes] = {}
    ids: set[str] = set()
    for item in state["files"]:
        if "text" in item:
            contents[item["path"]] = item["text"].encode("utf-8")
        else:
            receipt = all_maps[item["sha256"]]
            if not isinstance(receipt, dict) or set(receipt) != {"head", "readbacks"}:
                raise gws.GwsError("invalid object readback")
            if _reference(receipt["head"]) != item["object"]["head"]:
                raise gws.GwsError("object reference changed after commit")
            content, object_ids = _object_bytes(item, receipt)
            contents[item["path"]] = content
            ids.update(object_ids)
    return state, contents, ids


def verify(manifest: Path, readbacks: Path) -> dict[str, typing.Any]:
    """Verify every referenced object and return exact protected Drive IDs."""
    state, contents, ids = _verified(manifest, readbacks)
    return {
        "generation": state["generation"],
        "file_count": len(contents),
        "size": manifest.stat().st_size,
        "sha256": gws._digest(manifest.read_bytes()),
        "ids": sorted(ids),
    }


def restore(manifest: Path, readbacks: Path, workspace: Path) -> dict[str, typing.Any]:
    """Restore a complete workspace atomically from a committed JSON generation."""
    state, contents, _ = _verified(manifest, readbacks)
    if workspace.exists() or workspace.is_symlink():
        raise gws.GwsError("restore destination must not exist")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=workspace.parent, prefix=".tmp-restore-"))
    try:
        for root in ("output", "internal"):
            (stage / root).mkdir()
        for item in state["files"]:
            dest = stage / item["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(contents[item["path"]])
            if "mtime_ns" in item:
                stamp = item["mtime_ns"]
                os.utime(dest, ns=(stamp, stamp))
                if dest.stat().st_mtime_ns != stamp:
                    raise gws.GwsError("restored snapshot mtime precision mismatch")
        if workspace.exists() or workspace.is_symlink():
            raise gws.GwsError("restore destination already exists")
        stage.rename(workspace)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"generation": state["generation"], "file_count": len(contents)}
