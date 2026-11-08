"""admit sme_service_agreement loan document type

The SME signs the SME Service Agreement (Contract 2) with its enterprise
digital signature and uploads it back with the loan application; that upload
is a loan_application_documents row of document_type `sme_service_agreement`
(app/loans/agreement). This admits the type to the document_type check.

Revision ID: f736e64d867c
Revises: 33cbfd3788a4
Create Date: 2026-11-08 16:30:08.943205

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f736e64d867c'
down_revision: Union[str, None] = '33cbfd3788a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TYPES_BEFORE = (
    "'legal_charter', 'business_registration', 'vat_tax_zip', "
    "'financial_report', 'e_invoice_data', 'cic_report', 'tax_filings'"
)


def _check(types: str) -> None:
    op.drop_constraint("loan_application_documents_document_type_check", "loan_application_documents", type_="check")
    op.create_check_constraint(
        "loan_application_documents_document_type_check",
        "loan_application_documents",
        f"document_type IN ({types})",
    )


def upgrade() -> None:
    _check(TYPES_BEFORE + ", 'sme_service_agreement'")


def downgrade() -> None:
    # A signed agreement is the SME's executed contract; deleting it to fit
    # the old check would destroy it silently, so refuse instead.
    held = op.get_bind().execute(
        sa.text("SELECT count(*) FROM loan_application_documents WHERE document_type = 'sme_service_agreement'")
    ).scalar()
    if held:
        raise RuntimeError(f"{held} sme_service_agreement document(s) exist; resolve them before downgrading.")
    _check(TYPES_BEFORE)
