import pytest

from conftest import auth_headers


@pytest.fixture
def make_user(make_user, client):
    """The root make_user, plus a saved bank account for every SME.

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
        return user

    return _make
