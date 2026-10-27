#!/usr/bin/env python3
"""Seed OPEN marketplace listings on a LOCAL stack, through the real API.

Each project walks the same path an SME and an operator take in the app:

  SME:    POST /projects (+ inline DRAFT application) -> figures -> the five
          wizard documents (init-upload, PUT to storage, confirm) -> submit
  Admin:  financials -> score run -> lock it -> APPROVED decision, which makes
          the project ACTIVE (what /projects/public, the investor marketplace,
          lists)
  SME:    KYC_ID / KYC_ADDRESS / KYC_BUSINESS_REG files (/files/presign)
  Admin:  approve those three (T15, the listing gate) -> contract -> listing

The only write that skips the API is promoting the seed admin to ADMIN: no
endpoint can create the first admin (POST /admin/users/admins needs one).

The documents are synthetic, shaped like the fixtures in tests/uploads/ so the
confirm-time parsers accept them. None of them describe a real company.

Accounts the script creates (and the admin's TOTP secret, needed to sign in to
/admin) are written to `.env.seed-accounts`, gitignored by `.env.*`, and reused
on the next run. Re-running adds more projects; it never deletes anything.

Usage (API on :8000, compose Postgres, MinIO and Mailpit up):

  .venv/bin/python scripts/seed_open_listings.py [--count 3]
  .venv/bin/python scripts/seed_open_listings.py --system-admin   # a SYSTEM_ADMIN too
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import io
import random
import re
import secrets
import string
import sys
import time
import zipfile
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import pyotp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402

API = "http://127.0.0.1:8000"
MAILPIT = "http://localhost:8025"
ACCOUNTS_FILE = ROOT / ".env.seed-accounts"

PROJECTS = [
    {
        "legal_name": "Công ty TNHH Cà Phê Sương Mai",
        "industry": "Food & Beverage",
        "employee_count": 8,
        "city": "Đà Lạt",
        "street": "12 Đường Trần Phú",
        "requested_amount": 60_000_000,
        "duration_months": 3,
        "purpose": "Mua máy rang và nhập vụ cà phê mới",
        "monthly_revenue": 120_000_000,
    },
    {
        "legal_name": "Công ty TNHH Giải Pháp Phần Mềm Bến Thành",
        "industry": "IT Services",
        "employee_count": 15,
        "city": "Hồ Chí Minh",
        "street": "45 Đường Lê Lợi, Quận 1",
        "requested_amount": 80_000_000,
        "duration_months": 4,
        "purpose": "Vốn lưu động cho hợp đồng triển khai phần mềm",
        "monthly_revenue": 150_000_000,
    },
    {
        "legal_name": "Công ty TNHH Vận Tải Sông Hàn",
        "industry": "Logistics & Transport",
        "employee_count": 22,
        "city": "Đà Nẵng",
        "street": "88 Đường Bạch Đằng",
        "requested_amount": 100_000_000,
        "duration_months": 6,
        "purpose": "Bảo dưỡng đội xe và chi phí nhiên liệu mùa cao điểm",
        "monthly_revenue": 200_000_000,
    },
]


# --------------------------------------------------------------------------- #
# Synthetic documents
# --------------------------------------------------------------------------- #


def _months_back(n: int, today: date) -> list[tuple[int, int]]:
    """The `n` whole months before `today`'s, oldest first."""
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        out.append((y, m))
    return out[::-1]


def text_pdf(lines: list[str]) -> bytes:
    """A one-page PDF whose text layer reads back as `lines`, Vietnamese included.

    No PDF library ships with the app, and the base-14 fonts cannot encode
    letters like "Ụ". So every distinct character gets a one-byte code in a
    monospaced base font, and a ToUnicode map tells text extraction what each
    code means. Viewers draw the accented letters wrongly; pypdf, which is all
    the CIC parser uses, reads them exactly.
    """
    chars = sorted({c for line in lines for c in line} - {" "})
    if len(chars) > 223:
        raise ValueError("too many distinct characters for a one-byte font")
    code = {" ": 0x20, **{c: 0x21 + i for i, c in enumerate(chars)}}

    ops = ["BT", "/F1 8 Tf", "10 TL", "20 820 Td"]
    for line in lines:
        ops.append("<" + "".join(f"{code[c]:02X}" for c in line) + "> Tj T*")
    ops.append("ET")
    content = "\n".join(ops).encode("ascii")

    entries = [f"<{code[c]:02X}> <{ord(c):04X}>" for c in [" ", *chars]]
    blocks = []
    for i in range(0, len(entries), 100):
        chunk = entries[i:i + 100]
        blocks.append(f"{len(chunk)} beginbfchar\n" + "\n".join(chunk) + "\nendbfchar")
    cmap = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        + "\n".join(blocks)
        + "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
    ).encode("ascii")

    last = 0x20 + len(chars)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        (
            f"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /FirstChar 32 /LastChar {last} "
            f"/Widths [{' '.join(['600'] * (last - 31))}] /ToUnicode 6 0 R >>"
        ).encode("ascii"),
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % n + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


