"""Bounded, font-aware text extraction for text-based PDFs."""

from __future__ import annotations

import bisect
import hashlib
import re
import unicodedata
import zlib
from io import BytesIO
from urllib.parse import urlsplit

from errors import MonitorError
from network_policy import canonicalize_fragment_identity, canonicalize_url

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


def _decode_name(name: bytes) -> bytes:
    """Decode valid PDF name escapes in one lexical name token."""
    if not name.startswith(b"/"):
        raise MonitorError("pdf_malformed", "PDF name token has no slash prefix")
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
            while position < len(data) and data[position] not in (ord("\r"), ord("\n")):
                position += 1
            continue
        break
    return position


def _read_name(data: bytes, position: int) -> tuple[bytes, int]:
    if position >= len(data) or data[position] != ord("/"):
        raise MonitorError("pdf_malformed", "PDF dictionary expects a name")
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
    raise MonitorError("pdf_malformed", "PDF dictionary has an unterminated string")


def _skip_hex_string(data: bytes, position: int) -> int:
    position += 1
    while position < len(data) and data[position] != ord(">"):
        position += 1
    if position >= len(data):
        raise MonitorError(
            "pdf_malformed", "PDF dictionary has an unterminated hex string"
        )
    return position + 1


def _read_bare_token(data: bytes, position: int) -> tuple[bytes, int]:
    end = position
    while end < len(data) and data[end] not in _NAME_TERMINATORS:
        end += 1
    if end == position:
        raise MonitorError("pdf_malformed", "PDF dictionary has an invalid value")
    return data[position:end], end


def _skip_pdf_object(data: bytes, position: int, nesting: int = 0) -> int:
    """Skip one direct PDF object, including an indirect integer reference."""
    position = _skip_whitespace_and_comments(data, position)
    if position >= len(data):
        raise MonitorError("pdf_malformed", "PDF dictionary has a missing value")
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
        raise MonitorError("pdf_malformed", "PDF dictionary has an invalid value")

    first, end = _read_bare_token(data, position)
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


def _skip_pdf_array(data: bytes, position: int, nesting: int) -> int:
    if nesting > MAX_PDF_DICTIONARY_NESTING:
        raise MonitorError("pdf_malformed", "PDF dictionary nesting is too deep")
    position += 1
    while True:
        position = _skip_whitespace_and_comments(data, position)
        if position >= len(data):
            raise MonitorError(
                "pdf_malformed", "PDF dictionary has an unterminated array"
            )
        if data[position] == ord("]"):
            return position + 1
        position = _skip_pdf_object(data, position, nesting)


def _skip_pdf_dictionary(data: bytes, position: int, nesting: int) -> int:
    if nesting > MAX_PDF_DICTIONARY_NESTING:
        raise MonitorError("pdf_malformed", "PDF dictionary nesting is too deep")
    position += 2
    while True:
        position = _skip_whitespace_and_comments(data, position)
        if position >= len(data):
            raise MonitorError("pdf_malformed", "PDF dictionary is unterminated")
        if data[position : position + 2] == b">>":
            return position + 2
        _, position = _read_name(data, position)
        position = _skip_pdf_object(data, position, nesting)


def _filter_value(dictionary: bytes, position: int) -> tuple[list[bytes], int]:
    """Parse the direct name or name array allowed for a /Filter value."""
    position = _skip_whitespace_and_comments(dictionary, position)
    if position >= len(dictionary):
        raise MonitorError("pdf_malformed", "PDF stream dictionary has no filter value")
    if dictionary[position] == ord("/"):
        name, end = _read_name(dictionary, position)
        return [name], end
    if dictionary[position] != ord("["):
        raise MonitorError("pdf_malformed", "PDF filter value is not a name or array")

    filters: list[bytes] = []
    position += 1
    while True:
        position = _skip_whitespace_and_comments(dictionary, position)
        if position >= len(dictionary):
            raise MonitorError("pdf_malformed", "PDF filter array is unterminated")
        if dictionary[position] == ord("]"):
            return filters, position + 1
        name, position = _read_name(dictionary, position)
        filters.append(name)


