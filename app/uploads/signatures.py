"""Does an upload's content match its extension? (compliance audit SEC-04)

The presign step checks the filename's extension and the declared content
type, both of which the client chooses. Neither says what the bytes are: an
executable renamed `report.pdf` passed. At confirm the first bytes of every
upload are read back from storage (a ranged GET, PREFIX_LENGTH bytes) and
checked against the signature its extension promises. A mismatch is refused
and the object deleted.
"""
PREFIX_LENGTH = 1024

_ZIP_LOCAL_HEADER = b"PK\x03\x04"
_UTF8_BOM = b"\xef\xbb\xbf"


def _is_pdf(head: bytes) -> bool:
    # Readers accept leading junk before the header within the first 1 KB,
    # and some scanners and exporters write it; requiring offset 0 would
    # refuse real statements.
    return b"%PDF-" in head[:PREFIX_LENGTH]


def _is_zip(head: bytes) -> bool:
    # A local file header first. An empty archive (PK\x05\x06) holds nothing
    # to read, and a self-extracting archive starts with an executable.
    return head.startswith(_ZIP_LOCAL_HEADER)


def _is_xml(head: bytes) -> bool:
    return head.removeprefix(_UTF8_BOM).lstrip().startswith(b"<")


def _is_text(head: bytes) -> bool:
    return b"\x00" not in head


_CHECKS = {
    "pdf": _is_pdf,
    "zip": _is_zip,
    "xlsx": _is_zip,  # an Office Open XML workbook is a zip
    "xml": _is_xml,
    "csv": _is_text,
}


def signature_matches(extension: str, head: bytes) -> bool:
    """True when `head` (an upload's first bytes) fits `extension`. An
    extension with no known signature is refused, not waved through."""
    check = _CHECKS.get(extension.lower())
    return bool(check and head and check(head))
