"""Leads from the illustrative SME quiz on /rate (docs/specs/rates/sme-rate-quiz.md).

The quiz rate is computed in the browser; the backend stores what the visitor
saw after a bounds check, as a `rate_inquiries` row with source = QUIZ, so it
appears in the admin "Rate inquiries" list. Nothing here touches the
underwriting engine.
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.rates.models import RateInquiry
from conftest import auth_headers
from tests.rates.test_rate_inquiries import sample_inquiry_payload

QUIZ_LEADS = "/api/v1/rates/sme/quiz-leads"
ADMIN_LIST = "/admin/rates/inquiries"


def quiz_payload(**overrides) -> dict:
    body = {
        "requested_amount": 50_000_000,
        "tenor_months": 6,
        "operating_band": "Y1_3",
        "industry": "Retail Trade",
        "monthly_revenue": 100_000_000,
        "quiz_params_version": "sme-quiz-v1",
        "outcome": "QUOTED",
        "monthly_rate_pct": 1.21,
        "full_name": "  Trần   Thị Bán ",
        "phone": "0901 234 567",
        "consent_contact": True,
        "session_id": "sess_quiz_test",
        "tracking": {
            "utm_source": "facebook",
            "utm_medium": "paid",
            "utm_campaign": "sme-2026-11",
            "utm_content": None,
            "utm_term": None,
            "fbclid": "IwAR-test",
        },
    }
    body.update(overrides)
    return body


async def _row(db_session, inquiry_id: str) -> RateInquiry:
    result = await db_session.execute(
        select(RateInquiry).where(RateInquiry.id == uuid.UUID(inquiry_id))
    )
    return result.scalar_one()


# --------------------------------------------------------------------------- #
# Saving a lead
# --------------------------------------------------------------------------- #


async def test_quiz_lead_quoted_is_stored_as_quiz_row(client, db_session):
    resp = await client.post(QUIZ_LEADS, json=quiz_payload())
    assert resp.status_code == 201, resp.text

    row = await _row(db_session, resp.json()["inquiry_id"])
    assert row.source == "QUIZ"
    assert row.quiz_outcome == "QUOTED"
    assert row.quiz_params_version == "sme-quiz-v1"
    assert row.industry == "Retail Trade"
    assert row.requested_amount == 50_000_000
    assert row.tenor_months == 6
    assert row.monthly_revenue == 100_000_000
    assert row.operating_band == "Y1_3"
    # The monthly rate the visitor saw, in both columns.
    assert float(row.estimated_rate_min) == 1.21
    assert float(row.estimated_rate_max) == 1.21
    assert row.full_name == "Trần Thị Bán"
    assert row.phone == "0901 234 567"
    assert row.consented_contact_at is not None
    # None of the calculator's figures, and no engine output.
    assert row.revenue_l12m is None
    assert row.operating_months is None
    assert row.risk_tier is None
    assert row.calculated_monthly_payment is None


async def test_quiz_lead_declined_is_stored_without_rate(client, db_session):
    resp = await client.post(
        QUIZ_LEADS,
        json=quiz_payload(
            requested_amount=100_000_000,
            tenor_months=2,
            monthly_revenue=120_000_000,
            outcome="DECLINED",
            monthly_rate_pct=None,
        ),
    )
    assert resp.status_code == 201, resp.text

    row = await _row(db_session, resp.json()["inquiry_id"])
    assert row.quiz_outcome == "DECLINED"
    assert row.estimated_rate_min is None
    assert row.estimated_rate_max is None
    assert row.phone == "0901 234 567"


async def test_quiz_lead_stores_tracking_and_truncates_long_values(client, db_session):
    long_campaign = "c" * 300
    resp = await client.post(
        QUIZ_LEADS,
        json=quiz_payload(
            tracking={"utm_source": "facebook", "utm_campaign": long_campaign, "fbclid": "f" * 400}
        ),
    )
    assert resp.status_code == 201, resp.text

    row = await _row(db_session, resp.json()["inquiry_id"])
    assert row.utm_source == "facebook"
    assert row.utm_campaign == "c" * 100
    assert row.fbclid == "f" * 255
    assert row.utm_medium is None


async def test_quiz_lead_without_tracking_is_fine(client, db_session):
    resp = await client.post(QUIZ_LEADS, json=quiz_payload(tracking=None))
    assert resp.status_code == 201, resp.text
    row = await _row(db_session, resp.json()["inquiry_id"])
    assert row.utm_source is None and row.fbclid is None


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("consent", [False, "true", None])
async def test_quiz_lead_requires_literal_consent(client, db_session, consent):
    resp = await client.post(QUIZ_LEADS, json=quiz_payload(consent_contact=consent))
    assert resp.status_code == 422, resp.text
    count = (
        await db_session.execute(select(RateInquiry).where(RateInquiry.source == "QUIZ"))
    ).scalars().all()
    assert count == []


@pytest.mark.parametrize("rate", [1.15, 1.42, 0.5, 20.0])
async def test_quiz_lead_rejects_rate_outside_formula_bounds(client, rate):
    resp = await client.post(QUIZ_LEADS, json=quiz_payload(monthly_rate_pct=rate))
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("rate", [1.16, 1.17, 1.41])
async def test_quiz_lead_accepts_the_formula_floor_and_ceiling(client, rate):
    resp = await client.post(QUIZ_LEADS, json=quiz_payload(monthly_rate_pct=rate))
    assert resp.status_code == 201, resp.text


async def test_quiz_lead_rejects_quoted_without_rate(client):
    resp = await client.post(QUIZ_LEADS, json=quiz_payload(monthly_rate_pct=None))
    assert resp.status_code == 422, resp.text


async def test_quiz_lead_rejects_declined_with_rate(client):
    resp = await client.post(QUIZ_LEADS, json=quiz_payload(outcome="DECLINED"))
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"industry": "Casino"},
        {"operating_band": "FOREVER"},
        {"quiz_params_version": "sme-quiz-v0"},
        {"tenor_months": 7},
        {"tenor_months": 0},
        {"monthly_revenue": 0},
        {"full_name": "A"},
        {"phone": "12"},
    ],
)
async def test_quiz_lead_rejects_unknown_industry_and_band(client, overrides):
    resp = await client.post(QUIZ_LEADS, json=quiz_payload(**overrides))
    assert resp.status_code == 422, resp.text


async def test_quiz_lead_does_not_echo_a_rejected_industry(client):
    secret = "hunter2-" + "x" * 40
    resp = await client.post(QUIZ_LEADS, json=quiz_payload(industry=secret))
    assert resp.status_code == 422
    assert secret not in resp.text


@pytest.mark.parametrize("amount", [150_000_000, 1_000_000_000, 25_000_000, 10_000_000])
async def test_quiz_lead_rejects_amount_above_cap(client, amount):
    """Only the quiz's amount chips are accepted; 100M is the per-entity cap."""
    resp = await client.post(QUIZ_LEADS, json=quiz_payload(requested_amount=amount))
    assert resp.status_code == 422, resp.text


