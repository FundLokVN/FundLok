"""upload scan statuses (compliance audit SEC-04)

Loan application documents and project documents can now wait on, or fail,
a malware scan:

    PENDING --confirm--> SCANNING --clean--> UPLOADED
                                  --hit----> INFECTED     (object deleted)
                                  --error--> SCAN_FAILED  (scanner unreachable
                                                           or file over its limit)

`documents` already allowed SCANNING (set, until now, by a mock with no
scanner behind it) and gains INFECTED and SCAN_FAILED; loan application
documents gain all three. Existing rows are valid under the wider checks.

Revision ID: 99e3ad3fb068
Revises: ac9f152fa24e
Create Date: 2026-10-20 16:10:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '99e3ad3fb068'
down_revision: Union[str, None] = 'ac9f152fa24e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT = "loan_application_documents_status_check"
DOCUMENTS_CONSTRAINT = "documents_status_check"
DOCUMENTS_OLD = "'PENDING', 'UPLOADED', 'SCANNING', 'APPROVED', 'REJECTED', 'FAILED'"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "loan_application_documents", type_="check")
    op.create_check_constraint(
        CONSTRAINT,
        "loan_application_documents",
        "status IN ('PENDING', 'SCANNING', 'UPLOADED', 'INFECTED', 'SCAN_FAILED')",
    )
    op.drop_constraint(DOCUMENTS_CONSTRAINT, "documents", type_="check")
    op.create_check_constraint(
        DOCUMENTS_CONSTRAINT, "documents", f"status IN ({DOCUMENTS_OLD}, 'INFECTED', 'SCAN_FAILED')"
    )


def downgrade() -> None:
    op.execute("UPDATE documents SET status = 'FAILED' WHERE status IN ('INFECTED', 'SCAN_FAILED')")
    op.drop_constraint(DOCUMENTS_CONSTRAINT, "documents", type_="check")
    op.create_check_constraint(DOCUMENTS_CONSTRAINT, "documents", f"status IN ({DOCUMENTS_OLD})")

    # Rows mid-scan or refused by it have no place in the old two-state check:
    # back to PENDING, which is what a re-upload would start from anyway.
    op.execute(
        "UPDATE loan_application_documents SET status = 'PENDING' "
        "WHERE status IN ('SCANNING', 'INFECTED', 'SCAN_FAILED')"
    )
    op.drop_constraint(CONSTRAINT, "loan_application_documents", type_="check")
    op.create_check_constraint(
        CONSTRAINT,
        "loan_application_documents",
        "status IN ('PENDING', 'UPLOADED')",
    )
