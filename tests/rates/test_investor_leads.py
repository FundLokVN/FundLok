"""Investor tab of the public /rate page: estimate, lead capture, sign-up, admin.

Figures are asserted against the prototype's locked scenario
(FundLok_LaiSuat_Page_Prototype.html rev 6): Balanced, 12 months, monthly
reinvestment -> loan 14.00%, minus 1.00% expected loss, minus 2.50% fee =
10.50% per loan -> 17.27% a year. Those only hold at a 12% board rate, which is
what the seeded bank_rate_config row carries.
"""
import uuid

import pytest
from sqlalchemy import select

from app.rates.models import InvestorRateInquiry
from conftest import auth_headers

LEADS = "/api/v1/rates/investor/leads"


def lead_payload(**overrides) -> dict:
    body = {
        "full_name": "Nguyễn Văn A",
        "email": "Investor@Example.com",
        "phone": "0901 234 567",
        "locale": "vi",
        "amount_vnd": 1_000_000_000,
        "commitment_months": 12,
        "risk_tier": "balanced",
        "reinvestment_cadence": "monthly",
        "acknowledged_illustrative": True,
        "consent_contact": True,
    }
    body.update(overrides)
    return body


def choices(**overrides) -> dict:
    body = {
        "amount_vnd": 1_000_000_000,
        "commitment_months": 12,
        "risk_tier": "balanced",
        "reinvestment_cadence": "monthly",
    }
    body.update(overrides)
    return body


async def _latest_rate_is_12(db_session) -> bool:
    from app.underwriting.service import get_current_bank_rate

    return float((await get_current_bank_rate(db_session)).rate_pct) == 12.0


# --------------------------------------------------------------------------- #
# Estimate
# --------------------------------------------------------------------------- #


async def test_locked_scenario_reproduces_the_prototype(client, db_session):
    if not await _latest_rate_is_12(db_session):
        pytest.skip("a later board-rate row is seeded; locked figures assume 12%")
    resp = await client.post(LEADS, json=lead_payload())
    assert resp.status_code == 201, resp.text
    est = resp.json()["estimate"]
    assert est["loan_rate_pct"] == 14.0
    assert est["expected_loss_pct"] == 1.0
    assert est["fee_pct"] == 2.5
    assert est["net_per_loan_pct"] == 10.5
    assert est["net_apy_pct"] == 17.27
    # From the UNROUNDED yield (10.5 x 1.6448 = 17.2704%), as the prototype
    # does, so the return is not off by the display rounding of the headline.
    assert est["estimated_return_vnd"] == 172_704_000
    assert est["capital_turns"] == 3.0


@pytest.mark.parametrize(
    "tier,expected",
    [("conservative", 12.8), ("balanced", 14.0), ("growth", 15.2)],
)
async def test_tier_loan_rates(client, db_session, tier, expected):
    if not await _latest_rate_is_12(db_session):
        pytest.skip("locked figures assume a 12% board rate")
    resp = await client.get("/api/v1/rates/investor/tiers")
    assert resp.status_code == 200
    rates = {t["risk_tier"]: t["loan_rate_pct"] for t in resp.json()["tiers"]}
    assert rates[tier] == expected


