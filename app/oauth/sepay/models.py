import uuid

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.base import Base

STATUS_ACTIVE = "ACTIVE"
STATUS_NEEDS_RECONNECT = "NEEDS_RECONNECT"


class SepayConnection(Base):
    """One user's SePay OAuth tokens. Both tokens are Fernet-encrypted
    (app/oauth/sepay/crypto.py) and never leave the server."""

    __tablename__ = "sepay_connections"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=False)
    access_expires_at = Column(DateTime(timezone=True), nullable=False)
    refresh_expires_at = Column(DateTime(timezone=True), nullable=False)
    scopes = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default=STATUS_ACTIVE)
    connected_at = Column(DateTime(timezone=True), nullable=False)
    last_refreshed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','NEEDS_RECONNECT')", name="ck_sepay_connections_status"),
    )
