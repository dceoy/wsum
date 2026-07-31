"""Bounded text extraction for simple text-based PDF content streams."""

from __future__ import annotations

import re
import unicodedata
import zlib

from errors import MonitorError

STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
TEXT_BLOCK_RE = re.compile(rb"BT(.*?)ET", re.DOTALL)
TJ_RE = re.compile(rb"\[(.*?)\]\s*TJ", re.DOTALL)
TJ_ITEM_RE = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>")
SINGLE_TJ_RE = re.compile(rb"(\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>)\s*Tj")
METADATA_RE = re.compile(rb"/(Title|Author|Subject)\s*\((?:\\.|[^\\()])*\)")


def _decode_literal(value: bytes) -> str:
    if value.startswith(b"(") and value.endswith(b")"):
        value = value[1:-1]
    output = bytearray()
    position = 0
    escapes = {
        ord("n"): ord("\n"),
        ord("r"): ord("\r"),
        ord("t"): ord("\t"),
        ord("b"): ord("\b"),
        ord("f"): ord("\f"),
        ord("("): ord("("),
        ord(")"): ord(")"),
        ord("\\"): ord("\\"),
    }
    while position < len(value):
        current = value[position]
        if current != ord("\\"):
            output.append(current)
            position += 1
            continue
        position += 1
        if position >= len(value):
            break
        escaped = value[position]
        if escaped in escapes:
            output.append(escapes[escaped])
            position += 1
            continue
        if escaped in b"\r\n":
            if escaped == ord("\r") and position + 1 < len(value):
                if value[position + 1] == ord("\n"):
                    position += 1
            position += 1
            continue
        if ord("0") <= escaped <= ord("7"):
            end = position
            while end < min(position + 3, len(value)) and ord("0") <= value[end] <= ord(
                "7"
            ):
                end += 1
            output.append(int(value[position:end], 8) & 0xFF)
            position = end
            continue
        output.append(escaped)
        position += 1
    for encoding in ("utf-8", "utf-16-be", "latin-1"):
        try:
            return output.decode(encoding)
        except UnicodeDecodeError:
            continue
    return output.decode("latin-1", errors="replace")


def _decode_hex(value: bytes) -> str:
    compact = re.sub(rb"\s+", b"", value.strip()[1:-1])
    if len(compact) % 2:
        compact += b"0"
    try:
        raw = bytes.fromhex(compact.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    return raw.decode("latin-1", errors="replace")


def _bounded_decompress(value: bytes, limit: int) -> bytes:
    try:
        decompressor = zlib.decompressobj()
        result = decompressor.decompress(value, limit + 1)
        if len(result) > limit or decompressor.unconsumed_tail:
            raise MonitorError(
                "pdf_decompressed_too_large",
                "PDF decompressed stream exceeds the size limit",
            )
        result += decompressor.flush(limit + 1 - len(result))
    except zlib.error as exc:
        raise MonitorError("pdf_malformed", "PDF contains an invalid stream") from exc
    if len(result) > limit:
        raise MonitorError(
            "pdf_decompressed_too_large",
            "PDF decompressed stream exceeds the size limit",
        )
    return result


def _stream_data(pdf: bytes, limit: int) -> list[bytes]:
    streams: list[bytes] = []
    total = 0
    for match in STREAM_RE.finditer(pdf):
        dictionary = pdf[max(0, match.start() - 1_024) : match.start()]
        stream = match.group(1)
        if b"/FlateDecode" in dictionary:
            stream = _bounded_decompress(stream, limit - total)
        total += len(stream)
        if total > limit:
            raise MonitorError(
                "pdf_decompressed_too_large",
                "PDF content streams exceed the size limit",
            )
        streams.append(stream)
    return streams


def extract_pdf_text(
    pdf: bytes,
    *,
    max_input_bytes: int = 10_000_000,
    max_decompressed_bytes: int = 20_000_000,
    max_objects: int = 20_000,
) -> tuple[str, dict[str, str]]:
    if len(pdf) > max_input_bytes:
        raise MonitorError("response_too_large", "PDF exceeds the input size limit")
    if not pdf.startswith(b"%PDF-"):
        raise MonitorError("pdf_malformed", "document has no PDF signature")
    if b"/Encrypt" in pdf:
        raise MonitorError("pdf_encrypted", "encrypted PDFs are not supported")
    if len(re.findall(rb"\bobj\b", pdf)) > max_objects:
        raise MonitorError("pdf_object_limit", "PDF object count exceeds the limit")

    fragments: list[str] = []
    for stream in _stream_data(pdf, max_decompressed_bytes):
        for block in TEXT_BLOCK_RE.findall(stream):
            consumed: set[tuple[int, int]] = set()
            for array_match in TJ_RE.finditer(block):
                consumed.add(array_match.span())
                values = [
                    _decode_literal(item)
                    if item.startswith(b"(")
                    else _decode_hex(item)
                    for item in TJ_ITEM_RE.findall(array_match.group(1))
                ]
                text = "".join(values).strip()
                if text:
                    fragments.append(text)
            for match in SINGLE_TJ_RE.finditer(block):
                if any(start <= match.start() < end for start, end in consumed):
                    continue
                pdf_string = match.group(1)
                text = (
                    _decode_literal(pdf_string)
                    if pdf_string.startswith(b"(")
                    else _decode_hex(pdf_string)
                ).strip()
                if text:
                    fragments.append(text)
    lines = [
        re.sub(r"\s+", " ", unicodedata.normalize("NFKC", fragment)).strip()
        for fragment in fragments
    ]
    lines = [line for line in lines if line]
    if not lines:
        if b"/Subtype /Image" in pdf:
            raise MonitorError("pdf_image_only", "image-only PDFs are not supported")
        raise MonitorError("pdf_no_text", "PDF contains no extractable text")

    metadata: dict[str, str] = {}
    for match in METADATA_RE.finditer(pdf[: min(len(pdf), 2_000_000)]):
        full = match.group(0)
        key = match.group(1).decode("ascii").lower()
        start = full.find(b"(")
        metadata[key] = _decode_literal(full[start:])[:500]
    return "\n".join(lines), metadata
