"""admit sme_loan_contract loan document type

After approval the SME signs three documents and uploads them back
(app/loans/signing.py): the SME Service Agreement (sme_service_agreement),
the Standing Offer (sme_standing_offer) and the Loan Contract template
(Contract 5), signed by the SME and by its Personal Guarantor. The signed
template is a loan_application_documents row of document_type
`sme_loan_contract`. This admits the type to the document_type check.

Revision ID: 3f4ff91a3825
Revises: a37775d5b929
Create Date: 2026-11-10 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f4ff91a3825'
down_revision: Union[str, None] = 'a37775d5b929'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TYPES_BEFORE = (
    "'legal_charter', 'business_registration', 'vat_tax_zip', "
    "'financial_report', 'e_invoice_data', 'cic_report', 'tax_filings', "
    "'sme_service_agreement', 'sme_standing_offer'"
)


def _check(types: str) -> None:
    op.drop_constraint("loan_application_documents_document_type_check", "loan_application_documents", type_="check")
    op.create_check_constraint(
        "loan_application_documents_document_type_check",
        "loan_application_documents",
        f"document_type IN ({types})",
    )


def upgrade() -> None:
    _check(TYPES_BEFORE + ", 'sme_loan_contract'")


def downgrade() -> None:
    # A signed Loan Contract template is the SME's and the guarantor's executed
    # document; deleting it to fit the old check would destroy it silently.
    held = op.get_bind().execute(
        sa.text("SELECT count(*) FROM loan_application_documents WHERE document_type = 'sme_loan_contract'")
    ).scalar()
    if held:
        raise RuntimeError(f"{held} sme_loan_contract document(s) exist; resolve them before downgrading.")
    _check(TYPES_BEFORE)
