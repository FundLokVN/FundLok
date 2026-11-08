import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

MB = 1024 * 1024

# Per-wizard-step rules: allowed extensions and max size per document type.
# Step 1 (Hồ sơ pháp lý) produces two rows: legal_charter + business_registration.
DOCUMENT_TYPE_RULES: dict[str, dict] = {
    "legal_charter": {"extensions": {"pdf"}, "max_size": 25 * MB},
    "business_registration": {"extensions": {"pdf"}, "max_size": 25 * MB},
    "vat_tax_zip": {"extensions": {"zip"}, "max_size": 200 * MB},
    "financial_report": {"extensions": {"pdf"}, "max_size": 25 * MB},
    # One .zip: the folder of the tax portal's monthly invoice-list exports
    # (.xlsx, one per month), parsed at confirm by
    # app/uploads/parsers/einvoice.py. Loose .xlsx/.csv/.xml are refused: the
    # parser reads the zip, and a single loose month is not a 12-month history.
    "e_invoice_data": {"extensions": {"zip"}, "max_size": 100 * MB},
    "cic_report": {"extensions": {"pdf"}, "max_size": 25 * MB},
    # Step 3: the SME's tax filings as HTKK produced them — one .zip of the
    # folder (monthly VAT declaration zips + the year-end statement XML), or
    # the statement XML alone. Parsed at confirm by
    # app/uploads/parsers/tax_filings.py.
    "tax_filings": {"extensions": {"zip", "xml"}, "max_size": 100 * MB},
    # Step 4: the SME Service Agreement the SME downloaded from
    # GET /loans/applications/{id}/service-agreement, signed with its
    # enterprise digital signature. A signed PDF. Required to submit.
    "sme_service_agreement": {"extensions": {"pdf"}, "max_size": 25 * MB},
}

EXTENSION_CONTENT_TYPES: dict[str, frozenset[str]] = {
    "pdf": frozenset({"application/pdf"}),
    "zip": frozenset({"application/zip", "application/x-zip-compressed"}),
    "xlsx": frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
    "csv": frozenset({"text/csv"}),
    "xml": frozenset({"application/xml", "text/xml"}),
}


class UploadPresignRequest(BaseModel):
    loan_application_id: UUID
    document_type: str
    filename: str = Field(min_length=1, max_length=255)
    content_type: str
    size: int = Field(gt=0)


class UploadPresignResponse(BaseModel):
    document_id: UUID
    file_key: str
    upload_url: str
    expires_in: int


class UploadConfirmRequest(BaseModel):
    loan_application_id: UUID
    file_keys: list[str] = Field(min_length=1, max_length=20)


class LoanApplicationDocumentOut(BaseModel):
    id: UUID
    loan_application_id: UUID
    document_type: str
    file_key: str
    original_filename: str
    content_type: str | None
    file_size_bytes: int | None
    status: str
    uploaded_at: datetime | None

    model_config = {"from_attributes": True}


class UploadConfirmResponse(BaseModel):
    documents: list[LoanApplicationDocumentOut]


# --------------------------------------------------------------------------- #
# Generic (business/project-level) document presign+commit, merged in from
# app/files/ (HANDOFF-02 Fix C, module cleanup -- see docs/handoffs/HANDOFF-02-
# structural-fixes.md). Route paths and behavior are unchanged: still mounted
# under /files/*, kept as a separate schema set from the loan-application
# upload flow above since they model a different entity (Document, keyed by
# business_id/purpose) than LoanApplicationDocument.
# --------------------------------------------------------------------------- #

FILE_ALLOWED_MIME = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)
FILE_MAX_SIZE_BYTES = 25 * MB


class FilePresignRequest(BaseModel):
    business_id: UUID
    purpose: str
    # Bounded like UploadPresignRequest.filename above. Stored as given so the
    # UI can show the user their own filename; it is `safe_object_filename`d on
    # the way into the object key, which is where an unbounded raw value did
    # real damage. See that function for what and why.
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str

    @field_validator("mime_type")
    @classmethod
    def mime_allowlist(cls, v: str) -> str:
        if v not in FILE_ALLOWED_MIME:
            raise ValueError("mime_type not allowed")
        return v


