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


class InvestorRateInquiry(Base):
    """A lead from the investor tab of the public /rate page.

    Created when the visitor presses "Calculate my target yield" -- the lead
    capture moment in prototype rev 6: most people who calculate never sign up,
    and the business wants those names. `signed_up_at` is set later if they
    press "Sign up for more information".

    Holds personal data (name, email, phone) under Decree 13/2023/ND-CP, so the
    two consents the visitor gave are stored with timestamps, and only admins
    can read rows.

    The estimate columns are what the SERVER computed and showed, never a value
    echoed from the client -- the prototype's `shown_net_apy` read the rendered
    text back out of the DOM, which a caller could set to anything.
    """

    __tablename__ = "investor_rate_inquiries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Short, human-readable, read out over the phone ("FL-7K2QXD").
    reference = Column(String(16), nullable=False, unique=True)

    # Contact
    full_name = Column(String(120), nullable=False)
    email = Column(String(254), nullable=False)
    phone = Column(String(20), nullable=True)
    locale = Column(String(5), nullable=True)

    # Choices at calculate time
    amount_vnd = Column(BigInteger, nullable=False)
    commitment_months = Column(Integer, nullable=False)
    risk_tier = Column(String(20), nullable=False)
    reinvestment_cadence = Column(String(20), nullable=False)

    # What the server computed and showed for those choices
    bank_rate_pct = Column(Numeric(5, 2), nullable=False)
    loan_rate_pct = Column(Numeric(5, 2), nullable=False)
    net_per_loan_pct = Column(Numeric(5, 2), nullable=False)
    net_apy_pct = Column(Numeric(5, 2), nullable=False)
    estimated_return_vnd = Column(BigInteger, nullable=False)

    # Consent (Decree 13/2023) and the illustrative-only acknowledgement
    acknowledged_illustrative_at = Column(DateTime(timezone=True), nullable=False)
    consented_contact_at = Column(DateTime(timezone=True), nullable=False)

    # Sign-up: the choices they were looking at when they pressed it, which may
    # differ from calculate time because the estimate stays live afterwards.
    signed_up_at = Column(DateTime(timezone=True), nullable=True)
    signup_amount_vnd = Column(BigInteger, nullable=True)
    signup_commitment_months = Column(Integer, nullable=True)
    signup_risk_tier = Column(String(20), nullable=True)
    signup_reinvestment_cadence = Column(String(20), nullable=True)
    signup_net_apy_pct = Column(Numeric(5, 2), nullable=True)

    # Request metadata
    session_id = Column(String(128), nullable=True)
    ip_address = Column(INET, nullable=True)
    user_agent = Column(String(512), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("amount_vnd > 0", name="chk_investor_inquiries_amount"),
        CheckConstraint("commitment_months IN (6, 12)", name="chk_investor_inquiries_commitment"),
        CheckConstraint(
            "risk_tier IN ('conservative', 'balanced', 'growth')",
            name="chk_investor_inquiries_risk_tier",
        ),
        CheckConstraint(
            "reinvestment_cadence IN ('none', 'quarterly', 'monthly', 'weekly', 'daily')",
            name="chk_investor_inquiries_cadence",
        ),
        Index("idx_investor_inquiries_created_at", desc("created_at")),
        Index("idx_investor_inquiries_email", "email"),
    )
