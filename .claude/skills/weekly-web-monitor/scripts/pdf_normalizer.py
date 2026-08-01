"""Bounded, font-aware text extraction for text-based PDFs."""

from __future__ import annotations

import bisect
import re
import unicodedata
import zlib
from io import BytesIO

from errors import MonitorError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

# Locates only the "stream" keyword + its required end-of-line marker (CRLF
# or LF, never a bare CR -- see PDF spec 7.3.8.1). The stream's actual end
# is derived from the dictionary's /Length, not from scanning for the next
# "endstream" bytes (see _stream_data): an unfiltered content stream can
# legally contain that literal byte sequence inside a string operand, which
# would otherwise truncate the stream before later text-showing operators.
# The negative lookbehind keeps this from matching the "stream" that is
# itself the tail of an "endstream" keyword -- a stream's own trailing
# "endstream" is always followed later in the file by another real
# "stream" keyword (the next object), and without the lookbehind that
# "endstream\n" tail is indistinguishable from one.
STREAM_START_RE = re.compile(rb"(?<!end)stream(?:\r\n|\n)")
OBJECT_HEADER_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
LENGTH_RE = re.compile(rb"/Length\s+(\d+)(?:\s+(\d+)\s+R\b)?")
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
        if byte == 0x3C and index + 1 < length and data[index + 1] == 0x3C:
            # Skip both bytes of a dictionary opener so the second '<' is
            # not misclassified as the start of a hex string.
            index += 2
            continue
        if byte == 0x3C:
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


def _has_top_level_name_value(
    dictionary: bytes, key: bytes, expected_value: bytes
) -> bool:
    """Match a direct name value in the stream's outer dictionary only."""
    tokens = _scan_tokens(dictionary)
    cursor = 0
    dictionary_depth = 0
    array_depth = 0
    matched: bool | None = None
    for kind, start, end in tokens:
        while cursor < start:
            pair = dictionary[cursor : cursor + 2]
            if pair == b"<<":
                dictionary_depth += 1
                cursor += 2
                continue
            if pair == b">>":
                dictionary_depth -= 1
                cursor += 2
                continue
            if dictionary[cursor : cursor + 1] == b"[":
                array_depth += 1
            elif dictionary[cursor : cursor + 1] == b"]":
                array_depth -= 1
            cursor += 1
        cursor = end
        if kind != "name" or dictionary_depth != 1 or array_depth != 0:
            continue
        if dictionary[start:end] != key:
            continue
        if matched is not None:
            raise MonitorError(
                "pdf_malformed", "PDF stream dictionary repeats a classification key"
            )
        value_start = end
        while value_start < len(dictionary):
            if dictionary[value_start] in _PDF_WHITESPACE:
                value_start += 1
                continue
            if dictionary[value_start] == 0x25:  # comment
                while value_start < len(dictionary) and dictionary[value_start] not in (
                    0x0A,
                    0x0D,
                ):
                    value_start += 1
                continue
            break
        value_end = value_start + len(expected_value)
        if dictionary[value_start:value_end] != expected_value:
            matched = False
            continue
        matched = value_end == len(dictionary) or (
            dictionary[value_end] in _NAME_TERMINATORS
        )
    return matched is True


def _is_image_stream(dictionary: bytes) -> bool:
    return _has_top_level_name_value(
        dictionary, b"/Type", b"/XObject"
    ) and _has_top_level_name_value(dictionary, b"/Subtype", b"/Image")


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
        raise MonitorError("pdf_malformed", "PDF stream dictionary could not be parsed")
    return dictionary


def _object_offsets(pdf: bytes) -> tuple[list[int], dict[tuple[int, int], int]]:
    starts: list[int] = []
    by_id: dict[tuple[int, int], int] = {}
    for match in OBJECT_HEADER_RE.finditer(pdf):
        starts.append(match.end())
        by_id.setdefault((int(match.group(1)), int(match.group(2))), match.end())
    return starts, by_id


def _resolve_length(
    pdf: bytes, number: int, generation: int, object_by_id: dict[tuple[int, int], int]
) -> int:
    # An indirect /Length (e.g. "/Length 5 0 R") points at a separate
    # object whose body is just the integer length. Resolve it by object
    # identity rather than trusting an unrelated later match, and fail
    # closed if the referenced object cannot be found or is not a bare
    # integer.
    start = object_by_id.get((number, generation))
    if start is None:
        raise MonitorError(
            "pdf_malformed",
            "PDF stream /Length indirect reference could not be resolved",
        )
    match = re.match(rb"\s*(\d+)", pdf[start : start + 32])
    if match is None:
        raise MonitorError(
            "pdf_malformed", "PDF stream /Length object is not an integer"
        )
    return int(match.group(1))


