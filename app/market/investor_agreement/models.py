"""The Investor Service Agreement (Contract 4) each investor confirmed, and which version.

ONE ROW PER INVESTOR PER VERSION
The investor confirms it with their first commitment: agreeing to the commitment holds the amount and gives it a
code (no money moves), then they read the agreement filled in with their details and that commitment's Annex
(GET /funding/me/investor-agreement/document?tranche_id=), ask for a one-time code on their phone by Zalo
(POST /funding/me/investor-agreement/code) and confirm with it (POST /funding/me/investor-agreement). Until then
the payment details are refused (409 INVESTOR_AGREEMENT_REQUIRED). The row is the platform's record: when, which
version, the commitment it was confirmed with, Party B as filled in, how it was confirmed (signature_check), and the
confirmed PDF kept as evidence with its hash. Every later commitment confirms only its own Investment Confirmation
(investment_confirmations).

The table is evidence, so the app only ever INSERTS rows. A new version of the text (text.VERSION) asks every
investor to confirm again with their next commitment: a new row, the old ones left as they were.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, Text, UniqueConstraint, func,
)
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
    # How it was confirmed. Since v0.3.1: {verified_by: "ZALO_OTP", checked_at, phone, channel, code_id}, the
    # one-time code the investor confirmed with. Rows of v0.3 hold NEAC's verdict on an uploaded, digitally signed
    # copy instead (verified_by "NEAC", signer, issuer, ...). Null only for records made outside the flow (fixtures).
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


class AgreementSigningCode(Base):
    """One one-time code sent to an investor to confirm the Investor Service Agreement with a commitment.

    The code is the investor's signature on Contract 4 (service.sign_with_code), so it is bound to what it signs:
    the commitment and the agreement version it was asked for. Only the investor's most recent row can be redeemed.
    Rows are kept after use: they are the send counter (cooldown, daily limit) and the trail behind the signature.
    """

    __tablename__ = "investor_agreement_codes"
    __table_args__ = (Index("ix_investor_agreement_codes_investor_created", "investor_id", "created_at"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    investor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    tranche_id = Column(UUID(as_uuid=True), ForeignKey("tranches.id", ondelete="RESTRICT"), nullable=False)
    version = Column(Text, nullable=False)
    # Canonical +84xxxxxxxxx: the account's phone number when the code was sent.
    phone = Column(Text, nullable=False)
    # HMAC-SHA256 of the code, keyed by SECRET_KEY and bound to this row, commitment and version. Never the code.
    code_hash = Column(Text, nullable=False)
    channel = Column(Text, nullable=False)  # zalo | log
    expires_at = Column(DateTime(timezone=True), nullable=False)
    attempts = Column(Integer, nullable=False, server_default="0")
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, server_default=func.now())
