"""Bounded, font-aware text extraction for text-based PDFs."""

from __future__ import annotations

import bisect
import hashlib
import re
import unicodedata
import zlib
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from errors import MonitorError
from network_policy import canonicalize_fragment_identity, canonicalize_url

if TYPE_CHECKING:
    from collections.abc import Callable

    from pypdf import PdfReader
    from pypdf._page import PageObject

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
METADATA_RE = re.compile(rb"/(Title|Author|Subject)\s*\((?:\\.|[^\\()])*\)")

_PDF_WHITESPACE = frozenset(b" \t\r\n\f\x00")
_PDF_DELIMITERS = frozenset(b"()<>[]{}/%")
_NAME_TERMINATORS = _PDF_WHITESPACE | _PDF_DELIMITERS
MAX_PDF_DICTIONARY_NESTING = 100

_PERCENT = 0x25
_LPAREN = 0x28
_RPAREN = 0x29
_SOLIDUS = 0x2F
_LANGLE = 0x3C
_RANGLE = 0x3E
_BACKSLASH = 0x5C
_CR = 0x0D
_LF = 0x0A


def _scan_comment_token(data: bytes, index: int) -> tuple[tuple[str, int, int], int]:
    """Scan a '%' comment token, which runs to end of line (not consumed).

    Returns:
        The (token, next index) pair.
    """
    start = index
    length = len(data)
    while index < length and data[index] not in {_LF, _CR}:
        index += 1
    return ("comment", start, index), index


def _scan_literal_string_token(
    data: bytes, index: int
) -> tuple[tuple[str, int, int], int]:
    """Scan a '(' literal string token with balanced/escaped parens.

    Returns:
        The (token, next index) pair.

    Raises:
        MonitorError: If the string never terminates.
    """
    start = index
    length = len(data)
    depth = 1
    index += 1
    while index < length and depth > 0:
        current = data[index]
        if current == _BACKSLASH:  # backslash escapes the next byte
            index += 2
            continue
        if current == _LPAREN:
            depth += 1
        elif current == _RPAREN:
            depth -= 1
        index += 1
    if depth > 0:
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF content stream has an unterminated string")
    return ("literal_string", start, index), index


def _scan_hex_string_token(data: bytes, index: int) -> tuple[tuple[str, int, int], int]:
    """Scan a '<' hex string token (never the '<<' that opens a dictionary).

    Returns:
        The (token, next index) pair.

    Raises:
        MonitorError: If the hex string never terminates.
    """
    start = index
    length = len(data)
    index += 1
    while index < length and data[index] != _RANGLE:
        index += 1
    if index >= length:
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF content stream has an unterminated hex string")
    index += 1
    return ("hex_string", start, index), index


def _scan_name_token(data: bytes, index: int) -> tuple[tuple[str, int, int], int]:
    """Scan a '/' name token, terminated by whitespace/delimiter.

    Returns:
        The (token, next index) pair.
    """
    start = index
    length = len(data)
    index += 1
    while index < length and data[index] not in _NAME_TERMINATORS:
        index += 1
    return ("name", start, index), index


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
        if byte == _PERCENT:
            token, index = _scan_comment_token(data, index)
            tokens.append(token)
            continue
        if byte == _LPAREN:
            token, index = _scan_literal_string_token(data, index)
            tokens.append(token)
            continue
        if byte == _LANGLE and index + 1 < length and data[index + 1] == _LANGLE:
            # Skip both bytes of a dictionary opener so the second '<' is
            # not misclassified as the start of a hex string.
            index += 2
            continue
        if byte == _LANGLE:
            token, index = _scan_hex_string_token(data, index)
            tokens.append(token)
            continue
        if byte == _SOLIDUS:
            token, index = _scan_name_token(data, index)
            tokens.append(token)
            continue
        index += 1
    return tokens


_LITERAL_STRING_ESCAPES = {
    ord("n"): ord("\n"),
    ord("r"): ord("\r"),
    ord("t"): ord("\t"),
    ord("b"): ord("\b"),
    ord("f"): ord("\f"),
    ord("("): ord("("),
    ord(")"): ord(")"),
    ord("\\"): ord("\\"),
}


