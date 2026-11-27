import uuid

import pytest
from sqlalchemy import update

from app.direct_transfer.models import PaymentAccount
from conftest import auth_headers, record_test_project_kyb


@pytest.fixture
def make_user(make_user, client, db_session):
    """The root make_user, plus a saved, approved bank account for every SME.

    Investors only see a project whose owner has a bank account
    (app.projects.service.owner_has_bank_account), and most tests here are
    about something else the listing shows. Pass bank_account=False to test
    an SME who has not saved one yet.
    """

    async def _make(role: str | None = None, *args, bank_account: bool = True, **kwargs):
        user = await make_user(role, *args, **kwargs)
        if role == "SME" and bank_account:
            resp = await client.post(
                "/payment-accounts",
                json={
                    "holder_type": "ENTITY",
                    "bank_bin": "970436",  # Vietcombank
                    "account_number": "0011001234567",
                    "account_name": "TEST CO",
                },
                headers=auth_headers(user),
            )
            assert resp.status_code == 201, resp.text
            # Approval is an admin's call (tests/admin/test_payment_account_review.py);
            # here it is only setup, so a direct write like make_eligible_investor's.
            await db_session.execute(
                update(PaymentAccount)
                .where(PaymentAccount.id == uuid.UUID(resp.json()["id"]))
                .values(status="APPROVED")
            )
            await db_session.commit()
        return user

    return _make


@pytest.fixture
def post_verified_project(client, db_session):
    async def post(sme, payload):
        if sme["role"] == "SME":
            await record_test_project_kyb(db_session, sme, payload.get("tax_id"))
        return await client.post("/projects", json=payload, headers=auth_headers(sme))

    return post
