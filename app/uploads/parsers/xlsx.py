"""Read the first worksheet of an .xlsx file into rows of strings. Stdlib only.

WHY NOT openpyxl
It is not a dependency, and the tax portal's invoice-list export is a plain
single-sheet workbook: shared strings, inline strings and numbers, no formulas
that matter. Reading that directly is ~60 lines and keeps a parser that takes
UNTRUSTED uploads free of a large third-party surface.

SAFETY
An .xlsx is a zip of XML written by whoever uploaded it.
  * Every part read is capped (MAX_PART_BYTES) before it is parsed, so a
    compression bomb inside the workbook cannot exhaust memory.
  * XML is parsed with ElementTree, which never resolves external entities, on
    expat >= 2.4.1, which bounds entity-expansion ("billion laughs") attacks.
  * Rows and columns are capped; a sheet claiming a column like XFD1048576 is
    rejected rather than allocated.
"""
from __future__ import annotations

import re
import defusedxml.ElementTree as ET
import zipfile
from io import BytesIO

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_CELL_REF = re.compile(r"^([A-Z]{1,3})\d+$")

MAX_PART_BYTES = 40 * 1024 * 1024
MAX_ROWS = 50_000
MAX_COLUMNS = 64


class XlsxError(ValueError):
    """The file is not a workbook this reader can safely read."""


def _read_part(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_PART_BYTES:
        raise XlsxError(f"Workbook part {name} is too large")
    with archive.open(info) as handle:
        data = handle.read(MAX_PART_BYTES + 1)
    if len(data) > MAX_PART_BYTES:
        raise XlsxError(f"Workbook part {name} is too large")
    return data


def _column_index(ref: str) -> int:
    match = _CELL_REF.match(ref)
    if not match:
        raise XlsxError(f"Unreadable cell reference {ref!r}")
    index = 0
    for ch in match.group(1):
        index = index * 26 + (ord(ch) - 64)
    if index > MAX_COLUMNS:
        raise XlsxError("Worksheet is wider than any invoice listing")
    return index - 1


def _text(node: ET.Element) -> str:
    return "".join(t.text or "" for t in node.iter(f"{_NS}t"))


def read_first_sheet(raw: bytes) -> list[list[str | None]]:
    """Rows of the first worksheet, each padded to its last non-empty cell."""
    try:
        archive = zipfile.ZipFile(BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise XlsxError("Not an .xlsx workbook") from exc

    with archive:
        names = archive.namelist()
        sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not sheets:
            raise XlsxError("Workbook has no worksheet")

        try:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(_read_part(archive, "xl/sharedStrings.xml"))
                shared = [_text(si) for si in root.findall(f"{_NS}si")]
            sheet = ET.fromstring(_read_part(archive, sheets[0]))
        except ET.ParseError as exc:
            raise XlsxError("Workbook XML is malformed") from exc

    rows: list[list[str | None]] = []
    for row in sheet.iter(f"{_NS}row"):
        if len(rows) >= MAX_ROWS:
            raise XlsxError("Worksheet has more rows than any monthly listing")
        cells: dict[int, str | None] = {}
        for cell in row.findall(f"{_NS}c"):
            ref = cell.get("r")
            if ref is None:
                continue
            kind = cell.get("t")
            value_node = cell.find(f"{_NS}v")
            if kind == "inlineStr":
                value: str | None = _text(cell)
            elif value_node is None or value_node.text is None:
                value = None
            elif kind == "s":
                try:
                    value = shared[int(value_node.text)]
                except (ValueError, IndexError) as exc:
                    raise XlsxError("Shared-string index out of range") from exc
            else:
                value = value_node.text
            cells[_column_index(ref)] = value
        if cells:
            rows.append([cells.get(i) for i in range(max(cells) + 1)])
    return rows
