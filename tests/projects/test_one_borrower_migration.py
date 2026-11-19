"""Migration d5c1e8a7f302 (one-borrower v1.1): projects that had already ended are closed before the open-project
indexes go in, and the downgrade refuses once two projects share a tax code. Own throwaway database, like
tests/ledger/test_migration.py, so the downgrade cannot leave the rest of the suite without a schema."""
import os
import subprocess

import psycopg2
from sqlalchemy.engine import make_url

from conftest import ALEMBIC_COMMAND, ALEMBIC_DATABASE_URL, ROOT
from tests.ledger.test_migration import _admin_connect_kwargs

DB = "test_db_one_borrower_migration"
BEFORE = "a4d2c7e9b130"


def test_migration_closes_ended_projects_and_downgrade_refuses_shared_codes():
    admin = psycopg2.connect(**_admin_connect_kwargs())
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{DB}"')
        cur.execute(f'CREATE DATABASE "{DB}"')
    url = ALEMBIC_DATABASE_URL.replace(f"/{make_url(ALEMBIC_DATABASE_URL).database}", f"/{DB}", 1)
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        return subprocess.run([*ALEMBIC_COMMAND, *args], cwd=str(ROOT), env=env, capture_output=True, text=True)

    try:
        assert alembic("upgrade", BEFORE).returncode == 0
        conn = psycopg2.connect(make_url(url).render_as_string(hide_password=False).replace("+psycopg2", ""))
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (id, legal_name, tax_id, status) VALUES "
                "(gen_random_uuid(), 'Rejected', '0100000001', 'DRAFT'), "
                "(gen_random_uuid(), 'Under review', '0100000002', 'DRAFT'), "
                "(gen_random_uuid(), 'No application', '0100000003', 'DRAFT') RETURNING legal_name, id"
            )
            ids = dict(cur.fetchall())
            cur.execute(
                "INSERT INTO loan_applications (project_id, requested_amount, status, admin_approval) VALUES "
                "(%s, 50000000, 'REJECTED', 'REJECTED'), (%s, 50000000, 'SUBMITTED', 'PENDING')",
                (ids["Rejected"], ids["Under review"]),
            )

        upgraded = alembic("upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        with conn.cursor() as cur:
            cur.execute("SELECT legal_name, status FROM projects")
            assert dict(cur.fetchall()) == {"Rejected": "CLOSED", "Under review": "DRAFT", "No application": "DRAFT"}
            cur.execute("SELECT after_state->>'reason' FROM audit_logs WHERE entity_id = %s", (ids["Rejected"],))
            assert cur.fetchall() == [("MIGRATION_ALREADY_ENDED",)]
            # The rejected business borrows again under its own code.
            cur.execute("INSERT INTO projects (id, legal_name, tax_id, status) VALUES (gen_random_uuid(), 'Again', '0100000001', 'DRAFT')")

        refused = alembic("downgrade", BEFORE)
        assert refused.returncode != 0 and "0100000001" in refused.stderr, refused.stderr
        conn.close()
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{DB}" WITH (FORCE)')
        admin.close()
