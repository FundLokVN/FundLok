"""The Personal Guarantor's Zalo one-time-code signature (Money flow FINAL v1.0).

Spec: docs/specs/loans/guarantor-zalo-otp.md

The guarantor signs by a one-time code sent by Zalo to the phone on their FundLok account, not with a personal
digital signature: on Day 0 the Standing Offer (with the Loan Contract template, `LISTING_DOCUMENTS`), and on Day 13
each investor's loan contract (`WINDOW_LOAN_CONTRACTS`). One code signs every document of that step; what it signed
is kept with its hashes.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.base import Base

PURPOSE_LISTING_DOCUMENTS = "LISTING_DOCUMENTS"  # subject: the loan application (Day 0)
PURPOSE_WINDOW_LOAN_CONTRACTS = "WINDOW_LOAN_CONTRACTS"  # subject: the funding window (Day 13)
PURPOSES = (PURPOSE_LISTING_DOCUMENTS, PURPOSE_WINDOW_LOAN_CONTRACTS)
_PURPOSE_SQL = "purpose IN ('LISTING_DOCUMENTS','WINDOW_LOAN_CONTRACTS')"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GuarantorSigningCode(Base):
    """One code sent to the guarantor. A new one retires the last; single use, short lived, a few attempts."""

    __tablename__ = "guarantor_signing_codes"
    __table_args__ = (
        CheckConstraint(_PURPOSE_SQL, name="ck_guarantor_signing_codes_purpose"),
        Index("ix_guarantor_signing_codes_user_created", "user_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    purpose = Column(Text, nullable=False)
    subject_id = Column(UUID(as_uuid=True), nullable=False)
    phone = Column(Text, nullable=False)  # canonical +84...
    code_hash = Column(Text, nullable=False)
    channel = Column(Text, nullable=False)  # zalo | log
    expires_at = Column(DateTime(timezone=True), nullable=False)
    attempts = Column(Integer, nullable=False, server_default="0")
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, server_default=func.now())


class GuarantorSignature(Base):
    """The guarantor's signature on one step's documents: who, which phone, which code, and the documents' hashes."""

    __tablename__ = "guarantor_signatures"
    __table_args__ = (
        CheckConstraint(_PURPOSE_SQL, name="ck_guarantor_signatures_purpose"),
        UniqueConstraint("purpose", "subject_id", name="uq_guarantor_signatures_purpose_subject"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purpose = Column(Text, nullable=False)
    subject_id = Column(UUID(as_uuid=True), nullable=False)
    signer_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    signer_name = Column(Text, nullable=True)
    signer_id_number = Column(Text, nullable=False)  # the approved eKYC's ID number
    phone = Column(Text, nullable=False)
    code_id = Column(UUID(as_uuid=True), ForeignKey("guarantor_signing_codes.id", ondelete="RESTRICT"), nullable=False)
    # [{"document_type": ..., "sha256": ..., "check_code": ...}, ...]: exactly what the code signed.
    documents = Column(JSONB, nullable=False)
    signed_at = Column(DateTime(timezone=True), nullable=False)
    ip_address = Column(Text, nullable=True)
    user_agent = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, server_default=func.now())
