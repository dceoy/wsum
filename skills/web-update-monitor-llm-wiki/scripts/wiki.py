"""Deterministic compiler helper for the web-update-monitor LLM wiki composite.

The agent drafts pages; this helper only lists digest-verified evidence, serves
bounded reads, validates drafts, and applies them through a write-ahead
transaction with a processed ledger. It never calls an LLM.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager, suppress
from operator import itemgetter
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence
    from types import ModuleType

# flock exists only on POSIX; locking fails explicitly elsewhere.
fcntl: ModuleType | None = (
    importlib.import_module("fcntl") if os.name == "posix" else None
)

_KNOWLEDGE = "knowledge"
_PAGES = "pages"
_COMPILER = ".compiler"
_SCHEMA_FILE = "SCHEMA.md"
_INDEX_FILE = "index.md"
_LEDGER_FILE = "ledger.json"
_TRANSACTION_FILE = "transaction.json"
_LOCK_FILE = "lock"
_EVIDENCE = "evidence"
_EVIDENCE_SCHEMA = "wsum.evidence/1"
_PAYLOADS = ("parent.txt", "diff.txt", "links.json")
_PAYLOAD_LIMITS = {
    "parent.txt": 40 * 1024 * 1024,
    "diff.txt": 1024 * 1024,
    "links.json": 1024 * 1024,
}
_MAX_METADATA_BYTES = 2 * 1024 * 1024
_MAX_RECEIPT_BYTES = 64 * 1024

MAX_LIST_PAGE = 100
MAX_READ_BYTES = 32 * 1024
MAX_READ_LINES = 400
MAX_DRAFT_BYTES = 1024 * 1024
MAX_DRAFT_PAGES = 8
MAX_PAGE_BYTES = 256 * 1024
MAX_TRANSACTION_BYTES = 2 * 1024 * 1024
MAX_LEDGER_BYTES = 8 * 1024 * 1024
MAX_CITATIONS = 200
MAX_REASON_CHARS = 1000

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_CITE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_CITE_MARKER_RE = re.compile(r"\[\[cite:([A-Za-z0-9_-]{1,32})\]\]")
_LINK_RE = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_PAGE_LINK_RE = re.compile(r"^(?:pages/)?([a-z0-9][a-z0-9-]{0,63})\.md(?:#[^\s]*)?$")
_PAGE_NAME_RE = re.compile(r"^([a-z0-9][a-z0-9-]{0,63})\.md$")
_PAGE_FILE_RE = re.compile(r"^pages/([a-z0-9][a-z0-9-]{0,63})\.md$")
_EVIDENCE_LINK_RE = re.compile(
    r"^\.\./\.\./evidence/([0-9a-f]{64})/(?:parent\.txt|diff\.txt|links\.json)$"
)
_ENTRY_RE = re.compile(r"^link-([1-9][0-9]*)$")

_DEFAULT_SCHEMA = """# Wiki schema

Editorial rules for pages compiled from captured evidence. Edit freely; the
compiler re-reads this file before every draft.

- Pages live in `pages/<page-id>.md`; page IDs are stable lowercase slugs and
  titles are readable.
- Cite every substantive new or changed claim with a citation marker; the
  helper renders it as a relative link to digest-verified evidence.
- Distinguish what a source asserts from your synthesis, and qualify claims
  drawn from truncated or incomplete excerpts.
- Keep a `## History` section per page with dated entries. Dates come from
  cited evidence, never from when a bundle was archived.
- Preserve conflicting findings and dated superseded facts instead of silently
  overwriting them; mark uncertainty explicitly.
