"""Render Contract 4 (Investor Service Agreement) as a PDF, filled in with the
investor's own details, for the investor to review, sign with their digital
signature and upload back before their first commitment.

Same renderer as the SME Service Agreement (app/loans/agreement/pdf.py): PDF
because Vietnamese digital signatures (USB token / remote signing) sign PDFs,
Be Vietnam Pro embedded so the Vietnamese renders the same everywhere, and
anything the platform does not know printed as a dotted blank.
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

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
    # Filled in when the agreement is generated for the commitment it is signed with: that commitment's code and
    # its Annex rows (annex_rows_for). Otherwise the Annex prints as the blank template.
    commitment_ref: str | None = None
    annex: list[tuple[str, str]] | None = None


@dataclass(frozen=True)
class AnnexData:
    """One commitment's Investment Confirmation, every value known."""

    commitment_ref: str  # the tranche's reference code, also its transfer memo
    investor_name: str | None
    investor_id_number: str | None
    agreement_version: str
    agreement_signed_on: date
    listing_ref: str
    sme_name: str
    amount_vnd: int
    annual_rate_pct: Decimal
    term_months: int
    service_fee_vnd: int
    expected_disbursement_date: date
    confirmed_at: datetime  # already in Vietnam time


def vi_number(value) -> str:
    """20000000 -> "20.000.000", Decimal("13.750") -> "13,75": the contract's own way of writing numbers."""
    d = Decimal(str(value)).normalize()
    whole, _, frac = f"{d:f}".partition(".")
    whole = f"{int(whole):,}".replace(",", ".")
    return f"{whole},{frac}" if frac else whole


def annex_rows_for(
    *,
    listing_ref: str,
    sme_name: str,
    amount_vnd: int,
    annual_rate_pct,
    term_months: int,
    service_fee_vnd: int,
    expected_disbursement_date: date,
) -> list[tuple[str, str]]:
    """The Annex rows, filled in, under the template's own labels."""
    labels = [label for label, _ in text.ANNEX_ROWS]
    rate = vi_number(annual_rate_pct)
    values = [
        listing_ref,
        sme_name,
        f"{vi_number(amount_vnd)} VND",
        f"{rate}%/năm, {term_months} tháng / {rate}% a year, {term_months} months",
        f"P × 2,5% × M / 12 = {vi_number(service_fee_vnd)} VND + VAT",
        expected_disbursement_date.strftime("%d/%m/%Y"),
    ]
    return list(zip(labels, values))


def annex_rows(data: AnnexData) -> list[tuple[str, str]]:
    return annex_rows_for(
        listing_ref=data.listing_ref,
        sme_name=data.sme_name,
        amount_vnd=data.amount_vnd,
        annual_rate_pct=data.annual_rate_pct,
        term_months=data.term_months,
        service_fee_vnd=data.service_fee_vnd,
        expected_disbursement_date=data.expected_disbursement_date,
    )


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
    def __init__(self, footer_label: str):
        super().__init__(format="A4")
        self.footer_label = footer_label
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
            f"{text.VERSION} · {self.footer_label} · Trang {self.page_no()}/{{nb}}",
            align="C",
        )
        self.set_text_color(0, 0, 0)


def render(data: AgreementData) -> bytes:
    pdf = _AgreementPDF(f"Mã nhà đầu tư / Investor {data.investor_ref}")
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

    # Annex: the commitment it is signed with, filled in; else the blank template.
    gap(4)
    heading(text.ANNEX_TITLE)
    gap(1.5)
    if data.annex:
        pdf.set_font(FAMILY, style="B", size=10)
        pdf.multi_cell(0, 5.2, f"Mã cam kết / Commitment: {data.commitment_ref}", new_x="LMARGIN", new_y="NEXT")
        gap(1)
        rows = data.annex
        note = tuple(n.format(ref=data.commitment_ref) for n in text.ANNEX_NOTE_FILLED)
    else:
        rows = [(label, value.format(blank=text.BLANK)) for label, value in text.ANNEX_ROWS]
        note = text.ANNEX_NOTE
    pdf.set_font(FAMILY, size=9.5)
    with pdf.table(col_widths=(70, 100), text_align="LEFT", line_height=5.5, padding=1.6) as table:
        header = table.row()
        header.cell("Mục / Item")
        header.cell("Giá trị / Value")
        for label, value in rows:
            row = table.row()
            row.cell(label)
            row.cell(value)
    gap(1.5)
    pdf.set_font(FAMILY, style="I", size=8.5)
    pdf.set_text_color(100, 100, 100)
    pdf.multi_cell(0, 4.5, f"{note[0]} {note[1]}", new_x="LMARGIN", new_y="NEXT")
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


def render_annex(data: AnnexData) -> bytes:
    """One commitment's Investment Confirmation, filled in: kept as evidence when the investor agrees, and the copy
    they can open before paying."""
    pdf = _AgreementPDF(f"Mã cam kết / Commitment {data.commitment_ref}")
    pdf.set_title(text.ANNEX_TITLE)
    pdf.set_author("FundLok")
    pdf.set_subject(f"{data.agreement_version} – {data.commitment_ref}")
    pdf.add_page()

    def para(body: str, *, style: str = "", size: float = 10, grey: bool = False, markdown: bool = False):
        pdf.set_font(FAMILY, style=style, size=size)
        if grey:
            pdf.set_text_color(70, 70, 70)
        pdf.multi_cell(0, 5.2, body, markdown=markdown, align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    pdf.set_font(FAMILY, style="B", size=14)
    pdf.multi_cell(0, 7, text.ANNEX_TITLE, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1.5)
    pdf.set_font(FAMILY, size=8.5)
    pdf.set_text_color(100, 100, 100)
    pdf.multi_cell(
        0,
        4.5,
        f"Mã cam kết / Commitment: {data.commitment_ref}   ·   Phiên bản / Version: {data.agreement_version}",
        align="L",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2.5)

    signed_on = data.agreement_signed_on.strftime("%d/%m/%Y")
    para(text.ANNEX_PART_OF[0].format(version=data.agreement_version, signed_on=signed_on))
    para(text.ANNEX_PART_OF[1].format(version=data.agreement_version, signed_on=signed_on), style="I", size=9.5,
         grey=True)
    pdf.ln(2)
    para(text.ANNEX_PARTY_A, markdown=True)
    para(
        text.ANNEX_PARTY_B.format(
            name=_or_blank(data.investor_name, text.BLANK_NAME), code=_or_blank(data.investor_id_number)
        ),
        markdown=True,
    )
    pdf.ln(3)

    pdf.set_font(FAMILY, size=9.5)
    with pdf.table(col_widths=(70, 100), text_align="LEFT", line_height=5.5, padding=1.6) as table:
        header = table.row()
        header.cell("Mục / Item")
        header.cell("Giá trị / Value")
        for label, value in annex_rows(data):
            row = table.row()
            row.cell(label)
            row.cell(value)
    pdf.ln(3)

    # Article 7: what this confirmation is and when it takes effect.
    art7_vi, art7_en = dict(text.ARTICLES)["Điều 7. Xác nhận đầu tư / Investment Confirmation"][0]
    para(art7_vi)
    para(art7_en, style="I", size=9.5, grey=True)
    pdf.ln(5)

    at = data.confirmed_at.strftime("%H:%M %d/%m/%Y")
    para(text.ANNEX_CONFIRMED[0].format(at=at), style="B", size=9.5)
    para(text.ANNEX_CONFIRMED[1].format(at=at), style="I", size=9, grey=True)
    return bytes(pdf.output())
