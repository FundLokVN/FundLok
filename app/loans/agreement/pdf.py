"""Render Contract 2 (SME Service Agreement) as a PDF, filled in with the
SME's own details, for the SME to review, sign with its enterprise digital
signature and upload back.

PDF because Vietnamese enterprise digital signatures (USB token / remote
signing tools) sign PDFs. Fonts are Be Vietnam Pro (SIL OFL, fonts/OFL.txt),
embedded so the Vietnamese renders the same everywhere. Anything the platform
does not know yet prints as a dotted blank, as in the template, so the SME
sees exactly what is missing rather than a silently shorter contract.
"""
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fpdf import FPDF

from app.loans.agreement import text

FONT_DIR = Path(__file__).parent / "fonts"
FAMILY = "BeVietnamPro"


@dataclass(frozen=True)
class AgreementData:
    """What fills the blanks. None prints as a dotted blank."""

    application_ref: str
    prepared_on: date
    sme_name: str | None
    enterprise_code: str | None
    address: str | None
    legal_representative: str | None
    bank_account: str | None
    bank_name: str | None
    amount_vnd: int | None
    term_months: int | None


def _or_blank(value) -> str:
    value = "" if value is None else str(value).strip()
    return value or text.BLANK


def vnd(amount: int | None) -> str:
    """100000000 -> "100.000.000 đồng", the contract's own way of writing VND."""
    if amount is None:
        return text.BLANK
    return f"{int(amount):,}".replace(",", ".") + " đồng"


def party_b(data: AgreementData) -> tuple[str, str]:
    """Party B's paragraph in both languages, the values in bold."""
    fields = {
        "name": data.sme_name,
        "code": data.enterprise_code,
        "address": data.address,
        "representative": data.legal_representative,
        "account": data.bank_account,
        "bank": data.bank_name,
    }

    def fill(template: str, bold: bool) -> str:
        values = {k: _or_blank(v) for k, v in fields.items()}
        if bold:
            values = {k: (f"**{v}**" if v != text.BLANK else v) for k, v in values.items()}
        return template.format(**values)

    return fill(text.PARTY_B_VI, bold=True), fill(text.PARTY_B_EN, bold=False)


class _AgreementPDF(FPDF):
    def __init__(self, data: AgreementData):
        super().__init__(format="A4")
        self.data = data
        for style, name in (("", "Regular"), ("B", "Bold"), ("I", "Italic"), ("BI", "BoldItalic")):
            self.add_font(FAMILY, style=style, fname=str(FONT_DIR / f"BeVietnamPro-{name}.ttf"))
        self.set_margins(left=20, top=18, right=20)
        self.set_auto_page_break(auto=True, margin=18)
        self.alias_nb_pages()

    def footer(self):
        self.set_y(-12)
        self.set_font(FAMILY, size=7.5)
        self.set_text_color(120, 120, 120)
        self.cell(
            0,
            5,
            f"{text.VERSION} · Mã hồ sơ / Application {self.data.application_ref} · "
            f"Trang {self.page_no()}/{{nb}}",
            align="C",
        )
        self.set_text_color(0, 0, 0)


def render(data: AgreementData) -> bytes:
    pdf = _AgreementPDF(data)
    pdf.set_title(f"{text.TITLE[0]} / {text.TITLE[1]}")
    pdf.set_author("FundLok")
    pdf.set_subject(f"{text.VERSION} – {data.sme_name or ''}".strip(" –"))
    pdf.add_page()

    def vi(body: str, size: float = 10, markdown: bool = False):
        pdf.set_font(FAMILY, size=size)
        pdf.multi_cell(0, 5.2, body, markdown=markdown, align="L", new_x="LMARGIN", new_y="NEXT")

    def en(body: str, size: float = 9.5):
        pdf.set_font(FAMILY, style="I", size=size)
        pdf.set_text_color(70, 70, 70)
        pdf.multi_cell(0, 5, body, align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    def gap(mm: float = 2.5):
        pdf.ln(mm)

    # Title and the document's own reference
    pdf.set_font(FAMILY, style="B", size=15)
    pdf.multi_cell(0, 7.5, text.TITLE[0], new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(FAMILY, style="B", size=12)
    pdf.multi_cell(0, 6.5, text.TITLE[1], new_x="LMARGIN", new_y="NEXT")
    gap(1.5)
    pdf.set_font(FAMILY, size=8.5)
    pdf.set_text_color(100, 100, 100)
    pdf.multi_cell(
        0,
        4.5,
        f"Mã hồ sơ / Application: {data.application_ref}   ·   Ngày lập / Prepared: "
        f"{data.prepared_on.strftime('%d/%m/%Y')}   ·   Phiên bản / Version: {text.VERSION}",
        align="L",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.set_text_color(0, 0, 0)
    gap()

    vi(text.PREVAILS[0])
    en(text.PREVAILS[1])
    gap()
    vi(text.SIGNING[0])
    en(text.SIGNING[1])
    gap()
    vi(text.PARTY_A[0], markdown=True)
    en(text.PARTY_A[1])
    gap()
    b_vi, b_en = party_b(data)
    vi(b_vi, markdown=True)
    en(b_en)

    for heading, paragraphs in text.ARTICLES:
        gap(4)
        pdf.set_font(FAMILY, style="B", size=11)
        pdf.multi_cell(0, 6, heading, new_x="LMARGIN", new_y="NEXT")
        gap(1)
        for p_vi, p_en in paragraphs:
            vi(p_vi)
            en(p_en)
            gap(1.5)

    # Annex: the Listing Terms as far as they are known now.
    gap(4)
    pdf.set_font(FAMILY, style="B", size=11)
    pdf.multi_cell(0, 6, text.ANNEX_TITLE, new_x="LMARGIN", new_y="NEXT")
    gap(1.5)
    rows = [
        ("Số tiền gọi vốn / Amount", vnd(data.amount_vnd)),
        (
            "Thời hạn / Term",
            f"{data.term_months} tháng / {data.term_months} months" if data.term_months else text.BLANK,
        ),
        ("Lãi suất / Interest rate", f"{text.RATE_AFTER_ASSESSMENT[0]} / {text.RATE_AFTER_ASSESSMENT[1]}"),
    ]
    pdf.set_font(FAMILY, size=9.5)
    with pdf.table(col_widths=(55, 115), text_align="LEFT", line_height=5.5, padding=1.6) as table:
        for label, value in rows:
            row = table.row()
            row.cell(label)
            row.cell(value)

    # Signature blocks
    gap(10)
    top = pdf.get_y()
    half = (pdf.w - pdf.l_margin - pdf.r_margin) / 2
    for i, (role, who) in enumerate(text.SIGNATURES):
        pdf.set_xy(pdf.l_margin + i * half, top)
        pdf.set_font(FAMILY, style="B", size=9.5)
        pdf.multi_cell(half - 4, 5, role, align="C", new_x="LEFT", new_y="NEXT")
        pdf.set_x(pdf.l_margin + i * half)
        pdf.set_font(FAMILY, style="I", size=8.5)
        pdf.multi_cell(half - 4, 4.5, who, align="C", new_x="LEFT", new_y="NEXT")

    return bytes(pdf.output())