async def test_no_reinvestment_six_months_is_seven_percent(client, db_session):
    """The prototype's rev 5 bug fix: a multiplier reverse-engineered from 12
    months reported 3.26% here. One ~4-month loan in a 6-month window is
    10.50 x 4/6 = 7.00%."""
    if not await _latest_rate_is_12(db_session):
        pytest.skip("locked figures assume a 12% board rate")
    lead = (await client.post(LEADS, json=lead_payload())).json()
    resp = await client.post(
        f"{LEADS}/{lead['lead_id']}/estimate",
        json=choices(commitment_months=6, reinvestment_cadence="none"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["net_apy_pct"] == 7.0


async def test_loan_rate_follows_the_board_rate(client, monkeypatch):
    """The tier rates come from the engine's pricing formula and the board rate
    admin score runs read, not from constants copied into the page.

    Patched rather than inserted: conftest deliberately does NOT truncate
    bank_rate_config between tests (it holds seed reference data), so a row
    written here would reprice every later test in the session."""
    from types import SimpleNamespace

    from app.rates import investor_service

    async def _ten_percent(db):
        return SimpleNamespace(rate_pct=10.0)

    monkeypatch.setattr(investor_service, "get_current_bank_rate", _ten_percent)
    resp = await client.get("/api/v1/rates/investor/tiers")
    rates = {t["risk_tier"]: t["loan_rate_pct"] for t in resp.json()["tiers"]}
    # 10 + (20 - 10) * (100 - 75) / 100
    assert rates["balanced"] == 12.5


async def test_missing_board_rate_falls_back_instead_of_500(client, monkeypatch):
    """A public page shows a default-rate illustration, not a 500."""
    from fastapi import HTTPException

    from app.rates import investor_service

    async def _none_in_force(db):
        raise HTTPException(status_code=500, detail="no bank_rate_config row")

    monkeypatch.setattr(investor_service, "get_current_bank_rate", _none_in_force)
    resp = await client.get("/api/v1/rates/investor/tiers")
    assert resp.status_code == 200


async def test_rating_and_bank_rate_are_never_returned(client):
    """With a tier's internal rating and its loan rate, the pricing formula
    solves for the bank reference rate. Lok's standing rule: neither leaves
    the server."""
    for resp in (
        await client.get("/api/v1/rates/investor/tiers"),
        await client.post(LEADS, json=lead_payload()),
    ):
        text = resp.text.lower()
        for leaked in ("bank_rate", "bulr", "rating", "grade", "final_grade", '"ir"'):
            assert leaked not in text, f"{leaked} reached a public caller"


# --------------------------------------------------------------------------- #
# Lead capture
# --------------------------------------------------------------------------- #


async def test_calculate_stores_the_lead(client, db_session):
    resp = await client.post(
        LEADS, json=lead_payload(), headers={"User-Agent": "TestBrowser/1.0"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["reference"].startswith("FL-") and len(body["reference"]) == 9

    row = (
        await db_session.execute(
            select(InvestorRateInquiry).where(InvestorRateInquiry.id == uuid.UUID(body["lead_id"]))
        )
    ).scalar_one()
    assert row.full_name == "Nguyễn Văn A"
    assert row.email == "investor@example.com"  # normalised
    assert row.phone == "0901 234 567"
    assert row.risk_tier == "balanced"
    assert float(row.net_apy_pct) == body["estimate"]["net_apy_pct"]
    assert row.acknowledged_illustrative_at is not None
    assert row.consented_contact_at is not None
    assert row.signed_up_at is None
    assert row.user_agent == "TestBrowser/1.0"


@pytest.mark.parametrize("field", ["acknowledged_illustrative", "consent_contact"])
@pytest.mark.parametrize("value", [False, None, "yes"])
async def test_consents_must_be_literally_true(client, field, value):
    resp = await client.post(LEADS, json=lead_payload(**{field: value}))
    assert resp.status_code == 422, resp.text


async def test_consents_must_be_present(client):
    body = lead_payload()
    del body["consent_contact"]
    assert (await client.post(LEADS, json=body)).status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"full_name": ""},
        {"full_name": "A"},
        {"full_name": "Bad\nName"},
        {"full_name": "x" * 121},
        {"email": "not-an-email"},
        {"email": None},
        {"phone": "call me maybe"},
        {"phone": "1234"},
        {"amount_vnd": 0},
        {"amount_vnd": 999_999},
        {"amount_vnd": 10**13},
        {"commitment_months": 9},
        {"risk_tier": "yolo"},
        {"reinvestment_cadence": "hourly"},
        {"locale": "fr"},
    ],
)
async def test_invalid_lead_input_rejected(client, db_session, overrides):
    before = (await db_session.execute(select(InvestorRateInquiry))).scalars().all()
    resp = await client.post(LEADS, json=lead_payload(**overrides))
    assert resp.status_code == 422, resp.text
    after = (await db_session.execute(select(InvestorRateInquiry))).scalars().all()
    assert len(after) == len(before), "a rejected request must not store a row"


async def test_phone_is_optional(client):
    body = lead_payload()
    del body["phone"]
    assert (await client.post(LEADS, json=body)).status_code == 201
    assert (await client.post(LEADS, json=lead_payload(phone=""))).status_code == 201


async def test_captcha_is_enforced_when_configured(client, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "test-secret")
    import app.utils.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "settings", config.settings)
    resp = await client.post(LEADS, json=lead_payload())
    assert resp.status_code == 400, resp.text


# --------------------------------------------------------------------------- #
# Live estimate and sign-up
# --------------------------------------------------------------------------- #


async def test_estimate_requires_an_existing_lead(client):
    """The yield stays behind the lead capture on the server, not only in the
    browser: an unknown lead id cannot fetch one."""
    resp = await client.post(f"{LEADS}/{uuid.uuid4()}/estimate", json=choices())
    assert resp.status_code == 404


async def test_estimate_writes_nothing(client, db_session):
    lead = (await client.post(LEADS, json=lead_payload())).json()
    resp = await client.post(
        f"{LEADS}/{lead['lead_id']}/estimate", json=choices(risk_tier="growth")
    )
    assert resp.status_code == 200
    row = (
        await db_session.execute(
            select(InvestorRateInquiry).where(InvestorRateInquiry.id == uuid.UUID(lead["lead_id"]))
        )
    ).scalar_one()
    assert row.risk_tier == "balanced"


