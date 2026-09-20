"""add admin_approval to loan_applications

A funding request now needs TWO approvals before it can become a contract:

  1. the verification engine's (GVerify KYB/KYC) — automated, and already
     recorded on gverify_*_verifications;
  2. a FundLok operator's — recorded here.

Neither substitutes for the other. The engine says the business is who it
claims to be; this column says a human looked at the request and agreed it
should proceed. `create_contract` refuses anything not APPROVED here.

PENDING for existing rows is the honest default: nobody has reviewed them, so
they must not silently count as approved the moment the gate lands.

`decided_at` / `decision_note` already exist on this table and were never
written by anything — the admin decision fills them rather than adding parallel
columns that would mean the same thing.

Revision ID: 9f2c4a7d1e30
Revises: 7bbdda89ceba
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "9f2c4a7d1e30"
down_revision = "7bbdda89ceba"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "loan_applications",
        sa.Column(
            "admin_approval",
            sa.Text(),
            nullable=False,
            server_default="PENDING",
        ),
    )
    op.add_column(
        "loan_applications",
        sa.Column("admin_approved_by", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_loan_applications_admin_approved_by_users",
        "loan_applications",
        "users",
        ["admin_approved_by"],
        ["id"],
        ondelete="SET NULL",
    )
    # Raw DDL to match the other CHECK constraints on this schema, which are
    # all declared in migrations rather than on the ORM models (see the
    # alembic cap note in requirements.txt).
    op.execute(
        "ALTER TABLE loan_applications "
        "ADD CONSTRAINT ck_loan_applications_admin_approval "
        "CHECK (admin_approval IN ('PENDING', 'APPROVED', 'REJECTED'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE loan_applications "
        "DROP CONSTRAINT IF EXISTS ck_loan_applications_admin_approval"
    )
    op.drop_constraint(
        "fk_loan_applications_admin_approved_by_users",
        "loan_applications",
        type_="foreignkey",
    )
    op.drop_column("loan_applications", "admin_approved_by")
    op.drop_column("loan_applications", "admin_approval")
