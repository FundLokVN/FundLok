"""Render Contract 4 (Investor Service Agreement) as a PDF, filled in with the
investor's own details, for the investor to review, sign with their digital
signature and upload back before their first commitment.

Same renderer as the SME Service Agreement (app/loans/agreement/pdf.py): PDF
because Vietnamese digital signatures (USB token / remote signing) sign PDFs,
Be Vietnam Pro embedded so the Vietnamese renders the same everywhere, and
anything the platform does not know printed as a dotted blank.
"""
from dataclasses import dataclass
from datetime import date

from fpdf import FPDF

from app.loans.agreement.pdf import FAMILY, FONT_DIR
from app.market.investor_agreement import text


@dataclass(frozen=True)
class AgreementData:
    """What fills the blanks. None prints as a dotted blank."""

    investor_ref: str
    prepared_on: date
    name: str | None
    id_number: str | None
    address: str | None
    bank_account: str | None
    bank_name: str | None


def _or_blank(value, blank: str = text.BLANK) -> str:
    value = "" if value is None else str(value).strip()
    return value or blank


def party_b(data: AgreementData) -> tuple[str, str]:
    """Party B's paragraph in both languages, the known values in bold in the Vietnamese."""
    values = {
        "name": _or_blank(data.name, text.BLANK_NAME),
        "code": _or_blank(data.id_number),
        "address": _or_blank(data.address),
        "account": _or_blank(data.bank_account),
        "bank": _or_blank(data.bank_name),
    }
    bold = {k: (v if v in (text.BLANK, text.BLANK_NAME) else f"**{v}**") for k, v in values.items()}
    return text.PARTY_B_VI.format(**bold), text.PARTY_B_EN.format(**values)


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
            f"{text.VERSION} · Mã nhà đầu tư / Investor {self.data.investor_ref} · "
            f"Trang {self.page_no()}/{{nb}}",
            align="C",
        )
        self.set_text_color(0, 0, 0)


def render(data: AgreementData) -> bytes:
    pdf = _AgreementPDF(data)
    pdf.set_title(f"{text.TITLE[0]} / {text.TITLE[1]}")
    pdf.set_author("FundLok")
    pdf.set_subject(f"{text.VERSION} – {data.name or ''}".strip(" –"))
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

    def heading(body: str):
        pdf.set_font(FAMILY, style="B", size=11)
        pdf.multi_cell(0, 6, body, new_x="LMARGIN", new_y="NEXT")

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
        f"Mã nhà đầu tư / Investor: {data.investor_ref}   ·   Ngày lập / Prepared: "
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

    for title, paragraphs in text.ARTICLES:
        gap(4)
        heading(title)
        gap(1)
        for p_vi, p_en in paragraphs:
            vi(p_vi)
            en(p_en)
            gap(1.5)

    # Annex: the template, one per commitment, completed on the platform.
    gap(4)
    heading(text.ANNEX_TITLE)
    gap(1.5)
    pdf.set_font(FAMILY, size=9.5)
    with pdf.table(col_widths=(70, 100), text_align="LEFT", line_height=5.5, padding=1.6) as table:
        header = table.row()
        header.cell("Mục / Item")
        header.cell("Giá trị / Value")
        for label, value in text.ANNEX_ROWS:
            row = table.row()
            row.cell(label)
            row.cell(value.format(blank=text.BLANK))
    gap(1.5)
    pdf.set_font(FAMILY, style="I", size=8.5)
    pdf.set_text_color(100, 100, 100)
    pdf.multi_cell(0, 4.5, f"{text.ANNEX_NOTE[0]} {text.ANNEX_NOTE[1]}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)

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
