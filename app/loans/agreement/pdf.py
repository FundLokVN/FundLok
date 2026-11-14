"""Render Contract 2 (SME Service Agreement) as a PDF, filled in with the
SME's own details, for the SME to review, sign with its enterprise digital
signature and upload back.

PDF because Vietnamese enterprise digital signatures (USB token / remote
signing tools) sign PDFs. The contracts follow the usual Vietnamese contract
layout: the national title and motto (Quốc hiệu, Tiêu ngữ) at the top, the
date, the title centred in capitals with its number, numbered Articles, and
the parties' signature blocks side by side. Set in DejaVu Serif, the closest
freely redistributable face to the Times New Roman such contracts use, subset
to Latin, Vietnamese and the symbols the formulas need
(fonts/DejaVuSerif-*-VN.ttf, fonts/LICENSE_DEJAVU.txt), embedded so the
Vietnamese renders the same everywhere. Anything the platform does not know
yet prints as a dotted blank, as in the template, so the SME sees exactly what
is missing rather than a silently shorter contract (and signing refuses it:
app/loans/signing.py).
"""
import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from fpdf import FPDF
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject, TextStringObject

from app.loans.agreement import text

FONT_DIR = Path(__file__).parent / "fonts"
FAMILY = "BeVietnamPro"  # Contract 4's face (app/market/investor_agreement/pdf.py)
# Contracts 2, 3 and 5.
SERIF_FAMILY = "DejaVuSerifVN"
SERIF_FILES = {
    "": "DejaVuSerif-VN.ttf",
    "B": "DejaVuSerif-Bold-VN.ttf",
    "I": "DejaVuSerif-Italic-VN.ttf",
    "BI": "DejaVuSerif-BoldItalic-VN.ttf",
}
NATIONAL_TITLE = ("CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM", "THE SOCIALIST REPUBLIC OF VIETNAM")
NATIONAL_MOTTO = ("Độc lập - Tự do - Hạnh phúc", "Independence - Freedom - Happiness")
_EN_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
# The empty signature fields a document carries, by signer: signing tools (VNPT-CA, Viettel-CA, BKAV, FPT-CA ...)
# list them and sign into them, so each signature lands where the document says it goes.
SME_SIGNATURE_FIELD = "SME_Signature"
GUARANTOR_SIGNATURE_FIELD = "Guarantor_Signature"

_MM = 72 / 25.4  # PDF points per millimetre

# The details a document must be filled in with before it can be signed (app/loans/signing.record_signed refuses
# otherwise, SIGNING_DETAILS_MISSING), as ListingDocumentsOut.missing_details lists them for the SME to complete.
COMPANY_NAME = "COMPANY_NAME"  # KYB, else the project's legal name
ENTERPRISE_CODE = "ENTERPRISE_CODE"  # KYB, else the project's tax ID
COMPANY_ADDRESS = "COMPANY_ADDRESS"  # KYB, else the project's address
LEGAL_REPRESENTATIVE = "LEGAL_REPRESENTATIVE"  # KYB only
BANK_ACCOUNT = "BANK_ACCOUNT"  # the SME's approved payment account
GUARANTOR_NAME = "GUARANTOR_NAME"  # eKYC, else the account's name
GUARANTOR_ID = "GUARANTOR_ID"  # eKYC only
GUARANTOR_ADDRESS = "GUARANTOR_ADDRESS"  # eKYC, else what the SME entered on the signing step


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

    def missing_details(self) -> list[str]:
        """The SME's details this agreement (and every document naming the SME) still prints as a blank."""
        values = {
            COMPANY_NAME: self.sme_name,
            ENTERPRISE_CODE: self.enterprise_code,
            COMPANY_ADDRESS: self.address,
            LEGAL_REPRESENTATIVE: self.legal_representative,
            BANK_ACCOUNT: self.bank_account,
        }
        return [code for code, value in values.items() if not (value or "").strip()]

    def check_values(self) -> dict:
        """Everything the agreement is filled in with except the day it was prepared."""
        values = asdict(self)
        values.pop("prepared_on")
        return {"document": text.VERSION, **values}

    @property
    def check_code(self) -> str:
        return check_code(self.check_values())


def signer_name(person: str | None, company: str | None = None) -> str | None:
    """The name printed under a signature box: the person, and the company they sign for."""
    person = (person or "").strip()
    company = (company or "").strip()
    if person and company:
        return f"{person}\n{company}"
    return person or company or None


