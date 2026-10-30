"""payment accounts at banks outside the NAPAS list

A user whose bank is not in the NAPAS list (app/direct_transfer/data/vn_banks.json) can now save the
account under a typed bank name: bank_bin is NULL and bank_name holds what they typed. Such an account
gets no VietQR, and get_usable_account refuses it for funding and repayment.

ck_payment_accounts_bin still applies to every bin that is set, and uq_payment_accounts_user_bank_number
never matches a NULL bin, so the new partial index dedupes typed banks on (user, name, number) instead.

Downgrade fails while any account has a NULL bin: there is no valid BIN to put back, and deleting a
user's saved account is not a schema change's call.

Revision ID: 6fad6630401b
Revises: ac9f152fa24e
Create Date: 2026-10-29 13:57:55.506921

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6fad6630401b'
down_revision: Union[str, None] = 'ac9f152fa24e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('payment_accounts', 'bank_bin', existing_type=sa.TEXT(), nullable=True)
    op.create_index(
        'uq_payment_accounts_user_other_bank_number',
        'payment_accounts',
        ['user_id', 'bank_name', 'account_number'],
        unique=True,
        postgresql_where=sa.text('bank_bin IS NULL'),
    )


def downgrade() -> None:
    op.drop_index(
        'uq_payment_accounts_user_other_bank_number',
        table_name='payment_accounts',
        postgresql_where=sa.text('bank_bin IS NULL'),
    )
    op.alter_column('payment_accounts', 'bank_bin', existing_type=sa.TEXT(), nullable=False)
