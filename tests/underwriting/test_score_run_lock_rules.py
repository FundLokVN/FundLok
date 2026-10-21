"""Which score run an operator may lock.

approve_score_run only checked `status == "READY"`, so an admin could LOCK a
run the engine had decided REJECT (a hard gate: KYC/AML failed, fraud flags,
an excluded industry), or one still AI_PENDING, or an older run graded on
older evidence. A LOCKED run is the engine half of the two-approval gate: it
puts the project on the marketplace and backs the contract. These pin that
only the newest run, with a decision the engine did not block, can be locked.
"""
from uuid import UUID

import pytest
from sqlalchemy import select, update

from app.lending.models import ScoreRun
from tests.underwriting.test_score_run_persistence import scored_setup  # noqa: F401


async def _score(client, admin, application_id) -> dict:
    resp = await client.post(
        "/underwriting/score-runs",
        json={"application_id": application_id, "mode": None},
        headers=admin["headers"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _approve(client, admin, run_id):
    return await client.post(
        f"/underwriting/score-runs/{run_id}/approve", headers=admin["headers"]
    )


async def _set_decision(db_session, run_id, decision: str) -> None:
    await db_session.execute(
        update(ScoreRun).where(ScoreRun.id == UUID(run_id)).values(decision=decision)
    )
    await db_session.commit()


@pytest.mark.parametrize("decision", ["REJECT", "AI_PENDING"])
async def test_a_blocked_decision_cannot_be_locked(
    client, db_session, scored_setup, decision  # noqa: F811
):
    admin, app_id = scored_setup["admin"], scored_setup["application"]["id"]
    run = await _score(client, admin, app_id)
    await _set_decision(db_session, run["id"], decision)

    resp = await _approve(client, admin, run["id"])

    assert resp.status_code == 409, resp.text
    status = (
        await db_session.execute(select(ScoreRun.status).where(ScoreRun.id == UUID(run["id"])))
    ).scalar_one()
    assert status == "READY"


@pytest.mark.parametrize("decision", ["APPROVED", "REVIEW"])
async def test_an_approvable_decision_can_be_locked(
    client, db_session, scored_setup, decision  # noqa: F811
):
    admin, app_id = scored_setup["admin"], scored_setup["application"]["id"]
    run = await _score(client, admin, app_id)
    await _set_decision(db_session, run["id"], decision)

    resp = await _approve(client, admin, run["id"])

    assert resp.status_code == 200, resp.text


async def test_only_the_newest_run_can_be_locked(client, scored_setup):  # noqa: F811
    admin, app_id = scored_setup["admin"], scored_setup["application"]["id"]
    older = await _score(client, admin, app_id)
    newer = await _score(client, admin, app_id)

    refused = await _approve(client, admin, older["id"])
    assert refused.status_code == 409, refused.text

    locked = await _approve(client, admin, newer["id"])
    assert locked.status_code == 200, locked.text

    # Once one run is locked no other can be, so an application never ends
    # up backed by two LOCKED runs.
    again = await _approve(client, admin, older["id"])
    assert again.status_code == 409, again.text