def cic_report_pdf(legal_name: str, tax_id: str, today: date) -> bytes:
    """A business CIC report: the phrases the parser keys on, score and rank."""
    day = today.strftime("%d/%m/%Y")
    return text_pdf([
        "TRUNG TÂM THÔNG TIN TÍN DỤNG QUỐC GIA VIỆT NAM - CIC",
        "Web: https://cic.gov.vn",
        f"Số: {today.year}/SEED",
        "THÔNG TIN TÍN DỤNG KHÁCH HÀNG VAY DOANH NGHIỆP",
        f"Ngày hỏi tin: {day} 09:00:00",
        f"Tên doanh nghiệp: {legal_name.upper()}",
        f"Mã số thuế: {tax_id}",
        "2.2. Thông tin lịch sử nợ cần chú ý và nợ xấu",
        "Khách hàng không có lịch sử nợ xấu, nợ cần chú ý tại các TCTD",
        "Bảo đảm tiền vay bằng tài sản   Không có",
        "II. THÔNG TIN ĐIỂM TÍN DỤNG",
        "Điểm tín dụng:",
        "650",
        "Hạng:",
        "1",
        "Ngày chấm điểm:",
        day,
        "Điểm 403-429 430-454 455-479 480-544 545-571 572-587 588-605 606-621 622-644 645-706",
    ])


_EINVOICE_HEADER = [
    "STT", "Ký hiệu mẫu số", "Ký hiệu hóa đơn", "Số hóa đơn", "Ngày lập",
    "MST người bán/MST người xuất hàng", "Tên người bán/Tên người xuất hàng",
    "MST người mua/MST người nhận hàng", "Tên người mua/Tên người nhận hàng",
    "Địa chỉ người mua", "Tổng tiền chưa thuế", "Tổng tiền thuế",
    "Tổng tiền chiết khấu thương mại", "Tổng tiền phí", "Tổng tiền thanh toán",
    "Đơn vị tiền tệ", "Tỷ giá", "Trạng thái hóa đơn", "Kết quả kiểm tra hóa đơn",
]


def _xlsx_col(i: int) -> str:
    name, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        name = chr(65 + r) + name
    return name


def _listing_xlsx(year: int, month: int, rows: list[list[object]]) -> bytes:
    """One month of the tax portal's invoice-list export (tests/uploads/einvoice_fixtures.py)."""
    last = calendar.monthrange(year, month)[1]
    table = [["DANH SÁCH HÓA ĐƠN"], [f"Từ ngày 01/{month:02d}/{year} đến ngày {last:02d}/{month:02d}/{year}"],
             _EINVOICE_HEADER, *rows]
    xml_rows = []
    for r, row in enumerate(table, start=1):
        cells = []
        for c, value in enumerate(row):
            if value is None or value == "":
                continue
            ref = f"{_xlsx_col(c)}{r}"
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    )
    return _zip({"[Content_Types].xml": "<Types/>", "xl/workbook.xml": "<workbook/>",
                 "xl/worksheets/sheet1.xml": sheet})


