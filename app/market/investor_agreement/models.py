"""The Investor Service Agreement (Contract 4) each investor signed and uploaded, and which version.

ONE ROW PER INVESTOR PER VERSION
The investor signs it with their first commitment: agreeing to the commitment holds the amount and gives it a
code (no money moves), then they download the agreement filled in with their details and that commitment's
Annex (GET /funding/me/investor-agreement/document?tranche_id=), sign it with their digital signature and upload
the signed PDF (POST /funding/me/investor-agreement). Until then the payment details are refused (409
INVESTOR_AGREEMENT_REQUIRED). The row is the platform's record: when, which version, the commitment it was signed
with, Party B as filled in, and the signed file kept as evidence with its hash. Every later commitment signs only
its own Investment Confirmation (investment_confirmations).

The table is evidence, so the app only ever INSERTS rows. A new version of the text (text.VERSION) asks every
investor to sign again with their next commitment: a new row, the old ones left as they were.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, Date, DateTime, ForeignKey, Integer, Numeric, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InvestorServiceAgreement(Base):
    __tablename__ = "investor_service_agreements"
    __table_args__ = (
        UniqueConstraint("investor_id", "version", name="uq_investor_service_agreements_investor_version"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # RESTRICT, as on tranches: a signed agreement is evidence for the investments made under it.
    investor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    # text.VERSION of the PDF the investor downloaded and signed, e.g. "investor-agreement-v0.3"
    version = Column(Text, nullable=False)
    # When the signed copy was uploaded and accepted.
    signed_at = Column(DateTime(timezone=True), nullable=False)
    # The commitment it was signed with, whose Annex it carries filled in (null only for a record made outside
    # the commitment flow, e.g. test fixtures).
    tranche_id = Column(UUID(as_uuid=True), ForeignKey("tranches.id", ondelete="RESTRICT"), nullable=True)
    # Party B as the platform filled it in: holder_type, name, id_number, address, bank_account_number,
    # bank_account_name, bank_name (any may be null: printed as a dotted blank).
    party_b = Column(JSONB, nullable=False)
    # The signed PDF in evidence storage, its SHA-256, and the name and size it was uploaded with.
    signed_file_key = Column(Text, nullable=False)
    document_sha256 = Column(Text, nullable=False)
    file_name = Column(Text, nullable=True)
    file_size_bytes = Column(BigInteger, nullable=False)
    ip_address = Column(Text, nullable=True)
    user_agent = Column(Text, nullable=True)
    # NEAC's verdict on the signed copy when it was accepted (neac.py): signer, issuer, validity, OCSP/CRL, signing
    # time, and NEAC's raw answer. Null only for records made outside the upload (e.g. test fixtures).
    signature_check = Column(JSONB, nullable=True)
    signature_checked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, server_default=func.now())


class SignedInvestmentConfirmation(Base):
    """One commitment's Investment Confirmation (the agreement's Annex), filled in and confirmed in-app.

    Made when the investor agrees to a commitment, before the payment details are shown: the Annex values, the
    filled-in PDF kept as evidence, and the master agreement it is part of (Contract 4 Art. 7). The commitment's
    signed acceptance names it by id and the PDF's hash, so the in-app signature covers it. Insert-only, one per
    tranche.
    """

    __tablename__ = "investment_confirmations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tranche_id = Column(UUID(as_uuid=True), ForeignKey("tranches.id", ondelete="RESTRICT"), nullable=False, unique=True)
    investor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    agreement_id = Column(
        UUID(as_uuid=True), ForeignKey("investor_service_agreements.id", ondelete="RESTRICT"), nullable=False
    )
    # The Annex rows, as printed.
    listing_id = Column(UUID(as_uuid=True), ForeignKey("listings.id", ondelete="RESTRICT"), nullable=False)
    sme_name = Column(Text, nullable=False)
    amount_vnd = Column(BigInteger, nullable=False)
    annual_rate_pct = Column(Numeric(6, 3), nullable=False)
    term_months = Column(Integer, nullable=False)
    service_fee_vnd = Column(BigInteger, nullable=False)  # P × 2.5% × M / 12, before VAT
    service_fee_bps_per_year = Column(Integer, nullable=False)
    expected_disbursement_date = Column(Date, nullable=False)
    # The filled-in PDF in evidence storage, and its SHA-256.
    file_key = Column(Text, nullable=False)
    document_sha256 = Column(Text, nullable=False)
    confirmed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, server_default=func.now())