def _consume_escape_sequence(value: bytes, position: int) -> tuple[bytes, int]:
    """Decode one backslash escape sequence starting at ``value[position]``.

    ``position`` must point just past the backslash.

    Returns:
        A (decoded bytes, next position) pair. The decoded bytes are empty
        for a line-continuation escape (backslash-newline).
    """
    escaped = value[position]
    if escaped in _LITERAL_STRING_ESCAPES:
        return bytes((_LITERAL_STRING_ESCAPES[escaped],)), position + 1
    if escaped in b"\r\n":
        if (
            escaped == ord("\r")
            and position + 1 < len(value)
            and value[position + 1] == ord("\n")
        ):
            position += 1
        return b"", position + 1
    if ord("0") <= escaped <= ord("7"):
        end = position
        while end < min(position + 3, len(value)) and ord("0") <= value[end] <= ord(
            "7"
        ):
            end += 1
        return bytes((int(value[position:end], 8) & 0xFF,)), end
    return bytes((escaped,)), position + 1


def _decode_literal_bytes(value: bytes) -> bytes:
    """Unescape a PDF literal string's content (without the enclosing parens).

    Returns:
        The unescaped raw bytes.
    """
    output = bytearray()
    position = 0
    while position < len(value):
        current = value[position]
        if current != ord("\\"):
            output.append(current)
            position += 1
            continue
        position += 1
        if position >= len(value):
            break
        decoded, position = _consume_escape_sequence(value, position)
        output.extend(decoded)
    return bytes(output)


def _decode_literal(value: bytes) -> str:
    """Unescape and decode a PDF literal string value to text.

    Returns:
        The decoded text, trying UTF-8, UTF-16BE, then Latin-1 in order,
        falling back to a lossy Latin-1 decode if none succeed cleanly.
    """
    if value.startswith(b"(") and value.endswith(b")"):
        value = value[1:-1]
    output = _decode_literal_bytes(value)
    for encoding in ("utf-8", "utf-16-be", "latin-1"):
        try:
            return output.decode(encoding)
        except UnicodeDecodeError:
            continue
    return output.decode("latin-1", errors="replace")


def _bounded_decompress(value: bytes, limit: int) -> bytes:
    """Decompress ``value`` with zlib, bounded to ``limit`` output bytes.

    Returns:
        The decompressed bytes.

    Raises:
        MonitorError: If ``value`` is not valid zlib data, the
            decompressed size exceeds ``limit``, or the stream is
            truncated.
    """
    try:
        decompressor = zlib.decompressobj()
        result = decompressor.decompress(value, limit + 1)
        oversized = len(result) > limit or bool(decompressor.unconsumed_tail)
        if not oversized:
            result += decompressor.flush(limit + 1 - len(result))
    except zlib.error as exc:
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF contains an invalid stream") from exc
    if oversized or len(result) > limit:
        msg = "pdf_decompressed_too_large"
        raise MonitorError(
            msg,
            "PDF decompressed stream exceeds the size limit",
        )
    if not decompressor.eof:
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF contains a truncated compressed stream")
    return result


def _decode_name(name: bytes) -> bytes:
    """Decode valid PDF name escapes in one lexical name token.

    Returns:
        The decoded name bytes (still slash-prefixed).

    Raises:
        MonitorError: If ``name`` does not start with a slash.
    """
    if not name.startswith(b"/"):
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF name token has no slash prefix")
    decoded = bytearray(b"/")
    position = 1
    while position < len(name):
        if name[position] != ord("#"):
            decoded.append(name[position])
            position += 1
            continue
        if position + 2 >= len(name) or any(
            byte not in b"0123456789abcdefABCDEF"
            for byte in name[position + 1 : position + 3]
        ):
            # pypdf preserves malformed name escapes literally, so do the
            # same instead of introducing a parser differential.
            decoded.append(name[position])
            position += 1
            continue
        decoded.append(int(name[position + 1 : position + 3], 16))
        position += 3
    return bytes(decoded)