def einvoice_zip(tax_id: str, legal_name: str, monthly_revenue: int, today: date) -> bytes:
    """Twelve months of sales invoices, four buyers a month."""
    buyers = ("0100000001", "0100000002", "0100000003", "0100000004")
    shares = (0.4, 0.3, 0.2, 0.1)
    files, numbers = {}, {}
    for i, (y, m) in enumerate(_months_back(12, today), start=1):
        series = f"C{str(y)[2:]}TAA"
        rows = []
        for k, (buyer, share) in enumerate(zip(buyers, shares), start=1):
            numbers[series] = numbers.get(series, 0) + 1
            amount = float(round(monthly_revenue * share))
            rows.append([
                float(k), "1", series, float(numbers[series]), f"{5 + 5 * k:02d}/{m:02d}/{y}",
                tax_id, legal_name.upper(), buyer, f"KHÁCH HÀNG {buyer[-1]}", "Địa chỉ",
                amount, amount * 0.08, 0.0, "", amount * 1.08, "VND", "1.0",
                "Hóa đơn mới", "Đã cấp mã hóa đơn",
            ])
        files[f"Einvoice Data/HDT{i}.xlsx"] = _listing_xlsx(y, m, rows)
    return _zip(files)


def tax_filings_zip(tax_id: str, legal_name: str, monthly_revenue: int, today: date) -> bytes:
    """Last year's TT133 statements plus 24 signed monthly VAT declarations
    (tests/uploads/test_tax_filings.py, test_vat_parser.py)."""
    year = today.year - 1
    revenue_y = monthly_revenue * 12
    cogs, combined, net = int(revenue_y * 0.45), int(revenue_y * 0.15), int(revenue_y * 0.12)
    statements = (
        '<HSoThueDTu xmlns="http://kekhaithue.gdt.gov.vn/TKhaiThue"><HSoKhaiThue><TTinChung><TTinTKhaiThue>'
        f"<TKhaiThue><maTKhai>683</maTKhai><kyKKhai>{year}</kyKKhai></TKhaiThue>"
        f"<NNT><mst>{tax_id}</mst><tenNNT>{escape(legal_name.upper())}</tenNNT></NNT></TTinTKhaiThue></TTinChung>"
        "<PLuc><PL_KQHDSXKD><ThuyetMinh><ct01/></ThuyetMinh>"
        f"<NamNay><ct10>{revenue_y}</ct10><ct11>{cogs}</ct11><ct22>0</ct22><ct23>0</ct23>"
        f"<ct60>{net}</ct60><ct24>{combined}</ct24></NamNay>"
        "<NamTruoc><ct10>1</ct10><ct11>1</ct11></NamTruoc></PL_KQHDSXKD>"
        "<PL_LCTTTT><NamNay><ct35>0</ct35></NamNay></PL_LCTTTT></PLuc>"
        "</HSoKhaiThue><Signature>stub</Signature></HSoThueDTu>"
    )
    files: dict[str, bytes | str] = {f"BCTC_{year}.xml": statements}
    for y, m in _months_back(24, today):
        vat = (
            "<HSoThueDTu><TKhaiThue><TTinChung><TTinTKhaiThue>"
            f"<TKhaiThue><maTKhai>842</maTKhai><kyKKhai>{m:02d}/{y}</kyKKhai></TKhaiThue>"
            f"<NNT><mst>{tax_id}</mst><tenNNT>{escape(legal_name.upper())}</tenNNT></NNT>"
            "</TTinTKhaiThue></TTinChung><CTieuTKhaiChinh>"
            f"<ct29>0</ct29><ct32>{monthly_revenue}</ct32><ct34>{monthly_revenue}</ct34>"
            "</CTieuTKhaiChinh></TKhaiThue><Signature>stub</Signature></HSoThueDTu>"
        )
        files[f"24 months running VAT/{m}.{y}.zip"] = _zip({"files_113.xml": vat})
    return _zip(files)


