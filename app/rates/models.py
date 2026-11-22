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


# The calculator's figures. Required on a CALCULATOR row, absent on a QUIZ row.
_CALCULATOR_COLUMNS = (
    "operating_months",
    "employee_count",
    "revenue_l12m",
    "revenue_prev_12m",
    "cogs_l12m",
    "fixed_costs_l12m",
    "variable_costs_l12m",
    "estimated_rate_min",
    "estimated_rate_max",
)
_CALCULATOR_COMPLETE = "source <> 'CALCULATOR' OR (" + " AND ".join(
    f"{c} IS NOT NULL" for c in _CALCULATOR_COLUMNS
) + ")"
_QUIZ_COMPLETE = (
    "source <> 'QUIZ' OR ("
    "monthly_revenue IS NOT NULL AND operating_band IS NOT NULL"
    " AND quiz_outcome IS NOT NULL AND quiz_params_version IS NOT NULL"
    " AND full_name IS NOT NULL AND phone IS NOT NULL AND consented_contact_at IS NOT NULL"
    " AND ((quiz_outcome = 'QUOTED' AND estimated_rate_min IS NOT NULL AND estimated_rate_max IS NOT NULL)"
    " OR (quiz_outcome = 'DECLINED' AND estimated_rate_min IS NULL AND estimated_rate_max IS NULL)))"
)


class RateInquiry(Base):
    """One enquiry from the public /rate page.

    Two kinds of row share this table so both land in the same admin list:

    - CALCULATOR: the long form priced by `grade_lite` (`POST /calculate`).
    - QUIZ: the five-question illustrative quiz (`POST /sme/quiz-leads`). Its
      rate is computed in the browser from a published formula, never by the
      underwriting engine, and is stored as the monthly rate the visitor saw.
      See docs/specs/rates/sme-rate-quiz.md.
    """

    __tablename__ = "rate_inquiries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(String(128), nullable=True)
    ip_address = Column(INET, nullable=True)
    user_agent = Column(Text, nullable=True)

    source = Column(String(20), nullable=False, server_default="CALCULATOR")

    # Business Profile (calculator)
    industry = Column(String(100), nullable=False)
    operating_months = Column(Integer, nullable=True)
    employee_count = Column(Integer, nullable=True)

    # 12-Month Financials (VND, calculator)
    revenue_l12m = Column(BigInteger, nullable=True)
    revenue_prev_12m = Column(BigInteger, nullable=True)
    cogs_l12m = Column(BigInteger, nullable=True)
    fixed_costs_l12m = Column(BigInteger, nullable=True)
    variable_costs_l12m = Column(BigInteger, nullable=True)

    # Loan Request
    requested_amount = Column(BigInteger, nullable=False)
    tenor_months = Column(Integer, nullable=False)

    # Optional Seasonality & Concentration Factors
    peak_month_revenue = Column(BigInteger, nullable=True)
    lowest_month_revenue = Column(BigInteger, nullable=True)
    top_1_customer_share = Column(Numeric(5, 2), nullable=True)
    top_3_customer_share = Column(Numeric(5, 2), nullable=True)

    # Engine output (calculator), or the quiz's monthly rate in both columns
    estimated_rate_min = Column(Numeric(5, 2), nullable=True)
    estimated_rate_max = Column(Numeric(5, 2), nullable=True)
    risk_tier = Column(String(20), nullable=True)
    calculated_monthly_payment = Column(BigInteger, nullable=True)

    # Contact for follow-up (sales and marketing), given with consent on the
    # form. Nullable: inquiries made before the form asked have none.
    full_name = Column(String(120), nullable=True)
    company_name = Column(String(200), nullable=True)
    email = Column(String(254), nullable=True)
    phone = Column(String(20), nullable=True)
    consented_contact_at = Column(DateTime(timezone=True), nullable=True)

    # Quiz answers not covered by the columns above
    monthly_revenue = Column(BigInteger, nullable=True)
    operating_band = Column(String(10), nullable=True)
    quiz_outcome = Column(String(10), nullable=True)
    quiz_params_version = Column(String(20), nullable=True)

    # Where the visitor came from (ad tracking parameters on the landing URL)
    utm_source = Column(String(100), nullable=True)
    utm_medium = Column(String(100), nullable=True)
    utm_campaign = Column(String(100), nullable=True)
    utm_content = Column(String(100), nullable=True)
    utm_term = Column(String(100), nullable=True)
    fbclid = Column(String(255), nullable=True)

    # Metadata
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("requested_amount BETWEEN 20000000 AND 1000000000", name="chk_rate_inquiries_requested_amount"),
        CheckConstraint("tenor_months BETWEEN 1 AND 6", name="chk_rate_inquiries_tenor_months"),
        CheckConstraint("top_1_customer_share BETWEEN 0 AND 100", name="chk_rate_inquiries_top_1_customer_share"),
        CheckConstraint("top_3_customer_share BETWEEN 0 AND 100", name="chk_rate_inquiries_top_3_customer_share"),
        CheckConstraint("source IN ('CALCULATOR', 'QUIZ')", name="chk_rate_inquiries_source"),
        CheckConstraint("operating_band IN ('LT_1Y', 'Y1_3', 'GT_3Y')", name="chk_rate_inquiries_operating_band"),
        CheckConstraint("quiz_outcome IN ('QUOTED', 'DECLINED')", name="chk_rate_inquiries_quiz_outcome"),
        CheckConstraint("monthly_revenue > 0", name="chk_rate_inquiries_monthly_revenue"),
        CheckConstraint(_CALCULATOR_COMPLETE, name="chk_rate_inquiries_calculator_complete"),
        CheckConstraint(_QUIZ_COMPLETE, name="chk_rate_inquiries_quiz_complete"),
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
