"""The public rate calculator and its admin read side.

Since the calculator moved off its own hardcoded tier table and onto
`grade_lite` (the same adapter `/loans/rate-estimate` uses), these tests assert
PARITY with the engine rather than literal rates. Pinning "TIER_A is 1.2%/mo"
is what let the two prices drift apart in the first place: the literal passed
while `grading_params_v1.yaml` moved underneath it. Where an exact number still
appears below it is one the caller can derive from their own inputs, or one read
back out of the engine in the same test.
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.loans.lite_grading import grade_lite
from app.projects.engine_constraints import params
from app.rates.models import RateInquiry
from app.rates.schemas import FIGURE_MAX_VND
from conftest import auth_headers

CONSTRAINTS = params().loan_constraints
MIN_VND = int(CONSTRAINTS["min_vnd"])
MAX_VND = int(CONSTRAINTS["max_vnd"])
TENORS = tuple(int(m) for m in CONSTRAINTS["allowed_durations_months"])
RATE_CAP_PCT = float(params().pricing["rate_cap_pct"])

# An engine value, not a slug. `INDUSTRY_OPTIONS` on the frontend sends these
# verbatim and the engine has no row for anything else.
INDUSTRY = "Retail Trade"


def sample_inquiry_payload(**overrides) -> dict:
    body = {
        "industry": INDUSTRY,
        "operating_months": 36,
        "employee_count": 25,
        "revenue_l12m": 4000000000,
        "revenue_prev_12m": 3200000000,
        "cogs_l12m": 2400000000,
        "fixed_costs_l12m": 600000000,
        "variable_costs_l12m": 300000000,
        "requested_amount": 800000000,
        "tenor_months": 6,
        "seasonality": {
            "peak_month_revenue": 480000000,
            "lowest_month_revenue": 210000000,
            "top_1_customer_share": 18.0,
            "top_3_customer_share": 41.0,
        },
    }
    body.update(overrides)
    return body


def band_for(payload: dict):
    """The engine's own answer for a payload, for parity assertions."""
    seasonality = payload.get("seasonality") or {}
    return grade_lite(
        company_code="rate-calculator",
        industry=payload["industry"],
        employee_count=payload["employee_count"],
        incorporation_date_months=payload["operating_months"],
        loan_size_vnd=Decimal(payload["requested_amount"]),
        duration_months=payload["tenor_months"],
        figures={
            "revenue_last_12m": payload["revenue_l12m"],
            "revenue_prior_12m": payload["revenue_prev_12m"],
            "revenue_best_month": seasonality.get("peak_month_revenue"),
            "revenue_worst_month": seasonality.get("lowest_month_revenue"),
            "cogs_y1": payload["cogs_l12m"],
            "fixed_cost_y1": payload["fixed_costs_l12m"],
            "variable_cost_excl_cogs_y1": payload["variable_costs_l12m"],
            "owner_withdrawal_pct": None,
            "conc_top1_pct": seasonality.get("top_1_customer_share"),
            "conc_top3_pct": seasonality.get("top_3_customer_share"),
        },
    )


# --------------------------------------------------------------------------- #
# Pricing parity with the grading engine
# --------------------------------------------------------------------------- #


async def test_quoted_rate_is_the_engines_own_rate(client):
    """The whole point of the endpoint: the landing page and an admin score run
    must be the same calculation. If this drifts, a visitor is quoted one number
    and offered another after signing up."""
    payload = sample_inquiry_payload()
    resp = await client.post("/api/v1/rates/calculate", json=payload)
    assert resp.status_code == 200, resp.text

    band = band_for(payload)
    rate_range = resp.json()["data"]["rate_range"]

    assert rate_range["apr_min"] == round(band.rate_low_pct, 2)
    assert rate_range["apr_max"] == round(band.rate_high_pct, 2)
    assert rate_range["min_rate_monthly"] == round(band.rate_low_pct / 12, 2)
    assert rate_range["max_rate_monthly"] == round(band.rate_high_pct / 12, 2)