def _stream_data(pdf: bytes, limit: int) -> tuple[list[bytes], bool]:
    streams: list[bytes] = []
    has_image_stream = False
    total = 0
    object_starts, object_by_id = _object_offsets(pdf)
    cursor = 0
    while match := STREAM_START_RE.search(pdf, cursor):
        data_start = match.end()
        dictionary = _stream_dictionary(pdf, object_starts, match.start())
        length_match = LENGTH_RE.search(dictionary)
        if length_match is None:
            raise MonitorError("pdf_malformed", "PDF stream dictionary has no /Length")
        if length_match.group(2) is not None:
            length = _resolve_length(
                pdf,
                int(length_match.group(1)),
                int(length_match.group(2)),
                object_by_id,
            )
        else:
            length = int(length_match.group(1))
        data_end = data_start + length
        if data_end > len(pdf):
            raise MonitorError(
                "pdf_malformed", "PDF stream /Length exceeds the document size"
            )
        # /Length must land exactly on "endstream" (optionally preceded by
        # its own end-of-line marker). A mismatch means the declared length
        # cannot be trusted, and scanning ahead for the next literal
        # "endstream" bytes instead would reintroduce the exact
        # false-boundary risk this /Length-based parse exists to avoid.
        end_match = re.match(
            rb"\r?\n?endstream\b", pdf[data_end : data_end + 16]
        )
        if end_match is None:
            raise MonitorError(
                "pdf_malformed",
                "PDF stream /Length does not align with endstream",
            )
        # Search only after this validated stream boundary. Raw stream bytes
        # can legally contain the token ``stream`` and must not be parsed as
        # another object stream opener.
        cursor = data_end + end_match.end()
        stream = pdf[data_start:data_end]
        if _is_image_stream(dictionary):
            # PdfReader's text extractor deliberately skips Image XObjects.
            # Keep their encoded bytes under the whole-document input cap,
            # but do not apply text/content filter policy or decompression
            # accounting to image data that will never be interpreted as text.
            has_image_stream = True
            continue
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
    return streams, has_image_stream


def _extract_font_aware_text(
    pdf: bytes, *, max_pages: int, max_extracted_chars: int
) -> list[str]:
    """Extract text with the PDF's active font encodings and CMaps."""
    try:
        reader = PdfReader(BytesIO(pdf), strict=True)
        if reader.is_encrypted:
            raise MonitorError("pdf_encrypted", "encrypted PDFs are not supported")
        if len(reader.pages) > max_pages:
            raise MonitorError("pdf_page_limit", "PDF page count exceeds the limit")

        fragments: list[str] = []
        extracted_chars = 0
        for page in reader.pages:
            page_text = page.extract_text() or ""
            extracted_chars += len(page_text)
            if extracted_chars > max_extracted_chars:
                raise MonitorError(
                    "pdf_extracted_too_large",
                    "PDF extracted text exceeds the size limit",
                )
            fragments.extend(page_text.splitlines())
        return fragments
    except MonitorError:
        raise
    except (PdfReadError, KeyError, TypeError, ValueError, RecursionError) as exc:
        raise MonitorError("pdf_malformed", "PDF could not be parsed safely") from exc


def extract_pdf_text(
    pdf: bytes,
    *,
    max_input_bytes: int = 10_000_000,
    max_decompressed_bytes: int = 20_000_000,
    max_objects: int = 20_000,
    max_pages: int = 1_000,
) -> tuple[str, dict[str, str]]:
    if len(pdf) > max_input_bytes:
        raise MonitorError("response_too_large", "PDF exceeds the input size limit")
    if not pdf.startswith(b"%PDF-"):
        raise MonitorError("pdf_malformed", "document has no PDF signature")
    if b"/Encrypt" in pdf:
        raise MonitorError("pdf_encrypted", "encrypted PDFs are not supported")
    if len(re.findall(rb"\bobj\b", pdf)) > max_objects:
        raise MonitorError("pdf_object_limit", "PDF object count exceeds the limit")

    # Validate every text/content stream and bound its aggregate decoded size
    # before the font-aware parser sees it. Image XObjects are skipped because
    # PdfReader's text extractor does not decode them; their encoded bytes are
    # still covered by the whole-document input cap.
    streams, has_image_stream = _stream_data(pdf, max_decompressed_bytes)
    uses_fonts = any(
        marker in pdf
        for marker in (
            b"/Font",
            b"/BaseFont",
            b"/Encoding",
            b"/ToUnicode",
            b"/Type0",
            b"/ObjStm",
        )
    )
    if uses_fonts:
        fragments = _extract_font_aware_text(
            pdf,
            max_pages=max_pages,
            max_extracted_chars=max_decompressed_bytes,
        )
    else:
        # Retain the bounded lexer for deliberately simple, font-less PDF
        # content streams used by lightweight producers and fixtures.
        fragments = []
        for stream in streams:
            for block in _text_blocks(stream):
                fragments.extend(_text_fragments(block))
    lines = [
        re.sub(r"\s+", " ", unicodedata.normalize("NFKC", fragment)).strip()
        for fragment in fragments
    ]
    lines = [line for line in lines if line]
    if not lines:
        if has_image_stream:
            raise MonitorError("pdf_image_only", "image-only PDFs are not supported")
        raise MonitorError("pdf_no_text", "PDF contains no extractable text")

    metadata: dict[str, str] = {}
    for match in METADATA_RE.finditer(pdf[: min(len(pdf), 2_000_000)]):
        full = match.group(0)
        key = match.group(1).decode("ascii").lower()
        start = full.find(b"(")
        metadata[key] = _decode_literal(full[start:])[:500]
    return "\n".join(lines), metadata
