"""rate_inquiries: quiz leads from the illustrative /rate quiz

Revision ID: c4e9a2b7d1f6
Revises: d5c1e8a7f302
Create Date: 2026-11-20 12:00:00

Quiz leads share rate_inquiries with the long calculator so both appear in the
admin "Rate inquiries" list. A quiz row has none of the calculator's figures,
so those nine columns become nullable, and two CHECKs keep each kind of row
complete: a CALCULATOR row still needs every figure it needed before, and a
QUIZ row needs its own answers, the visitor's contact and consent.

See docs/specs/rates/sme-rate-quiz.md. Nothing here touches the underwriting
engine's tables.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4e9a2b7d1f6"
down_revision: Union[str, None] = "d5c1e8a7f302"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CALCULATOR_COLUMNS = (
    ("operating_months", sa.Integer()),
    ("employee_count", sa.Integer()),
    ("revenue_l12m", sa.BigInteger()),
    ("revenue_prev_12m", sa.BigInteger()),
    ("cogs_l12m", sa.BigInteger()),
    ("fixed_costs_l12m", sa.BigInteger()),
    ("variable_costs_l12m", sa.BigInteger()),
    ("estimated_rate_min", sa.Numeric(5, 2)),
    ("estimated_rate_max", sa.Numeric(5, 2)),
)

CALCULATOR_COMPLETE = "source <> 'CALCULATOR' OR (" + " AND ".join(
    f"{name} IS NOT NULL" for name, _ in CALCULATOR_COLUMNS
) + ")"

QUIZ_COMPLETE = (
    "source <> 'QUIZ' OR ("
    "monthly_revenue IS NOT NULL AND operating_band IS NOT NULL"
    " AND quiz_outcome IS NOT NULL AND quiz_params_version IS NOT NULL"
    " AND full_name IS NOT NULL AND phone IS NOT NULL AND consented_contact_at IS NOT NULL"
    " AND ((quiz_outcome = 'QUOTED' AND estimated_rate_min IS NOT NULL AND estimated_rate_max IS NOT NULL)"
    " OR (quiz_outcome = 'DECLINED' AND estimated_rate_min IS NULL AND estimated_rate_max IS NULL)))"
)

NEW_COLUMNS = (
    ("monthly_revenue", sa.BigInteger),
    ("operating_band", lambda: sa.String(length=10)),
    ("quiz_outcome", lambda: sa.String(length=10)),
    ("quiz_params_version", lambda: sa.String(length=20)),
    ("utm_source", lambda: sa.String(length=100)),
    ("utm_medium", lambda: sa.String(length=100)),
    ("utm_campaign", lambda: sa.String(length=100)),
    ("utm_content", lambda: sa.String(length=100)),
    ("utm_term", lambda: sa.String(length=100)),
    ("fbclid", lambda: sa.String(length=255)),
)

CHECKS = (
    ("chk_rate_inquiries_source", "source IN ('CALCULATOR', 'QUIZ')"),
    ("chk_rate_inquiries_operating_band", "operating_band IN ('LT_1Y', 'Y1_3', 'GT_3Y')"),
    ("chk_rate_inquiries_quiz_outcome", "quiz_outcome IN ('QUOTED', 'DECLINED')"),
    ("chk_rate_inquiries_monthly_revenue", "monthly_revenue > 0"),
    ("chk_rate_inquiries_calculator_complete", CALCULATOR_COMPLETE),
    ("chk_rate_inquiries_quiz_complete", QUIZ_COMPLETE),
)


def upgrade() -> None:
    # Every existing row came from the calculator.
    op.add_column(
        "rate_inquiries",
        sa.Column("source", sa.String(length=20), nullable=False, server_default="CALCULATOR"),
    )
    for name, make_type in NEW_COLUMNS:
        op.add_column("rate_inquiries", sa.Column(name, make_type(), nullable=True))
    for name, type_ in CALCULATOR_COLUMNS:
        op.alter_column("rate_inquiries", name, existing_type=type_, nullable=True)
    for name, condition in CHECKS:
        op.create_check_constraint(name, "rate_inquiries", condition)


def downgrade() -> None:
    # Quiz rows cannot satisfy the calculator's NOT NULLs, so they go first.
    op.execute("DELETE FROM rate_inquiries WHERE source = 'QUIZ'")
    for name, _ in reversed(CHECKS):
        op.drop_constraint(name, "rate_inquiries", type_="check")
    for name, type_ in CALCULATOR_COLUMNS:
        op.alter_column("rate_inquiries", name, existing_type=type_, nullable=False)
    for name, _ in reversed(NEW_COLUMNS):
        op.drop_column("rate_inquiries", name)
    op.drop_column("rate_inquiries", "source")
