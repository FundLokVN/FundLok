"""add loan_applications.guarantor_address

The Standing Offer (Contract 3) and the Loan Contract template (Contract 5)
name the Personal Guarantor with an address. The guarantor's approved eKYC
gives the name and ID number but no address (GVerify's OCR returns none), so
the SME enters it on the signing step (PUT /loans/applications/{id}/
guarantor-address) and it is kept here. Nullable: nothing to backfill, and
the eKYC's own address wins when it has one.

Revision ID: b8e2d6f4a1c7
Revises: 3f4ff91a3825
Create Date: 2026-11-11 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8e2d6f4a1c7'
down_revision: Union[str, None] = '3f4ff91a3825'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("loan_applications", sa.Column("guarantor_address", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("loan_applications", "guarantor_address")
