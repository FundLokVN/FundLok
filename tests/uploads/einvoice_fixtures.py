"""Synthetic tax-portal invoice listings, shaped like the real .xlsx export.

Real exports carry third parties' names and tax codes, so none are committed;
these reproduce the structure the parser depends on: a title row, a
"Từ ngày … đến ngày …" period row, the portal's exact header row, then one
row per invoice.
"""
from __future__ import annotations

import calendar
import io
import zipfile
from dataclasses import dataclass
from xml.sax.saxutils import escape

SELLER = "0312345678"

HEADER = [
    "STT", "Ký hiệu mẫu số", "Ký hiệu hóa đơn", "Số hóa đơn", "Ngày lập",
    "MST người bán/MST người xuất hàng", "Tên người bán/Tên người xuất hàng",
    "MST người mua/MST người nhận hàng", "Tên người mua/Tên người nhận hàng",
    "Địa chỉ người mua", "Tổng tiền chưa thuế", "Tổng tiền thuế",
    "Tổng tiền chiết khấu thương mại", "Tổng tiền phí", "Tổng tiền thanh toán",
    "Đơn vị tiền tệ", "Tỷ giá", "Trạng thái hóa đơn", "Kết quả kiểm tra hóa đơn",
]


@dataclass
class Inv:
    number: int
    buyer: str
    amount: int
    status: str = "Hóa đơn mới"
    day: int = 15
    series: str = "C26TAA"
    buyer_name: str = ""
    currency: str = "VND"
    rate: str = "1.0"
    seller: str = SELLER


def _col(i: int) -> str:
    name = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        name = chr(65 + r) + name
    return name


def _sheet(rows: list[list[object]]) -> str:
    out = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            if value is None or value == "":
                continue
            ref = f"{_col(c)}{r}"
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        out.append(f'<row r="{r}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(out)}</sheetData></worksheet>'
    )


def listing_xlsx(year: int, month: int, invoices: list[Inv], *, period: str | None = None) -> bytes:
    last = calendar.monthrange(year, month)[1]
    period = period or f"Từ ngày 01/{month:02d}/{year} đến ngày {last:02d}/{month:02d}/{year}"
    rows: list[list[object]] = [["DANH SÁCH HÓA ĐƠN"], [period], HEADER]
    for n, inv in enumerate(invoices, start=1):
        rows.append([
            float(n), "1", inv.series, float(inv.number), f"{inv.day:02d}/{month:02d}/{year}",
            inv.seller, "CÔNG TY TNHH MẪU", inv.buyer, inv.buyer_name or f"KHÁCH {inv.buyer or 'LẺ'}",
            "Địa chỉ", float(inv.amount), float(inv.amount) * 0.08, 0.0, "", float(inv.amount) * 1.08,
            inv.currency, inv.rate, inv.status, "Đã cấp mã hóa đơn",
        ])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", "<workbook/>")
        z.writestr("xl/worksheets/sheet1.xml", _sheet(rows))
    return buf.getvalue()


def archive(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def year_of_listings(start_year: int = 2025, start_month: int = 9, months: int = 12,
                     buyers: tuple[str, ...] = ("0100000001", "0100000002", "0100000003", "0100000004")) -> dict[str, bytes]:
    """`months` consecutive listings, numbering continuous per calendar-year series."""
    files, number, series_year = {}, {}, None
    y, m = start_year, start_month
    for i in range(months):
        series = f"C{str(y)[2:]}TAA"
        start = number.get(series, 0)
        invs = [Inv(start + k + 1, buyers[k % len(buyers)], 1_000_000 * (k + 1), series=series) for k in range(4)]
        number[series] = start + 4
        files[f"Einvoice Data/HDT{i + 1}.xlsx"] = listing_xlsx(y, m, invs)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return files
