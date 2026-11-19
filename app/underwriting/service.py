from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from types import MappingProxyType

from app.lending.models import LoanApplication, LoanApplicationFinancials, ScoreRun
from app.underwriting.grading import (
    GradingInput,
    GradingResult,
    grade,
    load_params,
    load_sector_reference,
    resolve_sector_inputs,
)
from app.underwriting.grading.params import ParamSet
from app.underwriting.models import BankRateConfig, ScoreRunInput
from app.underwriting.schemas import ApplicationFinancialsCreate


async def get_current_bank_rate(db: AsyncSession, as_of: Optional[date] = None) -> BankRateConfig:
    """T0b: the rate in force is the row with the greatest `effective_from`
    that is `<= as_of` (default: today). Raises if the table has no row
    that is yet in force -- there is always a seed row (12.0%, effective
    2026-09-17, the T0b resolution date), so this only fires if that seed
    was removed."""
    as_of = as_of or datetime.now(timezone.utc).date()
    result = await db.execute(
        select(BankRateConfig)
        .where(BankRateConfig.effective_from <= as_of)
        .order_by(BankRateConfig.effective_from.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=500,
            detail=f"no bank_rate_config row is in force as of {as_of} -- config error, not a valid state",
        )
    return row


async def set_bank_rate(db: AsyncSession, rate_pct: Decimal, effective_from: date, actor_id: UUID) -> BankRateConfig:
    """T0b: revise the board rate. Never edits or deletes an existing row
    -- a rate change is a new row with its own effective_from, so every
    past score run stays traceable to the rate actually in force when it
    ran."""
    row = BankRateConfig(rate_pct=rate_pct, effective_from=effective_from, created_by_actor_id=actor_id)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


def _insufficient_data_result_no_financials(params: ParamSet) -> GradingResult:
    """No `LoanApplicationFinancials` row exists yet for this application.
    `GradingInput` requires non-optional `industry`/`company_size`/
    `duration_months`/`loan_size_vnd`/cost fields that simply do not exist
    in that case, so `grade()` cannot be called at all -- there is nothing
    to validate. This mirrors `engine.py`'s own private
    `_insufficient_data_result()` shape exactly (R8: "no scoring
    attempted" -- None, never 0/0.0), without reaching into that module's
    private helper across a package boundary."""
    return GradingResult(
        engine_version=params.engine_version,
        params_version=params.params_version,
        derived=MappingProxyType({}),
        factor_scores=MappingProxyType({factor.key: None for factor in params.factors}),
        premiums=MappingProxyType({}),
        final_grade=None,
        interest_rate_pct=None,
        target_payment_vnd=None,
        target_daily_vnd=None,
        avg_daily_revenue_vnd=None,
        daily_repayment_rate=None,
        fired_gates=(),
        decision="INSUFFICIENT_DATA",
    )


def _resolve_sector(financials: LoanApplicationFinancials, params: ParamSet) -> tuple[dict, Optional[str]]:
    """T3: resolve sector factors/CAGR from the reference table unless the
    caller explicitly overrode them via the `*_override` columns (R6's
    upgrade path). Only touched here, assembling a GradingInput for a
    production caller -- never inside grade()/engine.py/factors.py/
    rollup.py (R2)."""
    overrides = {
        "regulatory": financials.ai_score_regulatory_override,
        "input_cost_vol": financials.ai_score_input_cost_vol_override,
        "cyclicality": financials.ai_score_cyclicality_override,
        "competitor": financials.ai_score_competitor_override,
        "macro": financials.ai_score_macro_override,
        "uncontrollable": financials.ai_score_uncontrollable_override,
        "founder": financials.ai_score_founder_override,
    }
    ai_score_overrides = {k: v for k, v in overrides.items() if v is not None}

    if ai_score_overrides and financials.sector_cagr_pct_override is not None:
        return {"ai_scores": ai_score_overrides, "sector_cagr_pct": financials.sector_cagr_pct_override}, None

    if financials.industry in params.supported_industries:
        sector_ref = load_sector_reference(supported_industries=params.supported_industries)
        resolved = resolve_sector_inputs(financials.industry, sector_ref)
        ai_scores = dict(resolved["ai_scores"])
        ai_scores.update(ai_score_overrides)
        sector_cagr_pct = (
            financials.sector_cagr_pct_override
            if financials.sector_cagr_pct_override is not None
            else resolved["sector_cagr_pct"]
        )
        return {"ai_scores": ai_scores, "sector_cagr_pct": sector_cagr_pct}, resolved["sector_reference_version"]

    # Excluded industry (gate 5 territory) or a genuinely unrecognised one.
    # For the latter, grade()'s own _validate_inputs raises ValueError --
    # this fallback just avoids a KeyError before that check runs. For an
    # excluded industry there is no sector reference row (the table only
    # covers the 15 supported industries), so ai_scores/cagr default to
    # "nothing supplied" unless overridden, which reads as AI_PENDING, not
    # a crash -- and gate 5 (hard) fires regardless of factor scores.
    return (
        {
            "ai_scores": ai_score_overrides,
            "sector_cagr_pct": financials.sector_cagr_pct_override
            if financials.sector_cagr_pct_override is not None
            else 0.0,
        },
        None,
    )


def build_grading_input(
    financials: LoanApplicationFinancials,
    *,
    loan_size_vnd: int,
    bank_rate_pct: float,
    resolved_sector: dict,
) -> GradingInput:
    """Assemble a `GradingInput` from the application's financials row
    plus the resolved sector inputs. `loan_size_vnd` comes from the
    `LoanApplication.requested_amount` -- it is not part of the mutable
    financials row (it's the application's own field). `bank_rate_pct`
    always comes from the caller (the current `bank_rate_config` row,
    D28/T0b) -- never from `GradingInput.bank_rate_pct`'s own dataclass
    default, which remains only a convenience for hand-built test inputs.
    """
    ai_scores = dict(resolved_sector["ai_scores"])
    sector_cagr_pct = resolved_sector["sector_cagr_pct"]

    monthly_revenue_raw = financials.monthly_revenue_vnd or [None] * 24
    monthly_revenue = tuple(Decimal(v) if v is not None else None for v in monthly_revenue_raw)

    def _dec_or_zero(v) -> Decimal:
        return Decimal(v) if v is not None else Decimal(0)

    return GradingInput(
        company_code=str(financials.application_id),
        industry=financials.industry,
        company_size=financials.company_size,
        loan_size_vnd=Decimal(loan_size_vnd),
        duration_months=financials.duration_months,
        operating_months=financials.operating_months,
        monthly_revenue=monthly_revenue,
        cogs_y1=_dec_or_zero(financials.cogs_y1_vnd),
        fixed_cost_y1=_dec_or_zero(financials.fixed_cost_y1_vnd),
        variable_cost_excl_cogs_y1=_dec_or_zero(financials.variable_cost_excl_cogs_y1_vnd),
        conc_top1_pct=financials.conc_top1_pct,
        conc_top3_pct=financials.conc_top3_pct,
        crr=financials.crr,
        rri=financials.rri,
        tcp=financials.tcp,
        sector_cagr_pct=sector_cagr_pct,
        ai_scores=ai_scores,
        owner_withdrawal=financials.owner_withdrawal,
        cic_score=financials.cic_score,
        kyc_aml_passed=financials.kyc_aml_passed,
        fraud_flags=tuple(financials.fraud_flags or ()),
        bank_rate_pct=bank_rate_pct,
    )


async def upsert_application_financials(
    db: AsyncSession, application_id: UUID, body: ApplicationFinancialsCreate
) -> LoanApplicationFinancials:
    """Create or replace the one `LoanApplicationFinancials` row for an
    application. Mutable/upsert by design (unlike `score_run_inputs`, the
    immutable snapshot taken AT SCORING TIME) -- see the model's own
    docstring."""
    result = await db.execute(select(LoanApplication).where(LoanApplication.id == application_id))
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    result = await db.execute(
        select(LoanApplicationFinancials).where(LoanApplicationFinancials.application_id == application_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = LoanApplicationFinancials(application_id=application_id)

    row.industry = body.industry
    row.company_size = body.company_size
    row.duration_months = body.duration_months
    row.operating_months = body.operating_months
    row.monthly_revenue_vnd = list(body.monthly_revenue_vnd) if body.monthly_revenue_vnd is not None else None
    row.cogs_y1_vnd = body.cogs_y1_vnd
    row.fixed_cost_y1_vnd = body.fixed_cost_y1_vnd
    row.variable_cost_excl_cogs_y1_vnd = body.variable_cost_excl_cogs_y1_vnd
    row.conc_top1_pct = body.conc_top1_pct
    row.conc_top3_pct = body.conc_top3_pct
    row.crr = body.crr
    row.rri = body.rri
    row.tcp = body.tcp
    row.sector_cagr_pct_override = body.sector_cagr_pct_override
    row.ai_score_regulatory_override = body.ai_score_regulatory_override
    row.ai_score_input_cost_vol_override = body.ai_score_input_cost_vol_override
    row.ai_score_cyclicality_override = body.ai_score_cyclicality_override
    row.ai_score_competitor_override = body.ai_score_competitor_override
    row.ai_score_macro_override = body.ai_score_macro_override
    row.ai_score_uncontrollable_override = body.ai_score_uncontrollable_override
    row.ai_score_founder_override = body.ai_score_founder_override
    row.owner_withdrawal = body.owner_withdrawal
    row.cic_score = body.cic_score
    row.kyc_aml_passed = body.kyc_aml_passed
    row.fraud_flags = list(body.fraud_flags)
    # Typed by an operator: the evidence fill at score time leaves it alone.
    row.source = "admin"

    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def start_score_run(
    db: AsyncSession, application_id: UUID, mode: Optional[str] = None
) -> tuple[ScoreRun, GradingResult]:
    # Locked: approve_score_run takes the same row lock, so a re-run cannot
    # slip in beside a run that is being locked (the "no LOCKED run" check
    # below would otherwise read before the lock commits).
    result = await db.execute(
        select(LoanApplication).where(LoanApplication.id == application_id).with_for_update()
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    # The first run moves SUBMITTED -> UNDER_REVIEW. A re-run is allowed while
    # the application is still under review and undecided, so an operator can
    # score again after the evidence or the financials change. Each run is a
    # NEW score_runs row (the old ones stay, append-only), so nothing is ever
    # recomputed silently (INV-5). Refused once a decision or a locked run
    # exists: from then on the score is what the decision was made on.
    if app.status not in ("SUBMITTED", "UNDER_REVIEW"):
        raise HTTPException(
            status_code=400,
            detail="Application must be in SUBMITTED status before scoring",
        )
    if app.status == "UNDER_REVIEW":
        if app.admin_approval != "PENDING":
            raise HTTPException(
                status_code=409,
                detail="This application has been decided; its score is not re-run",
            )
        locked = (
            await db.execute(
                select(ScoreRun.id).where(
                    ScoreRun.application_id == application_id, ScoreRun.status == "LOCKED"
                ).limit(1)
            )
        ).scalar_one_or_none()
        if locked is not None:
            raise HTTPException(
                status_code=409,
                detail="A locked score run already backs this application; it is not re-run",
            )

    params = load_params()
    bank_rate_row = await get_current_bank_rate(db)

    # Fill the financials row from what the application already holds (the
    # parsed uploads, the company form, the business verification) unless an
    # operator typed it. Imported here: app.loans.grading_inputs imports this
    # module for the bank rate.
    from app.loans.grading_inputs import sync_financials

    await sync_financials(db, application_id)

    result = await db.execute(
        select(LoanApplicationFinancials).where(LoanApplicationFinancials.application_id == application_id)
    )
    financials = result.scalar_one_or_none()

    if financials is None:
        # No financials submitted yet -- nothing to grade. INSUFFICIENT_DATA
        # per R8, not an error: see _insufficient_data_result_no_financials.
        grading_result = _insufficient_data_result_no_financials(params)
        sector_reference_version = None
    else:
        resolved_sector, sector_reference_version = _resolve_sector(financials, params)
        inputs = build_grading_input(
            financials,
            loan_size_vnd=int(app.requested_amount),
            bank_rate_pct=float(bank_rate_row.rate_pct),
            resolved_sector=resolved_sector,
        )
        try:
            grading_result = grade(inputs, params)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    sr = ScoreRun(
        application_id=application_id,
        status="READY",
        decision=grading_result.decision,
        engine_version=grading_result.engine_version,
        params_version=grading_result.params_version,
        sector_reference_version=sector_reference_version,
        final_grade_precise=grading_result.final_grade,
        interest_rate_pct=grading_result.interest_rate_pct,
        bank_rate_pct=bank_rate_row.rate_pct,
        bank_rate_effective_from=bank_rate_row.effective_from,
        daily_repayment_rate=(
            Decimal(str(grading_result.daily_repayment_rate)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if grading_result.daily_repayment_rate is not None
            else None
        ),
        overall_score=(
            Decimal(str(round(grading_result.final_grade, 2))) if grading_result.final_grade is not None else None
        ),
        recommended_terms={"mode": mode} if mode else None,
        factor_results=dict(grading_result.factor_scores),
    )
    app.status = "UNDER_REVIEW"
    db.add(app)
    db.add(sr)
    await db.flush()

    if financials is not None:
        db.add(
            ScoreRunInput(
                score_run_id=sr.id,
                company_code=str(application_id),
                industry=financials.industry,
                company_size=financials.company_size,
                loan_size_vnd=int(app.requested_amount),
                duration_months=financials.duration_months,
                operating_months=financials.operating_months,
                monthly_revenue_vnd=list(inputs.monthly_revenue),
                cogs_y1_vnd=financials.cogs_y1_vnd or 0,
                fixed_cost_y1_vnd=financials.fixed_cost_y1_vnd or 0,
                variable_cost_excl_cogs_y1_vnd=financials.variable_cost_excl_cogs_y1_vnd or 0,
                conc_top1_pct=financials.conc_top1_pct,
                conc_top3_pct=financials.conc_top3_pct,
                crr=financials.crr,
                rri=financials.rri,
                tcp=financials.tcp,
                sector_cagr_pct=inputs.sector_cagr_pct,
                sector_reference_version=sector_reference_version,
                ai_score_regulatory=inputs.ai_scores.get("regulatory"),
                ai_score_input_cost_vol=inputs.ai_scores.get("input_cost_vol"),
                ai_score_cyclicality=inputs.ai_scores.get("cyclicality"),
                ai_score_competitor=inputs.ai_scores.get("competitor"),
                ai_score_macro=inputs.ai_scores.get("macro"),
                ai_score_uncontrollable=inputs.ai_scores.get("uncontrollable"),
                ai_score_founder=inputs.ai_scores.get("founder"),
                owner_withdrawal=financials.owner_withdrawal,
                cic_score=financials.cic_score,
                kyc_aml_passed=financials.kyc_aml_passed,
                fraud_flags=list(financials.fraud_flags or ()),
                bank_rate_pct=bank_rate_row.rate_pct,
                bank_rate_effective_from=bank_rate_row.effective_from,
            )
        )
        await db.flush()

    await db.refresh(sr)
    return sr, grading_result


async def latest_score_run_id(db: AsyncSession, application_id: UUID) -> Optional[UUID]:
    return (
        await db.execute(
            select(ScoreRun.id)
            .where(ScoreRun.application_id == application_id)
            .order_by(ScoreRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_score_run(db: AsyncSession, score_run_id: UUID) -> ScoreRun:
    result = await db.execute(select(ScoreRun).where(ScoreRun.id == score_run_id))
    sr = result.scalar_one_or_none()
    if not sr:
        raise HTTPException(status_code=404, detail="Score run not found")
    return sr


async def replay_score_run(db: AsyncSession, score_run_id: UUID) -> tuple[ScoreRun, GradingResult]:
    """T4: 'replayable from stored inputs' -- rebuild the exact
    `GradingInput` from the append-only `score_run_inputs` row and re-run
    `grade()`, rather than persisting `premiums`/`fired_gates`/full
    `factor_scores` redundantly on every run. Used by the GET endpoint.

    Caveat, not solved by this change: this replays against the CURRENT
    `grading_params_v1.yaml` on disk, not a frozen historical copy. As
    long as nothing has changed since the run, replay reproduces it
    exactly (confirmed by test) -- but unlike `sector_reference_v1.yaml`
    (versioned by file per R7, so an old version stays loadable forever),
    the grading params file is mutated in place with only a version
    *string* bumped. A true historical replay across a params change
    would need the params file versioned by file the same way. Flagged,
    not built here -- out of T4's stated scope.
    """
    sr = await get_score_run(db, score_run_id)
    result = await db.execute(select(ScoreRunInput).where(ScoreRunInput.score_run_id == score_run_id))
    stored = result.scalar_one_or_none()
    if not stored:
        raise HTTPException(status_code=404, detail="Score run has no stored inputs to replay")

    params = load_params()
    ai_scores = {
        "regulatory": stored.ai_score_regulatory,
        "input_cost_vol": stored.ai_score_input_cost_vol,
        "cyclicality": stored.ai_score_cyclicality,
        "competitor": stored.ai_score_competitor,
        "macro": stored.ai_score_macro,
        "uncontrollable": stored.ai_score_uncontrollable,
        "founder": stored.ai_score_founder,
    }
    ai_scores = {k: v for k, v in ai_scores.items() if v is not None}

    inputs = GradingInput(
        company_code=stored.company_code,
        industry=stored.industry,
        company_size=stored.company_size,
        loan_size_vnd=Decimal(stored.loan_size_vnd),
        duration_months=stored.duration_months,
        operating_months=stored.operating_months,
        monthly_revenue=tuple(Decimal(v) if v is not None else None for v in stored.monthly_revenue_vnd),
        cogs_y1=Decimal(stored.cogs_y1_vnd),
        fixed_cost_y1=Decimal(stored.fixed_cost_y1_vnd),
        variable_cost_excl_cogs_y1=Decimal(stored.variable_cost_excl_cogs_y1_vnd),
        conc_top1_pct=stored.conc_top1_pct,
        conc_top3_pct=stored.conc_top3_pct,
        crr=stored.crr,
        rri=stored.rri,
        tcp=stored.tcp,
        sector_cagr_pct=stored.sector_cagr_pct,
        ai_scores=ai_scores,
        owner_withdrawal=stored.owner_withdrawal,
        cic_score=stored.cic_score,
        kyc_aml_passed=stored.kyc_aml_passed,
        fraud_flags=tuple(stored.fraud_flags or ()),
        bank_rate_pct=float(stored.bank_rate_pct),
    )
    grading_result = grade(inputs, params)
    return sr, grading_result


# Engine decisions an operator cannot lock. REJECT means a hard gate fired
# (KYC/AML failed, fraud flags, an excluded industry): locking it would turn
# the engine's absolute block into an operator's choice, recorded only as a
# generic approval. INSUFFICIENT_DATA means there was nothing to grade, so the
# run carries no grade or rate for a contract to use.
#
# AI_PENDING is NOT here: `founder` has no automatic source (only the
# operator's override), so it is the normal outcome of a complete run, and the
# rate is still computed (app/loans/lite_grading.py). Blocking it blocked
# nearly every approval.
_UNLOCKABLE_DECISIONS = frozenset({"REJECT", "INSUFFICIENT_DATA"})


async def approve_score_run(db: AsyncSession, score_run_id: UUID) -> tuple[ScoreRun, bool]:
    result = await db.execute(select(ScoreRun).where(ScoreRun.id == score_run_id))
    sr = result.scalar_one_or_none()
    if not sr:
        raise HTTPException(status_code=404, detail="Score run not found")
    if sr.status == "LOCKED":
        return sr, False

    # One LOCKED run backs an application (contracts and the marketplace read
    # it), so everything below runs under the application's row lock: the
    # same lock start_score_run and the operator decision take. Re-read the
    # run under it, since a concurrent approve may have just locked it.
    await db.execute(
        select(LoanApplication.id)
        .where(LoanApplication.id == sr.application_id)
        .with_for_update()
    )
    await db.refresh(sr)
    if sr.status == "LOCKED":
        return sr, False
    if sr.status != "READY":
        raise HTTPException(status_code=400, detail="Score run must be READY to approve")
    if sr.decision in _UNLOCKABLE_DECISIONS:
        raise HTTPException(
            status_code=409,
            detail=f"The engine decided {sr.decision}; this score run cannot be locked",
        )
    other_locked = (
        await db.execute(
            select(ScoreRun.id)
            .where(
                ScoreRun.application_id == sr.application_id,
                ScoreRun.status == "LOCKED",
                ScoreRun.id != sr.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if other_locked is not None:
        raise HTTPException(
            status_code=409,
            detail="Another score run is already locked for this application",
        )
    latest = (
        await db.execute(
            select(ScoreRun.id)
            .where(ScoreRun.application_id == sr.application_id)
            # id breaks ties: created_at is the transaction's now(), so runs
            # from one transaction share it and would otherwise come back in
            # any order.
            .order_by(ScoreRun.created_at.desc(), ScoreRun.id.desc())
            .limit(1)
        )
    ).scalar_one()
    if latest != sr.id:
        # An older run graded older evidence; locking it would put terms on
        # the contract that the current figures no longer support.
        raise HTTPException(
            status_code=409,
            detail="A newer score run exists for this application; approve that one",
        )
    sr.status = "LOCKED"
    sr.locked_at = datetime.now(timezone.utc)
    db.add(sr)
    await db.flush()
    # The engine's half just landed; if the operator already approved the
    # application, the project goes on the marketplace now.
    from app.projects.service import activate_project_if_approved

    await activate_project_if_approved(db, sr.application_id)
    await db.refresh(sr)
    return sr, True


def get_supported_industries() -> dict:
    """T3 / Issue 17: serve `supported_industries` over the API so Phat's
    dropdown is generated from the params file, never transcribed."""
    params = load_params()
    return {
        "supported_industries": list(params.supported_industries),
        "excluded_industries": list(params.excluded_industries),
        "params_version": params.params_version,
    }