def _zip(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def wizard_documents(spec: dict, tax_id: str, today: date) -> dict[str, tuple[str, str, bytes]]:
    """document_type -> (filename, content_type, bytes), the five the decision requires."""
    name = spec["legal_name"]
    return {
        "legal_charter": ("dieu-le-cong-ty.pdf", "application/pdf",
                          text_pdf([f"ĐIỀU LỆ CÔNG TY - {name}", f"MST {tax_id}", "Seed document, not a real charter."])),
        "business_registration": ("giay-dkkd.pdf", "application/pdf",
                                  text_pdf([f"GIẤY CHỨNG NHẬN ĐĂNG KÝ DOANH NGHIỆP - {name}", f"MST {tax_id}",
                                            "Seed document, not a real registration."])),
        "e_invoice_data": ("Einvoice Data.zip", "application/zip",
                           einvoice_zip(tax_id, name, spec["monthly_revenue"], today)),
        "tax_filings": ("To khai thue.zip", "application/zip",
                        tax_filings_zip(tax_id, name, spec["monthly_revenue"], today)),
        "cic_report": ("cic.pdf", "application/pdf", cic_report_pdf(name, tax_id, today)),
    }


def financials_for(spec: dict) -> dict:
    """What the operator types on the underwriting screen. Matches the uploads
    and grades APPROVED (tests/underwriting/test_score_run_persistence.py)."""
    revenue = spec["monthly_revenue"]
    return {
        "industry": spec["industry"],
        "company_size": "micro" if spec["employee_count"] <= 10 else "small",
        "duration_months": spec["duration_months"],
        "operating_months": 36,
        "monthly_revenue_vnd": [revenue] * 24,
        "cogs_y1_vnd": int(revenue * 12 * 0.45),
        "fixed_cost_y1_vnd": int(revenue * 12 * 0.15),
        "variable_cost_excl_cogs_y1_vnd": int(revenue * 12 * 0.05),
        "conc_top1_pct": 40.0,
        "conc_top3_pct": 90.0,
        "crr": 60.0,
        "rri": 60.0,
        "tcp": 60.0,
        "owner_withdrawal": 50.0,
        "cic_score": 650,
        "kyc_aml_passed": True,
        "fraud_flags": [],
    }


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #


def _password() -> str:
    # Random, so it cannot be in a breach corpus (the HIBP check at sign-up).
    body = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(14))
    return f"Fl{body}9!"


def _load_accounts() -> dict[str, str]:
    if not ACCOUNTS_FILE.exists():
        return {}
    out = {}
    for line in ACCOUNTS_FILE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _save_accounts(accounts: dict[str, str]) -> None:
    header = (
        "# Local test accounts made by scripts/seed_open_listings.py. Gitignored (.env.*).\n"
        "# Admins (SEED_ADMIN_*, SEED_SYSTEM_ADMIN_*) sign in with the password, then a code from the TOTP secret\n"
        "# (add it to an authenticator app, or: .venv/bin/python -c \"import pyotp; print(pyotp.TOTP('<secret>').now())\").\n"
    )
    ACCOUNTS_FILE.write_text(header + "".join(f"{k}={v}\n" for k, v in accounts.items()))


