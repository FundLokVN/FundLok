"""POST /loans/applications enforces the engine's loan-size bounds.

The inline application on POST /projects already did (see
tests/projects/test_create_project_validation.py); this endpoint stored any
positive amount. A score run with no financials never calls the engine, so an
out-of-range amount could reach a locked run and a contract unchecked. Bounds
are read from grading_params_v1.yaml, never retyped.
"""
import pytest
from sqlalchemy import func, select

from app.lending.models import LoanApplication
from app.projects.engine_constraints import params
from conftest import auth_headers

CONSTRAINTS = params().loan_constraints
MIN_VND = int(CONSTRAINTS["min_vnd"])
MAX_VND = int(CONSTRAINTS["max_vnd"])


async def _create(client, sme, project_id, amount):
    return await client.post(
        "/loans/applications",
        json={"business_id": project_id, "requested_amount": amount},
        headers=auth_headers(sme),
    )


@pytest.mark.parametrize("amount", [MIN_VND - 1, MIN_VND // 2, MAX_VND + 1, MAX_VND * 10])
async def test_create_application_amount_outside_engine_bounds_rejected(
    client, db_session, make_user, make_project, amount
):
    sme = await make_user(role="SME")
    project = await make_project(sme)

    resp = await _create(client, sme, project["id"], amount)

    assert resp.status_code == 422, resp.text
    assert "requested_amount must be between" in resp.text
    stored = await db_session.scalar(select(func.count()).select_from(LoanApplication))
    assert stored == 0


@pytest.mark.parametrize("amount", [MIN_VND, MAX_VND])
async def test_create_application_amount_at_the_boundaries_accepted(
    client, make_user, make_project, amount
):
    sme = await make_user(role="SME")
    project = await make_project(sme)

    resp = await _create(client, sme, project["id"], amount)

    assert resp.status_code == 201, resp.text
    assert resp.json()["requested_amount"] == amount
    assert resp.json()["status"] == "DRAFT"
