"""Features derived from uploaded evidence, stored apart from the source files.

Handbook §5.6: source documents and derived features are stored separately, so
any decision can be audited back to the evidence it came from. The file lives
in `loan_application_documents` / R2; what the parser read out of it lives
here, stamped with the parser version that produced it.
"""
import uuid

from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.base import Base


class EInvoiceSummary(Base):
    """What the e-invoice listings in one application's upload say.

    One row per application: a re-upload replaces it, because the application
    is graded on the evidence it was submitted with, not on every draft. The
    features are UNSIGNED (portal .xlsx exports) — see
    app/uploads/parsers/einvoice.py for what that means for their weight.
    """

    __tablename__ = "e_invoice_summaries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("loan_applications.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("loan_application_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    parser_version = Column(String(40), nullable=False)
    seller_tax_code = Column(String(20), nullable=False)
    period_start = Column(String(7), nullable=False)  # MM/YYYY
    period_end = Column(String(7), nullable=False)
    months_covered = Column(Integer, nullable=False)
    features = Column(JSONB, nullable=False)
    warnings = Column(JSONB, nullable=False, server_default="[]")
    # Why a mismatch with the verified company was waved through, when it was.
    note = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TaxFilingSummary(Base):
    """What an application's tax-filings upload says: the year-end statement
    figures (step 3) and, when the monthly VAT declarations were included, the
    signed revenue series. One row per application; a re-upload replaces it.
    """

    __tablename__ = "tax_filing_summaries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("loan_applications.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("loan_application_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    parser_version = Column(String(40), nullable=False)
    tax_code = Column(String(20), nullable=False)
    fiscal_year = Column(Integer, nullable=False)
    vat_months = Column(Integer, nullable=False, server_default="0")
    statements = Column(JSONB, nullable=False)
    vat = Column(JSONB)
    warnings = Column(JSONB, nullable=False, server_default="[]")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CicSummary(Base):
    """What an application's CIC credit report says: the score and rank, when
    CIC computed them, and the debt position. One row per application; a
    re-upload replaces it. Unsigned PDF — see app/uploads/parsers/cic.py.
    """

    __tablename__ = "cic_summaries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("loan_applications.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("loan_application_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    parser_version = Column(String(40), nullable=False)
    subject_type = Column(String(20), nullable=False)  # individual | business
    score = Column(Integer)
    rank = Column(Integer)
    scored_on = Column(Date)
    report = Column(JSONB, nullable=False)
    warnings = Column(JSONB, nullable=False, server_default="[]")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