async def test_signup_records_the_choices_on_screen(client, db_session):
    lead = (await client.post(LEADS, json=lead_payload())).json()
    resp = await client.post(
        f"{LEADS}/{lead['lead_id']}/signup",
        json=choices(risk_tier="growth", commitment_months=6, reinvestment_cadence="daily"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reference"] == lead["reference"]

    row = (
        await db_session.execute(
            select(InvestorRateInquiry).where(InvestorRateInquiry.id == uuid.UUID(lead["lead_id"]))
        )
    ).scalar_one()
    await db_session.refresh(row)
    assert row.signed_up_at is not None
    assert row.signup_risk_tier == "growth"
    assert row.signup_commitment_months == 6
    assert row.signup_reinvestment_cadence == "daily"
    # Recomputed server-side, never echoed from the client.
    expected = (
        await client.post(
            f"{LEADS}/{lead['lead_id']}/estimate",
            json=choices(risk_tier="growth", commitment_months=6, reinvestment_cadence="daily"),
        )
    ).json()["net_apy_pct"]
    assert float(row.signup_net_apy_pct) == expected


async def test_signup_ignores_a_client_supplied_yield(client):
    lead = (await client.post(LEADS, json=lead_payload())).json()
    resp = await client.post(
        f"{LEADS}/{lead['lead_id']}/signup", json={**choices(), "shown_net_apy": "99.99%"}
    )
    assert resp.status_code == 200


async def test_first_signup_stands(client, db_session):
    lead = (await client.post(LEADS, json=lead_payload())).json()
    first = await client.post(f"{LEADS}/{lead['lead_id']}/signup", json=choices(risk_tier="growth"))
    second = await client.post(
        f"{LEADS}/{lead['lead_id']}/signup", json=choices(risk_tier="conservative")
    )
    assert first.json()["signed_up_at"] == second.json()["signed_up_at"]
    row = (
        await db_session.execute(
            select(InvestorRateInquiry).where(InvestorRateInquiry.id == uuid.UUID(lead["lead_id"]))
        )
    ).scalar_one()
    await db_session.refresh(row)
    assert row.signup_risk_tier == "growth"


async def test_signup_for_unknown_lead_is_404(client):
    resp = await client.post(f"{LEADS}/{uuid.uuid4()}/signup", json=choices())
    assert resp.status_code == 404


async def test_public_responses_never_echo_contact_details(client):
    lead = (await client.post(LEADS, json=lead_payload())).json()
    for resp in (
        await client.post(f"{LEADS}/{lead['lead_id']}/estimate", json=choices()),
        await client.post(f"{LEADS}/{lead['lead_id']}/signup", json=choices()),
    ):
        assert "investor@example.com" not in resp.text
        assert "Nguyễn" not in resp.text


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #


async def test_admin_list_requires_admin(client, make_user):
    assert (await client.get("/admin/rates/investor-leads")).status_code == 401
    sme = await make_user(role="SME")
    investor_user = await make_user(role="INVESTOR")
    for user in (sme, investor_user):
        resp = await client.get("/admin/rates/investor-leads", headers=auth_headers(user))
        assert resp.status_code == 403


async def test_no_public_alias_for_admin_leads(client):
    """The SME inquiry routes are also served under /api/v1; these hold
    personal data and deliberately are not."""
    resp = await client.get("/api/v1/rates/investor-leads")
    assert resp.status_code in (404, 405)


async def test_admin_list_and_detail(client, make_admin):
    admin = await make_admin()
    lead = (await client.post(LEADS, json=lead_payload(full_name="Trần Thị B"))).json()
    await client.post(f"{LEADS}/{lead['lead_id']}/signup", json=choices())

    resp = await client.get("/admin/rates/investor-leads", headers=auth_headers(admin))
    assert resp.status_code == 200, resp.text
    item = next(i for i in resp.json()["items"] if i["id"] == lead["lead_id"])
    assert item["full_name"] == "Trần Thị B"
    assert item["reference"] == lead["reference"]
    assert item["signed_up_at"] is not None
    # Admins DO see the board rate the estimate was computed at — it is
    # internal data on an internal screen.
    assert item["bank_rate_pct"] > 0

    detail = await client.get(
        f"/admin/rates/investor-leads/{lead['lead_id']}", headers=auth_headers(admin)
    )
    assert detail.status_code == 200
    assert detail.json()["email"] == "investor@example.com"


async def test_admin_filters(client, make_admin):
    admin = await make_admin()
    tag = uuid.uuid4().hex[:8]
    signed = (await client.post(LEADS, json=lead_payload(full_name=f"Signed {tag}"))).json()
    await client.post(LEADS, json=lead_payload(full_name=f"Unsigned {tag}", risk_tier="growth"))
    await client.post(f"{LEADS}/{signed['lead_id']}/signup", json=choices())

    def get(query):
        return client.get(f"/admin/rates/investor-leads?{query}", headers=auth_headers(admin))

    by_name = (await get(f"search={tag}")).json()
    assert by_name["total"] == 2

    signed_only = (await get(f"search={tag}&signed_up=true")).json()
    assert [i["full_name"] for i in signed_only["items"]] == [f"Signed {tag}"]

    growth = (await get(f"search={tag}&risk_tier=growth")).json()
    assert [i["full_name"] for i in growth["items"]] == [f"Unsigned {tag}"]

    assert (await get("risk_tier=yolo")).status_code == 422
    # LIKE wildcards are literal.
    assert (await get("search=%25")).json()["total"] == 0
