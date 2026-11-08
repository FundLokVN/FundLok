"""The Investor Service Agreement (Contract 4) each investor signed and uploaded, and which version.

ONE ROW PER SIGNED UPLOAD
Before their first commitment the investor downloads the agreement filled in with their details
(GET /funding/me/investor-agreement/document), signs it with their digital signature and uploads the signed
PDF (POST /funding/me/investor-agreement). The row is the platform's record of it: when, which version, Party B
as filled in, and the signed file kept as evidence with its hash. Reserving refuses an investor with no row for
the current version (409 INVESTOR_AGREEMENT_REQUIRED); every later commitment signs only its Investment
Confirmation (the Annex, carried in the commitment's own signed acceptance).

The table is evidence, so the app only ever INSERTS rows. The investor's latest row for a version is the one that
counts: a wrong file can be replaced by a new upload until a commitment has been made under it. A new version of
the text (text.VERSION) asks every investor to sign again: a new row, the old ones left as they were.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InvestorServiceAgreement(Base):
    __tablename__ = "investor_service_agreements"
    __table_args__ = (
        Index("ix_investor_service_agreements_investor_version_signed", "investor_id", "version", "signed_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # RESTRICT, as on tranches: a signed agreement is evidence for the investments made under it.
    investor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    # text.VERSION of the PDF the investor downloaded and signed, e.g. "investor-agreement-v0.3"
    version = Column(Text, nullable=False)
    # When the signed copy was uploaded and accepted.
    signed_at = Column(DateTime(timezone=True), nullable=False)
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
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, server_default=func.now())