- Do not delete or rename pages.
"""
_DEFAULT_INDEX = "# Knowledge index\n\nNo pages yet.\n"


class WikiError(RuntimeError):
    """Expected compiler, evidence, draft, or recovery error."""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise WikiError(message)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _json_object(data: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WikiError(f"{description} is invalid") from exc
    _require(isinstance(value, dict), f"{description} is invalid")
    return cast("dict[str, Any]", value)


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WikiError(f"cannot stat {path.name}") from exc


def _read_file(path: Path, limit: int, description: str) -> bytes:
    info = _lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise WikiError(f"{description} is missing or invalid")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise WikiError(f"cannot read {description}") from exc
    _require(len(data) <= limit, f"{description} is missing or invalid")
    return data


def _optional_file(path: Path, limit: int, description: str) -> bytes | None:
    if _lstat(path) is None:
        return None
    return _read_file(path, limit, description)


def _hash_file(path: Path, limit: int, description: str) -> tuple[int, str]:
    info = _lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise WikiError(f"{description} is missing or invalid")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise WikiError(f"cannot read {description}") from exc
    _require(size <= limit, f"{description} is missing or invalid")
    return size, digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise WikiError(f"cannot fsync {path.name}") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise WikiError(f"cannot fsync {path.name}") from exc
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path, description: str) -> Path:
    info = _lstat(path)
    if info is None:
        try:
            path.mkdir(mode=0o755)
        except OSError as exc:
            raise WikiError(f"{description} is unavailable") from exc
        _fsync_directory(path.parent)
        return path
    _require(
        stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode),
        f"{description} must be a non-symlink directory",
    )
    return path


def _atomic_write(path: Path, data: bytes, description: str) -> None:
    """Replace one file atomically and verify it by reading it back."""
    info = _lstat(path)
    _require(
        info is None or stat.S_ISREG(info.st_mode),
        f"{description} must be a regular non-symlink file",
    )
    try:
        descriptor, name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
    except OSError as exc:
        raise WikiError(f"cannot create temporary {description}") from exc
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except OSError as exc:
        raise WikiError(f"cannot write {description}") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
    _fsync_directory(path.parent)
    _require(path.read_bytes() == data, f"{description} read-back mismatch")


def _lines(text: str) -> list[str]:
    """Split text into 1-based citation lines, ignoring one final newline."""
    parts = text.split("\n")
    if not parts[-1]:
        parts.pop()
    return parts


class _Workspace:
    def __init__(self, workspace: str | Path) -> None:
        root = Path(workspace)
        info = _lstat(root)
        _require(
            info is not None
            and stat.S_ISDIR(info.st_mode)
            and not stat.S_ISLNK(info.st_mode),
            "workspace must be an existing non-symlink directory",
        )
        self.root = root.resolve()
        self.knowledge = self.root / _KNOWLEDGE
        self.pages = self.knowledge / _PAGES
        self.compiler = self.knowledge / _COMPILER
        self.schema = self.knowledge / _SCHEMA_FILE
        self.index = self.knowledge / _INDEX_FILE
        self.ledger = self.compiler / _LEDGER_FILE
        self.transaction = self.compiler / _TRANSACTION_FILE
        self.evidence = self.root / _EVIDENCE

    def require_initialized(self) -> None:
        for path in (self.knowledge, self.pages, self.compiler):
            info = _lstat(path)
            _require(
                info is not None
                and stat.S_ISDIR(info.st_mode)
                and not stat.S_ISLNK(info.st_mode),
                "knowledge workspace is not initialized; run init",
            )


# --------------------------------------------------------------------------
# Evidence bundles
# --------------------------------------------------------------------------


def _load_bundle(ws: _Workspace, ingestion_id: str) -> dict[str, Any]:
    """Return digest-verified metadata for one committed evidence bundle."""
    _require(_HASH_RE.fullmatch(ingestion_id), "ingestion_id is invalid")
    info = _lstat(ws.evidence)
    _require(
        info is not None
        and stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode),
        "evidence directory is unavailable",
    )
    bundle = ws.evidence / ingestion_id
    info = _lstat(bundle)
    _require(
        info is not None
        and stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode),
        "evidence bundle is unavailable",
    )
    receipt = _json_object(
        _read_file(bundle / "committed.json", _MAX_RECEIPT_BYTES, "evidence receipt"),
        "evidence receipt",
    )
    metadata_bytes = _read_file(
        bundle / "metadata.json", _MAX_METADATA_BYTES, "evidence metadata"
    )
    metadata = _json_object(metadata_bytes, "evidence metadata")
    payloads = metadata.get("payloads")
    decision = metadata.get("decision")
    _require(
        metadata.get("schema") == _EVIDENCE_SCHEMA
        and metadata.get("ingestion_id") == ingestion_id
        and isinstance(metadata.get("target_id"), str)
        and isinstance(metadata.get("revision"), str)
        and isinstance(metadata.get("archived_at"), str)
        and isinstance(metadata.get("url"), str)
        and isinstance(payloads, dict)
        and set(cast("dict[str, Any]", payloads)) == set(_PAYLOADS)
        and isinstance(decision, dict),
        "evidence metadata is invalid",
    )
    canonical = _canonical([
        "wsum-ingestion",
        1,
        metadata["target_id"],
        metadata["revision"],
    ])
    _require(
        _sha256(canonical) == ingestion_id
        and receipt.get("schema") == _EVIDENCE_SCHEMA
        and receipt.get("ingestion_id") == ingestion_id
        and receipt.get("material") is True
        and receipt.get("manifest_sha256") == _sha256(metadata_bytes)
        and receipt.get("report_section_sha256")
        == cast("dict[str, Any]", decision).get("report_sha256"),
        "evidence receipt is invalid",
    )
    for name, entry in cast("dict[str, Any]", payloads).items():
        item = cast("dict[str, Any]", entry if isinstance(entry, dict) else {})
        size, digest = _hash_file(
            bundle / name, _PAYLOAD_LIMITS[name], f"evidence {name}"
        )
        _require(
            type(item.get("bytes")) is int
            and size == item["bytes"]
            and digest == item.get("sha256"),
            f"evidence {name} does not match its digest",
        )
    metadata["_manifest_sha256"] = receipt["manifest_sha256"]
    return metadata


def _bundle_text(ws: _Workspace, ingestion_id: str, name: str) -> str:
    return (ws.evidence / ingestion_id / name).read_text(encoding="utf-8")


def _link_entries(ws: _Workspace, ingestion_id: str) -> list[dict[str, Any]]:
    links = _json_object(
        _bundle_text(ws, ingestion_id, "links.json").encode("utf-8"), "links.json"
    )
    documents = links.get("documents")
    _require(isinstance(documents, list), "links.json is invalid")
    return [
        cast("dict[str, Any]", item)
        for item in cast("list[Any]", documents)
        if isinstance(item, dict)
    ]


def _locator_text(
    ws: _Workspace, ingestion_id: str, file: str, entry: str | None
) -> str:
    """Return the exact decoded text a citation locator refers to."""
    _require(file in _PAYLOADS, "citation file is invalid")
    if file != "links.json":
        _require(entry is None, "citation entry is only valid for links.json")
        return _bundle_text(ws, ingestion_id, file)
    match = _ENTRY_RE.fullmatch(entry or "")
    _require(match is not None, "links.json citations require a link-N entry")
    documents = _link_entries(ws, ingestion_id)
    index = int(cast("re.Match[str]", match).group(1)) - 1
    _require(index < len(documents), "citation entry does not exist")
    text = documents[index].get("text")
    _require(isinstance(text, str), "citation entry has no excerpt text")
    return cast("str", text)


# --------------------------------------------------------------------------
# Ledger, transaction, lock
# --------------------------------------------------------------------------


@contextmanager
def _locked(ws: _Workspace) -> Generator[None]:
    """Hold the process-owned exclusive compiler lock, released on exit."""
    ws.require_initialized()
    descriptor = os.open(ws.compiler / _LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _flock(descriptor)
        yield
    finally:
        os.close(descriptor)


def _flock(descriptor: int) -> None:
    if fcntl is None:
        raise WikiError("exclusive locking is unsupported on this platform")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise WikiError("another compiler invocation holds the lock") from exc


def _is_dict(value: object) -> bool:
    return isinstance(value, dict)


def _has_reason(value: object) -> bool:
    return isinstance(value, dict) and isinstance(
        cast("dict[str, object]", value).get("reason"), str
    )


def _all_values(value: object, predicate: Callable[[object], bool]) -> bool:
    return isinstance(value, dict) and all(
        predicate(item) for item in cast("dict[str, object]", value).values()
    )


def _read_ledger(ws: _Workspace) -> dict[str, Any]:
    data = _optional_file(ws.ledger, MAX_LEDGER_BYTES, "compiler ledger")
    if data is None:
        return {"version": 1, "processed": {}, "blocked": {}}
    ledger = _json_object(data, "compiler ledger")
    _require(
        set(ledger) == {"version", "processed", "blocked"}
        and ledger["version"] == 1
        and _all_values(ledger["processed"], _is_dict)
        and _all_values(ledger["blocked"], _has_reason),
        "compiler ledger is invalid",
    )
    return ledger


def _write_ledger(ws: _Workspace, ledger: Mapping[str, Any]) -> None:
    data = (json.dumps(ledger, sort_keys=True, indent=1) + "\n").encode("utf-8")
    _require(len(data) <= MAX_LEDGER_BYTES, "compiler ledger exceeds its size limit")
    _atomic_write(ws.ledger, data, "compiler ledger")


def _current_sha(path: Path, description: str) -> str | None:
    data = _optional_file(path, MAX_PAGE_BYTES, description)
    return None if data is None else _sha256(data)


def _file_path(ws: _Workspace, relative: str) -> Path:
    if relative == _INDEX_FILE:
        return ws.index
    match = _PAGE_FILE_RE.fullmatch(relative)
    _require(match is not None, "transaction contains an unsafe path")
    return ws.pages / f"{cast('re.Match[str]', match).group(1)}.md"


def _transaction_digest(txn: Mapping[str, Any]) -> str:
    return _sha256(
        _canonical({
            "files": [
                {key: item[key] for key in ("path", "old_sha256", "new_sha256")}
                for item in txn["files"]
            ],
            "ingestion_id": txn["ingestion_id"],
            "manifest_sha256": txn["manifest_sha256"],
            "noop": txn["noop"],
            "schema_sha256": txn["schema_sha256"],
        })
    )


def _read_transaction(ws: _Workspace) -> dict[str, Any] | None:
    data = _optional_file(ws.transaction, 2 * MAX_TRANSACTION_BYTES, "transaction")
    if data is None:
        return None
    txn = _json_object(data, "transaction")
    files = txn.get("files")
    valid = (
        txn.get("version") == 1
        and isinstance(files, list)
        and isinstance(txn.get("ingestion_id"), str)
        and _HASH_RE.fullmatch(str(txn.get("manifest_sha256"))) is not None
        and _HASH_RE.fullmatch(str(txn.get("schema_sha256"))) is not None
        and (txn.get("noop") is None or isinstance(txn.get("noop"), str))
    )
    _require(valid, "transaction is invalid")
    for raw in cast("list[Any]", files):
        item = cast("dict[str, Any]", raw if isinstance(raw, dict) else {})
        content = item.get("content")
        _require(
            isinstance(content, str)
            and isinstance(item.get("path"), str)
            and _HASH_RE.fullmatch(str(item.get("new_sha256"))) is not None
            and (
                item.get("old_sha256") is None
                or _HASH_RE.fullmatch(str(item["old_sha256"]))
            )
            and _sha256(content.encode("utf-8")) == item["new_sha256"],
            "transaction is invalid",
        )
        _file_path(ws, item["path"])
    _require(
        txn.get("transaction_sha256") == _transaction_digest(txn),
        "transaction digest mismatch",
    )
    return txn


def _ledger_entry(txn: Mapping[str, Any]) -> dict[str, Any]:
    paths = [str(item["path"]) for item in txn["files"]]
    return {
        "ingestion_id": txn["ingestion_id"],
        "manifest_sha256": txn["manifest_sha256"],
        "outcome": "noop" if txn["noop"] is not None else "compiled",
        "pages": sorted(path for path in paths if path != _INDEX_FILE),
        "reason": txn["noop"],
        "schema_sha256": txn["schema_sha256"],
        "transaction_sha256": txn["transaction_sha256"],
    }


def _replay(ws: _Workspace, txn: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a frozen transaction idempotently, then record it in the ledger."""
    schema_sha = _current_sha(ws.schema, "schema")
    _require(
        schema_sha == txn["schema_sha256"],
        "conflict: SCHEMA.md changed since the transaction was frozen",
    )
    pending: list[tuple[Path, str]] = []
    conflicts: list[str] = []
    for item in txn["files"]:
        path = _file_path(ws, item["path"])
        current = _current_sha(path, item["path"])
        if current == item["new_sha256"]:
            continue
        if current == item["old_sha256"]:
            pending.append((path, item["content"]))
        else:
            conflicts.append(str(item["path"]))
    _require(
        not conflicts,
        "conflict: destinations match neither the old nor the planned content: "
        + ", ".join(conflicts),
    )
    for path, content in pending:
        _atomic_write(path, content.encode("utf-8"), path.name)
    ledger = _read_ledger(ws)
    entry = _ledger_entry(txn)
    ledger["processed"][txn["ingestion_id"]] = entry
    ledger["blocked"].pop(txn["ingestion_id"], None)
    _write_ledger(ws, ledger)
    with suppress(OSError):
        ws.transaction.unlink()
    _fsync_directory(ws.compiler)
    return entry


