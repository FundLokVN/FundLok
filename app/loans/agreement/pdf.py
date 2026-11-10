"""Render Contract 2 (SME Service Agreement) as a PDF, filled in with the
SME's own details, for the SME to review, sign with its enterprise digital
signature and upload back.

PDF because Vietnamese enterprise digital signatures (USB token / remote
signing tools) sign PDFs. Fonts are Be Vietnam Pro (SIL OFL, fonts/OFL.txt),
embedded so the Vietnamese renders the same everywhere. Anything the platform
does not know yet prints as a dotted blank, as in the template, so the SME
sees exactly what is missing rather than a silently shorter contract.
"""
import hashlib
import io
import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from fpdf import FPDF
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject, TextStringObject

from app.loans.agreement import text

FONT_DIR = Path(__file__).parent / "fonts"
FAMILY = "BeVietnamPro"

# The empty signature fields a document carries, by signer: signing tools (VNPT-CA, Viettel-CA, BKAV, FPT-CA ...)
# list them and sign into them, so each signature lands where the document says it goes.
SME_SIGNATURE_FIELD = "SME_Signature"
GUARANTOR_SIGNATURE_FIELD = "Guarantor_Signature"

_MM = 72 / 25.4  # PDF points per millimetre


def check_code(values: dict) -> str:
    """A short fingerprint of what a document was filled in with: "XXXX-XXXX-XXXX".

    Printed on every page, and worked out again when the signed copy comes back (app/loans/signing.py): a copy
    downloaded before the terms or the parties' details changed, or edited before signing, does not carry the
    current one. Not a security control on its own (NEAC checks the document is unchanged since signing); it ties
    the signed text to the approved numbers."""
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    code = digest[:12].upper()
    return f"{code[:4]}-{code[4:8]}-{code[8:]}"


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
    # The rate FundLok proposed after its assessment (the LOCKED score run), % a year; None before that.
    rate_pct: Decimal | None = None

    def check_values(self) -> dict:
        """Everything the agreement is filled in with except the day it was prepared."""
        values = asdict(self)
        values.pop("prepared_on")
        return {"document": text.VERSION, **values}

    @property
    def check_code(self) -> str:
        return check_code(self.check_values())


def _or_blank(value) -> str:
    value = "" if value is None else str(value).strip()
    return value or text.BLANK


def vnd(amount: int | None) -> str:
    """100000000 -> "100.000.000 đồng", the contract's own way of writing VND."""
    if amount is None:
        return text.BLANK
    return f"{int(amount):,}".replace(",", ".") + " đồng"


def rate(pct: Decimal) -> tuple[str, str]:
    """14.5 -> ("14,5", "14.5"): the rate as Vietnamese and English write it, no trailing zeros."""
    en = format(Decimal(pct).quantize(Decimal("0.001")).normalize(), "f")
    return en.replace(".", ","), en


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