def _skip_whitespace_and_comments(data: bytes, position: int) -> int:
    while position < len(data):
        if data[position] in _PDF_WHITESPACE:
            position += 1
            continue
        if data[position] == ord("%"):
            while position < len(data) and data[position] not in {ord("\r"), ord("\n")}:
                position += 1
            continue
        break
    return position


def _read_name(data: bytes, position: int) -> tuple[bytes, int]:
    if position >= len(data) or data[position] != ord("/"):
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF dictionary expects a name")
    end = position + 1
    while end < len(data) and data[end] not in _NAME_TERMINATORS:
        end += 1
    return _decode_name(data[position:end]), end


def _skip_literal_string(data: bytes, position: int) -> int:
    depth = 1
    position += 1
    while position < len(data):
        current = data[position]
        if current == ord("\\"):
            position += 2
            continue
        if current == ord("("):
            depth += 1
        elif current == ord(")"):
            depth -= 1
            if depth == 0:
                return position + 1
        position += 1
    msg = "pdf_malformed"
    raise MonitorError(msg, "PDF dictionary has an unterminated string")


def _skip_hex_string(data: bytes, position: int) -> int:
    position += 1
    while position < len(data) and data[position] != ord(">"):
        position += 1
    if position >= len(data):
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF dictionary has an unterminated hex string")
    return position + 1


def _read_bare_token(data: bytes, position: int) -> tuple[bytes, int]:
    end = position
    while end < len(data) and data[end] not in _NAME_TERMINATORS:
        end += 1
    if end == position:
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF dictionary has an invalid value")
    return data[position:end], end


def _skip_indirect_reference_suffix(data: bytes, end: int, first: bytes) -> int:
    """Skip a "N G R" indirect-reference suffix following a bare integer.

    Returns:
        The end index of the whole reference if one follows ``first`` at
        ``end``, else ``end`` unchanged (``first`` was just a bare number).
    """
    if not first.isdigit():
        return end
    middle = _skip_whitespace_and_comments(data, end)
    if middle >= len(data) or data[middle] in _PDF_DELIMITERS:
        return end
    second, second_end = _read_bare_token(data, middle)
    if not second.isdigit():
        return end
    suffix = _skip_whitespace_and_comments(data, second_end)
    if data[suffix : suffix + 1] != b"R":
        return end
    reference_end = suffix + 1
    if reference_end < len(data) and data[reference_end] not in _NAME_TERMINATORS:
        return end
    return reference_end


def _skip_pdf_object(data: bytes, position: int, nesting: int = 0) -> int:
    """Skip one direct PDF object, including an indirect integer reference.

    Returns:
        The index just past the skipped object.

    Raises:
        MonitorError: If the value is missing or malformed.
    """
    position = _skip_whitespace_and_comments(data, position)
    if position >= len(data):
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF dictionary has a missing value")
    if data[position : position + 2] == b"<<":
        return _skip_pdf_dictionary(data, position, nesting + 1)
    if data[position] == ord("["):
        return _skip_pdf_array(data, position, nesting + 1)
    if data[position] == ord("("):
        return _skip_literal_string(data, position)
    if data[position] == ord("<"):
        return _skip_hex_string(data, position)
    if data[position] == ord("/"):
        _, end = _read_name(data, position)
        return end
    if data[position] in _PDF_DELIMITERS:
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF dictionary has an invalid value")

    first, end = _read_bare_token(data, position)
    return _skip_indirect_reference_suffix(data, end, first)


def _skip_pdf_array(data: bytes, position: int, nesting: int) -> int:
    if nesting > MAX_PDF_DICTIONARY_NESTING:
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF dictionary nesting is too deep")
    position += 1
    while True:
        position = _skip_whitespace_and_comments(data, position)
        if position >= len(data):
            msg = "pdf_malformed"
            raise MonitorError(msg, "PDF dictionary has an unterminated array")
        if data[position] == ord("]"):
            return position + 1
        position = _skip_pdf_object(data, position, nesting)


def _skip_pdf_dictionary(data: bytes, position: int, nesting: int) -> int:
    if nesting > MAX_PDF_DICTIONARY_NESTING:
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF dictionary nesting is too deep")
    position += 2
    while True:
        position = _skip_whitespace_and_comments(data, position)
        if position >= len(data):
            msg = "pdf_malformed"
            raise MonitorError(msg, "PDF dictionary is unterminated")
        if data[position : position + 2] == b">>":
            return position + 2
        _, position = _read_name(data, position)
        position = _skip_pdf_object(data, position, nesting)