def _recover(ws: _Workspace) -> dict[str, Any] | None:
    """Finish or retire an interrupted transaction before any other work."""
    txn = _read_transaction(ws)
    if txn is None:
        return None
    ledger = _read_ledger(ws)
    recorded = ledger["processed"].get(txn["ingestion_id"])
    if recorded is None:
        return _replay(ws, txn)
    _require(
        recorded.get("transaction_sha256") == txn["transaction_sha256"]
        and recorded.get("manifest_sha256") == txn["manifest_sha256"]
        and recorded.get("schema_sha256") == txn["schema_sha256"],
        "ledger and transaction mismatch; resolve manually",
    )
    # Completed apply whose journal cleanup failed: retire without replaying so
    # later user edits are preserved.
    with suppress(OSError):
        ws.transaction.unlink()
    _fsync_directory(ws.compiler)
    return cast("dict[str, Any]", recorded)


# --------------------------------------------------------------------------
# Draft validation
# --------------------------------------------------------------------------


def _page_title(data: bytes) -> str:
    first = data.decode("utf-8", errors="replace").split("\n", 1)[0]
    return first[2:].strip() if first.startswith("# ") else ""


def _existing_pages(ws: _Workspace) -> list[str]:
    try:
        names = sorted(item.name for item in ws.pages.iterdir())
    except OSError as exc:
        raise WikiError("cannot list pages") from exc
    page_ids: list[str] = []
    for name in names:
        match = _PAGE_NAME_RE.fullmatch(name)
        info = _lstat(ws.pages / name)
        if match and info is not None and stat.S_ISREG(info.st_mode):
            page_ids.append(match.group(1))
    return page_ids


