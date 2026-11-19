"""one borrower v1.1: one project in progress per business, so a business can borrow again

v1.0 (6664b74ae183) made a business's project unique forever, so a business that repaid in full, or whose round
failed or request was refused, could never apply again: its verified tax code was already taken (D-040, D-050,
docs/specs/projects/one-borrower.md v1.1). A project is now CLOSED when its request ends
(app/projects/borrower.close_project_if_finished), and the tax code is unique only among projects not CLOSED or
ARCHIVED.

In order:
  1. close every project that has already ended, by the same rule as borrower._request_ended (no round live, and
     every round failed, cancelled or repaid, or every application rejected), with an audit row for each;
  2. drop uq_projects_tax_business and projects_tax_id_key (migration 0001's unique on projects.tax_id);
  3. add uq_projects_one_open_per_business and uq_projects_one_open_per_tax_id, partial on open projects.

Every project is unique today, so step 3 cannot find two open projects for one business. The downgrade puts the
old indexes back and refuses if two projects now share a business or tax code.

Revision ID: d5c1e8a7f302
Revises: a4d2c7e9b130
Create Date: 2026-11-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d5c1e8a7f302"
down_revision: Union[str, Sequence[str], None] = "a4d2c7e9b130"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OPEN = "status NOT IN ('CLOSED', 'ARCHIVED')"
LIVE_WINDOW = "('OPEN','FULL','CLOSING','AWAITING_SME','AWAITING_PAYMENT','REPAYING')"
ENDED_WINDOW = "('FAILED','REPAID','CANCELLED')"

CLOSE_ENDED = f"""
WITH closed AS (
    UPDATE projects p SET status = 'CLOSED', updated_at = now()
    FROM projects old  -- the row as it was, for the audit's before_state
    WHERE old.id = p.id AND p.{OPEN}
      AND NOT EXISTS (SELECT 1 FROM funding_windows w WHERE w.project_id = p.id AND w.status IN {LIVE_WINDOW})
      AND (
        (EXISTS (SELECT 1 FROM funding_windows w WHERE w.project_id = p.id)
         AND NOT EXISTS (SELECT 1 FROM funding_windows w WHERE w.project_id = p.id AND w.status NOT IN {ENDED_WINDOW}))
        OR
        (EXISTS (SELECT 1 FROM loan_applications a WHERE a.project_id = p.id)
         AND NOT EXISTS (SELECT 1 FROM loan_applications a WHERE a.project_id = p.id
                         AND a.status <> 'REJECTED' AND a.admin_approval <> 'REJECTED'))
      )
    RETURNING p.id, old.status AS before
)
INSERT INTO audit_logs (entity_type, entity_id, action, actor_id, before_state, after_state)
SELECT 'PROJECT', id, 'PROJECT_CLOSED', NULL, jsonb_build_object('status', before),
       jsonb_build_object('status', 'CLOSED', 'reason', 'MIGRATION_ALREADY_ENDED')
FROM closed
"""


def upgrade() -> None:
    op.execute(CLOSE_ENDED)
    op.drop_index("uq_projects_tax_business", table_name="projects")
    op.drop_constraint("projects_tax_id_key", "projects", type_="unique")
    op.create_index(
        "uq_projects_one_open_per_business", "projects", ["tax_business"], unique=True, postgresql_where=sa.text(OPEN)
    )
    op.create_index(
        "uq_projects_one_open_per_tax_id", "projects", ["tax_id"], unique=True, postgresql_where=sa.text(OPEN)
    )


def downgrade() -> None:
    bind = op.get_bind()
    for column in ("tax_business", "tax_id"):
        shared = bind.execute(
            sa.text(
                f"SELECT {column}, string_agg(id::text || ' (' || status || ')', ', ') FROM projects "
                f"WHERE {column} IS NOT NULL GROUP BY {column} HAVING count(*) > 1"
            )
        ).all()
        if shared:
            listed = "\n".join(f"  {row[0]}: {row[1]}" for row in shared)
            raise RuntimeError(
                f"Cannot go back to one project per business: these share a {column}\n{listed}\n"
                "Keep one project per business (change the others' tax codes), then downgrade again."
            )
    op.drop_index("uq_projects_one_open_per_tax_id", table_name="projects", postgresql_where=sa.text(OPEN))
    op.drop_index("uq_projects_one_open_per_business", table_name="projects", postgresql_where=sa.text(OPEN))
    op.create_unique_constraint("projects_tax_id_key", "projects", ["tax_id"])
    op.create_index("uq_projects_tax_business", "projects", ["tax_business"], unique=True)