async def test_quoted_rate_never_exceeds_the_configured_cap(client):
    """`pricing.rate_cap_pct` is the engine's ceiling. The old hand-rolled table
    did not apply it at all — TIER_C plus both concentration penalties quoted
    2.4%/mo, an APR of 28.8%, well above the 20% cap."""
    payload = sample_inquiry_payload(
        operating_months=7,
        employee_count=3,
        revenue_l12m=600_000_000,
        revenue_prev_12m=900_000_000,
        cogs_l12m=480_000_000,
        fixed_costs_l12m=60_000_000,
        variable_costs_l12m=30_000_000,
        requested_amount=MIN_VND,
        seasonality={"top_1_customer_share": 95.0, "top_3_customer_share": 100.0},
    )
    resp = await client.post("/api/v1/rates/calculate", json=payload)
    assert resp.status_code == 200, resp.text

    rate_range = resp.json()["data"]["rate_range"]
    assert rate_range["apr_max"] <= RATE_CAP_PCT
    assert rate_range["apr_min"] <= rate_range["apr_max"]


async def test_engine_assumptions_are_surfaced(client):
    """`grade_lite` brackets an unknown CIC score and assumes KYC passes. The
    response has to carry that, or the page presents a guess as a quote."""
    resp = await client.post("/api/v1/rates/calculate", json=sample_inquiry_payload())
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["provisional"] is True
    assert "CIC_BRACKETED" in data["assumption_codes"]
    assert "KYC_ASSUMED_PASS" in data["assumption_codes"]


async def test_numeric_grade_is_never_returned(client):
    """Pricing is `min(cap, BULR + (cap - BULR) * (100 - grade) / 100)`. Return
    the grade next to the rate and that solves for the bank reference rate in a
    single unauthenticated request (grading-engine-core.md §10)."""
    resp = await client.post("/api/v1/rates/calculate", json=sample_inquiry_payload())
    assert resp.status_code == 200

    body = resp.text
    for leaked in ("final_grade", "grade_low", "grade_high", "score_low", "score_high"):
        assert leaked not in body, f"{leaked} must not reach a public caller"


async def test_tier_is_derived_from_the_engine_grade(client):
    from app.rates.service import tier_for_grade

    payload = sample_inquiry_payload()
    resp = await client.post("/api/v1/rates/calculate", json=payload)
    assert resp.status_code == 200, resp.text

    band = band_for(payload)
    assert resp.json()["data"]["risk_profile"]["tier"] == tier_for_grade(band.grade_low)


# --------------------------------------------------------------------------- #
# Request handling
# --------------------------------------------------------------------------- #


async def test_self_reported_metrics_and_db_persistence(client, db_session):
    """The three percentages are the caller's own figures rearranged, so they
    stay exact — they disclose nothing about the model."""
    payload = sample_inquiry_payload()
    resp = await client.post("/api/v1/rates/calculate", json=payload)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "success"
    inquiry_id = uuid.UUID(data["inquiry_id"])

    risk_profile = data["data"]["risk_profile"]
    assert risk_profile["growth_rate_pct"] == 25.0
    assert risk_profile["ebitda_margin_pct"] == 17.5
    assert risk_profile["debt_to_revenue_pct"] == 20.0
    assert risk_profile["is_operating_loss"] is False

    row = (
        await db_session.execute(select(RateInquiry).where(RateInquiry.id == inquiry_id))
    ).scalar_one()

    assert row.industry == INDUSTRY
    assert row.operating_months == 36
    assert row.employee_count == 25
    assert row.revenue_l12m == 4000000000
    assert row.requested_amount == 800000000
    assert row.tenor_months == 6
    assert row.peak_month_revenue == 480000000
    assert row.lowest_month_revenue == 210000000
    assert float(row.top_1_customer_share) == 18.0
    assert float(row.top_3_customer_share) == 41.0
    assert row.risk_tier == risk_profile["tier"]
    # The stored monthly rates are what the caller was quoted, not a re-derivation.
    rate_range = data["data"]["rate_range"]
    assert float(row.estimated_rate_min) == rate_range["min_rate_monthly"]
    assert float(row.estimated_rate_max) == rate_range["max_rate_monthly"]
    assert row.calculated_monthly_payment == data["data"]["estimated_monthly_payment"]["min"]


