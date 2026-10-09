"""Deterministic local Google Workspace projection and snapshot operations.

Google connector transport and permissions remain the host agent's responsibility.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import typing
import zipfile
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
SNAPSHOT_RE = re.compile(r"^workspace-(\d{8}T\d{6}Z)\.zip$")
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_ZIP = 128 * 1024 * 1024
MAX_FILES = 2000
MAX_ENTRY = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024


class GwsError(ValueError):
    """Reject invalid connector data or untrusted workspace archives."""


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _stream_digest(
    source: typing.IO[bytes], max_size: int, limit_message: str
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(CHUNK_SIZE):
        size += len(chunk)
        if size > max_size:
            raise GwsError(limit_message)
        digest.update(chunk)
    return size, digest.hexdigest()


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


def _timestamp(value: str) -> datetime:
    try:
        result = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise GwsError(f"invalid generation: {value}") from exc
    if result.strftime("%Y%m%dT%H%M%SZ") != value:
        raise GwsError(f"invalid generation: {value}")
    return result


def next_generation(names: list[str], now: datetime) -> str:
    """Derive monotonic filename time and reject duplicate Drive snapshot names."""
    known: set[str] = set()
    newest = None
    for name in names:
        match = SNAPSHOT_RE.fullmatch(name)
        if not match:
            continue
        if name in known:
            raise GwsError(f"duplicate snapshot filename: {name}")
        known.add(name)
        instant = _timestamp(match.group(1))
        if newest is None or instant > newest:
            newest = instant
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


def _snapshot_manifest(
    workspace: Path,
) -> tuple[list[dict[str, typing.Any]], list[tuple[str, Path, int, str]]]:
    manifest_files: list[dict[str, typing.Any]] = []
    contents: list[tuple[str, Path, int, str]] = []
    total = 0
    for name, path in _workspace_files(workspace):
        reported_size = path.stat().st_size
        if reported_size > MAX_ENTRY or total + reported_size > MAX_TOTAL:
            raise GwsError("workspace size limit exceeded")
        with path.open("rb") as source:
            size, digest = _stream_digest(
                source,
                min(MAX_ENTRY, MAX_TOTAL - total),
                "workspace size limit exceeded",
            )
        if size != reported_size:
            raise GwsError(f"workspace file changed during snapshot: {name}")
        total += size
        manifest_files.append({
            "path": name,
            "size": size,
            "sha256": digest,
        })
        contents.append((name, path, size, digest))
    return manifest_files, contents


def _write_snapshot_member(
    handle: zipfile.ZipFile,
    entry: tuple[str, Path, int, str],
    written_total: int,
) -> int:
    name, path, expected_size, expected_digest = entry
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source, handle.open(name, "w") as target:
        while chunk := source.read(CHUNK_SIZE):
            size += len(chunk)
            if size > MAX_ENTRY or written_total + size > MAX_TOTAL:
                raise GwsError("workspace size limit exceeded")
            digest.update(chunk)
            target.write(chunk)
    if size != expected_size or digest.hexdigest() != expected_digest:
        raise GwsError(f"workspace file changed during snapshot: {name}")
    return size


def _write_snapshot_members(
    handle: zipfile.ZipFile, contents: list[tuple[str, Path, int, str]]
) -> None:
    written_total = 0
    for entry in contents:
        written_total += _write_snapshot_member(handle, entry, written_total)


def pack(workspace: Path, archive: Path, generation: str) -> dict[str, typing.Any]:
    _timestamp(generation)
    if archive.name != f"workspace-{generation}.zip":
        raise GwsError("archive filename and generation mismatch")
    if archive.exists():
        raise GwsError("refusing to overwrite existing snapshot")
    manifest_files, contents = _snapshot_manifest(workspace)
    manifest = {"version": 1, "generation": generation, "files": manifest_files}
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=archive.parent, prefix=".tmp-", delete=False
    ) as stream:
        temp = Path(stream.name)
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            handle.writestr(
                "manifest.json",
                json.dumps(manifest, separators=(",", ":"), ensure_ascii=False),
            )
            _write_snapshot_members(handle, contents)
        if temp.stat().st_size > MAX_ZIP:
            raise GwsError("snapshot compressed size limit exceeded")
        verify(temp, expected_name=archive.name)
        if archive.exists():
            raise GwsError("refusing to overwrite existing snapshot")
        os.link(
            temp, archive
        )  # exclusive creation; never replace a committed generation
    finally:
        temp.unlink(missing_ok=True)
    return {
        "filename": archive.name,
        "size": archive.stat().st_size,
        "sha256": _digest_file(archive, MAX_ZIP),
    }


def _digest_file(path: Path, max_size: int) -> str:
    with path.open("rb") as source:
        _, digest = _stream_digest(
            source, max_size, "snapshot compressed size limit exceeded"
        )
    return digest


def _safe_path(name: str) -> bool:
    parts = name.split("/")
    return (
        len(parts) > 1
        and parts[0] in {"internal", "output"}
        and all(part not in {"", ".", ".."} for part in parts)
        and "\\" not in name
        and not name.startswith("/")
        and "\x00" not in name
    )


def verify(archive: Path, *, expected_name: str | None = None) -> dict[str, typing.Any]:
    """Validate the complete ZIP before installing any workspace content."""
    match = SNAPSHOT_RE.fullmatch(expected_name or archive.name)
    if not match:
        raise GwsError("invalid snapshot filename")
    _timestamp(match.group(1))
    if archive.stat().st_size > MAX_ZIP:
        raise GwsError("snapshot compressed size limit exceeded")
    with zipfile.ZipFile(archive) as handle:
        entries = handle.infolist()
        if not entries or len(entries) > MAX_FILES + 1:
            raise GwsError("invalid snapshot file count")
        names: set[str] = set()
        total = 0
        for entry in entries:
            name = entry.filename
            if name in names or (name != "manifest.json" and not _safe_path(name)):
                raise GwsError(f"unsafe or duplicate archive path: {name}")
            names.add(name)
            mode = (entry.external_attr >> 16) & 0xFFFF
            if (
                entry.flag_bits & 1
                or entry.is_dir()
                or (stat.S_IFMT(mode) not in {0, stat.S_IFREG})
            ):
                raise GwsError(f"non-regular or encrypted ZIP entry: {name}")
            if entry.file_size > MAX_ENTRY:
                raise GwsError("archive entry exceeds size limit")
            total += entry.file_size
            if total > MAX_TOTAL:
                raise GwsError("archive exceeds expanded size limit")
        if "manifest.json" not in names:
            raise GwsError("missing manifest")
        manifest: typing.Any = json.loads(handle.read("manifest.json"))
        if not isinstance(manifest, dict):
            raise GwsError("invalid manifest header")
        manifest = typing.cast("dict[str, typing.Any]", manifest)
        if (
            set(manifest) != {"version", "generation", "files"}
            or manifest["version"] != 1
            or manifest["generation"] != match.group(1)
            or not isinstance(manifest["files"], list)
        ):
            raise GwsError("invalid manifest header")
        recorded: set[str] = set()
        manifest_files = typing.cast("list[typing.Any]", manifest["files"])
        for raw_item in manifest_files:
            if not isinstance(raw_item, dict):
                raise GwsError("invalid manifest file record")
            item = typing.cast("dict[str, typing.Any]", raw_item)
            if (
                set(item) != {"path", "size", "sha256"}
                or not isinstance(item["path"], str)
                or not _safe_path(item["path"])
                or item["path"] in recorded
                or not isinstance(item["size"], int)
                or isinstance(item["size"], bool)
                or item["size"] < 0
                or not isinstance(item["sha256"], str)
                or not SHA_RE.fullmatch(item["sha256"])
            ):
                raise GwsError("invalid manifest file record")
            recorded.add(item["path"])
            info = handle.getinfo(item["path"]) if item["path"] in names else None
            if info is None or info.file_size != item["size"]:
                raise GwsError("manifest size or entry mismatch")
            with handle.open(info) as source:
                size, digest = _stream_digest(
                    source, MAX_ENTRY, "archive entry exceeds size limit"
                )
            if size != item["size"] or digest != item["sha256"]:
                raise GwsError(f"archive digest mismatch: {item['path']}")
        if recorded != names - {"manifest.json"}:
            raise GwsError("manifest and workspace entries do not match")
    return {
        "generation": match.group(1),
        "file_count": len(recorded),
        "size": archive.stat().st_size,
        "sha256": _digest_file(archive, MAX_ZIP),
    }


def restore(archive: Path, destination: Path) -> dict[str, typing.Any]:
    """Restore to a new workspace only, after complete verification."""
    result = verify(archive)
    if destination.exists() or destination.is_symlink():
        raise GwsError("restore destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".tmp-restore-", dir=destination.parent))
    try:
        for root in ("output", "internal"):
            (stage / root).mkdir()
        with zipfile.ZipFile(archive) as handle:
            for info in handle.infolist():
                if info.filename == "manifest.json":
                    continue
                path = stage.joinpath(*info.filename.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                with handle.open(info) as source, path.open("xb") as target:
                    shutil.copyfileobj(source, target)
        if destination.exists():
            raise GwsError("restore destination already exists")
        stage.rename(destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return result


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
    p = commands.add_parser("project")
    p.add_argument("--sheet-json", type=Path, required=True)
    p.add_argument("--targets", type=Path, required=True)
    p.add_argument("--core-skill-dir", type=Path, required=True)
    p = commands.add_parser("next-generation")
    p.add_argument("--names-json", type=Path, required=True)
    p = commands.add_parser("pack")
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--generation", required=True)
    p = commands.add_parser("verify")
    p.add_argument("--archive", type=Path, required=True)
    p = commands.add_parser("restore")
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--workspace", type=Path, required=True)
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
        if args.command == "project":
            result = {
                "target_groups": project(
                    args.sheet_json, args.targets, args.core_skill_dir
                )
            }
        elif args.command == "next-generation":
            raw: typing.Any = json.loads(args.names_json.read_text())
            if not isinstance(raw, list):
                raise GwsError("names JSON must be an array of strings")
            raw_names = typing.cast("list[typing.Any]", raw)
            if any(not isinstance(name, str) for name in raw_names):
                raise GwsError("names JSON must be an array of strings")
            names = typing.cast("list[str]", raw_names)
            generation = next_generation(names, datetime.now(UTC))
            result = {
                "generation": generation,
                "filename": f"workspace-{generation}.zip",
            }
        elif args.command == "pack":
            result = pack(args.workspace, args.archive, args.generation)
        elif args.command == "verify":
            result = verify(args.archive)
        elif args.command == "restore":
            result = restore(args.archive, args.workspace)
        elif args.command == "ledger-validate":
            value = _read_ledger(args.ledger, args.reports_dir)
            result = {"valid": True, "report_count": len(value["reports"])}
        else:
            result = ledger(args.ledger, args.report, args.record)
        print(json.dumps(result, sort_keys=True))
    except (
        OSError,
        ValueError,
        zipfile.BadZipFile,
        KeyError,
        TypeError,
        ImportError,
    ) as exc:
        parser.exit(1, f"gws: {exc}\n")


if __name__ == "__main__":
    main()