def _filter_value(dictionary: bytes, position: int) -> tuple[list[bytes], int]:
    """Parse the direct name or name array allowed for a /Filter value.

    Returns:
        A (filter names, next position) tuple.

    Raises:
        MonitorError: If the filter value is missing, malformed, or not a
            name or array.
    """
    position = _skip_whitespace_and_comments(dictionary, position)
    if position >= len(dictionary):
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF stream dictionary has no filter value")
    if dictionary[position] == ord("/"):
        name, end = _read_name(dictionary, position)
        return [name], end
    if dictionary[position] != ord("["):
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF filter value is not a name or array")

    filters: list[bytes] = []
    position += 1
    while True:
        position = _skip_whitespace_and_comments(dictionary, position)
        if position >= len(dictionary):
            msg = "pdf_malformed"
            raise MonitorError(msg, "PDF filter array is unterminated")
        if dictionary[position] == ord("]"):
            return filters, position + 1
        name, position = _read_name(dictionary, position)
        filters.append(name)


def _stream_filters(dictionary: bytes) -> list[bytes]:
    position = _skip_whitespace_and_comments(dictionary, 0)
    if dictionary[position : position + 2] != b"<<":
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF stream dictionary could not be parsed")
    position += 2
    filters: list[bytes] | None = None
    while True:
        position = _skip_whitespace_and_comments(dictionary, position)
        if position >= len(dictionary):
            msg = "pdf_malformed"
            raise MonitorError(msg, "PDF stream dictionary is unterminated")
        if dictionary[position : position + 2] == b">>":
            return filters or []
        name, position = _read_name(dictionary, position)
        if name == b"/Filter":
            if filters is not None:
                msg = "pdf_malformed"
                raise MonitorError(msg, "PDF stream dictionary repeats /Filter")
            filters, position = _filter_value(dictionary, position)
            continue
        position = _skip_pdf_object(dictionary, position)


def _advance_bracket_depth(
    dictionary: bytes, cursor: int, target: int, dictionary_depth: int, array_depth: int
) -> tuple[int, int, int]:
    """Advance ``cursor`` to ``target``, tracking dict/array nesting depth.

    Returns:
        A (new cursor, dictionary_depth, array_depth) tuple.
    """
    while cursor < target:
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
    return cursor, dictionary_depth, array_depth


def _matches_expected_name_value(
    dictionary: bytes, after_key_end: int, expected_value: bytes
) -> bool:
    """Check whether the name value right after a matched key is ``expected_value``.

    Returns:
        Whether the (whitespace/comment-skipped) value at ``after_key_end``
        equals ``expected_value`` and is itself a complete name token.
    """
    value_start = after_key_end
    while value_start < len(dictionary):
        if dictionary[value_start] in _PDF_WHITESPACE:
            value_start += 1
            continue
        if dictionary[value_start] == _PERCENT:
            while value_start < len(dictionary) and dictionary[value_start] not in {
                _LF,
                _CR,
            }:
                value_start += 1
            continue
        break
    value_end = value_start + len(expected_value)
    if dictionary[value_start:value_end] != expected_value:
        return False
    return value_end == len(dictionary) or (dictionary[value_end] in _NAME_TERMINATORS)


