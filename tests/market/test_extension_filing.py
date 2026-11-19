"""The business's extension filing route (automatic-extension.md). The rules are tested in
tests/direct_transfer/test_automatic_extension.py; this checks the wiring."""
import uuid

from tests.market.test_funding_flow import PNG, _open_project


async def test_extension_filing_route_is_the_business_s_and_follows_the_filing_window(
    client, db_session, make_admin, make_user
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    url = f"/repayment/rounds/{setup['window'].id}/extension-filings"
    form = {"period_month": "2020-01", "revenue_vnd": "1000000", "evidence_kind": "TAX_DECLARATION"}

    def upload():
        return {"file": ("declaration.png", PNG + uuid.uuid4().bytes, "image/png")}

    investor = await make_user(role="INVESTOR")
    assert (await client.post(url, data=form, files=upload(), headers=investor["headers"])).status_code == 403
    stranger = await make_user(role="SME")
    other = await client.post(url, data=form, files=upload(), headers=stranger["headers"])
    assert other.status_code == 404
    # January 2020 could only be filed from 1 to 5 February 2020.
    closed = await client.post(url, data=form, files=upload(), headers=setup["sme"]["headers"])
    assert closed.status_code == 409 and closed.headers["X-Error-Code"] == "FILING_CLOSED"
    bad = await client.post(url, data=form | {"period_month": "2020-13"}, files=upload(), headers=setup["sme"]["headers"])
    assert bad.status_code == 422