async def test_input_sanitization_formatted_delimiters(client):
    """Commas, dots and currency symbols parse into clean integers."""
    payload = sample_inquiry_payload(
        revenue_l12m="4,000,000,000",
        revenue_prev_12m="3.200.000.000",
        cogs_l12m="2,400,000,000 ₫",
        fixed_costs_l12m="600,000,000 VND",
        variable_costs_l12m="300.000.000",
        requested_amount="800,000,000",
        seasonality={
            "peak_month_revenue": "480,000,000",
            "lowest_month_revenue": "210,000,000",
            "top_1_customer_share": "18.0%",
            "top_3_customer_share": "41.0%",
        },
    )

    resp = await client.post("/api/v1/rates/calculate", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["risk_profile"]["growth_rate_pct"] == 25.0
    assert data["risk_profile"]["ebitda_margin_pct"] == 17.5
    assert data["risk_profile"]["debt_to_revenue_pct"] == 20.0


async def test_costs_above_revenue_is_a_422_not_a_quote(client):
    """The engine's `compute_derived` refuses to score a loss-making applicant
    (D21). That is a real answer about their numbers, so it is a 422 carrying
    the engine's own explanation — the old table quoted these applicants
    TIER_C and priced them anyway."""
    payload = sample_inquiry_payload(
        revenue_l12m=2000000000,
        cogs_l12m=1500000000,
        fixed_costs_l12m=600000000,
        variable_costs_l12m=300000000,  # total 2.4B > revenue 2.0B
    )
    resp = await client.post("/api/v1/rates/calculate", json=payload)
    assert resp.status_code == 422, resp.text
    assert "costs" in resp.json()["detail"].lower()


async def test_unknown_industry_rejected_with_a_named_field(client):
    """The engine raises on an industry it has no sector row for. Validating in
    the schema makes that a 422 naming `industry` rather than a 500."""
    resp = await client.post(
        "/api/v1/rates/calculate", json=sample_inquiry_payload(industry="retail_fmcg")
    )
    assert resp.status_code == 422, resp.text
    assert "industry" in resp.text


async def test_every_supported_industry_is_accepted(client):
    """The frontend's INDUSTRY_OPTIONS mirrors this list by hand, so a value it
    can offer must never 422."""
    for industry in params().supported_industries:
        resp = await client.post(
            "/api/v1/rates/calculate", json=sample_inquiry_payload(industry=industry)
        )
        assert resp.status_code == 200, f"{industry}: {resp.text}"


async def test_bounds_validation_requested_amount_and_tenor(client):
    for amount in (MIN_VND - 1, MAX_VND + 1):
        resp = await client.post(
            "/api/v1/rates/calculate", json=sample_inquiry_payload(requested_amount=amount)
        )
        assert resp.status_code == 422, f"{amount}: {resp.text}"

    for tenor in (0, max(TENORS) + 1):
        resp = await client.post(
            "/api/v1/rates/calculate", json=sample_inquiry_payload(tenor_months=tenor)
        )
        assert resp.status_code == 422, f"{tenor}: {resp.text}"


async def test_boundary_values_exact_bounds_allowed(client):
    for amount, tenor in ((MIN_VND, min(TENORS)), (MAX_VND, max(TENORS))):
        resp = await client.post(
            "/api/v1/rates/calculate",
            json=sample_inquiry_payload(requested_amount=amount, tenor_months=tenor),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["estimated_monthly_payment"]["min"] > 0


@pytest.mark.parametrize(
    "field",
    [
        "revenue_l12m",
        "revenue_prev_12m",
        "cogs_l12m",
        "fixed_costs_l12m",
        "variable_costs_l12m",
    ],
)
async def test_absurd_figures_are_rejected_not_handed_to_the_driver(client, field):
    """Python ints are unbounded; PostgreSQL BIGINT is not. Without a ceiling
    these validated cleanly, ran through the metrics, and then raised inside
    asyncpg — a 500 from a public unauthenticated endpoint."""
    resp = await client.post(
        "/api/v1/rates/calculate",
        json=sample_inquiry_payload(**{field: FIGURE_MAX_VND + 1}),
    )
    assert resp.status_code == 422, resp.text


async def test_zero_prior_revenue_is_refused_rather_than_guessed(client):
    """A BEHAVIOUR CHANGE, and the right one. Year-on-year growth is a scored
    factor, so with no prior year the engine has nothing to score and says so.
    The old table silently substituted 0% growth and priced the applicant on an
    invented number — a business in its first year was quoted a rate derived
    from a figure nobody supplied. The message is written to be shown."""
    payload = sample_inquiry_payload(revenue_prev_12m=0)
    resp = await client.post("/api/v1/rates/calculate", json=payload)
    assert resp.status_code == 422, resp.text
    assert "revenue" in resp.json()["detail"].lower()


async def test_omitted_seasonality_and_flat_keys(client):
    payload = sample_inquiry_payload()
    del payload["seasonality"]
    payload["peak_month_revenue"] = 450000000
    payload["lowest_month_revenue"] = 200000000
    payload["top_1_customer_share"] = 15.0

    resp = await client.post("/api/v1/rates/calculate", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success"
    # No best/worst month given at all would model revenue as flat; these were
    # supplied flat rather than nested, so the shape must still reach the engine.
    assert "REVENUE_FLAT" not in resp.json()["data"]["assumption_codes"]


# --------------------------------------------------------------------------- #
# Abuse surface: this endpoint is public, unauthenticated and writes a row
# --------------------------------------------------------------------------- #


async def test_session_id_from_header_is_length_validated(client):
    """`session_id` is a String(128). The router backfills it from a header, and
    plain attribute assignment skips Pydantic unless `validate_assignment` is
    on — so an oversized header used to reach the column and 500."""
    resp = await client.post(
        "/api/v1/rates/calculate",
        json=sample_inquiry_payload(),
        headers={"X-Session-ID": "x" * 5000},
    )
    assert resp.status_code == 422, resp.text


async def test_session_id_from_header_is_stored(client, db_session):
    resp = await client.post(
        "/api/v1/rates/calculate",
        json=sample_inquiry_payload(),
        headers={"X-Session-ID": "test-session-12345", "User-Agent": "TestBrowser/1.0"},
    )
    assert resp.status_code == 200, resp.text
    inquiry_id = uuid.UUID(resp.json()["inquiry_id"])

    row = (
        await db_session.execute(select(RateInquiry).where(RateInquiry.id == inquiry_id))
    ).scalar_one()
    assert row.session_id == "test-session-12345"
    assert row.user_agent == "TestBrowser/1.0"


async def test_body_session_id_is_length_validated(client):
    resp = await client.post(
        "/api/v1/rates/calculate", json=sample_inquiry_payload(session_id="x" * 5000)
    )
    assert resp.status_code == 422, resp.text


async def test_forwarded_for_is_ignored_when_no_proxy_is_configured(client, db_session):
    """`TRUSTED_PROXY_HOPS` defaults to 0. X-Forwarded-For is a free-text header
    a client appends to, so trusting its leftmost entry lets a caller choose the
    IP an operator sees on their own abuse report."""
    resp = await client.post(
        "/api/v1/rates/calculate",
        json=sample_inquiry_payload(),
        headers={"X-Forwarded-For": "203.0.113.195, 70.41.3.18"},
    )
    assert resp.status_code == 200, resp.text
    inquiry_id = uuid.UUID(resp.json()["inquiry_id"])

    row = (
        await db_session.execute(select(RateInquiry).where(RateInquiry.id == inquiry_id))
    ).scalar_one()
    assert str(row.ip_address or "") != "203.0.113.195"


async def test_forwarded_for_reads_the_hop_the_proxy_wrote(client, db_session, monkeypatch):
    """With one trusted proxy in front, the entry that proxy appended is the
    RIGHTMOST one — everything left of it came from the caller."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    resp = await client.post(
        "/api/v1/rates/calculate",
        json=sample_inquiry_payload(),
        headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.195"},
    )
    assert resp.status_code == 200, resp.text
    inquiry_id = uuid.UUID(resp.json()["inquiry_id"])

    row = (
        await db_session.execute(select(RateInquiry).where(RateInquiry.id == inquiry_id))
    ).scalar_one()
    assert str(row.ip_address) == "203.0.113.195"


async def test_oversized_user_agent_is_truncated(client, db_session):
    """`user_agent` is an unbounded TEXT column on a public endpoint."""
    resp = await client.post(
        "/api/v1/rates/calculate",
        json=sample_inquiry_payload(),
        headers={"User-Agent": "A" * 4000},
    )
    assert resp.status_code == 200, resp.text
    inquiry_id = uuid.UUID(resp.json()["inquiry_id"])

    row = (
        await db_session.execute(select(RateInquiry).where(RateInquiry.id == inquiry_id))
    ).scalar_one()
    assert len(row.user_agent) <= 512


async def test_captcha_is_enforced_when_a_secret_is_configured(client, monkeypatch):
    """The endpoint writes a row per call with no authentication. Turnstile is
    the only thing standing between it and an unbounded insert loop."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "test-secret")
    resp = await client.post("/api/v1/rates/calculate", json=sample_inquiry_payload())
    assert resp.status_code == 400, resp.text
    assert "Turnstile" in resp.json()["detail"]


async def test_captcha_token_is_verified_not_merely_present(client, monkeypatch):
    from app.core.config import settings
    from app.utils import captcha as captcha_module

    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "test-secret")

    seen = {}

    class _Resp:
        @staticmethod
        def json():
            return {"success": False, "error-codes": ["invalid-input-response"]}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, data=None):
            seen["token"] = data["response"]
            return _Resp()

    monkeypatch.setattr(captcha_module.httpx, "AsyncClient", _FakeClient)

    resp = await client.post(
        "/api/v1/rates/calculate",
        json=sample_inquiry_payload(turnstile_token="forged-token"),
    )
    assert resp.status_code == 400, resp.text
    assert seen["token"] == "forged-token"


async def test_no_captcha_configured_means_no_captcha_required(client):
    """Local dev and CI have no secret; the verifier no-ops, as it does for the
    contact form."""
    resp = await client.post(
        "/api/v1/rates/calculate", json=sample_inquiry_payload(turnstile_token=None)
    )
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# Admin Inquiries Endpoint: GET /api/v1/rates/inquiries & /admin/rates/inquiries
# --------------------------------------------------------------------------- #


async def test_admin_get_endpoint_unauthorized_for_anonymous(client):
    resp = await client.get("/api/v1/rates/inquiries")
    assert resp.status_code == 401


async def test_admin_get_endpoint_forbidden_for_sme(client, make_user):
    sme = await make_user(role="SME")
    resp = await client.get("/api/v1/rates/inquiries", headers=auth_headers(sme))
    assert resp.status_code == 403


async def test_admin_get_endpoint_success_with_formatted_table_fields(client, make_admin):
    admin = await make_admin()

    post_resp = await client.post("/api/v1/rates/calculate", json=sample_inquiry_payload())
    assert post_resp.status_code == 200, post_resp.text
    inquiry_id = post_resp.json()["inquiry_id"]
    quoted = post_resp.json()["data"]

    resp = await client.get("/api/v1/rates/inquiries", headers=auth_headers(admin))
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["total"] >= 1
    assert body["page"] == 1

    item = next(i for i in body["items"] if i["id"] == inquiry_id)
    assert item["industry_display"] == "Retail Trade"
    assert item["tenure_staff_display"] == "36 mos / 25 staff"
    assert item["revenue_l12m_formatted"] == "4.000.000.000 ₫"
    assert item["yoy_growth_display"] == "+25.0%"
    assert item["loan_ask_display"] == "800M ₫ / 6 mos"
    assert item["created_at_formatted"] != ""
    # The operator sees exactly what the visitor was quoted.
    assert item["estimated_rate_min"] == quoted["rate_range"]["min_rate_monthly"]
    assert item["estimated_rate_max"] == quoted["rate_range"]["max_rate_monthly"]
    assert item["risk_tier"] == quoted["risk_profile"]["tier"]


async def test_admin_get_inquiry_by_id(client, make_admin):
    admin = await make_admin()

    post_resp = await client.post("/api/v1/rates/calculate", json=sample_inquiry_payload())
    inquiry_id = post_resp.json()["inquiry_id"]

    resp = await client.get(f"/api/v1/rates/inquiries/{inquiry_id}", headers=auth_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["id"] == inquiry_id

    fake_id = uuid.uuid4()
    resp_404 = await client.get(f"/api/v1/rates/inquiries/{fake_id}", headers=auth_headers(admin))
    assert resp_404.status_code == 404


async def test_admin_rates_inquiries_alias_path(client, make_admin):
    admin = await make_admin()

    post_resp = await client.post("/api/v1/rates/calculate", json=sample_inquiry_payload())
    inquiry_id = post_resp.json()["inquiry_id"]

    resp = await client.get("/admin/rates/inquiries", headers=auth_headers(admin))
    assert resp.status_code == 200, resp.text
    assert any(i["id"] == inquiry_id for i in resp.json()["items"])

    detail_resp = await client.get(
        f"/admin/rates/inquiries/{inquiry_id}", headers=auth_headers(admin)
    )
    assert detail_resp.status_code == 200
    assert detail_resp.json()["id"] == inquiry_id


async def test_admin_inquiries_filtering_and_pagination(client, make_admin):
    admin = await make_admin()

    await client.post(
        "/api/v1/rates/calculate", json=sample_inquiry_payload(industry="Textile & Garment")
    )
    await client.post(
        "/api/v1/rates/calculate", json=sample_inquiry_payload(industry="Food & Beverage")
    )
    tier_resp = await client.post(
        "/api/v1/rates/calculate", json=sample_inquiry_payload(industry="Logistics & Transport")
    )
    assert tier_resp.status_code == 200, tier_resp.text
    seeded_tier = tier_resp.json()["data"]["risk_profile"]["tier"]

    f_resp = await client.get(
        "/api/v1/rates/inquiries?industry=Textile", headers=auth_headers(admin)
    )
    assert f_resp.status_code == 200
    items = f_resp.json()["items"]
    assert len(items) >= 1
    assert all("textile" in i["industry"].lower() for i in items)

    tier_filtered = await client.get(
        f"/api/v1/rates/inquiries?risk_tier={seeded_tier}", headers=auth_headers(admin)
    )
    assert tier_filtered.status_code == 200
    tier_items = tier_filtered.json()["items"]
    assert len(tier_items) >= 1
    assert all(i["risk_tier"] == seeded_tier for i in tier_items)

    page1 = await client.get(
        "/api/v1/rates/inquiries?page=1&page_size=1", headers=auth_headers(admin)
    )
    assert page1.status_code == 200
    assert len(page1.json()["items"]) == 1
    assert page1.json()["total"] >= 3


async def test_system_admin_role_can_access_inquiries(client, make_user, db_session):
    user = await make_user(role=None)
    from app.users.models import User

    result = await db_session.execute(select(User).where(User.id == user["id"]))
    row = result.scalar_one()
    row.role = "SYSTEM_ADMIN"
    await db_session.commit()

    resp = await client.get("/api/v1/rates/inquiries", headers=auth_headers(user))
    assert resp.status_code == 200


async def test_refusals_carry_a_translatable_error_code(client):
    """The engine's prose is English and this page defaults to Vietnamese, so
    every refusal names a code the UI can translate. The header is read
    cross-origin, which needs `expose_headers` on the CORS middleware — see
    app/main.py."""
    resp = await client.post(
        "/api/v1/rates/calculate", json=sample_inquiry_payload(revenue_prev_12m=0)
    )
    assert resp.status_code == 422, resp.text
    assert resp.headers["x-error-code"] == "REVENUE_BOTH_YEARS_REQUIRED"


def test_every_lite_grading_code_has_frontend_copy():
    """A code with no translation shows the English fallback, which is the thing
    the codes exist to avoid. The frontend mirrors this list by hand (the same
    arrangement as INDUSTRY_OPTIONS), so this test is what catches the drift."""
    import json
    import re
    from pathlib import Path

    source = Path("app/loans/lite_grading.py").read_text(encoding="utf-8")
    codes = set(re.findall(r'code="([A-Z_]+)"', source))
    codes.add("UNSCOREABLE")  # the constructor default

    i18n = Path(__file__).resolve().parents[2].parent / "Frontendfundlok" / "lib" / "i18n"
    if not i18n.exists():
        pytest.skip("frontend checkout not present")

    for name in ("en", "vi"):
        strings = json.loads((i18n / f"{name}.json").read_text(encoding="utf-8"))
        translated = set(strings["ratePage"]["errorCode"])
        assert not codes - translated, f"{name}.json is missing {sorted(codes - translated)}"


# --------------------------------------------------------------------------- #
# Second security pass
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "seasonality",
    [
        # best month larger than the whole year
        {"peak_month_revenue": 900_000_000_000_000, "lowest_month_revenue": 1},
        # worst month above the best
        {"peak_month_revenue": 1, "lowest_month_revenue": 900_000_000},
        # top-1 customer holding more than the top-3 combined
        {"top_1_customer_share": 90, "top_3_customer_share": 10},
    ],
)
async def test_mutually_impossible_figures_are_rejected(client, seasonality):
    """Each field passed its own range check and the combination was priced
    anyway, with a stored row. The form already blocks these; the API now
    agrees with it."""
    resp = await client.post(
        "/api/v1/rates/calculate", json=sample_inquiry_payload(seasonality=seasonality)
    )
    assert resp.status_code == 422, resp.text


async def test_cross_field_rules_apply_to_flat_keys_too(client):
    payload = sample_inquiry_payload()
    del payload["seasonality"]
    payload["peak_month_revenue"] = 1
    payload["lowest_month_revenue"] = 900_000_000
    resp = await client.post("/api/v1/rates/calculate", json=payload)
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf"])
async def test_non_finite_percentage_strings_are_rejected(client, value):
    """float("nan") parses from a string, and NaN compares False against both
    ends of a range check, so it used to pass `0 <= v <= 100`."""
    resp = await client.post(
        "/api/v1/rates/calculate",
        json=sample_inquiry_payload(seasonality={"top_1_customer_share": value}),
    )
    assert resp.status_code == 422, resp.text


async def test_captcha_outage_fails_closed(client, monkeypatch):
    """A non-JSON siteverify body (an outage page) used to escape as an
    unhandled exception. It must refuse, never wave the request through."""
    from app.core.config import settings
    from app.utils import captcha as captcha_module

    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "test-secret")

    class _Resp:
        status_code = 502

        @staticmethod
        def json():
            raise ValueError("not json")

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, data=None):
            return _Resp()

    monkeypatch.setattr(captcha_module.httpx, "AsyncClient", _FakeClient)
    resp = await client.post(
        "/api/v1/rates/calculate", json=sample_inquiry_payload(turnstile_token="t")
    )
    assert resp.status_code == 503, resp.text