def _has_top_level_name_value(
    dictionary: bytes, key: bytes, expected_value: bytes
) -> bool:
    """Match a direct name value in the stream's outer dictionary only.

    Returns:
        Whether ``key`` appears at dictionary depth 1 (outside any nested
        array/dictionary) with a name value equal to ``expected_value``.

    Raises:
        MonitorError: If ``key`` appears more than once at that depth.
    """
    tokens = _scan_tokens(dictionary)
    cursor = 0
    dictionary_depth = 0
    array_depth = 0
    matched: bool | None = None
    for kind, start, end in tokens:
        cursor, dictionary_depth, array_depth = _advance_bracket_depth(
            dictionary, cursor, start, dictionary_depth, array_depth
        )
        cursor = end
        if kind != "name" or dictionary_depth != 1 or array_depth != 0:
            continue
        if dictionary[start:end] != key:
            continue
        if matched is not None:
            msg = "pdf_malformed"
            raise MonitorError(
                msg, "PDF stream dictionary repeats a classification key"
            )
        matched = _matches_expected_name_value(dictionary, end, expected_value)
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
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF stream has no enclosing object dictionary")
    dictionary = pdf[object_starts[index] : stream_start]
    stripped = dictionary.strip()
    if not (stripped.startswith(b"<<") and stripped.endswith(b">>")):
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF stream dictionary could not be parsed")
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
        msg = "pdf_malformed"
        raise MonitorError(
            msg,
            "PDF stream /Length indirect reference could not be resolved",
        )
    match = re.match(rb"\s*(\d+)", pdf[start : start + 32])
    if match is None:
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF stream /Length object is not an integer")
    return int(match.group(1))