class ContractPDF(FPDF):
    """A FundLok contract page: Be Vietnam Pro embedded, A4, `footer_text` and the page number on every page
    (so a signed copy says which document and version it is), and the bilingual blocks the contracts are made of."""

    def __init__(self, footer_text: str):
        super().__init__(format="A4")
        self.footer_text = footer_text
        # (field name, label, page index, x, y, w, h in mm) of each signature box drawn.
        self.signature_fields: list[tuple[str, str, int, float, float, float, float]] = []
        for style, name in (("", "Regular"), ("B", "Bold"), ("I", "Italic"), ("BI", "BoldItalic")):
            self.add_font(FAMILY, style=style, fname=str(FONT_DIR / f"BeVietnamPro-{name}.ttf"))
        self.set_margins(left=20, top=18, right=20)
        self.set_auto_page_break(auto=True, margin=18)
        self.alias_nb_pages()

    def footer(self):
        self.set_y(-12)
        self.set_font(FAMILY, size=7.5)
        self.set_text_color(120, 120, 120)
        self.cell(0, 5, f"{self.footer_text} · Trang {self.page_no()}/{{nb}}", align="C")
        self.set_text_color(0, 0, 0)

    def vi(self, body: str, size: float = 10, markdown: bool = False):
        self.set_font(FAMILY, size=size)
        self.multi_cell(0, 5.2, body, markdown=markdown, align="L", new_x="LMARGIN", new_y="NEXT")

    def en(self, body: str, size: float = 9.5):
        self.set_font(FAMILY, style="I", size=size)
        self.set_text_color(70, 70, 70)
        self.multi_cell(0, 5, body, align="L", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    def gap(self, mm: float = 2.5):
        self.ln(mm)

    def title_block(self, title: tuple[str, str], meta: str):
        self.set_font(FAMILY, style="B", size=15)
        self.multi_cell(0, 7.5, title[0], new_x="LMARGIN", new_y="NEXT")
        self.set_font(FAMILY, style="B", size=12)
        self.multi_cell(0, 6.5, title[1], new_x="LMARGIN", new_y="NEXT")
        self.gap(1.5)
        self.set_font(FAMILY, size=8.5)
        self.set_text_color(100, 100, 100)
        self.multi_cell(0, 4.5, meta, align="L", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.gap()

    def heading(self, body: str):
        self.set_font(FAMILY, style="B", size=11)
        self.multi_cell(0, 6, body, new_x="LMARGIN", new_y="NEXT")

    def articles(self, articles: list[tuple[str, list[tuple[str, str]]]]):
        for title, paragraphs in articles:
            self.gap(4)
            self.heading(title)
            self.gap(1)
            for p_vi, p_en in paragraphs:
                self.vi(p_vi)
                self.en(p_en)
                self.gap(1.5)

    def rows(self, rows: list[tuple[str, str]], col_widths: tuple[int, int] = (55, 115)):
        self.set_font(FAMILY, size=9.5)
        with self.table(col_widths=col_widths, text_align="LEFT", line_height=5.5, padding=1.6) as table:
            for label, value in rows:
                row = table.row()
                row.cell(label)
                row.cell(value)

    def signature_blocks(
        self,
        signatures: tuple[tuple[str, str], ...],
        fields: tuple[str | None, ...] = (),
    ):
        """The signature blocks side by side. A block with a field name in `fields` (same position) gets a box
        marked "sign here" and, in the output, an empty signature field over it (see `output`)."""
        box_h = 24.0
        self.gap(10)
        # Keep each block and its box together on one page.
        if self.get_y() + 14 + box_h > self.page_break_trigger:
            self.add_page()
        top = self.get_y()
        half = (self.w - self.l_margin - self.r_margin) / 2
        bottoms = []
        for i, (role, who) in enumerate(signatures):
            x = self.l_margin + i * half
            self.set_xy(x, top)
            self.set_font(FAMILY, style="B", size=9.5)
            self.multi_cell(half - 4, 5, role, align="C", new_x="LEFT", new_y="NEXT")
            self.set_x(x)
            self.set_font(FAMILY, style="I", size=8.5)
            self.multi_cell(half - 4, 4.5, who, align="C", new_x="LEFT", new_y="NEXT")
            bottoms.append(self.get_y())
        box_top = max(bottoms) + 2
        for i, name in enumerate(fields):
            if not name:
                continue
            x = self.l_margin + i * half + 4
            w = half - 12
            self.set_draw_color(150, 150, 150)
            self.set_dash_pattern(dash=1.2, gap=1.2)
            self.rect(x, box_top, w, box_h)
            self.set_dash_pattern()
            self.set_draw_color(0, 0, 0)
            self.set_xy(x, box_top + box_h / 2 - 2.5)
            self.set_font(FAMILY, size=7.5)
            self.set_text_color(150, 150, 150)
            self.cell(w, 5, "Ký số tại đây / Sign here", align="C")
            self.set_text_color(0, 0, 0)
            self.signature_fields.append((name, signatures[i][0], self.page_no() - 1, x, box_top, w, box_h))
        self.set_y(box_top + box_h + 2)

    def output_bytes(self) -> bytes:
        """The PDF, with an empty signature field over each "sign here" box."""
        raw = bytes(self.output())
        return add_signature_fields(raw, self.signature_fields) if self.signature_fields else raw


def add_signature_fields(raw: bytes, fields) -> bytes:
    """Add empty (unsigned) signature fields to a PDF: the AcroForm a signing tool reads to know where each
    signature goes. Unsigned, so the PDF still carries no /ByteRange (signature.has_digital_signature is False)."""
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(raw)))
    refs = ArrayObject()
    for name, label, page_index, x, y, w, h in fields:
        page = writer.pages[page_index]
        height = float(page.mediabox.height)
        widget = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/FT"): NameObject("/Sig"),
                NameObject("/T"): TextStringObject(name),
                NameObject("/TU"): TextStringObject(label),
                NameObject("/Rect"): ArrayObject(
                    [
                        FloatObject(round(x * _MM, 2)),
                        FloatObject(round(height - (y + h) * _MM, 2)),
                        FloatObject(round((x + w) * _MM, 2)),
                        FloatObject(round(height - y * _MM, 2)),
                    ]
                ),
                NameObject("/F"): NumberObject(4),  # print
                NameObject("/P"): page.indirect_reference,
            }
        )
        ref = writer._add_object(widget)
        if "/Annots" in page:
            page["/Annots"].get_object().append(ref)
        else:
            page[NameObject("/Annots")] = ArrayObject([ref])
        refs.append(ref)
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"): refs})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def render(data: AgreementData) -> bytes:
    code = data.check_code
    pdf = ContractPDF(f"{text.VERSION} · Mã hồ sơ / Application {data.application_ref} · Mã đối chiếu / Check {code}")
    pdf.set_title(f"{text.TITLE[0]} / {text.TITLE[1]}")
    pdf.set_author("FundLok")
    pdf.set_subject(f"{text.VERSION} – {data.sme_name or ''}".strip(" –"))
    pdf.add_page()

    pdf.title_block(
        text.TITLE,
        f"Mã hồ sơ / Application: {data.application_ref}   ·   Ngày lập / Prepared: "
        f"{data.prepared_on.strftime('%d/%m/%Y')}   ·   Phiên bản / Version: {text.VERSION}   ·   "
        f"Mã đối chiếu / Check: {code}",
    )
    pdf.vi(text.PREVAILS[0])
    pdf.en(text.PREVAILS[1])
    pdf.gap()
    pdf.vi(text.SIGNING[0])
    pdf.en(text.SIGNING[1])
    pdf.gap()
    pdf.vi(text.PARTY_A[0], markdown=True)
    pdf.en(text.PARTY_A[1])
    pdf.gap()
    b_vi, b_en = party_b(data)
    pdf.vi(b_vi, markdown=True)
    pdf.en(b_en)

    pdf.articles(text.ARTICLES)

    # Annex: the Listing Terms. Signed after the assessment, so the rate is FundLok's proposal; a copy downloaded
    # before it says the rate comes from the assessment.
    pdf.gap(4)
    pdf.heading(text.ANNEX_TITLE)
    pdf.gap(1.5)
    if data.rate_pct is not None:
        rate_vi, rate_en = rate(data.rate_pct)
        rate_value = f"{rate_vi}%/năm / {rate_en}% a year"
    else:
        rate_value = f"{text.RATE_AFTER_ASSESSMENT[0]} / {text.RATE_AFTER_ASSESSMENT[1]}"
    pdf.rows(
        [
            ("Số tiền gọi vốn / Amount", vnd(data.amount_vnd)),
            (
                "Thời hạn / Term",
                f"{data.term_months} tháng / {data.term_months} months" if data.term_months else text.BLANK,
            ),
            ("Lãi suất / Interest rate", rate_value),
        ]
    )

    # Party A's block is FundLok's countersignature, not signed here; Party B signs into its field.
    pdf.signature_blocks(text.SIGNATURES, fields=(None, SME_SIGNATURE_FIELD))
    return pdf.output_bytes()