async def test_engine_internal_errors_are_not_forwarded(client, monkeypatch):
    """grade()'s own ValueErrors are developer messages naming internal
    parameters. The caller gets a generic 422; the detail goes to the log."""
    from app.rates import service

    def _boom(**kwargs):
        raise ValueError("bank_rate_default_pct=12.0 outside pricing envelope")

    monkeypatch.setattr(service, "grade_lite", _boom)
    resp = await client.post("/api/v1/rates/calculate", json=sample_inquiry_payload())
    assert resp.status_code == 422
    assert "bank_rate" not in resp.text
    assert resp.headers["x-error-code"] == "UNSCOREABLE"


async def test_admin_risk_tier_filter_is_an_enum(client, make_admin):
    admin = await make_admin()
    resp = await client.get(
        "/api/v1/rates/inquiries?risk_tier=TIER_Z", headers=auth_headers(admin)
    )
    assert resp.status_code == 422


async def test_admin_industry_filter_escapes_like_wildcards(client, make_admin):
    """`%` is a LIKE wildcard; unescaped, a filter of "%" matched every row."""
    admin = await make_admin()
    await client.post("/api/v1/rates/calculate", json=sample_inquiry_payload())
    resp = await client.get(
        "/api/v1/rates/inquiries?industry=%25", headers=auth_headers(admin)
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
