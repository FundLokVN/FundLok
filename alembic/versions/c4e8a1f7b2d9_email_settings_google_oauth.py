"""email settings: Google OAuth refresh token replaces the Gmail app password

Revision ID: c4e8a1f7b2d9
Revises: 5146db408f85
Create Date: 2026-11-07 10:00:00.000000

Admin > Email moves from SMTP + App Password to Google OAuth 2.0 and the
Gmail API (docs/specs/admin/internal-email.md).

- app_password_encrypted is dropped. A stored App Password cannot become an
  OAuth token, so every admin connects again with "Connect with Google".
  Their sender name, signature and sending switch are kept.
- gmail_address becomes nullable: it now comes from Google at connect time,
  and a row can exist (sender name, signature) before any account is
  connected. Existing addresses are kept, so the console can show which
  account to reconnect.
- last_verified_at is cleared: a test email proved the App Password, not a
  token that does not exist yet.

Downgrade restores the column empty (the passwords are gone) and fills any
missing gmail_address with the admin's own login email so NOT NULL holds.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4e8a1f7b2d9'
down_revision: Union[str, None] = '5146db408f85'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('email_settings', sa.Column('refresh_token_encrypted', sa.Text(), nullable=True))
    op.add_column('email_settings', sa.Column('oauth_scopes', sa.Text(), nullable=True))
    op.add_column(
        'email_settings', sa.Column('token_updated_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.alter_column('email_settings', 'gmail_address', existing_type=sa.Text(), nullable=True)
    op.execute("UPDATE email_settings SET last_verified_at = NULL")
    op.drop_column('email_settings', 'app_password_encrypted')


def downgrade() -> None:
    op.add_column(
        'email_settings', sa.Column('app_password_encrypted', sa.Text(), nullable=True)
    )
    op.execute(
        "UPDATE email_settings AS s SET gmail_address = u.email "
        "FROM users AS u WHERE s.user_id = u.id AND s.gmail_address IS NULL"
    )
    op.alter_column('email_settings', 'gmail_address', existing_type=sa.Text(), nullable=False)
    op.drop_column('email_settings', 'token_updated_at')
    op.drop_column('email_settings', 'oauth_scopes')
    op.drop_column('email_settings', 'refresh_token_encrypted')