def _validate_streams(pdf: bytes, limit: int) -> bool:
    """Bound every non-image stream before passing the PDF to pypdf.

    Returns:
        Whether any image (XObject) stream was found.

    Raises:
        MonitorError: If any stream is malformed, uses an unsupported
            filter, or the aggregate decompressed size exceeds ``limit``.
    """
    has_image_stream = False
    total = 0
    object_starts, object_by_id = _object_offsets(pdf)
    cursor = 0
    while match := STREAM_START_RE.search(pdf, cursor):
        data_start = match.end()
        dictionary = _stream_dictionary(pdf, object_starts, match.start())
        length_match = LENGTH_RE.search(dictionary)
        if length_match is None:
            msg = "pdf_malformed"
            raise MonitorError(msg, "PDF stream dictionary has no /Length")
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
            msg = "pdf_malformed"
            raise MonitorError(msg, "PDF stream /Length exceeds the document size")
        # /Length must land exactly on "endstream" (optionally preceded by
        # its own end-of-line marker). A mismatch means the declared length
        # cannot be trusted, and scanning ahead for the next literal
        # "endstream" bytes instead would reintroduce the exact
        # false-boundary risk this /Length-based parse exists to avoid.
        end_match = re.match(rb"\r?\n?endstream\b", pdf[data_end : data_end + 16])
        if end_match is None:
            msg = "pdf_malformed"
            raise MonitorError(
                msg,
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
            msg = "pdf_unsupported_filter"
            raise MonitorError(
                msg,
                "PDF stream uses an unsupported filter",
            )
        total += len(stream)
        if total > limit:
            msg = "pdf_decompressed_too_large"
            raise MonitorError(
                msg,
                "PDF content streams exceed the size limit",
            )
    return has_image_stream


# Bound both the number of link annotations examined per document and the
# length of the URL text embedded in the normalized output. Mirrors
# html_normalizer.MAX_LINK_ANNOTATIONS/MAX_LINK_URL_CHARS: page.extract_text()
# only reads the visible content stream and omits /Annots URI actions, so
# two PDFs with the same visible label but a changed clickable destination
# (e.g. an "Apply" link retargeted to a different URL) would otherwise
# normalize identically. The cap applies to every annotation node walked,
# not just ones that resolve to an emitted destination, so a page with a
# huge /Annots array cannot force unbounded work before a Link/URI
# annotation is ever found.
MAX_PDF_LINK_ANNOTATIONS = 500
MAX_PDF_LINK_URL_CHARS = 300


@dataclass(slots=True)
class _PdfLinkBudget:
    """A mutable remaining-link-annotation counter for one PDF."""

    remaining: int


def _resolve_pdf_object(value: object) -> object:
    get_object = getattr(value, "get_object", None)
    return get_object() if callable(get_object) else value


def _pdf_link_destination(uri: str) -> str:
    # Same credential/SSRF policy and fragment-identity handling as
    # html_normalizer._link_destination and
    # feed_normalizer._content_link_destination: reject credential-bearing
    # HTTP(S) destinations (fail closed), omit non-web schemes (mailto:,
    # tel:, ...), and fold a non-sensitive fragment back into the identity
    # since canonicalize_url always strips it.
    #
    # A /URI action with no scheme at all (e.g. "/apply-v1") is a relative
    # reference, not a non-web scheme -- this normalizer has no document
    # base to resolve it against, so a destination-only change to a
    # relative URI action must fail closed instead of being silently
    # treated the same as an intentionally-ignored mailto:/tel: link.
    value = uri.strip()
    if not value:
        return ""
    try:
        canonical, _ = canonicalize_url(value)
    except MonitorError:
        try:
            scheme = urlsplit(value).scheme.lower()
        except ValueError:
            scheme = value.partition(":")[0].lower()
        if not scheme:
            msg = "pdf_relative_link_action"
            raise MonitorError(
                msg,
                "PDF link annotation has a relative URI action with no "
                "document base to resolve it against",
            ) from None
        if scheme not in {"http", "https"}:
            return ""
        raise
    fragment = canonicalize_fragment_identity(urlsplit(value).fragment)
    identity = f"{canonical}#{fragment}" if fragment else canonical
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{identity[:MAX_PDF_LINK_URL_CHARS]} [sha256:{digest}]"


def _page_link_destinations(page: object, budget: _PdfLinkBudget) -> list[str]:
    annotations = getattr(page, "annotations", None)
    if not annotations:
        return []
    lines: list[str] = []
    for annotation in annotations:
        if budget.remaining <= 0:
            msg = "pdf_link_limit"
            raise MonitorError(msg, "PDF has too many link annotations")
        budget.remaining -= 1
        obj = _resolve_pdf_object(annotation)
        if not isinstance(obj, dict):
            continue
        obj = cast("dict[str, object]", obj)
        if obj.get("/Subtype") != "/Link":
            continue
        action = _resolve_pdf_object(obj.get("/A"))
        if not isinstance(action, dict):
            continue
        action = cast("dict[str, object]", action)
        if action.get("/S") != "/URI":
            continue
        uri = _resolve_pdf_object(action.get("/URI"))
        if not isinstance(uri, str):
            continue
        destination = _pdf_link_destination(uri)
        if destination:
            lines.append(f"[link: {destination}]")
    return lines


def _load_pdf_reader() -> tuple[Callable[..., PdfReader], type[Exception]]:
    """Load the optional parser only when a PDF actually needs normalization.

    Returns:
        A (``PdfReader`` class, ``PdfReadError`` class) pair.

    Raises:
        MonitorError: If the optional pypdf package is not installed.
    """
    try:
        # Deliberately lazy: pypdf is an optional runtime dependency, only
        # needed once a document has already passed the byte-level PDF
        # validation above, so a page that never reaches this function
        # never pays the import cost or requires the package at all.
        from pypdf import PdfReader  # ruff: ignore[import-outside-top-level]
        from pypdf.errors import PdfReadError  # ruff: ignore[import-outside-top-level]
    except ImportError as exc:
        msg = "pdf_parser_unavailable"
        raise MonitorError(
            msg,
            "PDF normalization requires the optional pypdf package",
        ) from exc
    return PdfReader, PdfReadError


def _construct_pdf_reader(
    pdf: bytes,
    pdf_reader_cls: Callable[..., PdfReader],
    pdf_read_error_cls: type[Exception],
) -> PdfReader:
    """Construct a strict PdfReader over ``pdf``, translating parse errors.

    Returns:
        The constructed reader.

    Raises:
        MonitorError: If ``pdf`` cannot be parsed safely.
    """
    try:
        return pdf_reader_cls(BytesIO(pdf), strict=True)
    except (pdf_read_error_cls, KeyError, TypeError, ValueError, RecursionError) as exc:
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF could not be parsed safely") from exc


def _read_page_text(page: PageObject, pdf_read_error_cls: type[Exception]) -> str:
    """Extract one page's text, translating parse errors.

    Returns:
        The page's extracted text, or ``""`` if pypdf extracted nothing.

    Raises:
        MonitorError: If the page cannot be parsed safely.
    """
    try:
        return page.extract_text() or ""
    except (pdf_read_error_cls, KeyError, TypeError, ValueError, RecursionError) as exc:
        msg = "pdf_malformed"
        raise MonitorError(msg, "PDF could not be parsed safely") from exc


def _extract_font_aware_text(
    pdf: bytes, *, max_pages: int, max_extracted_chars: int
) -> list[str]:
    """Extract text with the PDF's active font encodings and CMaps.

    Returns:
        The extracted text fragments (page lines plus link annotations).

    Raises:
        MonitorError: If the PDF is encrypted, malformed, exceeds the page
            or extracted-text size limit, or has too many link
            annotations.
    """
    pdf_reader_cls, pdf_read_error_cls = _load_pdf_reader()
    reader = _construct_pdf_reader(pdf, pdf_reader_cls, pdf_read_error_cls)
    if reader.is_encrypted:
        msg = "pdf_encrypted"
        raise MonitorError(msg, "encrypted PDFs are not supported")
    if len(reader.pages) > max_pages:
        msg = "pdf_page_limit"
        raise MonitorError(msg, "PDF page count exceeds the limit")

    fragments: list[str] = []
    extracted_chars = 0
    link_budget = _PdfLinkBudget(MAX_PDF_LINK_ANNOTATIONS)
    for page in reader.pages:
        page_text = _read_page_text(page, pdf_read_error_cls)
        extracted_chars += len(page_text)
        if extracted_chars > max_extracted_chars:
            msg = "pdf_extracted_too_large"
            raise MonitorError(
                msg,
                "PDF extracted text exceeds the size limit",
            )
        fragments.extend(page_text.splitlines())
        fragments.extend(_page_link_destinations(page, link_budget))
    return fragments


def extract_pdf_text(
    pdf: bytes,
    *,
    max_input_bytes: int = 10_000_000,
    max_decompressed_bytes: int = 20_000_000,
    max_objects: int = 20_000,
    max_pages: int = 1_000,
) -> tuple[str, dict[str, str]]:
    """Extract and normalize bounded, font-aware text from a text-based PDF.

    Returns:
        A (normalized text, metadata) tuple, with the metadata carrying
        any Title/Author/Subject document info found.

    Raises:
        MonitorError: If the PDF is invalid, oversized, encrypted, or
            contains no extractable text.
    """
    if len(pdf) > max_input_bytes:
        msg = "response_too_large"
        raise MonitorError(msg, "PDF exceeds the input size limit")
    if not pdf.startswith(b"%PDF-"):
        msg = "pdf_malformed"
        raise MonitorError(msg, "document has no PDF signature")
    if b"/Encrypt" in pdf:
        msg = "pdf_encrypted"
        raise MonitorError(msg, "encrypted PDFs are not supported")
    if len(re.findall(rb"\bobj\b", pdf)) > max_objects:
        msg = "pdf_object_limit"
        raise MonitorError(msg, "PDF object count exceeds the limit")

    # Validate every text/content stream and bound its aggregate decoded size
    # before pypdf sees it. Image XObjects are skipped because pypdf's text
    # extractor does not decode them; their encoded bytes are still covered by
    # the whole-document input cap. Every accepted PDF then uses pypdf so
    # active fonts, inherited resources, and CMaps determine its text.
    has_image_stream = _validate_streams(pdf, max_decompressed_bytes)
    fragments = _extract_font_aware_text(
        pdf,
        max_pages=max_pages,
        max_extracted_chars=max_decompressed_bytes,
    )
    lines = [
        re.sub(r"\s+", " ", unicodedata.normalize("NFKC", fragment)).strip()
        for fragment in fragments
    ]
    lines = [line for line in lines if line]
    if not lines:
        if has_image_stream:
            msg = "pdf_image_only"
            raise MonitorError(msg, "image-only PDFs are not supported")
        msg = "pdf_no_text"
        raise MonitorError(msg, "PDF contains no extractable text")

    metadata: dict[str, str] = {}
    for match in METADATA_RE.finditer(pdf[: min(len(pdf), 2_000_000)]):
        full = match.group(0)
        key = match.group(1).decode("ascii").lower()
        start = full.find(b"(")
        metadata[key] = _decode_literal(full[start:])[:500]
    return "\n".join(lines), metadata