def _check_links(ws: _Workspace, text: str, pages: set[str], description: str) -> None:
    _require(
        len(_CITE_MARKER_RE.findall(text)) == text.count("[[cite:"),
        f"{description} contains a malformed citation marker",
    )
    for target in _LINK_RE.findall(text):
        evidence = _EVIDENCE_LINK_RE.fullmatch(target)
        if evidence is not None:
            # Previously rendered citations survive redrafting only while the
            # evidence they point at still verifies.
            _load_bundle(ws, evidence.group(1))
            continue
        match = _PAGE_LINK_RE.fullmatch(target)
        _require(
            target.startswith(("http://", "https://", "#"))
            or (match is not None and match.group(1) in pages),
            f"{description} contains a broken or unsupported link: {target}",
        )


def _validate_citations(
    ws: _Workspace, draft: Mapping[str, Any], bodies: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    citations = cast("dict[str, Any]", draft["citations"])
    _require(
        isinstance(draft["citations"], dict) and len(citations) <= MAX_CITATIONS,
        "citations must be an object within the citation limit",
    )
    rendered: dict[str, dict[str, Any]] = {}
    used: set[str] = set()
    for page_id, body in bodies.items():
        keys = _CITE_MARKER_RE.findall(body)
        _require(keys, f"page {page_id} has no citation")
        used.update(keys)
    _require(
        used == set(citations),
        "citations must match the markers used in page bodies",
    )
    cache: dict[tuple[str, str, str | None], list[str]] = {}
    bundles: dict[str, dict[str, Any]] = {}
    for key, raw in citations.items():
        locator = cast("dict[str, Any]", raw if isinstance(raw, dict) else {})
        ingestion_id = locator.get("ingestion_id")
        file = locator.get("file")
        entry = locator.get("entry")
        start = locator.get("start_line")
        end = locator.get("end_line")
        _require(
            _CITE_KEY_RE.fullmatch(key) is not None
            and set(locator)
            <= {"ingestion_id", "file", "entry", "start_line", "end_line"}
            and isinstance(ingestion_id, str)
            and isinstance(file, str)
            and (entry is None or isinstance(entry, str))
            and type(start) is int
            and type(end) is int,
            f"citation {key} is malformed",
        )
        if ingestion_id not in bundles:
            bundles[cast("str", ingestion_id)] = _load_bundle(
                ws, cast("str", ingestion_id)
            )
        metadata = bundles[cast("str", ingestion_id)]
        cache_key = (
            cast("str", ingestion_id),
            cast("str", file),
            cast("str | None", entry),
        )
        if cache_key not in cache:
            cache[cache_key] = _lines(
                _locator_text(ws, cache_key[0], cache_key[1], cache_key[2])
            )
        total = len(cache[cache_key])
        _require(
            1 <= cast("int", start) <= cast("int", end) <= total,
            f"citation {key} line range is outside the evidence text",
        )
        location = cast("str", file) + (f"#{entry}" if entry else "")
        title = str(metadata["url"]).replace('"', "%22")
        rendered[key] = {
            "markdown": (
                f"[evidence {str(ingestion_id)[:12]} {location} L{start}-{end}]"
                f'(../../{_EVIDENCE}/{ingestion_id}/{file} "{title}")'
            )
        }
    return rendered


def _plan(
    ws: _Workspace, ingestion_id: str, draft: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate a complete draft against current files and build a transaction."""
    _require(
        set(draft)
        == {"citations", "index", "ingestion_id", "noop", "pages", "schema_sha256"}
        and draft["ingestion_id"] == ingestion_id,
        "draft must have exactly the supported fields for this ingestion",
    )
    metadata = _load_bundle(ws, ingestion_id)
    schema = _optional_file(ws.schema, MAX_PAGE_BYTES, "schema")
    _require(
        schema is not None and draft["schema_sha256"] == _sha256(schema),
        "schema changed or does not match the draft; reread and redraft",
    )
    files: list[dict[str, Any]] = []
    noop = draft["noop"]
    if noop is not None:
        _require(
            isinstance(noop, str)
            and 0 < len(noop.strip()) <= MAX_REASON_CHARS
            and draft["pages"] == []
            and draft["index"] is None
            and draft["citations"] == {},
            "a no-op needs a reason and no pages, index, or citations",
        )
    else:
        files = _plan_files(ws, draft)
    txn: dict[str, Any] = {
        "files": files,
        "ingestion_id": ingestion_id,
        "manifest_sha256": metadata["_manifest_sha256"],
        "noop": None if noop is None else noop.strip(),
        "schema_sha256": _sha256(cast("bytes", schema)),
        "version": 1,
    }
    txn["transaction_sha256"] = _transaction_digest(txn)
    _require(
        len(json.dumps(txn).encode("utf-8")) <= MAX_TRANSACTION_BYTES,
        "transaction exceeds its size limit",
    )
    return txn


def _plan_files(ws: _Workspace, draft: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_pages = cast("list[Any]", draft["pages"])
    index = cast("dict[str, Any]", draft["index"])
    _require(
        isinstance(draft["pages"], list)
        and 0 < len(raw_pages) <= MAX_DRAFT_PAGES
        and isinstance(draft["index"], dict),
        f"a draft needs 1 to {MAX_DRAFT_PAGES} pages and an index",
    )
    existing = set(_existing_pages(ws))
    new_ids: set[str] = set()
    bodies: dict[str, str] = {}
    prepared: list[tuple[str, str, str, Any]] = []
    for raw in raw_pages:
        page = cast("dict[str, Any]", raw if isinstance(raw, dict) else {})
        page_id = page.get("id")
        title = page.get("title")
        body = page.get("body")
        _require(
            set(page) == {"body", "expected_sha256", "id", "title"}
            and isinstance(page_id, str)
            and _PAGE_ID_RE.fullmatch(page_id) is not None
            and page_id not in new_ids
            and isinstance(title, str)
            and 0 < len(title) <= 200
            and "\n" not in title
            and isinstance(body, str)
            and 0 < len(body.encode("utf-8")) <= MAX_PAGE_BYTES,
            "draft page is malformed, duplicated, or too large",
        )
        new_ids.add(cast("str", page_id))
        bodies[cast("str", page_id)] = cast("str", body)
        prepared.append((
            cast("str", page_id),
            cast("str", title),
            cast("str", body),
            page["expected_sha256"],
        ))
    known = existing | new_ids
    index_content = index.get("content")
    _require(
        set(index) == {"content", "expected_sha256"}
        and isinstance(index_content, str)
        and 0 < len(index_content.encode("utf-8")) <= MAX_PAGE_BYTES
        and "[[cite:" not in index_content,
        "draft index is malformed or too large",
    )
    for page_id, _title, body, _expected in prepared:
        _check_links(ws, body, known, f"page {page_id}")
    _check_links(ws, cast("str", index_content), known, "index")
    citations = _validate_citations(ws, draft, bodies)
    files: list[dict[str, Any]] = []
    for page_id, title, body, expected in prepared:
        path = ws.pages / f"{page_id}.md"
        current = _current_sha(path, f"page {page_id}")
        _require(
            expected == current,
            f"page {page_id} changed or exists; reread it and redraft",
        )
        rendered = _CITE_MARKER_RE.sub(
            lambda match: citations[match.group(1)]["markdown"], body
        )
        content = f"# {title}\n\n{rendered.strip()}\n"
        files.append(_file_entry(f"{_PAGES}/{page_id}.md", current, content))
    index_current = _current_sha(ws.index, "index")
    _require(
        index.get("expected_sha256") == index_current,
        "index changed; reread it and redraft",
    )
    files.append(_file_entry(_INDEX_FILE, index_current, cast("str", index_content)))
    _require(
        sum(len(item["content"].encode("utf-8")) for item in files)
        <= MAX_TRANSACTION_BYTES // 2,
        "transaction exceeds its size limit",
    )
    return files


def _file_entry(path: str, old_sha256: str | None, content: str) -> dict[str, Any]:
    return {
        "content": content,
        "new_sha256": _sha256(content.encode("utf-8")),
        "old_sha256": old_sha256,
        "path": path,
    }


def _read_draft() -> dict[str, Any]:
    data = sys.stdin.buffer.read(MAX_DRAFT_BYTES + 1)
    _require(len(data) <= MAX_DRAFT_BYTES, "draft exceeds its size limit")
    return _json_object(data, "draft")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def init(workspace: str | Path) -> dict[str, Any]:
    """Create the knowledge workspace without overwriting user content."""
    ws = _Workspace(workspace)
    _ensure_directory(ws.knowledge, "knowledge directory")
    _ensure_directory(ws.pages, "pages directory")
    _ensure_directory(ws.compiler, "compiler directory")
    created: list[str] = []
    for path, content in ((ws.schema, _DEFAULT_SCHEMA), (ws.index, _DEFAULT_INDEX)):
        if _lstat(path) is None:
            _atomic_write(path, content.encode("utf-8"), path.name)
            created.append(path.name)
    return {"action": "initialized", "created": created}


def _summary(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "archived_at": metadata["archived_at"],
        "diff_truncated": metadata.get("diff_truncated"),
        "ingestion_id": metadata["ingestion_id"],
        "payload_bytes": {
            name: entry["bytes"] for name, entry in metadata["payloads"].items()
        },
        "target_id": metadata["target_id"],
        "url": metadata.get("url"),
    }


def list_ingestions(
    workspace: str | Path, *, limit: int = MAX_LIST_PAGE, offset: int = 0
) -> dict[str, Any]:
    """List committed, uncompiled bundles in stable archival order."""
    _require(1 <= limit <= MAX_LIST_PAGE and offset >= 0, "invalid list page")
    ws = _Workspace(workspace)
    with _locked(ws):
        _recover(ws)
        ledger = _read_ledger(ws)
    names: list[str] = []
    if _lstat(ws.evidence) is not None:
        try:
            names = sorted(item.name for item in ws.evidence.iterdir())
        except OSError as exc:
            raise WikiError("cannot list evidence") from exc
    eligible: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    for name in names:
        if name in ledger["processed"] or not _HASH_RE.fullmatch(name):
            continue
        try:
            summary = _summary(_load_bundle(ws, name))
        except WikiError as exc:
            invalid.append({"error": str(exc), "ingestion_id": name})
            continue
        if name in ledger["blocked"]:
            blocked.append({**summary, "reason": ledger["blocked"][name]["reason"]})
        else:
            eligible.append(summary)
    eligible.sort(key=itemgetter("archived_at", "ingestion_id"))
    page = eligible[offset : offset + limit]
    return {
        "blocked": blocked,
        "eligible": page,
        "invalid": invalid,
        "next_offset": offset + limit if offset + limit < len(eligible) else None,
        "total_eligible": len(eligible),
    }


def show(workspace: str | Path, ingestion_id: str) -> dict[str, Any]:
    """Return verified metadata and link-entry handles for one bundle."""
    ws = _Workspace(workspace)
    metadata = _load_bundle(ws, ingestion_id)
    entries = [
        {
            "entry_id": item.get("entry_id"),
            "error": item.get("error"),
            "line_count": len(_lines(item["text"]))
            if isinstance(item.get("text"), str)
            else 0,
            "truncated": item.get("truncated"),
            "url": item.get("url"),
        }
        for item in _link_entries(ws, ingestion_id)
    ]
    public = {key: value for key, value in metadata.items() if not key.startswith("_")}
    return {
        "line_counts": {
            name: len(_lines(_bundle_text(ws, ingestion_id, name)))
            for name in ("parent.txt", "diff.txt")
        },
        "link_entries": entries,
        "metadata": public,
    }


def read_lines(
    workspace: str | Path,
    ingestion_id: str,
    file: str,
    *,
    entry: str | None = None,
    start: int = 1,
    end: int = MAX_READ_LINES,
) -> dict[str, Any]:
    """Return a bounded 1-based line range from verified evidence text."""
    ws = _Workspace(workspace)
    _load_bundle(ws, ingestion_id)
    _require(
        1 <= start <= end and end - start + 1 <= MAX_READ_LINES,
        f"line range must be ascending and at most {MAX_READ_LINES} lines",
    )
    lines = _lines(_locator_text(ws, ingestion_id, file, entry))
    selected: list[str] = []
    used = 0
    for line in lines[start - 1 : end]:
        cost = len(line.encode("utf-8")) + 1
        if used + cost > MAX_READ_BYTES:
            _require(
                selected,
                f"line {start} exceeds the per-read byte limit; block this ingestion",
            )
            break
        selected.append(line)
        used += cost
    last = start + len(selected) - 1
    truncated = last < min(end, len(lines))
    return {
        "end_line": last,
        "entry": entry,
        "file": file,
        "ingestion_id": ingestion_id,
        "next_line": last + 1 if truncated else None,
        "start_line": start,
        "text": "\n".join(selected),
        "total_lines": len(lines),
        "truncated": truncated,
    }


def pages(
    workspace: str | Path, *, limit: int = MAX_LIST_PAGE, offset: int = 0
) -> dict[str, Any]:
    """List existing pages with titles and hashes for routing."""
    _require(1 <= limit <= MAX_LIST_PAGE and offset >= 0, "invalid list page")
    ws = _Workspace(workspace)
    with _locked(ws):
        _recover(ws)
        page_ids = _existing_pages(ws)
        entries: list[dict[str, Any]] = []
        for page_id in page_ids[offset : offset + limit]:
            data = _read_file(
                ws.pages / f"{page_id}.md", MAX_PAGE_BYTES, f"page {page_id}"
            )
            entries.append({
                "bytes": len(data),
                "id": page_id,
                "sha256": _sha256(data),
                "title": _page_title(data),
            })
        schema = _read_file(ws.schema, MAX_PAGE_BYTES, "schema")
        index = _read_file(ws.index, MAX_PAGE_BYTES, "index")
    return {
        "index_sha256": _sha256(index),
        "next_offset": offset + limit if offset + limit < len(page_ids) else None,
        "pages": entries,
        "schema_sha256": _sha256(schema),
        "total": len(page_ids),
    }


def base(workspace: str | Path, page_ids: Sequence[str]) -> dict[str, Any]:
    """Read schema, index, and selected pages as one consistent draft base."""
    _require(len(page_ids) <= MAX_DRAFT_PAGES, "too many pages requested")
    ws = _Workspace(workspace)
    with _locked(ws):
        _recover(ws)
        schema = _read_file(ws.schema, MAX_PAGE_BYTES, "schema")
        index = _read_file(ws.index, MAX_PAGE_BYTES, "index")
        result: dict[str, Any] = {
            "index": {"content": index.decode("utf-8"), "sha256": _sha256(index)},
            "pages": {},
            "schema": {"content": schema.decode("utf-8"), "sha256": _sha256(schema)},
        }
        for page_id in page_ids:
            _require(_PAGE_ID_RE.fullmatch(page_id), "page id is invalid")
            data = _optional_file(
                ws.pages / f"{page_id}.md", MAX_PAGE_BYTES, f"page {page_id}"
            )
            result["pages"][page_id] = (
                None
                if data is None
                else {"content": data.decode("utf-8"), "sha256": _sha256(data)}
            )
    return result


def validate(
    workspace: str | Path, ingestion_id: str, draft: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate a draft without mutating managed files."""
    ws = _Workspace(workspace)
    with _locked(ws):
        _recover(ws)
        _require(
            ingestion_id not in _read_ledger(ws)["processed"],
            "ingestion is already compiled",
        )
        txn = _plan(ws, ingestion_id, draft)
    return _plan_summary(txn)


def _plan_summary(txn: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "action": "valid",
        "files": [
            {
                "new_sha256": item["new_sha256"],
                "old_sha256": item["old_sha256"],
                "path": item["path"],
            }
            for item in txn["files"]
        ],
        "ingestion_id": txn["ingestion_id"],
        "noop": txn["noop"],
        "transaction_sha256": txn["transaction_sha256"],
    }


def apply(
    workspace: str | Path, ingestion_id: str, draft: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate, journal, and apply one draft; completed ingestions are no-ops."""
    ws = _Workspace(workspace)
    with _locked(ws):
        _recover(ws)
        recorded = _read_ledger(ws)["processed"].get(ingestion_id)
        if recorded is not None:
            return {"action": "already_compiled", "entry": recorded}
        txn = _plan(ws, ingestion_id, draft)
        data = (json.dumps(txn, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write(ws.transaction, data, "transaction")
        entry = _replay(ws, txn)
    return {"action": "compiled", "entry": entry}


def block(workspace: str | Path, ingestion_id: str, reason: str) -> dict[str, Any]:
    """Record a visible blocked reason without processing the ingestion."""
    reason = reason.strip()
    _require(0 < len(reason) <= MAX_REASON_CHARS, "reason must be 1 to 1000 characters")
    ws = _Workspace(workspace)
    with _locked(ws):
        _recover(ws)
        _load_bundle(ws, ingestion_id)
        ledger = _read_ledger(ws)
        _require(
            ingestion_id not in ledger["processed"], "ingestion is already compiled"
        )
        ledger["blocked"][ingestion_id] = {"reason": reason}
        _write_ledger(ws, ledger)
    return {"action": "blocked", "ingestion_id": ingestion_id, "reason": reason}


def status(workspace: str | Path) -> dict[str, Any]:
    """Report the unfinished transaction, if any, without modifying files."""
    ws = _Workspace(workspace)
    ws.require_initialized()
    txn = _read_transaction(ws)
    ledger = _read_ledger(ws)
    result: dict[str, Any] = {
        "blocked": len(ledger["blocked"]),
        "processed": len(ledger["processed"]),
        "transaction": None,
    }
    if txn is None:
        return result
    schema_sha = _current_sha(ws.schema, "schema")
    destinations: list[dict[str, Any]] = []
    for item in txn["files"]:
        current = _current_sha(_file_path(ws, item["path"]), item["path"])
        if current == item["new_sha256"]:
            state = "new"
        elif current == item["old_sha256"]:
            state = "old"
        else:
            state = "conflict"
        destinations.append({
            "current_sha256": current,
            "new_sha256": item["new_sha256"],
            "old_sha256": item["old_sha256"],
            "path": item["path"],
            "planned_content": item["content"] if state == "conflict" else None,
            "state": state,
        })
    result["transaction"] = {
        "destinations": destinations,
        "ingestion_id": txn["ingestion_id"],
        "schema_changed": schema_sha != txn["schema_sha256"],
    }
    return result


def recover(workspace: str | Path) -> dict[str, Any]:
    """Run recovery explicitly and report what it did."""
    ws = _Workspace(workspace)
    with _locked(ws):
        entry = _recover(ws)
    return {
        "action": "recovered" if entry is not None else "nothing_to_recover",
        "entry": entry,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    list_parser = commands.add_parser("list")
    list_parser.add_argument("--limit", type=int, default=MAX_LIST_PAGE)
    list_parser.add_argument("--offset", type=int, default=0)
    show_parser = commands.add_parser("show")
    show_parser.add_argument("--ingestion-id", required=True)
    read_parser = commands.add_parser("read")
    read_parser.add_argument("--ingestion-id", required=True)
    read_parser.add_argument("--file", required=True)
    read_parser.add_argument("--entry")
    read_parser.add_argument("--start", type=int, default=1)
    read_parser.add_argument("--end", type=int, default=MAX_READ_LINES)
    pages_parser = commands.add_parser("pages")
    pages_parser.add_argument("--limit", type=int, default=MAX_LIST_PAGE)
    pages_parser.add_argument("--offset", type=int, default=0)
    base_parser = commands.add_parser("base")
    base_parser.add_argument("--page", action="append", default=[])
    for name in ("validate", "apply"):
        draft_parser = commands.add_parser(name)
        draft_parser.add_argument("--ingestion-id", required=True)
    block_parser = commands.add_parser("block")
    block_parser.add_argument("--ingestion-id", required=True)
    block_parser.add_argument("--reason", required=True)
    commands.add_parser("status")
    commands.add_parser("recover")
    return parser


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    workspace = args.workspace
    commands: dict[str, Callable[[], dict[str, Any]]] = {
        "init": lambda: init(workspace),
        "list": lambda: list_ingestions(
            workspace, limit=args.limit, offset=args.offset
        ),
        "show": lambda: show(workspace, args.ingestion_id),
        "read": lambda: read_lines(
            workspace,
            args.ingestion_id,
            args.file,
            entry=args.entry,
            start=args.start,
            end=args.end,
        ),
        "pages": lambda: pages(workspace, limit=args.limit, offset=args.offset),
        "base": lambda: base(workspace, args.page),
        "validate": lambda: validate(workspace, args.ingestion_id, _read_draft()),
        "apply": lambda: apply(workspace, args.ingestion_id, _read_draft()),
        "block": lambda: block(workspace, args.ingestion_id, args.reason),
        "status": lambda: status(workspace),
        "recover": lambda: recover(workspace),
    }
    return commands[args.command]()


def main(argv: Sequence[str] | None = None) -> int:
    """Run one helper command and print its JSON result."""
    args = _parser().parse_args(argv)
    try:
        result = _dispatch(args)
    except WikiError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