def address_line(address) -> str | None:
    """A project's address (its JSON: street, city, state, postal_code, country) as one line, the way the company
    form shows it; None when it has none."""
    if not isinstance(address, dict):
        return None
    parts = [str(address.get(key) or "").strip() for key in ("street", "city", "state", "postal_code", "country")]
    return ", ".join(p for p in parts if p) or None


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
    """A FundLok contract page in the Vietnamese contract layout: DejaVu Serif embedded, A4, `footer_text` and the
    page number on every page (so a signed copy says which document and version it is), and the bilingual blocks
    the contracts are made of."""

    def __init__(self, footer_text: str):
        super().__init__(format="A4")
        self.footer_text = footer_text
        # (field name, label, page index, x, y, w, h in mm) of each signature box drawn.
        self.signature_fields: list[tuple[str, str, int, float, float, float, float]] = []
        for style, file in SERIF_FILES.items():
            self.add_font(SERIF_FAMILY, style=style, fname=str(FONT_DIR / file))
        self.set_margins(left=20, top=18, right=20)
        self.set_auto_page_break(auto=True, margin=18)
        self.alias_nb_pages()

    def footer(self):
        self.set_y(-12)
        self.set_font(SERIF_FAMILY, size=7.5)
        self.set_text_color(120, 120, 120)
        self.cell(0, 5, f"{self.footer_text} · Trang {self.page_no()}/{{nb}}", align="C")
        self.set_text_color(0, 0, 0)

    def vi(self, body: str, size: float = 10.5, markdown: bool = False):
        self.set_font(SERIF_FAMILY, size=size)
        self.multi_cell(0, 5.4, body, markdown=markdown, align="J", new_x="LMARGIN", new_y="NEXT")

    def en(self, body: str, size: float = 9.5):
        self.set_font(SERIF_FAMILY, style="I", size=size)
        self.set_text_color(70, 70, 70)
        self.multi_cell(0, 5, body, align="J", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    def gap(self, mm: float = 2.5):
        self.ln(mm)

    def _centred(self, body: str, size: float, style: str = "", height: float = 5.6):
        self.set_font(SERIF_FAMILY, style=style, size=size)
        self.multi_cell(0, height, body, align="C", new_x="LMARGIN", new_y="NEXT")

    def _rule(self, width: float):
        """The short line under the motto."""
        x = (self.w - width) / 2
        y = self.get_y() + 0.6
        self.set_line_width(0.3)
        self.line(x, y, x + width, y)
        self.set_y(y + 1.6)

    def national_header(self):
        """Quốc hiệu and Tiêu ngữ, then their English, each motto underlined."""
        for i in (0, 1):
            self._centred(NATIONAL_TITLE[i], 12 if i == 0 else 10.5, "B")
            self._centred(NATIONAL_MOTTO[i], 11.5 if i == 0 else 10, "B")
            self.set_font(SERIF_FAMILY, style="B", size=11.5 if i == 0 else 10)
            self._rule(self.get_string_width(NATIONAL_MOTTO[i]) * 0.55)
            self.gap(1.5 if i == 0 else 0)

    def title_block(self, title: tuple[str, str], *, number: str, prepared_on: date, meta: str):
        """The Vietnamese contract opening: national header, the date, the title in capitals with its number, and
        FundLok's own references (application, version, check code) in small print."""
        self.national_header()
        self.gap(3)
        self.set_font(SERIF_FAMILY, style="I", size=10)
        self.multi_cell(
            0,
            5,
            f"Ngày {prepared_on.day:02d} tháng {prepared_on.month:02d} năm {prepared_on.year} / "
            f"{prepared_on.day} {_EN_MONTHS[prepared_on.month - 1]} {prepared_on.year}",
            align="R",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        self.gap(4)
        # "5. Hợp đồng cho vay" -> "HỢP ĐỒNG CHO VAY" (the leading number is the Contract set's, kept in `meta`).
        self._centred(re.sub(r"^\d+\.\s*", "", title[0]).upper(), 14, "B", height=7)
        self._centred(title[1].upper(), 11, "B", height=6)
        self.gap(1)
        self._centred(f"Số / No.: {number}", 10.5)
        self.gap(1)
        self.set_text_color(110, 110, 110)
        self._centred(meta, 8, height=4.2)
        self.set_text_color(0, 0, 0)
        self.gap(3)

    def heading(self, body: str):
        self.set_font(SERIF_FAMILY, style="B", size=11)
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
        self.set_font(SERIF_FAMILY, size=9.5)
        # No heading row: every row is a label and its value (a heading row would print bold and repeat on the
        # next page when the table breaks).
        with self.table(
            col_widths=col_widths, text_align="LEFT", line_height=5.5, padding=1.6, first_row_as_headings=False
        ) as table:
            for label, value in rows:
                row = table.row()
                row.cell(label)
                row.cell(value)

    def signature_blocks(
        self,
        signatures: tuple[tuple[str, str], ...],
        fields: tuple[str | None, ...] = (),
        names: tuple[str | None, ...] = (),
    ):
        """The signature blocks side by side, as Vietnamese contracts close: the party in capitals, how it signs,
        the signature, and the signer's name under it (`names`, same position; none prints no line). A block with a
        field name in `fields` gets a box marked "sign here" and, in the output, an empty signature field over it
        (see `output_bytes`)."""
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
            self.set_font(SERIF_FAMILY, style="B", size=9.5)
            self.multi_cell(half - 4, 5, role, align="C", new_x="LEFT", new_y="NEXT")
            self.set_x(x)
            self.set_font(SERIF_FAMILY, style="I", size=8.5)
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
            self.set_font(SERIF_FAMILY, size=7.5)
            self.set_text_color(150, 150, 150)
            self.cell(w, 5, "Ký số tại đây / Sign here", align="C")
            self.set_text_color(0, 0, 0)
            self.signature_fields.append((name, signatures[i][0], self.page_no() - 1, x, box_top, w, box_h))
        bottom = box_top + box_h + 2
        for i, signer in enumerate(names):
            if not signer:
                continue
            self.set_xy(self.l_margin + i * half, bottom)
            self.set_font(SERIF_FAMILY, style="B", size=9.5)
            self.multi_cell(half - 4, 5, signer, align="C", new_x="LEFT", new_y="NEXT")
        self.set_y(max([bottom, self.get_y()]) + 2)

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
        number=text.NUMBER.format(ref=data.application_ref),
        prepared_on=data.prepared_on,
        meta=f"Văn bản số 2 / Document 2   ·   Mã hồ sơ / Application: {data.application_ref}   ·   "
        f"Phiên bản / Version: {text.VERSION}   ·   Mã đối chiếu / Check: {code}",
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
    pdf.signature_blocks(
        text.SIGNATURES,
        fields=(None, SME_SIGNATURE_FIELD),
        names=(text.PARTY_A_SIGNER, signer_name(data.legal_representative, data.sme_name)),
    )
    return pdf.output_bytes()
