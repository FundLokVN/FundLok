"""admit sme_standing_offer loan document type

After FundLok approves a request, the SME signs two documents and uploads them
back (app/loans/signing.py): the SME Service Agreement (Contract 2), already a
`sme_service_agreement` row, and the Standing Offer to Conclude Loan Contracts
(Contract 3), signed by the SME and by its Personal Guarantor. The signed
Standing Offer is a loan_application_documents row of document_type
`sme_standing_offer`. This admits the type to the document_type check.

Revision ID: a37775d5b929
Revises: 0538f8bc779a
Create Date: 2026-11-10 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a37775d5b929'
down_revision: Union[str, None] = '0538f8bc779a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TYPES_BEFORE = (
    "'legal_charter', 'business_registration', 'vat_tax_zip', "
    "'financial_report', 'e_invoice_data', 'cic_report', 'tax_filings', "
    "'sme_service_agreement'"
)


def _check(types: str) -> None:
    op.drop_constraint("loan_application_documents_document_type_check", "loan_application_documents", type_="check")
    op.create_check_constraint(
        "loan_application_documents_document_type_check",
        "loan_application_documents",
        f"document_type IN ({types})",
    )


def upgrade() -> None:
    _check(TYPES_BEFORE + ", 'sme_standing_offer'")


def downgrade() -> None:
    # A signed Standing Offer is the SME's executed offer to every investor;
    # deleting it to fit the old check would destroy it silently, so refuse.
    held = op.get_bind().execute(
        sa.text("SELECT count(*) FROM loan_application_documents WHERE document_type = 'sme_standing_offer'")
    ).scalar()
    if held:
        raise RuntimeError(f"{held} sme_standing_offer document(s) exist; resolve them before downgrading.")
    _check(TYPES_BEFORE)