def _verification_token(email: str, timeout: float = 20.0) -> str:
    """The token from the verification email Mailpit caught for `email`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = httpx.get(f"{MAILPIT}/api/v1/search", params={"query": f'to:"{email}"'}).json()
        for msg in found.get("messages", []):
            body = httpx.get(f"{MAILPIT}/api/v1/message/{msg['ID']}").json()
            m = re.search(r"verify-email\?token=([A-Za-z0-9._\-]+)", body.get("Text", "") + body.get("HTML", ""))
            if m:
                return m.group(1)
        time.sleep(1)
    raise RuntimeError(f"no verification email for {email} in Mailpit")


def _register_and_verify(email: str, password: str, full_name: str) -> None:
    r = httpx.post(f"{API}/auth/register", json={"email": email, "password": password, "full_name": full_name})
    _ok(r, f"register {email}")
    r = httpx.get(f"{API}/auth/verify-email", params={"token": _verification_token(email)})
    _ok(r, f"verify {email}")


async def _promote(email: str, role: str) -> None:
    """The one direct write: no endpoint can make the first ADMIN, and a
    SYSTEM_ADMIN is provisioned out of band by design (app/admin/schemas.py)."""
    import asyncpg

    dsn = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("UPDATE users SET role = $1 WHERE email = $2", role, email)
    finally:
        await conn.close()


def _totp_code(secret: str, not_this: str | None = None) -> str:
    """A current code, waiting out the 30s step when `not_this` was just spent
    (a code is single-use)."""
    totp = pyotp.TOTP(secret)
    code = totp.now()
    while code == not_this:
        time.sleep(1)
        code = totp.now()
    return code


def admin_session(accounts: dict[str, str], role: str = "ADMIN") -> httpx.Client:
    """A 2FA session for the seed ADMIN, or the seed SYSTEM_ADMIN. They are
    separate accounts: neither role passes the other's require_roles checks
    (underwriting, contracts and listings are ADMIN only)."""
    prefix = "SEED_ADMIN" if role == "ADMIN" else "SEED_SYSTEM_ADMIN"
    email = accounts.get(f"{prefix}_EMAIL")
    if not email:
        slug = prefix.removeprefix("SEED_").lower().replace("_", "-")
        email, password = f"seed-{slug}-{secrets.token_hex(3)}@example.com", _password()
        _register_and_verify(email, password, "Seed System Admin" if role == "SYSTEM_ADMIN" else "Seed Admin")
        asyncio.run(_promote(email, role))
        accounts.update({f"{prefix}_EMAIL": email, f"{prefix}_PASSWORD": password})
        _save_accounts(accounts)

    client = httpx.Client(base_url=API, timeout=60)
    login = {"email": email, "password": accounts[f"{prefix}_PASSWORD"]}
    spent = None
    if not accounts.get(f"{prefix}_TOTP_SECRET"):
        # A password-only admin session may enrol a first factor and nothing else.
        _ok(client.post("/auth/login", json=login), f"{role} login (enrol)")
        secret = _ok(client.post("/auth/2fa/setup", json={"password": login["password"]}), "2fa setup")["secret"]
        spent = _totp_code(secret)
        _ok(client.post("/auth/2fa/enable", json={"code": spent}), "2fa enable")
        accounts[f"{prefix}_TOTP_SECRET"] = secret
        _save_accounts(accounts)
        client.cookies.clear()

    challenge = _ok(client.post("/auth/login", json=login), f"{role} login")
    code = _totp_code(accounts[f"{prefix}_TOTP_SECRET"], not_this=spent)
    _ok(client.post("/auth/login/2fa", json={"challenge_token": challenge["challenge_token"], "code": code}),
        f"{role} 2fa login")
    return client


def sme_session(accounts: dict[str, str]) -> httpx.Client:
    email = accounts.get("SEED_SME_EMAIL")
    fresh = not email
    if fresh:
        email, password = f"seed-sme-{secrets.token_hex(3)}@example.com", _password()
        _register_and_verify(email, password, "Seed SME Owner")
        accounts.update(SEED_SME_EMAIL=email, SEED_SME_PASSWORD=password)
        _save_accounts(accounts)

    client = httpx.Client(base_url=API, timeout=60)
    _ok(client.post("/auth/login", json={"email": email, "password": accounts["SEED_SME_PASSWORD"]}), "sme login")
    if fresh:
        _ok(client.patch("/users/me/role", json={"role": "SME"}), "sme role")
    return client


def investor_session(accounts: dict[str, str]) -> httpx.Client:
    """An investor to browse the marketplace with. It has no KYC, so placing an
    order is refused until it passes identity verification like any investor."""
    email = accounts.get("SEED_INVESTOR_EMAIL")
    fresh = not email
    if fresh:
        email, password = f"seed-investor-{secrets.token_hex(3)}@example.com", _password()
        _register_and_verify(email, password, "Seed Investor")
        accounts.update(SEED_INVESTOR_EMAIL=email, SEED_INVESTOR_PASSWORD=password)
        _save_accounts(accounts)

    client = httpx.Client(base_url=API, timeout=60)
    _ok(client.post("/auth/login", json={"email": email, "password": accounts["SEED_INVESTOR_PASSWORD"]}),
        "investor login")
    if fresh:
        _ok(client.patch("/users/me/role", json={"role": "INVESTOR"}), "investor role")
    return client


# --------------------------------------------------------------------------- #
# One project, end to end
# --------------------------------------------------------------------------- #


def _ok(resp: httpx.Response, what: str):
    if resp.status_code >= 400:
        raise RuntimeError(f"{what}: HTTP {resp.status_code} {resp.text}")
    return resp.json() if resp.content else None


def seed_project(sme: httpx.Client, admin: httpx.Client, spec: dict, today: date) -> dict:
    tax_id = "03" + "".join(random.choices(string.digits, k=8))
    project = _ok(sme.post("/projects", json={
        "legal_name": spec["legal_name"],
        "tax_id": tax_id,
        "industry": spec["industry"],
        "employee_count": spec["employee_count"],
        "address": {"street": spec["street"], "city": spec["city"], "country": "VN"},
        "incorporation_date": f"{today.year - 3}-03-15",
        "loan_application": {
            "requested_amount": spec["requested_amount"],
            "duration_months": spec["duration_months"],
            "purpose": spec["purpose"],
        },
    }), "create project")
    app_id = project["loan_application"]["id"]

    revenue = spec["monthly_revenue"]
    _ok(sme.put(f"/loans/applications/{app_id}/figures", json={
        "revenue_last_12m": revenue * 12,
        "revenue_prior_12m": revenue * 12,
        "cogs_y1": int(revenue * 12 * 0.45),
        "fixed_cost_y1": int(revenue * 12 * 0.15),
        "variable_cost_excl_cogs_y1": int(revenue * 12 * 0.05),
        "conc_top1_pct": 40.0,
        "conc_top3_pct": 90.0,
    }), "figures")

    keys = []
    for doc_type, (filename, content_type, raw) in wizard_documents(spec, tax_id, today).items():
        init = _ok(sme.post("/uploads/init-upload", json={
            "loan_application_id": app_id, "document_type": doc_type,
            "filename": filename, "content_type": content_type, "size": len(raw),
        }), f"init-upload {doc_type}")
        put = httpx.put(init["upload_url"], content=raw, headers={"Content-Type": content_type})
        if put.status_code >= 400:
            raise RuntimeError(f"storage PUT {doc_type}: HTTP {put.status_code} {put.text[:300]}")
        keys.append(init["file_key"])
    _ok(sme.post("/uploads/confirm", json={"loan_application_id": app_id, "file_keys": keys}), "confirm uploads")
    _ok(sme.post(f"/loans/applications/{app_id}/submit"), "submit")

    _ok(admin.put(f"/underwriting/applications/{app_id}/financials", json=financials_for(spec)), "financials")
    run = _ok(admin.post("/underwriting/score-runs", json={"application_id": app_id}), "score run")
    if run["decision"] in ("REJECT", "INSUFFICIENT_DATA"):
        raise RuntimeError(f"engine decided {run['decision']} for {spec['legal_name']}; adjust financials_for()")
    _ok(admin.post(f"/underwriting/score-runs/{run['id']}/approve"), "lock score run")
    _ok(admin.post(f"/admin/applications/{app_id}/decision",
                   json={"decision": "APPROVED", "note": "Seed data: approved for local testing."}), "decision")

    for purpose in ("KYC_ID", "KYC_ADDRESS", "KYC_BUSINESS_REG"):
        doc = _ok(sme.post("/files/presign", json={
            "business_id": project["id"], "purpose": purpose,
            "filename": f"{purpose.lower()}.pdf", "mime_type": "application/pdf",
        }), f"presign {purpose}")
        _ok(admin.post(f"/lending/documents/{doc['file_id']}/approve",
                       json={"rationale": "Seed data: approved for local testing."}), f"approve {purpose}")

    contract = _ok(admin.post("/contracts/", json={"application_id": app_id, "score_run_id": run["id"]}), "contract")
    listing = _ok(admin.post("/market/listings", json={
        "contract_id": contract["id"],
        "target_amount": spec["requested_amount"],
        "min_ticket": 1_000_000,
    }), "listing")
    return {
        "name": spec["legal_name"],
        "project_id": project["id"],
        "application_id": app_id,
        "grade": (run.get("grade") or {}).get("display"),
        "rate_pct": (run.get("pricing") or {}).get("interest_rate_pct"),
        "listing_id": listing["id"],
        "listing_status": listing["status"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=len(PROJECTS), help="projects to seed (default 3)")
    parser.add_argument("--system-admin", action="store_true",
                        help="only make (or check) the seed SYSTEM_ADMIN account; seed no projects")
    args = parser.parse_args()

    accounts = _load_accounts()
    if args.system_admin:
        me = _ok(admin_session(accounts, role="SYSTEM_ADMIN").get("/users/me"), "system admin profile")
        print(f"{me['role']} {me['email']} signs in with 2FA")
        print(f"Accounts: {ACCOUNTS_FILE.relative_to(ROOT)}")
        return 0

    admin = admin_session(accounts)
    sme = sme_session(accounts)
    today = date.today()

    for spec in (PROJECTS * (args.count // len(PROJECTS) + 1))[: args.count]:
        result = seed_project(sme, admin, spec, today)
        print(f"{result['listing_status']:<5} {result['name']}  grade={result['grade']} "
              f"rate={result['rate_pct']}%  project={result['project_id']}  listing={result['listing_id']}")

    marketplace = _ok(investor_session(accounts).get("/projects/public"), "marketplace")
    print(f"\n/projects/public now lists {len(marketplace)} project(s) for investors")
    print(f"Accounts: {ACCOUNTS_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