async def test_quiz_lead_requires_captcha_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "CAPTCHA_REQUIRED", True)
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "test-secret")
    resp = await client.post(QUIZ_LEADS, json=quiz_payload(turnstile_token=None))
    assert resp.status_code == 400, resp.text


async def test_quiz_lead_refused_when_captcha_required_without_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "CAPTCHA_REQUIRED", True)
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "")
    resp = await client.post(QUIZ_LEADS, json=quiz_payload())
    assert resp.status_code == 503, resp.text


# --------------------------------------------------------------------------- #
# Admin list
# --------------------------------------------------------------------------- #


async def test_admin_list_shows_quiz_rows_with_display_strings(client, make_admin):
    admin = await make_admin()
    quoted = (await client.post(QUIZ_LEADS, json=quiz_payload())).json()["inquiry_id"]
    declined = (
        await client.post(
            QUIZ_LEADS,
            json=quiz_payload(
                requested_amount=100_000_000,
                tenor_months=2,
                monthly_revenue=120_000_000,
                outcome="DECLINED",
                monthly_rate_pct=None,
                operating_band="LT_1Y",
            ),
        )
    ).json()["inquiry_id"]

    resp = await client.get(ADMIN_LIST, headers=auth_headers(admin))
    assert resp.status_code == 200, resp.text
    items = {i["id"]: i for i in resp.json()["items"]}

    q = items[quoted]
    assert q["source"] == "QUIZ"
    assert q["tier_display"] == "Quiz"
    assert q["calculated_rate_display"] == "1.21% / mo"
    assert q["estimated_rate_min"] == 1.21
    assert q["tenure_staff_display"] == "1–3 yrs (quiz)"
    assert q["revenue_l12m_formatted"] == "100.000.000 ₫ / mo"
    assert q["yoy_growth_display"] == "—"
    assert q["yoy_growth_pct"] is None
    assert q["loan_ask_display"] == "50M ₫ / 6 mos"
    assert q["full_name"] == "Trần Thị Bán"
    assert q["phone"] == "0901 234 567"
    assert q["utm_source"] == "facebook"
    assert q["fbclid"] == "IwAR-test"
    assert q["monthly_revenue"] == 100_000_000
    assert q["quiz_outcome"] == "QUOTED"

    d = items[declined]
    assert d["calculated_rate_display"] == "Declined"
    assert d["estimated_rate_min"] is None
    assert d["tenure_staff_display"] == "< 1 yr (quiz)"

    detail = await client.get(f"{ADMIN_LIST}/{quoted}", headers=auth_headers(admin))
    assert detail.status_code == 200, detail.text
    assert detail.json()["source"] == "QUIZ"


