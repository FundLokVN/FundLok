"""a loan per investment: facility no longer unique per contract

Investments now start repaying as soon as staff approve them (DirectTransferParams.repay_from_approval): each
approved investment becomes its own facility, so one contract (one funding round) has a facility per investment.
The round-at-close design formed one facility per contract and enforced it with uq_facility_contract_id; this
drops that constraint and keeps an index for the lookups by contract.

Downgrade restores the constraint, which fails while any contract has more than one facility.

Revision ID: 5146db408f85
Revises: 099de0f5b480
Create Date: 2026-11-02 16:40:00

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '5146db408f85'
down_revision: Union[str, None] = '099de0f5b480'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_facility_contract_id", "facility", type_="unique")
    op.create_index("ix_facility_contract_id", "facility", ["contract_id"])


def downgrade() -> None:
    op.drop_index("ix_facility_contract_id", table_name="facility")
    op.create_unique_constraint("uq_facility_contract_id", "facility", ["contract_id"])
