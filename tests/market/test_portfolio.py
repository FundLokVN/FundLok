"""The investor's Transactions and Analytics pages, from their direct-transfer tickets and repayment days: each
investment out, each day the business repaid them in, and the portfolio month by month."""
from tests.admin.test_repayment_receipts import _pay, _receipts, _repaying

# A real score run's factor scores (each 0-100; revenue_type not scored).
FACTORS = {
    "cic": 57.25, "scsr": 100.0, "macro": 40.0, "founder": 50.0, "conc_top1": 90.01, "conc_top3": 77.71,
    "competitor": 35.0, "regulatory": 45.0, "cyclicality": 65.0, "seasonality": 64.44, "gross_margin": 26.51,
    "loan_revenue": 100.0, "revenue_type": None, "revenue_trend": 44.36, "sector_growth": 64.03, "input_cost_vol": 25.0,
    "uncontrollable": 20.0, "cost_flexibility": 97.32, "operating_margin": 55.78, "owner_withdrawal": 100.0,
    "revenue_volatility": 84.09, "contribution_margin": 9.81,
}


async def test_the_investor_sees_their_investment_and_each_repayment(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    mine = lambda: client.get("/funding/me/transactions", headers=s["investor"]["headers"])

    [investment] = (await mine()).json()
    assert investment["type"] == "INVESTMENT" and investment["status"] == "COMPLETED"
    assert investment["amount"] == -30_000_000 and investment["reference"] == s["ticket"]["reference_code"]
    assert investment["counterparty"].startswith("ABC Co") and investment["method_key"] == "bankTransfer"

    # The business pays the first day: the business's word until FundLok checks its receipt.
    await _pay(client, s)
    schedule = (
        await client.get(f"/repayment/tranches/{s['ticket']['id']}/schedule", headers=s["investor"]["headers"])
    ).json()
    first_day = [r for r in schedule if r["repayment_day_id"] in s["day_ids"]]
    [repaid] = [r for r in (await mine()).json() if r["type"] == "RETURN"]
    assert repaid["status"] == "PENDING"
    # One row for the day, the first-period interest folded in: what reached the investor, after tax.
    assert repaid["amount"] == sum(r["net_vnd"] for r in first_day)

    # The investor confirms it arrived.
    [receipt] = (await _receipts(client, s["admin"]))["items"]
    confirmed = await client.post(f"/repayment/receipts/{receipt['id']}/confirm", headers=s["investor"]["headers"])
    assert confirmed.status_code == 200, confirmed.text
    [repaid] = [r for r in (await mine()).json() if r["type"] == "RETURN"]
    assert repaid["status"] == "COMPLETED"

    # Analytics: invested this month, and what came back, capital and earnings after tax apart.
    analytics = (await client.get("/funding/me/analytics", headers=s["investor"]["headers"])).json()
    assert len(analytics["monthly"]) >= 12
    last = analytics["monthly"][-1]
    assert last["deployed_cumulative"] == 30_000_000
    assert last["principal_monthly"] == sum(r["principal_vnd"] for r in first_day)
    assert last["yield_monthly"] == sum(r["interest_vnd"] - r["tax_withheld_vnd"] for r in first_day)
    assert last["returns_monthly"] == last["principal_monthly"] + last["yield_monthly"] == repaid["amount"]
    assert last["returns_cumulative"] == sum(p["returns_monthly"] for p in analytics["monthly"])
    assert sum(a["deployed"] for a in analytics["allocation"]) == 30_000_000
    status = {s_["statusKey"]: s_["amount"] for s_ in analytics["capital_status"]}
    assert status["completed"] == last["principal_monthly"] and status["writtenDown"] == 0
    assert status["repaying"] == 30_000_000 - last["principal_monthly"] and sum(status.values()) == 30_000_000
    assert analytics["active_positions"] == 1


async def test_an_investor_sees_only_their_own_money(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    other = await make_eligible_investor(await make_user(role="INVESTOR"))

    assert (await client.get("/funding/me/transactions", headers=other["headers"])).json() == []
    empty = (await client.get("/funding/me/analytics", headers=other["headers"])).json()
    assert empty == {"monthly": [], "allocation": [], "capital_status": [], "active_positions": 0}
    assert (await client.get("/funding/me/transactions", headers=s["sme"]["headers"])).status_code == 403


async def test_the_business_sees_investments_in_repayments_out_and_fundlok_s_fee(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    from tests.market.test_round_lifecycle import _approved

    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    ledger = lambda: client.get("/funding/me/business-transactions", headers=s["sme"]["headers"])

    [received] = (await ledger()).json()
    assert received["type"] == "DISBURSEMENT" and received["status"] == "COMPLETED"
    assert received["amount"] == 30_000_000 and received["counterparty"] == "N. A***"  # masked

    await _pay(client, s)
    schedule = (
        await client.get(f"/repayment/tranches/{s['ticket']['id']}/schedule", headers=s["sme"]["headers"])
    ).json()
    first_day = [r for r in schedule if r["repayment_day_id"] in s["day_ids"]]
    [repaid] = [r for r in (await ledger()).json() if r["type"] == "REPAYMENT"]
    # What the business transferred, out; checked by FundLok before it counts as confirmed.
    assert repaid["status"] == "PENDING" and repaid["amount"] == -sum(r["net_vnd"] for r in first_day)

    # Closing the round issues FundLok's activation fee: an invoice, owed until paid.
    binh = await make_eligible_investor(await make_user(role="INVESTOR"))
    await _approved(client, s["admin"], binh, s["window"].id, 20_000_000)
    await client.post(f"/funding/windows/{s['window'].id}/close-early", headers=s["sme"]["headers"])
    advanced = await client.post(f"/admin/funding/rounds/{s['window'].id}/advance", headers=s["admin"]["headers"])
    assert advanced.json()["status"] == "REPAYING", advanced.text
    [fee] = [r for r in (await ledger()).json() if r["type"] == "FEE"]
    assert fee["status"] == "PENDING" and fee["amount"] < 0 and fee["counterparty"] == "FundLok"

    # An investor never sees the business's ledger.
    assert (await client.get("/funding/me/business-transactions", headers=s["investor"]["headers"])).status_code == 403


async def test_the_business_sees_its_loan_against_its_revenue_and_its_score(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    import datetime as dt
    import uuid

    from sqlalchemy import select

    from app.direct_transfer.models import FundingWindow
    from app.lending.models import Contract, ScoreRun
    from app.underwriting.models import ScoreRunInput

    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    # The round was offered on a locked score, with the revenue it was sized on.
    window = await db_session.get(FundingWindow, uuid.UUID(s["ticket"]["window_id"]))
    contract = await db_session.get(Contract, window.contract_id)
    run = (await db_session.execute(select(ScoreRun))).scalars().first()
    contract.score_run_id = run.id
    run.factor_results = FACTORS
    run.final_grade_precise = None  # worked out from the factors
    db_session.add(ScoreRunInput(
        id=uuid.uuid4(), score_run_id=run.id, company_code="ABC", industry="Retail Trade", company_size="SMALL",
        loan_size_vnd=80_000_000, duration_months=4, operating_months=36,
        monthly_revenue_vnd=[1_000_000_000] * 12 + [3_000_000_000] * 12,  # the last 12 months count
        cogs_y1_vnd=0, fixed_cost_y1_vnd=0, variable_cost_excl_cogs_y1_vnd=0, sector_cagr_pct=5.0,
        bank_rate_pct=4.5, bank_rate_effective_from=dt.date(2026, 1, 1),
    ))
    await db_session.commit()
    await _pay(client, s)

    analytics = (await client.get("/funding/me/business-analytics", headers=s["sme"]["headers"])).json()

    facility = analytics["facility"]
    schedule = (
        await client.get(f"/repayment/tranches/{s['ticket']['id']}/schedule", headers=s["sme"]["headers"])
    ).json()
    assert facility["principal"] == 30_000_000
    assert facility["total_obligation"] == sum(r["gross_vnd"] for r in schedule)  # tax included
    assert facility["interest_rate_pct"] == 16.0 and facility["term_months"] == 4
    assert facility["days_remaining"] == 83  # the first day is paid
    assert facility["on_time_streak"] == 1
    repaid = sum(r["gross_vnd"] for r in schedule if r["repayment_day_id"] in s["day_ids"])
    assert analytics["months"][-1]["repaid_cumulative"] == repaid
    # Repayment against the average monthly revenue of the last 12 months filed.
    assert analytics["revenue_basis_vnd"] == 3_000_000_000
    assert analytics["months"][-1]["revenue_share"] == repaid / 3_000_000_000
    # The score's four groups, weighted 40/25/25/10, add back to the score.
    assert [f["key"] for f in analytics["score_factors"]] == ["bcq", "rsg", "sector", "behavioral"]
    assert [f["weight"] for f in analytics["score_factors"]] == [0.4, 0.25, 0.25, 0.1]
    assert abs(sum(f["score"] * f["weight"] for f in analytics["score_factors"]) - facility["score"]) < 0.01


async def test_a_business_with_nothing_repaying_has_no_loan_to_show(client, db_session, make_admin, make_user):
    from tests.market.test_funding_flow import _open_project

    setup = await _open_project(client, db_session, make_admin, make_user)

    analytics = (await client.get("/funding/me/business-analytics", headers=setup["sme"]["headers"])).json()

    assert analytics == {"facility": None, "months": [], "score_factors": [], "revenue_basis_vnd": None}
    assert (await client.get("/funding/me/business-transactions", headers=setup["sme"]["headers"])).json() == []
