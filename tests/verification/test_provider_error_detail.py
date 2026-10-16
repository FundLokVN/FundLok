"""A Didit failure never reaches the client verbatim (vbsec 2026-10-16).

/kyc/start, /kyc/sync and their /kyb twins returned ``str(DiditError)`` as the
502 detail. That string carries Didit's raw response body or the resolver
error ("Could not reach Didit: [Errno 8] ..."). The 502 now says only that the
provider failed; the detail goes to the log.
"""
from types import SimpleNamespace

import pytest

from app.verification.client import DiditError
from app.verification.router import PROVIDER_FAILED_DETAIL

LEAKY = 'Didit returned 500: {"internal": "stack trace", "host": "10.0.0.7"}'


async def _raise(*args, **kwargs):
    raise DiditError(LEAKY)


@pytest.mark.parametrize(("prefix", "role"), [("/kyc", "INVESTOR"), ("/kyb", "SME")])
async def test_start_hides_the_provider_error(client, make_user, monkeypatch, prefix, role):
    monkeypatch.setattr("app.verification.service.start_verification", _raise)
    user = await make_user(role=role)

    resp = await client.post(f"{prefix}/start", headers=user["headers"])

    assert resp.status_code == 502
    assert resp.json() == {"detail": PROVIDER_FAILED_DETAIL}


@pytest.mark.parametrize(("prefix", "role"), [("/kyc", "INVESTOR"), ("/kyb", "SME")])
async def test_sync_hides_the_provider_error(client, make_user, monkeypatch, prefix, role):
    async def _existing(*args, **kwargs):
        return SimpleNamespace(id=None)

    monkeypatch.setattr("app.verification.service.get_status", _existing)
    monkeypatch.setattr("app.verification.service.sync_verification", _raise)
    user = await make_user(role=role)

    resp = await client.post(f"{prefix}/sync", headers=user["headers"])

    assert resp.status_code == 502
    assert resp.json() == {"detail": PROVIDER_FAILED_DETAIL}