# Characters allowed to survive into an object key. Everything else — path
# separators, control characters, URL metacharacters, non-ASCII — collapses to
# an underscore.
_SAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def safe_object_filename(raw: str) -> str:
    """Reduce a client filename to something safe to put in an object key.

    The generic presign builds its key as
    `fundlok/{business_id}/{uuid4}/{filename}` and then interpolates that key
    into a URL. A raw client value there is wrong on three counts:

    * `..` and `/` segments. S3/R2 treat keys as opaque strings so there is no
      filesystem traversal, but an HTTP client normalising `..` in the URL path
      before sending signs one key and writes another — at best a broken
      upload, at worst an object landing outside the intended prefix.
    * URL metacharacters (`?`, `#`, `&`, spaces) silently truncate or split the
      `?key=` query parameter the upload URL is built from.
    * Control characters, including CR/LF, which have no business in a key that
      may end up in a header or a log line.

    Keeping a readable, sanitised name is deliberate: the uuid4 segment already
    guarantees uniqueness, so the filename is only ever a human hint. The
    original, unmodified value is still stored in `Document.filename` for
    display — sanitising is about what reaches the KEY, not about losing the
    user's filename.
    """
    # Basename only: strip both separators, since a Windows client sends "\".
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = _SAFE_FILENAME_CHARS.sub("_", name)
    # A name that was entirely separators/dots ("..", "/", "") leaves nothing
    # usable; the uuid4 segment carries the identity, so a constant is fine.
    name = name.lstrip(".") or "upload"
    # Belt and braces: the 255 bound above applies to the raw input, and
    # sanitising never lengthens a name, but keep the key segment bounded
    # regardless of how this is called.
    return name[:255]


class FilePresignResponse(BaseModel):
    file_id: UUID
    upload_url: str


class FileCommitRequest(BaseModel):
    checksum: str
    size: int

    @field_validator("size")
    @classmethod
    def size_cap(cls, v: int) -> int:
        if v < 0 or v > FILE_MAX_SIZE_BYTES:
            raise ValueError("size out of allowed range")
        return v


class FileCommitResponse(BaseModel):
    file_id: UUID
    status: str


class EInvoiceMonthOut(BaseModel):
    period: str  # MM/YYYY
    revenue_vnd: int


class EInvoicePreviewOut(BaseModel):
    """The step-2 prefill: what the SME's e-invoice zip says, before Send.

    Advisory only. Confirm re-parses the stored file, so nothing here can plant
    a figure the stored evidence does not support.
    """

    seller_tax_code: str
    period_start: str
    period_end: str
    months_covered: int
    monthly_revenue: list[EInvoiceMonthOut]
    # Set only from 12 consecutive months: a partial year is not "the last 12
    # months", and the SME types the figure instead.
    revenue_last_12m: int | None
    revenue_best_month: int | None
    revenue_worst_month: int | None
    conc_top1_pct: float | None
    conc_top3_pct: float | None
    warnings: list[dict[str, str]]


class VatMonthOut(BaseModel):
    period: str  # MM/YYYY
    revenue_vnd: int


class TaxFilingsPreviewOut(BaseModel):
    """The prefill from the SME's tax filings, before Send. Advisory: confirm
    re-parses the stored file."""

    fiscal_year: int
    regime: str
    signed: bool
    tax_code: str
    cogs_y1: int
    owner_withdrawal_pct: float | None
    # Fixed and variable cost. No filing labels a cost fixed or variable, so
    # these follow the stated rule in app/uploads/parsers/statements.py:
    # fixed = management expense + financial expense, variable = selling expense.
    fixed_cost_y1: int
    variable_cost_excl_cogs_y1: int
    # What the two are made of, so the wizard can show its working. The
    # management expense is line 26 on TT200 and the combined line 24 on TT133,
    # which has no selling line (selling_expense_vnd is 0 there).
    admin_expense_vnd: int
    selling_expense_vnd: int
    financial_expense_vnd: int
    revenue_net_vnd: int
    net_profit_vnd: int
    interest_expense_vnd: int
    vat_months: int
    vat_period: str | None
    # The signed monthly revenue, oldest first ([34] on each 01/GTGT). Empty when
    # the upload was the statement alone. The wizard uses it to work out the 12
    # months BEFORE the e-invoice window, which no 12-month export can state and
    # the SME would otherwise type.
    vat_monthly_revenue: list[VatMonthOut] = []
    warnings: list[dict[str, str]]


class CicPreviewOut(BaseModel):
    """What step 4 shows the moment the SME picks the CIC report. Advisory:
    confirm re-parses the stored file. Amounts are CIC's own unit, million
    đồng."""

    subject_type: Literal["individual", "business"]
    subject_name: str | None
    score: int | None
    rank: int | None
    rank_band: list[int] | None
    rank_label: Literal["very_good", "good", "average", "below_average", "poor"] | None
    percentile: int | None
    scored_on: str | None
    queried_on: str | None
    age_days: int | None
    lenders: int
    debt_total_vnd_million: float | None
    debt_attention_vnd_million: float | None
    debt_bad_vnd_million: float | None
    negative_history: bool | None
    signed: bool
    warnings: list[dict[str, str]]
