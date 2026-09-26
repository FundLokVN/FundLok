import uuid
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    desc,
    func,
)
from sqlalchemy.dialects.postgresql import INET, UUID

from app.core.base import Base


class RateInquiry(Base):
    __tablename__ = "rate_inquiries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(String(128), nullable=True)
    ip_address = Column(INET, nullable=True)
    user_agent = Column(Text, nullable=True)

    # Business Profile
    industry = Column(String(100), nullable=False)
    operating_months = Column(Integer, nullable=False)
    employee_count = Column(Integer, nullable=False)

    # 12-Month Financials (VND)
    revenue_l12m = Column(BigInteger, nullable=False)
    revenue_prev_12m = Column(BigInteger, nullable=False)
    cogs_l12m = Column(BigInteger, nullable=False)
    fixed_costs_l12m = Column(BigInteger, nullable=False)
    variable_costs_l12m = Column(BigInteger, nullable=False)

    # Loan Request
    requested_amount = Column(BigInteger, nullable=False)
    tenor_months = Column(Integer, nullable=False)

    # Optional Seasonality & Concentration Factors
    peak_month_revenue = Column(BigInteger, nullable=True)
    lowest_month_revenue = Column(BigInteger, nullable=True)
    top_1_customer_share = Column(Numeric(5, 2), nullable=True)
    top_3_customer_share = Column(Numeric(5, 2), nullable=True)

    # Engine Output
    estimated_rate_min = Column(Numeric(5, 2), nullable=False)
    estimated_rate_max = Column(Numeric(5, 2), nullable=False)
    risk_tier = Column(String(20), nullable=True)
    calculated_monthly_payment = Column(BigInteger, nullable=True)

    # Metadata
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("requested_amount BETWEEN 20000000 AND 1000000000", name="chk_rate_inquiries_requested_amount"),
        CheckConstraint("tenor_months BETWEEN 1 AND 6", name="chk_rate_inquiries_tenor_months"),
        CheckConstraint("top_1_customer_share BETWEEN 0 AND 100", name="chk_rate_inquiries_top_1_customer_share"),
        CheckConstraint("top_3_customer_share BETWEEN 0 AND 100", name="chk_rate_inquiries_top_3_customer_share"),
        Index("idx_rate_inquiries_industry", "industry"),
        Index("idx_rate_inquiries_created_at", desc("created_at")),
    )
