"""POST /loans/rate-estimate — the landing-page calculator.

Unauthenticated and reachable by anyone, so this model is the only thing
between the open internet and the grading engine. Most of what is asserted
here is therefore rejection, plus two properties that are compliance
requirements rather than preferences:

  * the response is a RANGE, never a single rate;
  * it carries nothing the pricing could be reconstructed from — no reference
    rate, no params version, no sector table version.

The engine's arithmetic belongs to tests/loans/test_lite_grading.py; this file
is about the public boundary.
"""

import pytest


def payload(**overrides) -> dict:
    """A profitable retailer the engine can score."""
    body = {
        "industry": "Retail Trade",
        "operating_months": 60,
        "employee_count": 25,
        "revenue_last_12m": 4_000_000_000,
        "revenue_prior_12m": 3_200_000_000,
        "cogs_y1": 2_400_000_000,
        "fixed_cost_y1": 600_000_000,
        "variable_cost_excl_cogs_y1": 300_000_000,
        "loan_amount": 800_000_000,
        "duration_months": 9,
        "revenue_best_month": 480_000_000,
        "revenue_worst_month": 210_000_000,
        "conc_top1_pct": 18.0,
        "conc_top3_pct": 41.0,
    }
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not None or k in overrides}


async def post(client, **overrides):
    return await client.post("/loans/rate-estimate", json=payload(**overrides))


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


async def test_anonymous_visitor_gets_a_band(client):
    """No account, no cookie, no header — the whole point of the page."""
    client.cookies.clear()

    resp = await post(client)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rate_low_pct"] < body["rate_high_pct"]
    assert body["score_low"] < body["score_high"]


async def test_rate_stays_inside_the_statutory_ceiling(client):
    body = (await post(client)).json()

    assert 0 < body["rate_low_pct"] <= 20.0
    assert 0 < body["rate_high_pct"] <= 20.0


async def test_response_leaks_nothing_about_pricing(client):
    """The reference rate and the rating formula are internal inputs and never
    reach a public page. This is the assertion that keeps them off it."""
    body = (await post(client)).json()

    for leaked in (
        "params_version",
        "sector_reference_version",
        "bank_rate_pct",
        "reference_rate",
        "decision_low",
        "decision_high",
    ):
        assert leaked not in body, f"{leaked} must not reach a public response"


async def test_assumptions_come_with_codes_to_translate(client):
    """A Vietnamese visitor must not read English prose. Codes are what the
    page renders; the English text is only the fallback."""
    body = (await post(client)).json()

    assert body["assumption_codes"], "a band with no assumptions reads as a quote"
    assert len(body["assumption_codes"]) == len(body["assumptions"])
    assert "CIC_BRACKETED" in body["assumption_codes"]


async def test_a_flat_year_is_disclosed(client):
    """Omitting best/worst month models a flat year, which flatters a seasonal
    business — so the visitor is told."""
    body = (
        await post(client, revenue_best_month=None, revenue_worst_month=None)
    ).json()

    assert "REVENUE_FLAT" in body["assumption_codes"]


async def test_a_thinner_margin_prices_worse(client):
    """Proof the visitor's figures actually reach the engine rather than a
    default being scored."""
    healthy = (await post(client)).json()
    thinner = (await post(client, cogs_y1=3_000_000_000)).json()

    assert thinner["rate_low_pct"] > healthy["rate_low_pct"]
    assert thinner["score_low"] < healthy["score_low"]


# --------------------------------------------------------------------------- #
# The public boundary: every bad input is a 422, never a 500
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "overrides",
    [
        {"industry": "Casino"},
        {"industry": "gambling"},
        {"duration_months": 7},
        {"duration_months": 0},
        {"loan_amount": 100_000},
        {"loan_amount": 9_000_000_000},
        {"employee_count": 0},
        {"employee_count": 5_000},
        {"revenue_last_12m": 0},
        {"revenue_last_12m": -1},
        {"conc_top1_pct": 120},
        {"operating_months": 0},
    ],
    ids=lambda o: next(iter(o)) + "=" + str(next(iter(o.values()))),
)
async def test_out_of_bounds_input_is_rejected(client, overrides):
    resp = await post(client, **overrides)

    assert resp.status_code == 422, resp.text


async def test_costs_above_revenue_is_a_422_not_a_500(client):
    """A real answer about the visitor's numbers, not a crash."""
    resp = await post(
        client,
        cogs_y1=4_000_000_000,
        fixed_cost_y1=900_000_000,
        variable_cost_excl_cogs_y1=500_000_000,
    )

    assert resp.status_code == 422
    assert "cost" in resp.json()["detail"].lower()


async def test_a_month_cannot_out_earn_its_own_year(client):
    resp = await post(client, revenue_best_month=9_000_000_000)

    assert resp.status_code == 422


async def test_top1_cannot_exceed_top3(client):
    resp = await post(client, conc_top1_pct=80.0, conc_top3_pct=40.0)

    assert resp.status_code == 422


async def test_unknown_fields_are_rejected(client):
    """`extra: forbid`. An open endpoint should not quietly accept whatever a
    caller invents."""
    body = payload()
    body["owner_withdrawal_pct"] = 25.0

    resp = await client.post("/loans/rate-estimate", json=body)

    assert resp.status_code == 422


async def test_nothing_is_persisted(client, db_session):
    """Stateless by design: an open endpoint that writes rows is a way to fill
    the database from the internet."""
    from sqlalchemy import func, select

    from app.lending.models import LoanApplication

    before = (
        await db_session.execute(select(func.count(LoanApplication.id)))
    ).scalar()
    await post(client)
    after = (
        await db_session.execute(select(func.count(LoanApplication.id)))
    ).scalar()

    assert before == after