def _stream_filters(dictionary: bytes) -> list[bytes]:
    position = _skip_whitespace_and_comments(dictionary, 0)
    if dictionary[position : position + 2] != b"<<":
        raise MonitorError("pdf_malformed", "PDF stream dictionary could not be parsed")
    position += 2
    filters: list[bytes] | None = None
    while True:
        position = _skip_whitespace_and_comments(dictionary, position)
        if position >= len(dictionary):
            raise MonitorError("pdf_malformed", "PDF stream dictionary is unterminated")
        if dictionary[position : position + 2] == b">>":
            return filters or []
        name, position = _read_name(dictionary, position)
        if name == b"/Filter":
            if filters is not None:
                raise MonitorError(
                    "pdf_malformed", "PDF stream dictionary repeats /Filter"
                )
            filters, position = _filter_value(dictionary, position)
            continue
        position = _skip_pdf_object(dictionary, position)


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


def _validate_streams(pdf: bytes, limit: int) -> bool:
    """Bound every non-image stream before passing the PDF to pypdf."""
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


class _PdfLinkBudget:
    __slots__ = ("remaining",)

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining


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
            raise MonitorError("pdf_link_limit", "PDF has too many link annotations")
        budget.remaining -= 1
        obj = _resolve_pdf_object(annotation)
        if not isinstance(obj, dict) or obj.get("/Subtype") != "/Link":
            continue
        action = _resolve_pdf_object(obj.get("/A"))
        if not isinstance(action, dict) or action.get("/S") != "/URI":
            continue
        uri = _resolve_pdf_object(action.get("/URI"))
        if not isinstance(uri, str):
            continue
        destination = _pdf_link_destination(uri)
        if destination:
            lines.append(f"[link: {destination}]")
    return lines


def _load_pdf_reader() -> tuple[object, type[Exception]]:
    """Load the optional parser only when a PDF actually needs normalization."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise MonitorError(
            "pdf_parser_unavailable",
            "PDF normalization requires the optional pypdf package",
        ) from exc
    return PdfReader, PdfReadError


def _extract_font_aware_text(
    pdf: bytes, *, max_pages: int, max_extracted_chars: int
) -> list[str]:
    """Extract text with the PDF's active font encodings and CMaps."""
    PdfReader, PdfReadError = _load_pdf_reader()
    try:
        reader = PdfReader(BytesIO(pdf), strict=True)
        if reader.is_encrypted:
            raise MonitorError("pdf_encrypted", "encrypted PDFs are not supported")
        if len(reader.pages) > max_pages:
            raise MonitorError("pdf_page_limit", "PDF page count exceeds the limit")

        fragments: list[str] = []
        extracted_chars = 0
        link_budget = _PdfLinkBudget(MAX_PDF_LINK_ANNOTATIONS)
        for page in reader.pages:
            page_text = page.extract_text() or ""
            extracted_chars += len(page_text)
            if extracted_chars > max_extracted_chars:
                raise MonitorError(
                    "pdf_extracted_too_large",
                    "PDF extracted text exceeds the size limit",
                )
            fragments.extend(page_text.splitlines())
            fragments.extend(_page_link_destinations(page, link_budget))
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
            raise MonitorError("pdf_image_only", "image-only PDFs are not supported")
        raise MonitorError("pdf_no_text", "PDF contains no extractable text")

    metadata: dict[str, str] = {}
    for match in METADATA_RE.finditer(pdf[: min(len(pdf), 2_000_000)]):
        full = match.group(0)
        key = match.group(1).decode("ascii").lower()
        start = full.find(b"(")
        metadata[key] = _decode_literal(full[start:])[:500]
    return "\n".join(lines), metadata
