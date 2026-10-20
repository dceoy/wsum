"""Deterministic tar.gz snapshots transported as verified text chunk chains.

The archive format complements the legacy per-file wsum-text-1 manifests.
Only the caller uses Google Drive; this module performs local byte verification.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import typing
from pathlib import Path

import gws

# pyright: reportPrivateUsage=false  # shared validation layer lives in gws.py

FORMAT = "wsum-archive-1"
CHUNK_BYTES = 24 * 1024
MAX_CHUNKS = 2048
MAX_CHUNK_TEXT = 40 * 1024
MAX_ARCHIVE = CHUNK_BYTES * MAX_CHUNKS
METADATA = "workspace-index.json"


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path, limit: int = gws.MAX_MANIFEST) -> typing.Any:
    return json.loads(
        gws._text_bytes(path, limit), object_pairs_hook=gws._unique_object
    )


def _ref(value: object) -> dict[str, typing.Any]:
    """Validate an immutable Drive chunk reference."""
    if not isinstance(value, dict):
        raise gws.GwsError("invalid archive chunk reference")
    record = typing.cast("dict[str, typing.Any]", value)
    if (
        set(record) != {"id", "size", "sha256"}
        or not gws._file_id(record["id"])
        or type(record["size"]) is not int
        or not 0 < record["size"] <= MAX_CHUNK_TEXT
        or not isinstance(record["sha256"], str)
        or not gws.SHA_RE.fullmatch(record["sha256"])
    ):
        raise gws.GwsError("invalid archive chunk reference")
    return record


def _manifest(path: Path) -> dict[str, typing.Any]:
    """Validate the archive root manifest before trusting any file ID."""
    raw = _load_json(path)
    if not isinstance(raw, dict):
        raise gws.GwsError("invalid archive manifest")
    value = typing.cast("dict[str, typing.Any]", raw)
    archive_raw = value.get("archive")
    if not isinstance(archive_raw, dict):
        raise gws.GwsError("invalid archive manifest")
    archive = typing.cast("dict[str, typing.Any]", archive_raw)
    if (
        set(value) != {"format", "generation", "archive", "count", "head"}
        or value["format"] != FORMAT
        or not isinstance(value["generation"], str)
        or set(archive) != {"size", "sha256"}
        or type(archive["size"]) is not int
        or not 0 < archive["size"] <= MAX_ARCHIVE
        or not isinstance(archive["sha256"], str)
        or not gws.SHA_RE.fullmatch(archive["sha256"])
        or type(value["count"]) is not int
        or not 1 <= value["count"] <= MAX_CHUNKS
    ):
        raise gws.GwsError("invalid archive manifest")
    gws._timestamp(value["generation"])
    _ref(value["head"])
    return value


def _entry(name: str, content: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mtime = 0
    info.mode = 0o600
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def pack(workspace: Path, snapshot: Path, generation: str) -> dict[str, typing.Any]:
    """Create a deterministic gzip tar and its bounded Base64 fragments."""
    gws._timestamp(generation)
    if snapshot.exists() or snapshot.is_symlink():
        raise gws.GwsError("refusing to overwrite existing snapshot")
    files = gws._workspace_files(workspace)
    stage = Path(tempfile.mkdtemp(prefix=".tmp-archive-", dir=snapshot.parent))
    try:
        entries: list[dict[str, typing.Any]] = []
        payloads: list[tuple[str, bytes]] = []
        total = 0
        for name, path in files:
            if not gws._safe_path(name):
                raise gws.GwsError("unsafe workspace path")
            content = gws._text_bytes(path)
            total += len(content)
            if total > gws.MAX_TOTAL:
                raise gws.GwsError("workspace size limit exceeded")
            record: dict[str, typing.Any] = {
                "path": name,
                "size": len(content),
                "sha256": _hash(content),
            }
            if name.startswith("internal/state/snapshots/"):
                record["mtime_ns"] = path.stat().st_mtime_ns
            entries.append(record)
            payloads.append((name, content))
        archive = stage / "workspace.tar.gz"
        with archive.open("wb") as output:
            with gzip.GzipFile(
                fileobj=output, mode="wb", mtime=0, filename=""
            ) as zipped:
                with tarfile.open(
                    fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT
                ) as tar:
                    index = gws._json_bytes(entries)
                    tar.addfile(_entry(METADATA, index), io.BytesIO(index))
                    for name, content in payloads:
                        tar.addfile(_entry(name, content), io.BytesIO(content))
        size = archive.stat().st_size
        if not 0 < size <= MAX_ARCHIVE:
            raise gws.GwsError("compressed archive exceeds chunk transport limit")
        (stage / "fragments").mkdir()
        digest = hashlib.sha256()
        count = 0
        with archive.open("rb") as source:
            while part := source.read(CHUNK_BYTES):
                if count >= MAX_CHUNKS:
                    raise gws.GwsError("too many archive chunks")
                digest.update(part)
                (stage / "fragments" / f"{count:04d}.txt").write_text(
                    base64.b64encode(part).decode("ascii"), encoding="ascii"
                )
                count += 1
        draft = {
            "format": FORMAT,
            "generation": generation,
            "archive": {"size": size, "sha256": digest.hexdigest()},
            "count": count,
        }
        (stage / "draft.json").write_bytes(gws._json_bytes(draft))
        stage.rename(snapshot)
        return draft
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def link(
    fragment: Path, next_ref: Path, output: Path, index: int
) -> dict[str, typing.Any]:
    """Wrap a fragment with the previously uploaded successor reference."""
    if output.exists() or output.is_symlink():
        raise gws.GwsError("refusing to overwrite linked chunk")
    encoded = gws._text_bytes(fragment, MAX_CHUNK_TEXT).decode("ascii")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise gws.GwsError("invalid staged chunk Base64") from exc
    if not content or len(content) > CHUNK_BYTES or index < 0:
        raise gws.GwsError("invalid staged chunk")
    nxt = _load_json(next_ref, MAX_CHUNK_TEXT)
    if nxt is not None:
        _ref(nxt)
    body = gws._json_bytes({"index": index, "payload": encoded, "next": nxt})
    if len(body) > MAX_CHUNK_TEXT:
        raise gws.GwsError("linked chunk exceeds text limit")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        stream.write(body)
    return {"sha256": _hash(body), "size": len(body)}


def prepare(snapshot: Path, head_ref: Path, destination: Path) -> dict[str, typing.Any]:
    """Create a compact final manifest after every chunk has verified."""
    if destination.exists() or destination.is_symlink():
        raise gws.GwsError("refusing to overwrite manifest")
    draft = _load_json(snapshot / "draft.json")
    if not isinstance(draft, dict):
        raise gws.GwsError("invalid staged archive")
    record = typing.cast("dict[str, typing.Any]", draft)
    if set(record) != {"format", "generation", "archive", "count"} or record["format"] != FORMAT:
        raise gws.GwsError("invalid staged archive")
    manifest = {**record, "head": _ref(_load_json(head_ref, MAX_CHUNK_TEXT))}
    content = gws._json_bytes(manifest)
    destination.write_bytes(content)
    try:
        _manifest(destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return {"sha256": _hash(content), "size": len(content)}


def _archive_bytes(
    manifest: Path, readbacks: Path
) -> tuple[dict[str, typing.Any], bytes, list[str]]:
    value = _manifest(manifest)
    paths = _load_json(readbacks)
    if not isinstance(paths, dict):
        raise gws.GwsError("invalid archive readback map")
    paths = typing.cast("dict[str, object]", paths)
    seen: set[str] = set()
    parts: list[bytes] = []
    current: object = value["head"]
    total = 0
    digest = hashlib.sha256()
    for index in range(value["count"]):
        ref = _ref(current)
        file_id = ref["id"]
        if file_id in seen or not isinstance(paths.get(file_id), str):
            raise gws.GwsError("missing, repeated or cyclic archive chunk")
        seen.add(file_id)
        content = gws._text_bytes(Path(paths[file_id]), MAX_CHUNK_TEXT)
        if len(content) != ref["size"] or _hash(content) != ref["sha256"]:
            raise gws.GwsError("archive chunk readback hash mismatch")
        raw_record = json.loads(content, object_pairs_hook=gws._unique_object)
        if not isinstance(raw_record, dict):
            raise gws.GwsError("invalid archive chunk structure")
        record = typing.cast("dict[str, typing.Any]", raw_record)
        if (
            set(record) != {"index", "payload", "next"}
            or type(record["index"]) is not int
            or record["index"] != index
            or not isinstance(record["payload"], str)
        ):
            raise gws.GwsError("invalid archive chunk structure")
        try:
            block = base64.b64decode(record["payload"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise gws.GwsError("invalid archive chunk encoding") from exc
        if not block or len(block) > CHUNK_BYTES:
            raise gws.GwsError("invalid archive chunk size")
        total += len(block)
        if total > MAX_ARCHIVE:
            raise gws.GwsError("archive exceeds compressed size limit")
        digest.update(block)
        parts.append(block)
        current = record["next"]
        if index < value["count"] - 1:
            _ref(current)
        elif current is not None:
            raise gws.GwsError("unexpected additional archive chunk")
    if (
        total != value["archive"]["size"]
        or digest.hexdigest() != value["archive"]["sha256"]
    ):
        raise gws.GwsError("archive size or SHA-256 mismatch")
    return value, b"".join(parts), list(seen)


def _validated_files(archive: bytes) -> dict[str, tuple[bytes, int | None]]:
    """Extract only verified regular files; do not follow tar paths or links."""
    files: dict[str, tuple[bytes, int | None]] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = iter(tar)
            first = next(members)
            if (
                first.name != METADATA
                or not first.isfile()
                or first.size > gws.MAX_MANIFEST
            ):
                raise gws.GwsError("archive workspace index is missing")
            index_stream = tar.extractfile(first)
            if index_stream is None:
                raise gws.GwsError("unreadable archive workspace index")
            entries = json.loads(
                index_stream.read(), object_pairs_hook=gws._unique_object
            )
            if not isinstance(entries, list) or len(entries) > gws.MAX_FILES:
                raise gws.GwsError("invalid archive workspace index")
            expected: dict[str, dict[str, typing.Any]] = {}
            total = 0
            for raw_value in typing.cast("list[object]", entries):
                if not isinstance(raw_value, dict):
                    raise gws.GwsError("invalid archive file metadata")
                value = typing.cast("dict[str, typing.Any]", raw_value)
                if set(value) - {"mtime_ns"} != {"path", "size", "sha256"}:
                    raise gws.GwsError("invalid archive file metadata")
                name = value["path"]
                if (
                    not isinstance(name, str)
                    or not gws._safe_path(name)
                    or name in expected
                    or type(value["size"]) is not int
                    or not 0 <= value["size"] <= gws.MAX_ENTRY
                    or not isinstance(value["sha256"], str)
                    or not gws.SHA_RE.fullmatch(value["sha256"])
                    or (
                        "mtime_ns" in value
                        and (
                            type(value["mtime_ns"]) is not int
                            or not 0 <= value["mtime_ns"] <= 2**63 - 1
                            or not name.startswith("internal/state/snapshots/")
                        )
                    )
                ):
                    raise gws.GwsError("invalid or duplicate archive file metadata")
                expected[name] = value
                total += value["size"]
            if total > gws.MAX_TOTAL or any(
                "/".join(name.split("/")[:i]) in expected
                for name in expected
                for i in range(1, len(name.split("/")))
            ):
                raise gws.GwsError("archive workspace paths or size exceed limits")
            for member in members:
                if (
                    member.name not in expected
                    or member.name in files
                    or not member.isfile()
                ):
                    raise gws.GwsError("unsafe or unexpected archive member")
                record = expected[member.name]
                if member.size != record["size"]:
                    raise gws.GwsError("archive member size mismatch")
                stream = tar.extractfile(member)
                if stream is None:
                    raise gws.GwsError("unreadable archive member")
                content = stream.read(record["size"] + 1)
                if len(content) != record["size"] or _hash(content) != record["sha256"]:
                    raise gws.GwsError("archive member SHA-256 mismatch")
                content.decode("utf-8")
                if any(b < 32 and b not in (9, 10, 13) for b in content):
                    raise gws.GwsError("unsupported control bytes in archive")
                files[member.name] = (content, record.get("mtime_ns"))
            if set(files) != set(expected):
                raise gws.GwsError("incomplete archive workspace")
    except (tarfile.TarError, EOFError, StopIteration, OSError) as exc:
        raise gws.GwsError("invalid or truncated archive") from exc
    return files


def verify(manifest: Path, readbacks: Path) -> dict[str, typing.Any]:
    """Validate exact Drive readbacks, compressed hash and all tar members."""
    value, archive, ids = _archive_bytes(manifest, readbacks)
    _validated_files(archive)
    return {
        "generation": value["generation"],
        "file_count": value["count"],
        "size": manifest.stat().st_size,
        "sha256": _hash(manifest.read_bytes()),
        "ids": ids,
    }


def restore(
    manifest: Path, readbacks: Path, destination: Path
) -> dict[str, typing.Any]:
    """Restore a new workspace atomically from verified archive content."""
    value, archive, _ = _archive_bytes(manifest, readbacks)
    files = _validated_files(archive)
    if destination.exists() or destination.is_symlink():
        raise gws.GwsError("restore destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".tmp-restore-", dir=destination.parent))
    try:
        for root in ("output", "internal"):
            (stage / root).mkdir()
        for name, (content, stamp) in files.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            if stamp is not None:
                os.utime(path, ns=(stamp, stamp))
                if path.stat().st_mtime_ns != stamp:
                    raise gws.GwsError("snapshot mtime precision is unsupported")
        if destination.exists() or destination.is_symlink():
            raise gws.GwsError("restore destination already exists")
        stage.rename(destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"generation": value["generation"], "file_count": len(files)}
