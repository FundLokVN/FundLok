"""Day-14 transfer rule: QR codes, cut-offs, late-money top-up pledges, reminders

Spec: docs/specs/direct_transfer/day14-transfer-rule.md section 3 (Loc, 18 Nov 2026).

New tables: payment_qrs (one row per QR shown, code = reference + 4 digits, valid 15 minutes), top_up_pledges
(late money on Day 14 becomes the lender's top-up), transfer_reminders (Day-14 "sign and pay by 14:45" log).
funding_windows: no_show_at (15:00) and receipt_deadline_at (15:30), nullable. transfers: qr_code, payment_timing
and late_reason, nullable. Additive only: no existing row changes.

Revision ID: d14a7c2e5b31
Revises: b4d8e1a7c953
Create Date: 2026-11-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd14a7c2e5b31'
down_revision: Union[str, Sequence[str], None] = 'b4d8e1a7c953'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _uuid(name: str, *args, **kw) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, **kw)


def upgrade() -> None:
    op.add_column("funding_windows", _ts("no_show_at", nullable=True))
    op.add_column("funding_windows", _ts("receipt_deadline_at", nullable=True))

    op.add_column("transfers", sa.Column("qr_code", sa.Text(), nullable=True))
    op.add_column("transfers", sa.Column("payment_timing", sa.Text(), nullable=True))
    op.add_column("transfers", sa.Column("late_reason", sa.Text(), nullable=True))
    op.create_check_constraint("ck_transfers_payment_timing", "transfers", "payment_timing IN ('ON_TIME','LATE')")
    op.create_check_constraint(
        "ck_transfers_late_reason", "transfers", "late_reason IN ('EXPIRED_CODE','NO_QR_CODE','AFTER_CUTOFF')"
    )

    op.create_table(
        "payment_qrs",
        _uuid("id", primary_key=True),
        _uuid("tranche_id", sa.ForeignKey("tranches.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("code", sa.Text(), nullable=False, unique=True),
        _ts("issued_at", nullable=False),
        _ts("expires_at", nullable=False),
        _ts("superseded_at", nullable=True),
        _ts("created_at", server_default=sa.func.now()),
        sa.CheckConstraint("expires_at > issued_at", name="ck_payment_qrs_window"),
    )
    op.create_index("ix_payment_qrs_tranche_issued", "payment_qrs", ["tranche_id", "issued_at"])

    op.create_table(
        "top_up_pledges",
        _uuid("id", primary_key=True),
        _uuid("window_id", sa.ForeignKey("funding_windows.id", ondelete="RESTRICT"), nullable=False),
        _uuid("investor_id", sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        _uuid("tranche_id", sa.ForeignKey("tranches.id", ondelete="RESTRICT"), nullable=False, unique=True),
        _uuid("transfer_id", sa.ForeignKey("transfers.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PLEDGED"),
        _ts("recorded_at", nullable=False),
        _ts("signed_at", nullable=True),
        _ts("confirmed_at", nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
        _ts("created_at", server_default=sa.func.now()),
        sa.CheckConstraint("amount_vnd > 0", name="ck_top_up_pledges_amount"),
        sa.CheckConstraint("reason IN ('EXPIRED_CODE','NO_QR_CODE','AFTER_CUTOFF')", name="ck_top_up_pledges_reason"),
        sa.CheckConstraint("status IN ('PLEDGED','SIGNED','CONFIRMED','VOID')", name="ck_top_up_pledges_status"),
    )
    op.create_index("ix_top_up_pledges_investor", "top_up_pledges", ["investor_id"])

    op.create_table(
        "transfer_reminders",
        _uuid("tranche_id", sa.ForeignKey("tranches.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("channel", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("sent_to", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        _ts("claimed_at", nullable=False),
        _ts("sent_at", nullable=True),
        sa.CheckConstraint("kind IN ('DAY14_MORNING','DAY14_AFTERNOON')", name="ck_transfer_reminders_kind"),
        sa.CheckConstraint("channel IN ('IN_APP','EMAIL','ZALO')", name="ck_transfer_reminders_channel"),
        sa.CheckConstraint(
            "status IN ('SENDING','SENT','FAILED','SKIPPED')", name="ck_transfer_reminders_status"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    pledges = bind.execute(sa.text("SELECT count(*) FROM top_up_pledges")).scalar_one()
    if pledges:
        raise RuntimeError(
            f"Cannot downgrade: {pledges} top-up pledge(s) exist. They record late money an SME kept for a lender; "
            "settle or export them first."
        )
    op.drop_table("transfer_reminders")
    op.drop_index("ix_top_up_pledges_investor", table_name="top_up_pledges")
    op.drop_table("top_up_pledges")
    op.drop_index("ix_payment_qrs_tranche_issued", table_name="payment_qrs")
    op.drop_table("payment_qrs")
    op.drop_constraint("ck_transfers_late_reason", "transfers", type_="check")
    op.drop_constraint("ck_transfers_payment_timing", "transfers", type_="check")
    op.drop_column("transfers", "late_reason")
    op.drop_column("transfers", "payment_timing")
    op.drop_column("transfers", "qr_code")
    op.drop_column("funding_windows", "receipt_deadline_at")
    op.drop_column("funding_windows", "no_show_at")
