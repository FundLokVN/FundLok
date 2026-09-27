"""Re-running a score run on an application that is still under review.

The first run moves SUBMITTED -> UNDER_REVIEW. An operator can score again
while the application is undecided — typically after its financials change —
and each run is a new row, never an edit of the old one. Once a run is locked
or the application decided, the score is what the decision rested on, so a
re-run is refused.
"""
from uuid import UUID

import pytest
from sqlalchemy import select

from app.lending.models import LoanApplication, ScoreRun
from app.utils.audit import AuditLog
from tests.underwriting.test_score_run_persistence import _put_financials, scored_setup  # noqa: F401


async def _score(client, admin, application_id):
    return await client.post(
        "/underwriting/score-runs",
        json={"application_id": application_id, "mode": None},
        headers=admin["headers"],
    )


async def test_rerun_after_the_financials_arrive_produces_a_grade(client, db_session, scored_setup):  # noqa: F811
    admin, app_id = scored_setup["admin"], scored_setup["application"]["id"]

    first = await _score(client, admin, app_id)
    assert first.status_code == 201, first.text
    assert first.json()["decision"] == "INSUFFICIENT_DATA"

    await _put_financials(client, admin, app_id)
    second = await _score(client, admin, app_id)
    assert second.status_code == 201, second.text
    assert second.json()["decision"] != "INSUFFICIENT_DATA"
    assert second.json()["id"] != first.json()["id"]

    # Both runs are kept: the re-run appends, it does not overwrite.
    runs = (await db_session.execute(select(ScoreRun).where(ScoreRun.application_id == UUID(app_id)))).scalars().all()
    assert len(runs) == 2


async def test_a_rerun_is_audited_with_the_run_it_follows(client, db_session, scored_setup):  # noqa: F811
    admin, app_id = scored_setup["admin"], scored_setup["application"]["id"]
    first = (await _score(client, admin, app_id)).json()
    second = (await _score(client, admin, app_id)).json()

    entry = (
        await db_session.execute(select(AuditLog).where(AuditLog.entity_id == UUID(second["id"])))
    ).scalar_one()
    assert entry.action == "RERUN"
    assert entry.after_state["previous_score_run_id"] == first["id"]


async def test_no_rerun_once_a_run_is_locked(client, scored_setup):  # noqa: F811
    admin, app_id = scored_setup["admin"], scored_setup["application"]["id"]
    run = (await _score(client, admin, app_id)).json()
    locked = await client.post(f"/underwriting/score-runs/{run['id']}/approve", headers=admin["headers"])
    assert locked.status_code == 200, locked.text

    again = await _score(client, admin, app_id)
    assert again.status_code == 409
    assert "locked" in again.json()["detail"]


@pytest.mark.parametrize("decision", ["APPROVED", "REJECTED"])
async def test_no_rerun_once_the_application_is_decided(client, db_session, scored_setup, decision):  # noqa: F811
    admin, app_id = scored_setup["admin"], scored_setup["application"]["id"]
    assert (await _score(client, admin, app_id)).status_code == 201

    row = (await db_session.execute(select(LoanApplication).where(LoanApplication.id == UUID(app_id)))).scalar_one()
    row.admin_approval = decision
    await db_session.commit()

    again = await _score(client, admin, app_id)
    assert again.status_code == 409
    assert "decided" in again.json()["detail"]
