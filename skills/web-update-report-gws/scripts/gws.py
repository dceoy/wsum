"""Deterministic local Google Workspace projection and snapshot operations.

Google connector transport and permissions remain the host agent's responsibility.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import importlib.util
import json
import operator
import os
import re
import shutil
import stat
import sys
import tempfile
import typing
from datetime import UTC, datetime, timedelta
from pathlib import Path

FIELDS = (
    "name",
    "url",
    "publisher",
    "category",
    "keywords",
    "criteria",
    "priority",
    "enabled",
)
SNAPSHOT_RE = re.compile(r"^workspace-(\d{8}T\d{6}Z)\.json$")
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_FILES = 2000
MAX_ENTRY = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
CONTROL_LIMIT = 32
MAX_MANIFEST = 2 * 1024 * 1024
MAX_TARGET_CSV = 1024 * 1024
MAX_BINDING_BYTES = 4096
MAX_DRIVE_ID_LENGTH = 256
FOLDER_BINDING_ID_COUNT = 3


class GwsError(ValueError):
    """Reject invalid connector data or untrusted workspace text."""


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".tmp-", delete=False
    ) as stream:
        temp = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def transfer_encode(path: Path) -> dict[str, str | int]:
    """Expose exact local UTF-8 bytes for a connector base64Content upload."""
    content = _text_bytes(path)
    return {
        "base64Content": base64.b64encode(content).decode("ascii"),
        "sha256": _digest(content),
        "size": len(content),
    }


def transfer_text(path: Path) -> dict[str, str | int]:
    """Relay exact local UTF-8 contents to Drive create_file.textContent."""
    content = _text_bytes(path)
    return {"textContent": content.decode("utf-8"), "sha256": _digest(content), "size": len(content)}


def transfer_decode(
    source: Path, destination: Path, size: int, sha256: str | None
) -> dict[str, str | int | bool]:
    """Materialize a connector Base64 response and check independent expectations."""
    if destination.exists() or destination.is_symlink():
        raise GwsError("transfer destination must not exist")
    if size < 0 or size > MAX_ENTRY:
        raise GwsError("invalid transfer byte size")
    if sha256 is not None and not SHA_RE.fullmatch(sha256):
        raise GwsError("invalid expected transfer SHA-256")
    encoded = source.read_bytes().strip()
    if len(encoded) > ((MAX_ENTRY + 2) // 3) * 4:
        raise GwsError("encoded transfer exceeds size limit")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise GwsError("invalid connector Base64 content") from exc
    if len(content) != size:
        raise GwsError("connector transfer size mismatch")
    digest = _digest(content)
    if sha256 is not None and digest != sha256:
        raise GwsError("connector transfer SHA-256 mismatch")
    _write_atomic(destination, content)
    return {"sha256": digest, "size": len(content), "verified": sha256 is not None}


def _timestamp(value: str) -> datetime:
    try:
        result = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise GwsError(f"invalid generation: {value}") from exc
    if result.strftime("%Y%m%dT%H%M%SZ") != value:
        raise GwsError(f"invalid generation: {value}")
    return result


def _snapshot_record(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise GwsError("snapshot must contain name and id strings")
    record = typing.cast("dict[str, object]", value)
    if set(record) != {"name", "id"} or any(
        not isinstance(item, str) or not item.strip() for item in record.values()
    ):
        raise GwsError("snapshot must contain name and id strings")
    return typing.cast("dict[str, str]", record)


def next_generation(snapshots: object, now: datetime, expected_snapshot: object) -> str:
    """Reject a changed active Drive snapshot before deriving a monotonic time."""
    if not isinstance(snapshots, list):
        raise GwsError("snapshots JSON must be an array of name/id records")
    expected = (
        _snapshot_record(expected_snapshot) if expected_snapshot is not None else None
    )
    known: set[str] = set()
    newest = None
    active = None
    for value in typing.cast("list[object]", snapshots):
        record = _snapshot_record(value)
        name = record["name"]
        match = SNAPSHOT_RE.fullmatch(name)
        if not match:
            continue
        if name in known:
            raise GwsError(f"duplicate snapshot filename: {name}")
        known.add(name)
        instant = _timestamp(match.group(1))
        if newest is None or instant > newest:
            newest = instant
            active = record
    if active != expected:
        raise GwsError("active snapshot changed since restore or last commit")
    result = now.astimezone(UTC).replace(microsecond=0)
    if newest is not None:
        result = max(result, newest + timedelta(seconds=1))
    return result.strftime("%Y%m%dT%H%M%SZ")


def _sheet_projection(
    sheet: Path,
) -> tuple[list[list[typing.Any]], dict[str, int]]:
    raw: typing.Any = json.loads(sheet.read_text(encoding="utf-8"))
    rows_value: typing.Any = (
        typing.cast("dict[str, typing.Any]", raw).get("values")
        if isinstance(raw, dict)
        else raw
    )
    if not isinstance(rows_value, list) or not rows_value:
        raise GwsError("Sheet input must be a non-empty array of value arrays")
    raw_rows = typing.cast("list[typing.Any]", rows_value)
    if any(not isinstance(row, list) for row in raw_rows):
        raise GwsError("Sheet input must be a non-empty array of value arrays")
    rows = typing.cast("list[list[typing.Any]]", raw_rows)
    if any(not isinstance(v, (str, int, float, bool)) for row in rows for v in row):
        raise GwsError("Sheet cells must be scalar values")
    names = [str(value).strip() for value in rows[0]]
    if any(names.count(name) > 1 for name in FIELDS if name in names):
        raise GwsError("duplicate selected Sheet header")
    if not all(name in names for name in ("name", "url")):
        raise GwsError("Sheet must contain name and url headers")
    index = {name: names.index(name) for name in FIELDS if name in names}
    for row_number, row in enumerate(rows[1:], start=2):
        if len(row) > len(names) and any(
            str(cell).strip() for cell in row[len(names) :]
        ):
            raise GwsError(f"Sheet row {row_number} has cells beyond the header")
    return rows, index


def _write_projection(
    path: Path, rows: list[list[typing.Any]], index: dict[str, int]
) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(FIELDS)
        for row in rows[1:]:
            if not any(str(cell).strip() for cell in row):
                continue
            writer.writerow([
                str(row[index[name]])
                if name in index and index[name] < len(row)
                else ""
                for name in FIELDS
            ])
        output.flush()
        os.fsync(output.fileno())


def _load_core_targets(temp: Path, core_skill: Path) -> list[object]:
    scripts = core_skill / "scripts"
    core_path = scripts / "workspace.py"
    if not core_path.is_file():
        raise GwsError(f"core skill not installed at {core_skill}")
    core_path_id = hashlib.sha256(str(core_path.resolve()).encode()).hexdigest()[:16]
    module_name = f"_wsum_core_workspace_{core_path_id}_{id(temp)}"
    spec = importlib.util.spec_from_file_location(module_name, core_path)
    loader = spec.loader if spec is not None else None
    if spec is None or loader is None:
        raise GwsError(f"cannot load core skill at {core_path}")
    core = importlib.util.module_from_spec(spec)
    try:
        sys.path.insert(0, str(scripts))
        sys.modules[module_name] = core
        loader.exec_module(core)
        return typing.cast("list[object]", core.load_targets(temp))
    finally:
        sys.modules.pop(module_name, None)
        sys.path.remove(str(scripts))


def project(sheet: Path, destination: Path, core_skill: Path) -> int:
    """Atomically project Connector values only after core CSV validation."""
    rows, index = _sheet_projection(sheet)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=".tmp-", delete=False
    ) as stream:
        temp = Path(stream.name)
    try:
        _write_projection(temp, rows, index)
        targets = _load_core_targets(temp, core_skill)
        temp.replace(destination)
        return len(targets)
    finally:
        temp.unlink(missing_ok=True)


def project_csv(
    source: Path,
    destination: Path,
    core_skill: Path,
    *,
    drive_size: int | None = None,
) -> int:
    """Validate local or Drive CSV bytes before atomically replacing targets."""
    if drive_size is not None and (
        isinstance(drive_size, bool) or drive_size <= 0 or drive_size > MAX_TARGET_CSV
    ):
        raise GwsError("invalid Drive CSV size metadata")
    if source.is_symlink() or not source.is_file():
        raise GwsError("CSV input must be an existing regular file")
    with source.open("rb") as stream:
        content = stream.read(MAX_TARGET_CSV + 1)
    if len(content) > MAX_TARGET_CSV:
        raise GwsError("CSV input exceeds 1 MiB")
    if drive_size is not None and len(content) != drive_size:
        raise GwsError("Drive CSV download size mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=".tmp-", delete=False
    ) as stream:
        temp = Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        targets = _load_core_targets(temp, core_skill)
        temp.replace(destination)
        return len(targets)
    finally:
        temp.unlink(missing_ok=True)


def folder_binding(
    parent_id: str, workspaces_id: str, reports_id: str
) -> dict[str, object]:
    """Create a strict immutable binding to the three Drive folder IDs."""
    ids = (parent_id, workspaces_id, reports_id)
    if (
        any(
            not value
            or len(value) > MAX_DRIVE_ID_LENGTH
            or any(char.isspace() for char in value)
            for value in ids
        )
        or len(set(ids)) != FOLDER_BINDING_ID_COUNT
    ):
        raise GwsError("invalid or duplicate Drive folder IDs")
    return {
        "version": 1,
        "parent_id": parent_id,
        "workspaces_id": workspaces_id,
        "reports_id": reports_id,
    }


def binding_file(
    path: Path,
    parent_id: str,
    workspaces_id: str,
    reports_id: str,
    *,
    create: bool,
) -> dict[str, object]:
    """Create once, or verify an exact immutable parent-folder binding."""
    expected = folder_binding(parent_id, workspaces_id, reports_id)
    content = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        if path.is_symlink() or not path.is_file():
            raise GwsError("folder binding must be a regular file")
        if path.stat().st_size > MAX_BINDING_BYTES:
            raise GwsError("folder binding exceeds size limit")
        actual: typing.Any = json.loads(path.read_bytes())
        if actual != expected or not isinstance(actual, dict):
            raise GwsError("Drive folder binding IDs or schema changed")
        content = path.read_bytes()
    return {"size": len(content), "sha256": _digest(content), "binding": expected}


def _workspace_files(workspace: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for root in ("output", "internal"):
        folder = workspace / root
        if not folder.is_dir() or folder.is_symlink():
            raise GwsError(f"workspace root must be a real directory: {root}")
        for path in folder.rglob("*"):
            if path.name.startswith(".tmp-") or (
                path.name.endswith(".tmp") and path.name.startswith(".")
            ):
                continue
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise GwsError(f"non-regular workspace entry: {path}")
            files.append((path.relative_to(workspace).as_posix(), path))
    files.sort()
    if len(files) > MAX_FILES:
        raise GwsError("too many workspace files")
    return files


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _text_bytes(path: Path, limit: int = MAX_ENTRY) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise GwsError("text input must be a regular file")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise GwsError("text size limit exceeded")
    text = content.decode("utf-8")
    if any(ord(char) < CONTROL_LIMIT and char not in "\t\r\n" for char in text):
        raise GwsError("unsupported binary/control content")
    return content


def _safe_path(name: str) -> bool:
    parts = name.split("/")
    return (
        len(parts) > 1
        and parts[0] in {"internal", "output"}
        and all(part not in {"", ".", ".."} for part in parts)
        and "\\" not in name
        and not any(ord(char) < CONTROL_LIMIT for char in name)
    )


def _file_id(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value))


def _unique_object(pairs: list[tuple[str, typing.Any]]) -> dict[str, typing.Any]:
    result: dict[str, typing.Any] = {}
    for key, value in pairs:
        if key in result:
            raise GwsError("duplicate JSON key")
        result[key] = value
    return result


def _manifest(path: Path, *, draft: bool = False) -> dict[str, typing.Any]:
    value: typing.Any = json.loads(
        _text_bytes(path, MAX_MANIFEST), object_pairs_hook=_unique_object
    )
    if (
        not isinstance(value, dict)
        or set(typing.cast("dict[str, typing.Any]", value))
        != {"format", "generation", "files"}
        or value["format"] != "wsum-text-1"
        or not isinstance(value["generation"], str)
        or not isinstance(value["files"], list)
        or len(typing.cast("list[typing.Any]", value["files"])) > MAX_FILES
    ):
        raise GwsError("invalid text manifest header")
    value = typing.cast("dict[str, typing.Any]", value)
    _timestamp(value["generation"])
    names: set[str] = set()
    total = 0
    for raw_item in value["files"]:
        if not isinstance(raw_item, dict):
            raise GwsError("invalid manifest file record")
        item = typing.cast("dict[str, typing.Any]", raw_item)
        if (
            set(item) - {"mtime_ns"}
            != ({"path", "size", "sha256"} | (set() if draft else {"id"}))
            or (
                "mtime_ns" in item
                and (
                    type(item["mtime_ns"]) is not int
                    or not 0 <= item["mtime_ns"] <= 2**63 - 1
                )
            )
            or not isinstance(item["path"], str)
            or not _safe_path(item["path"])
            or item["path"] in names
            or type(item["size"]) is not int
            or not 0 <= item["size"] <= MAX_ENTRY
            or not isinstance(item["sha256"], str)
            or not SHA_RE.fullmatch(item["sha256"])
            or (not draft and not _file_id(item["id"]))
        ):
            raise GwsError("invalid, unsafe or duplicate manifest file record")
        names.add(item["path"])
        total += item["size"]
    if total > MAX_TOTAL or any(
        ancestor in names
        for name in names
        for ancestor in (
            "/".join(name.split("/")[:i]) for i in range(1, len(name.split("/")))
        )
    ):
        raise GwsError("manifest size limit or conflicting paths")
    return value


def _check_payload(item: dict[str, typing.Any], path: Path) -> bytes:
    content = _text_bytes(path)
    if len(content) != item["size"] or _digest(content) != item["sha256"]:
        raise GwsError(f"text size or SHA-256 mismatch: {item['path']}")
    return content


def pack(workspace: Path, snapshot: Path, generation: str) -> dict[str, typing.Any]:
    """Stage exact UTF-8 payloads and a deterministic draft; no Drive IDs yet."""
    _timestamp(generation)
    if snapshot.exists() or snapshot.is_symlink():
        raise GwsError("refusing to overwrite existing snapshot")
    files: list[dict[str, typing.Any]] = []
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".tmp-pack-", dir=snapshot.parent))
    try:
        (stage / "payloads").mkdir()
        total = 0
        for name, path in _workspace_files(workspace):
            if not _safe_path(name):
                raise GwsError("unsafe workspace path")
            content = _text_bytes(path)
            total += len(content)
            if total > MAX_TOTAL:
                raise GwsError("workspace size limit exceeded")
            digest = _digest(content)
            (stage / "payloads" / f"{digest}.txt").write_bytes(content)
            item = {"path": name, "size": len(content), "sha256": digest}
            if name.startswith("internal/state/snapshots/"):
                item["mtime_ns"] = path.stat().st_mtime_ns
            files.append(item)
        manifest = {"format": "wsum-text-1", "generation": generation, "files": files}
        (stage / "draft.json").write_bytes(_json_bytes(manifest))
        _manifest(stage / "draft.json", draft=True)
        stage.rename(snapshot)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return manifest


def prepare(snapshot: Path, receipts: Path, destination: Path) -> dict[str, typing.Any]:
    """Verify exact-ID payload read-backs before emitting the final manifest.

    Receipts map each staged SHA-256 to {id, file}, where file is a local
    exact-ID read-back. These are transport evidence supplied by the agent.
    """
    manifest = _manifest(snapshot / "draft.json", draft=True)
    uploads: typing.Any = json.loads(receipts.read_bytes())
    if not isinstance(uploads, dict):
        raise GwsError("invalid receipts")
    uploads = typing.cast("dict[str, typing.Any]", uploads)
    if set(uploads) != {item["sha256"] for item in manifest["files"]}:
        raise GwsError("incomplete payload receipts")
    ids: set[str] = set()
    for item in manifest["files"]:
        raw_receipt = uploads[item["sha256"]]
        if not isinstance(raw_receipt, dict):
            raise GwsError("invalid payload receipt")
        receipt = typing.cast("dict[str, typing.Any]", raw_receipt)
        if (
            set(receipt) != {"id", "file"}
            or not _file_id(receipt["id"])
            or not isinstance(receipt["file"], str)
        ):
            raise GwsError("invalid payload receipt")
        _check_payload(item, snapshot / "payloads" / f"{item['sha256']}.txt")
        _check_payload(item, Path(receipt["file"]))
        item["id"] = receipt["id"]
        ids.add(receipt["id"])
    if len(ids) != len(uploads):
        raise GwsError("distinct payloads require distinct Drive IDs")
    if destination.exists() or destination.is_symlink():
        raise GwsError("refusing to overwrite manifest")
    content = _json_bytes(manifest)
    if len(content) > MAX_MANIFEST:
        raise GwsError("manifest size limit exceeded")
    with destination.open("xb") as stream:
        stream.write(content)
    return {"size": len(content), "sha256": _digest(content)}


def _verified_payloads(
    manifest: Path, readbacks: Path
) -> tuple[dict[str, typing.Any], dict[str, bytes]]:
    value = _manifest(manifest)
    paths: typing.Any = json.loads(readbacks.read_bytes())
    if not isinstance(paths, dict):
        raise GwsError("readbacks must map exact Drive IDs to local files")
    paths = typing.cast("dict[str, typing.Any]", paths)
    contents: dict[str, bytes] = {}
    for item in value["files"]:
        if not isinstance(paths.get(item["id"]), str):
            raise GwsError("missing exact-ID payload read-back")
        contents[item["path"]] = _check_payload(item, Path(paths[item["id"]]))
    return value, contents


def _manifest_format(path: Path) -> str:
    """Read the untrusted header without yet trusting its contents."""
    raw: object = json.loads(
        _text_bytes(path, MAX_MANIFEST), object_pairs_hook=_unique_object
    )
    if not isinstance(raw, dict):
        raise GwsError("invalid manifest header")
    value = typing.cast("dict[str, typing.Any]", raw)
    kind = value.get("format")
    if not isinstance(kind, str):
        raise GwsError("invalid manifest format")
    return kind


def verify(manifest: Path, readbacks: Path) -> dict[str, typing.Any]:
    """Validate referenced objects or legacy payloads by exact Drive ID."""
    if _manifest_format(manifest) == "wsum-json-1":
        import gws_json

        return gws_json.verify(manifest, readbacks)
    value, _ = _verified_payloads(manifest, readbacks)
    return {
        "generation": value["generation"],
        "file_count": len(value["files"]),
        "size": manifest.stat().st_size,
        "sha256": _digest(manifest.read_bytes()),
    }


def restore(
    manifest: Path, readbacks: Path, destination: Path
) -> dict[str, typing.Any]:
    """Install verified bytes into a new workspace, leaving failures uninstalled."""
    if _manifest_format(manifest) == "wsum-json-1":
        import gws_json

        return gws_json.restore(manifest, readbacks, destination)
    value, contents = _verified_payloads(manifest, readbacks)
    if destination.exists() or destination.is_symlink():
        raise GwsError("restore destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".tmp-restore-", dir=destination.parent))
    try:
        for root in ("output", "internal"):
            (stage / root).mkdir()
        for name, content in contents.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for item in value["files"]:
            if "mtime_ns" in item:
                path = stage / item["path"]
                stamp = item["mtime_ns"]
                os.utime(path, ns=(stamp, stamp))
                if path.stat().st_mtime_ns != stamp:
                    raise GwsError(
                        "restored snapshot identity precision is unsupported"
                    )
        if destination.exists() or destination.is_symlink():
            raise GwsError("restore destination already exists")
        stage.rename(destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"generation": value["generation"], "file_count": len(contents)}


def retention(generations: Path) -> dict[str, list[str]]:
    """Plan deletion only after verifying all generations in a complete listing.

    Each record has name/id (manifest identity), manifest (local exact-ID
    read-back), and readbacks (local JSON mapping payload IDs to read-backs).
    The caller must revalidate the listing before deleting exact IDs.
    """
    records: typing.Any = json.loads(generations.read_bytes())
    if not isinstance(records, list):
        raise GwsError("generations must be an array")
    records = typing.cast("list[dict[str, typing.Any]]", records)
    identities = [
        _snapshot_record({"name": row["name"], "id": row["id"]}) for row in records
    ]
    if len({row["id"] for row in identities}) != len(identities):
        raise GwsError("duplicate manifest IDs")
    next_generation(identities, datetime.now(UTC), _active(identities))
    payloads: dict[str, set[str]] = {}
    for row in records:
        manifest_path = Path(row["manifest"])
        if _manifest_format(manifest_path) == "wsum-json-1":
            import gws_json

            value = gws_json.verify(manifest_path, Path(row["readbacks"]))
            protected_ids = set(value["ids"])
        else:
            value, _ = _verified_payloads(manifest_path, Path(row["readbacks"]))
            protected_ids = {item["id"] for item in value["files"]}
        if row["name"] != f"workspace-{value['generation']}.json":
            raise GwsError("manifest filename and generation mismatch")
        payloads[row["id"]] = protected_ids
    ordered = sorted(identities, key=operator.itemgetter("name"), reverse=True)
    retained = {row["id"] for row in ordered[:3]}
    discarded = {row["id"] for row in ordered[3:]}
    protected = retained | set[str]().union(*(payloads[item] for item in retained))
    removed = discarded | set[str]().union(*(payloads[item] for item in discarded))
    if discarded & protected:
        raise GwsError("manifest and payload IDs overlap")
    return {"retain": sorted(retained), "delete": sorted(removed - protected)}


def _active(records: list[dict[str, str]]) -> dict[str, str] | None:
    return max(records, key=operator.itemgetter("name")) if records else None


def _read_ledger(path: Path, reports_dir: Path) -> dict[str, typing.Any]:
    value: typing.Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise GwsError("invalid delivery ledger")
    value = typing.cast("dict[str, typing.Any]", value)
    if (
        set(value) != {"version", "reports"}
        or value["version"] != 1
        or not isinstance(value["reports"], dict)
    ):
        raise GwsError("invalid delivery ledger")
    reports = typing.cast("dict[str, typing.Any]", value["reports"])
    if any(
        not RUN_ID_RE.fullmatch(run_id)
        or not isinstance(digest, str)
        or not SHA_RE.fullmatch(digest)
        for run_id, digest in reports.items()
    ):
        raise GwsError("invalid delivery ledger")
    for run_id, raw_digest in reports.items():
        expected_digest = typing.cast("str", raw_digest)
        report = reports_dir / f"{run_id}.md"
        if report.is_symlink() or not report.is_file():
            raise GwsError(f"delivery ledger references missing report: {run_id}")
        if _digest(report.read_bytes()) != expected_digest:
            raise GwsError(f"delivered canonical report changed: {run_id}")
    return value


def ledger(path: Path, report: Path, record: bool) -> dict[str, typing.Any]:
    run_id = report.stem
    if not RUN_ID_RE.fullmatch(run_id) or report.suffix != ".md":
        raise GwsError("invalid report filename")
    value = _read_ledger(path, report.parent)
    if report.is_symlink() or not report.is_file():
        raise GwsError("canonical report must be a regular file")
    digest = _digest(report.read_bytes())
    existing = value["reports"].get(run_id)
    if existing is not None and existing != digest:
        raise GwsError("delivered canonical report changed")
    if record and existing is None:
        value["reports"][run_id] = digest
        _write_atomic(
            path, json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        )
    return {
        "run_id": run_id,
        "sha256": digest,
        "delivered": record or existing == digest,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("transfer-encode")
    p.add_argument("--file", type=Path, required=True)
    p = commands.add_parser("transfer-text")
    p.add_argument("--file", type=Path, required=True)
    p = commands.add_parser("transfer-decode")
    p.add_argument("--base64-file", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--size", type=int, required=True)
    p.add_argument("--sha256")
    p = commands.add_parser("project")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--sheet-json", type=Path)
    source.add_argument("--csv", type=Path)
    source.add_argument("--drive-csv", type=Path)
    p.add_argument("--drive-size", type=int)
    p.add_argument("--targets", type=Path, required=True)
    p.add_argument("--core-skill-dir", type=Path, required=True)
    p = commands.add_parser("folder-binding")
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--parent-id", required=True)
    p.add_argument("--workspaces-id", required=True)
    p.add_argument("--reports-id", required=True)
    p.add_argument("--create", action="store_true")
    p = commands.add_parser("next-generation")
    p.add_argument("--snapshots-json", type=Path, required=True)
    p.add_argument("--expected-snapshot-json", type=Path, required=True)
    p = commands.add_parser("pack")
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--snapshot", type=Path, required=True)
    p.add_argument("--generation", required=True)
    p = commands.add_parser("json-pack")
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--snapshot", type=Path, required=True)
    p.add_argument("--generation", required=True)
    p = commands.add_parser("json-link")
    p.add_argument("--fragment", type=Path, required=True)
    p.add_argument("--next-json", type=Path, required=True)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("json-prepare")
    p.add_argument("--snapshot", type=Path, required=True)
    p.add_argument("--receipts", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--snapshot", type=Path, required=True)
    p.add_argument("--receipts", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    for command in ("verify", "restore"):
        p = commands.add_parser(command)
        p.add_argument("--manifest", type=Path, required=True)
        p.add_argument("--readbacks", type=Path, required=True)
        if command == "restore":
            p.add_argument("--workspace", type=Path, required=True)
    p = commands.add_parser("retention")
    p.add_argument("--generations", type=Path, required=True)
    p = commands.add_parser("ledger-validate")
    p.add_argument("--ledger", type=Path, required=True)
    p.add_argument("--reports-dir", type=Path, required=True)
    p = commands.add_parser("ledger")
    p.add_argument("--ledger", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--record", action="store_true")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        if args.command == "transfer-encode":
            result = transfer_encode(args.file)
        elif args.command == "transfer-text":
            result = transfer_text(args.file)
        elif args.command == "transfer-decode":
            result = transfer_decode(
                args.base64_file, args.output, args.size, args.sha256
            )
        elif args.command == "project":
            if args.drive_csv is None and args.drive_size is not None:
                raise GwsError("Drive CSV metadata requires --drive-csv")
            if args.drive_csv is not None and args.drive_size is None:
                raise GwsError("Drive CSV requires --drive-size")
            result = {
                "target_groups": (
                    project(args.sheet_json, args.targets, args.core_skill_dir)
                    if args.sheet_json is not None
                    else project_csv(
                        args.drive_csv if args.drive_csv is not None else args.csv,
                        args.targets,
                        args.core_skill_dir,
                        drive_size=args.drive_size,
                    )
                )
            }
        elif args.command == "folder-binding":
            result = binding_file(
                args.file,
                args.parent_id,
                args.workspaces_id,
                args.reports_id,
                create=args.create,
            )
        elif args.command == "next-generation":
            generation = next_generation(
                json.loads(args.snapshots_json.read_text()),
                datetime.now(UTC),
                json.loads(args.expected_snapshot_json.read_text()),
            )
            result = {
                "generation": generation,
                "filename": f"workspace-{generation}.json",
            }
        elif args.command == "pack":
            result = pack(args.workspace, args.snapshot, args.generation)
        elif args.command == "json-pack":
            import gws_json

            result = gws_json.pack(args.workspace, args.snapshot, args.generation)
        elif args.command == "json-link":
            import gws_json

            result = gws_json.link(args.fragment, args.next_json, args.index, args.output)
        elif args.command == "json-prepare":
            import gws_json

            result = gws_json.prepare(args.snapshot, args.receipts, args.manifest)
        elif args.command == "prepare":
            result = prepare(args.snapshot, args.receipts, args.manifest)
        elif args.command == "verify":
            result = verify(args.manifest, args.readbacks)
        elif args.command == "restore":
            result = restore(args.manifest, args.readbacks, args.workspace)
        elif args.command == "retention":
            result = retention(args.generations)
        elif args.command == "ledger-validate":
            value = _read_ledger(args.ledger, args.reports_dir)
            result = {"valid": True, "report_count": len(value["reports"])}
        else:
            result = ledger(args.ledger, args.report, args.record)
        print(json.dumps(result, sort_keys=True))
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        ImportError,
    ) as exc:
        parser.exit(1, f"gws: {exc}\n")


if __name__ == "__main__":
    main()
