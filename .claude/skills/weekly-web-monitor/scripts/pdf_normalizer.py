"""Bounded text extraction for simple text-based PDF content streams."""

from __future__ import annotations

import bisect
import re
import unicodedata
import zlib

from errors import MonitorError

STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
OBJECT_HEADER_RE = re.compile(rb"\d+\s+\d+\s+obj\b")
FILTER_RE = re.compile(rb"/Filter\s*(/[A-Za-z0-9]+|\[[^\]]*\])")
FILTER_NAME_RE = re.compile(rb"/[A-Za-z0-9]+")
TEXT_BLOCK_RE = re.compile(rb"BT(.*?)ET", re.DOTALL)
TJ_ARRAY_RE = re.compile(rb"\[(.*?)\]\s*TJ", re.DOTALL)
METADATA_RE = re.compile(rb"/(Title|Author|Subject)\s*\((?:\\.|[^\\()])*\)")

_PDF_WHITESPACE = frozenset(b" \t\r\n\f\x00")
_PDF_DELIMITERS = frozenset(b"()<>[]{}/%")
_NAME_TERMINATORS = _PDF_WHITESPACE | _PDF_DELIMITERS


def _scan_tokens(data: bytes) -> list[tuple[str, int, int]]:
    # Classify comment, name, and string regions by real PDF lexical rules
    # rather than matching operator/operand bytes with flat regexes against
    # raw content. Without this, literal bytes "BT"/"ET" occurring inside a
    # name token (e.g. "/ETMarker") are indistinguishable from the real
    # end-text operator, and a flat "\((?:\\.|[^\\()])*\)" string regex
    # cannot represent balanced *nested* parens (e.g. "(Old (status))"),
    # silently truncating or dropping the operand. This scanner mirrors the
    # PDF spec's token boundaries directly: a name/string/comment always
    # extends to its own terminator regardless of what other operators it
    # textually resembles. Fail closed (rather than treating the remainder
    # as unmasked) when a string/comment never terminates, since silently
    # ignoring the rest of the stream would drop any real operators after
    # it -- the same silent-miss failure mode this guards against.
    tokens: list[tuple[str, int, int]] = []
    length = len(data)
    index = 0
    while index < length:
        byte = data[index]
        if byte == 0x25:  # '%' comment runs to end of line (not consumed)
            start = index
            while index < length and data[index] not in (0x0A, 0x0D):
                index += 1
            tokens.append(("comment", start, index))
            continue
        if byte == 0x28:  # '(' literal string, balanced/escaped parens
            start = index
            depth = 1
            index += 1
            while index < length and depth > 0:
                current = data[index]
                if current == 0x5C:  # backslash escapes the next byte
                    index += 2
                    continue
                if current == 0x28:
                    depth += 1
                elif current == 0x29:
                    depth -= 1
                index += 1
            if depth > 0:
                raise MonitorError(
                    "pdf_malformed", "PDF content stream has an unterminated string"
                )
            tokens.append(("literal_string", start, index))
            continue
        if byte == 0x3C and (index + 1 >= length or data[index + 1] != 0x3C):
            # '<' hex string, but not the '<<' that opens a dictionary
            start = index
            index += 1
            while index < length and data[index] != 0x3E:
                index += 1
            if index >= length:
                raise MonitorError(
                    "pdf_malformed", "PDF content stream has an unterminated hex string"
                )
            index += 1
            tokens.append(("hex_string", start, index))
            continue
        if byte == 0x2F:  # '/' name token, terminated by whitespace/delimiter
            start = index
            index += 1
            while index < length and data[index] not in _NAME_TERMINATORS:
                index += 1
            tokens.append(("name", start, index))
            continue
        index += 1
    return tokens


def _mask_strings_and_comments(stream: bytes) -> bytes:
    # Replace comment/string/name regions with same-length, content-free
    # placeholder bytes so operator matching (BT/ET, TJ array brackets) on
    # the masked copy only ever sees bytes that are real operators, while
    # callers still slice the *original* bytes at the located offsets to
    # decode the real operand content.
    masked = bytearray(stream)
    for _, start, end in _scan_tokens(stream):
        masked[start:end] = b"." * (end - start)
    return bytes(masked)


def _string_spans(data: bytes) -> list[tuple[int, int]]:
    return [
        (start, end)
        for kind, start, end in _scan_tokens(data)
        if kind in ("literal_string", "hex_string")
    ]


def _decode_operand(data: bytes, start: int, end: int) -> str:
    value = data[start:end]
    if value.startswith(b"("):
        return _decode_literal(value)
    return _decode_hex(value)