async def test_admin_list_filters_by_source(client, make_admin):
    admin = await make_admin()
    quiz_id = (await client.post(QUIZ_LEADS, json=quiz_payload())).json()["inquiry_id"]
    calc = await client.post("/api/v1/rates/calculate", json=sample_inquiry_payload())
    assert calc.status_code == 200, calc.text
    calc_id = calc.json()["inquiry_id"]

    only_quiz = await client.get(f"{ADMIN_LIST}?source=QUIZ", headers=auth_headers(admin))
    ids = {i["id"] for i in only_quiz.json()["items"]}
    assert quiz_id in ids and calc_id not in ids

    only_calc = await client.get(f"{ADMIN_LIST}?source=CALCULATOR", headers=auth_headers(admin))
    ids = {i["id"] for i in only_calc.json()["items"]}
    assert calc_id in ids and quiz_id not in ids

    bad = await client.get(f"{ADMIN_LIST}?source=OTHER", headers=auth_headers(admin))
    assert bad.status_code == 422


async def test_quiz_leads_are_admin_only(client, make_user):
    anon = await client.get(f"{ADMIN_LIST}?source=QUIZ")
    assert anon.status_code == 401
    sme = await make_user(role="SME")
    forbidden = await client.get(f"{ADMIN_LIST}?source=QUIZ", headers=auth_headers(sme))
    assert forbidden.status_code == 403


async def test_calculator_rows_unchanged(client, make_admin, db_session):
    """The long calculator still stores a complete CALCULATOR row and lists as before."""
    admin = await make_admin()
    resp = await client.post("/api/v1/rates/calculate", json=sample_inquiry_payload())
    assert resp.status_code == 200, resp.text
    inquiry_id = resp.json()["inquiry_id"]

    row = await _row(db_session, inquiry_id)
    assert row.source == "CALCULATOR"
    assert row.revenue_l12m == 4_000_000_000
    assert row.quiz_outcome is None

    item = next(
        i
        for i in (await client.get(ADMIN_LIST, headers=auth_headers(admin))).json()["items"]
        if i["id"] == inquiry_id
    )
    assert item["source"] == "CALCULATOR"
    assert item["tenure_staff_display"] == "36 mos / 25 staff"
    assert item["yoy_growth_display"] == "+25.0%"


# --------------------------------------------------------------------------- #
# Database constraints
# --------------------------------------------------------------------------- #


async def test_db_rejects_incomplete_quiz_row(db_session):
    """A QUIZ row without a phone cannot be written, whatever the app does."""
    db_session.add(
        RateInquiry(
            source="QUIZ",
            industry="Retail Trade",
            requested_amount=50_000_000,
            tenor_months=6,
            monthly_revenue=100_000_000,
            operating_band="Y1_3",
            quiz_outcome="QUOTED",
            quiz_params_version="sme-quiz-v1",
            estimated_rate_min=1.21,
            estimated_rate_max=1.21,
            full_name="Someone",
            phone=None,
            consented_contact_at=datetime.now(timezone.utc),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_db_rejects_incomplete_calculator_row(db_session):
    """The calculator's figures are still required on a CALCULATOR row."""
    db_session.add(
        RateInquiry(
            industry="Retail Trade",
            requested_amount=50_000_000,
            tenor_months=6,
            estimated_rate_min=1.2,
            estimated_rate_max=1.4,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_db_rejects_declined_quiz_row_with_rate(db_session):
    db_session.add(
        RateInquiry(
            source="QUIZ",
            industry="Retail Trade",
            requested_amount=100_000_000,
            tenor_months=2,
            monthly_revenue=120_000_000,
            operating_band="Y1_3",
            quiz_outcome="DECLINED",
            quiz_params_version="sme-quiz-v1",
            estimated_rate_min=1.21,
            estimated_rate_max=1.21,
            full_name="Someone",
            phone="0901234567",
            consented_contact_at=datetime.now(timezone.utc),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
