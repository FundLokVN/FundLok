"""create_rate_inquiries_table

Revision ID: abdfb76f0409
Revises: c1d4e7a9b230
Create Date: 2026-09-26 22:42:42.813936

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'abdfb76f0409'
down_revision: Union[str, None] = 'c1d4e7a9b230'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rate_inquiries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),

        # Business Profile
        sa.Column("industry", sa.String(length=100), nullable=False),
        sa.Column("operating_months", sa.Integer(), nullable=False),
        sa.Column("employee_count", sa.Integer(), nullable=False),

        # 12-Month Financials (VND)
        sa.Column("revenue_l12m", sa.BigInteger(), nullable=False),
        sa.Column("revenue_prev_12m", sa.BigInteger(), nullable=False),
        sa.Column("cogs_l12m", sa.BigInteger(), nullable=False),
        sa.Column("fixed_costs_l12m", sa.BigInteger(), nullable=False),
        sa.Column("variable_costs_l12m", sa.BigInteger(), nullable=False),

        # Loan Request
        sa.Column("requested_amount", sa.BigInteger(), nullable=False),
        sa.Column("tenor_months", sa.Integer(), nullable=False),

        # Optional Seasonality & Concentration Factors
        sa.Column("peak_month_revenue", sa.BigInteger(), nullable=True),
        sa.Column("lowest_month_revenue", sa.BigInteger(), nullable=True),
        sa.Column("top_1_customer_share", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("top_3_customer_share", sa.Numeric(precision=5, scale=2), nullable=True),

        # Engine Output
        sa.Column("estimated_rate_min", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("estimated_rate_max", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("risk_tier", sa.String(length=20), nullable=True),
        sa.Column("calculated_monthly_payment", sa.BigInteger(), nullable=True),

        # Metadata
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),

        # Constraints
        sa.CheckConstraint("requested_amount BETWEEN 20000000 AND 1000000000", name="chk_rate_inquiries_requested_amount"),
        sa.CheckConstraint("tenor_months BETWEEN 1 AND 6", name="chk_rate_inquiries_tenor_months"),
        sa.CheckConstraint("top_1_customer_share BETWEEN 0 AND 100", name="chk_rate_inquiries_top_1_customer_share"),
        sa.CheckConstraint("top_3_customer_share BETWEEN 0 AND 100", name="chk_rate_inquiries_top_3_customer_share"),
    )
    op.create_index("idx_rate_inquiries_industry", "rate_inquiries", ["industry"])
    op.create_index("idx_rate_inquiries_created_at", "rate_inquiries", [sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index("idx_rate_inquiries_created_at", table_name="rate_inquiries")
    op.drop_index("idx_rate_inquiries_industry", table_name="rate_inquiries")
    op.drop_table("rate_inquiries")
