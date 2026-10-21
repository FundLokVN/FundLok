"""The operator decision and the score-run lock serialise on the application row.

decide_application checked `admin_approval == "PENDING"` on a row read without
a lock, then wrote the decision: two concurrent decisions (two operators, or
one double-submitting APPROVED and REJECTED) could both pass the check, and
the result was a rejected application whose project was already live. Both it
and approve_score_run now read the application FOR UPDATE, so the second
request waits for the first and sees its outcome.

The suite runs every request on one connection, so a real race cannot be
staged here; these pin that the lock is taken, which is what prevents it.
"""
import pytest
from sqlalchemy import event

from app.core.database import engine
from tests.admin.test_admin_approval import make_submitted_application  # noqa: F401
from tests.underwriting.test_score_run_persistence import scored_setup  # noqa: F401


@pytest.fixture
def statements():
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", record)


def _locks_the_application(sql: list[str]) -> bool:
    return any("FROM loan_applications" in s and "FOR UPDATE" in s for s in sql)


async def test_a_decision_reads_the_application_for_update(
    client, make_admin, make_submitted_application, statements  # noqa: F811
):
    _, _, application = await make_submitted_application()
    admin = await make_admin()
    statements.clear()

    resp = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "REJECTED", "note": "Incomplete"},
        headers=admin["headers"],
    )

    assert resp.status_code == 200, resp.text
    assert _locks_the_application(statements)


async def test_locking_a_score_run_takes_the_same_row_lock(
    client, scored_setup, statements  # noqa: F811
):
    admin, app_id = scored_setup["admin"], scored_setup["application"]["id"]
    run = await client.post(
        "/underwriting/score-runs",
        json={"application_id": app_id, "mode": None},
        headers=admin["headers"],
    )
    statements.clear()

    resp = await client.post(
        f"/underwriting/score-runs/{run.json()['id']}/approve", headers=admin["headers"]
    )

    assert resp.status_code == 200, resp.text
    assert _locks_the_application(statements)