def _text_blocks(stream: bytes) -> list[bytes]:
    masked = _mask_strings_and_comments(stream)
    return [
        stream[match.start(1) : match.end(1)]
        for match in TEXT_BLOCK_RE.finditer(masked)
    ]


def _text_fragments(block: bytes) -> list[str]:
    masked_block = _mask_strings_and_comments(block)
    spans = _string_spans(block)
    consumed: set[int] = set()
    fragments: list[str] = []

    for array_match in TJ_ARRAY_RE.finditer(masked_block):
        array_start, array_end = array_match.span(1)
        array_spans = [
            (start, end) for start, end in spans if array_start <= start < array_end
        ]
        consumed.update(start for start, _ in array_spans)
        text = "".join(
            _decode_operand(block, start, end) for start, end in array_spans
        ).strip()
        if text:
            fragments.append(text)

    length = len(masked_block)
    for start, end in spans:
        if start in consumed:
            continue
        position = end
        while position < length and masked_block[position] in _PDF_WHITESPACE:
            position += 1
        is_show_text_operator = masked_block[position : position + 2] == b"Tj" or (
            masked_block[position : position + 1] in (b"'", b'"')
        )
        if not is_show_text_operator:
            continue
        text = _decode_operand(block, start, end).strip()
        if text:
            fragments.append(text)
    return fragments


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
    if not decompressor.eof:
        raise MonitorError(
            "pdf_malformed", "PDF contains a truncated compressed stream"
        )
    return result


def _stream_filters(dictionary: bytes) -> list[bytes]:
    match = FILTER_RE.search(dictionary)
    if match is None:
        return []
    value = match.group(1)
    if value.startswith(b"["):
        return FILTER_NAME_RE.findall(value)
    return [value]


def _stream_dictionary(
    pdf: bytes, object_starts: list[int], stream_start: int
) -> bytes:
    # The dictionary that governs a stream is everything between its
    # enclosing "N G obj" header and the "stream" keyword -- unlike a fixed
    # lookbehind window, this is correct regardless of how large the
    # dictionary is. Fail closed if no enclosing object can be found or the
    # bytes between them do not look like a dictionary, rather than silently
    # treating an unprovable association as "no filter".
    index = bisect.bisect_right(object_starts, stream_start) - 1
    if index < 0:
        raise MonitorError(
            "pdf_malformed", "PDF stream has no enclosing object dictionary"
        )
    dictionary = pdf[object_starts[index] : stream_start]
    stripped = dictionary.strip()
    if not (stripped.startswith(b"<<") and stripped.endswith(b">>")):
        raise MonitorError(
            "pdf_malformed", "PDF stream dictionary could not be parsed"
        )
    return dictionary


def _stream_data(pdf: bytes, limit: int) -> list[bytes]:
    streams: list[bytes] = []
    total = 0
    object_starts = [match.end() for match in OBJECT_HEADER_RE.finditer(pdf)]
    for match in STREAM_RE.finditer(pdf):
        dictionary = _stream_dictionary(pdf, object_starts, match.start())
        stream = match.group(1)
        filters = _stream_filters(dictionary)
        if filters == [b"/FlateDecode"]:
            stream = _bounded_decompress(stream, limit - total)
        elif filters:
            raise MonitorError(
                "pdf_unsupported_filter",
                "PDF stream uses an unsupported filter",
            )
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
    if (
        b"/ToUnicode" in pdf
        or b"/Differences" in pdf
        or b"/Encoding" in pdf
        or b"/Type0" in pdf
        or b"/ObjStm" in pdf
    ):
        # Text-showing operators carry character codes, not text -- codes
        # are only text once resolved through the active font's /Encoding
        # (any named encoding such as /WinAnsiEncoding or /MacRomanEncoding,
        # not only a /Differences remap) and any /ToUnicode CMap. Composite
        # (/Type0) fonts always route codes through a CMap too. Compressed
        # object streams (/ObjStm) can hide any of these declarations from
        # this raw marker scan entirely, since font dictionaries stored
        # there never appear as literal bytes in the file. This extractor
        # decodes raw string bytes directly (UTF-16BE/Latin-1) without
        # resolving any of that, so an active encoding that isn't provably
        # byte-identity-safe can change what a viewer renders while every
        # code byte -- and this extractor's hash -- stays identical. Reject
        # rather than silently hash the wrong text.
        raise MonitorError(
            "pdf_unsupported_encoding",
            "PDF uses font encodings that require CMap resolution",
        )
    if len(re.findall(rb"\bobj\b", pdf)) > max_objects:
        raise MonitorError("pdf_object_limit", "PDF object count exceeds the limit")

    fragments: list[str] = []
    for stream in _stream_data(pdf, max_decompressed_bytes):
        for block in _text_blocks(stream):
            fragments.extend(_text_fragments(block))
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
