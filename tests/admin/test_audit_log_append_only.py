"""SEC-02: audit_logs is append-only at the database layer.

Migration 17ec24f17dba adds a row trigger that refuses UPDATE and DELETE and
revokes both privileges from the app's role, mirroring the ledger's trigger
tests (tests/ledger/test_ledger_foundation.py). TRUNCATE stays allowed, which
every test here relies on: conftest.py empties audit_logs between tests.
"""
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.lending.models import AuditLog
from app.utils.audit import append_audit


async def _audit_row(db_session) -> AuditLog:
    append_audit(
        db_session,
        entity_type="USER",
        entity_id=uuid.uuid4(),
        action="APPEND_ONLY_PROBE",
        actor_id=None,
        after_state={"probe": True},
    )
    await db_session.commit()
    result = await db_session.execute(select(AuditLog).where(AuditLog.action == "APPEND_ONLY_PROBE"))
    return result.scalar_one()


async def test_new_audit_rows_can_still_be_written(db_session):
    row = await _audit_row(db_session)
    assert row.after_state == {"probe": True}


async def test_updating_an_audit_row_is_refused_by_the_database(db_session):
    row_id = (await _audit_row(db_session)).id  # the rollback below expires the object

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            text("UPDATE audit_logs SET action = 'TAMPERED' WHERE id = :id"), {"id": row_id}
        )
    await db_session.rollback()

    action = await db_session.scalar(select(AuditLog.action).where(AuditLog.id == row_id))
    assert action == "APPEND_ONLY_PROBE"


async def test_deleting_an_audit_row_is_refused_by_the_database(db_session):
    row_id = (await _audit_row(db_session)).id  # the rollback below expires the object

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(text("DELETE FROM audit_logs WHERE id = :id"), {"id": row_id})
    await db_session.rollback()

    assert await db_session.scalar(select(AuditLog.id).where(AuditLog.id == row_id)) == row_id


async def test_a_user_with_audit_history_cannot_be_deleted(db_session, make_user):
    """actor_id is ON DELETE SET NULL, so deleting the user would rewrite
    their audit rows, and the trigger refuses that. Erasure has to anonymise
    the user row instead (see the migration's docstring)."""
    member = await make_user(role="INVESTOR")
    signed_in = await db_session.scalar(
        select(AuditLog.id).where(AuditLog.actor_id == uuid.UUID(member["id"]))
    )
    assert signed_in is not None, "expected the sign-in to have left an audit row"

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            text("DELETE FROM users WHERE id = :id"), {"id": uuid.UUID(member["id"])}
        )
    await db_session.rollback()


async def test_the_app_role_holds_no_update_or_delete_privilege(db_session):
    """The tests connect as the role that ran the migrations, as the app does
    in every deployed environment. It is a superuser here, so it would
    bypass the REVOKE at run time; reading the table's ACL checks the REVOKE
    itself."""
    result = await db_session.execute(
        text(
            """
            SELECT a.privilege_type
              FROM pg_class c
             CROSS JOIN LATERAL aclexplode(c.relacl) a
             WHERE c.oid = 'audit_logs'::regclass
               AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = current_user)
            """
        )
    )
    privileges = set(result.scalars())

    assert {"INSERT", "SELECT"} <= privileges
    assert not privileges & {"UPDATE", "DELETE"}
