"""Assemble the grading engine's inputs from everything an application holds.

WHY
A score run reads one `loan_application_financials` row, and nothing wrote it
from what the SME actually submitted — so every score run on a real
application came back INSUFFICIENT_DATA. This module is the first half of the
bridge: it gathers each `GradingInput` field from its best available source and
says, field by field, where the value came from and what is still missing. `assemble`
writes nothing; `sync_financials` is the second half: at score time it writes
the `loan_application_financials` row the engine reads, from `assemble`.

SOURCES, IN ORDER OF WEIGHT
  vat         the signed monthly VAT declarations (step-3 upload).
  statements  the signed year-end financial statements (step-3 upload).
  e-invoice   the parsed portal listings (app/uploads/parsers/einvoice.py).
              Unsigned — see that module — but real invoice-level data.
  cic         the CIC credit-report PDF (step-4 upload). Unsigned.
  kyb         the GVerify business verification (verified identity).
  project     what the SME filled in on the application form.
  typed       the SME's typed figures (LoanApplication.self_reported_figures).
  sector      the sector reference table (DRAFT, unreviewed).
  config      bank_rate_config, as admin score runs read it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gverify.models import STATUS_APPROVED, STATUS_REJECTED, GVerifyKybVerification
from app.lending.models import LoanApplication, LoanApplicationFinancials, Project, ProjectOwnership
from app.projects.engine_constraints import company_size_for_headcount, params
from app.underwriting.grading.sector_reference import load_sector_reference, resolve_sector_inputs
from app.underwriting.service import get_current_bank_rate
from app.uploads.models import CicSummary, EInvoiceSummary, TaxFilingSummary
from app.loans.lite_grading import owner_withdrawal_score
from app.uploads.parsers.einvoice import grading_fields


@dataclass
class Field:
    value: Any
    source: str | None
    status: str  # "filled" | "missing" | "partial"
    note: str | None = None


def _filled(value, source, note=None) -> Field:
    return Field(value, source, "filled", note)


def _missing(note) -> Field:
    return Field(None, None, "missing", note)


def _months_since(start: date, today: date) -> int:
    return (today.year - start.year) * 12 + (today.month - start.month) - (today.day < start.day)


async def assemble(db: AsyncSession, application_id: UUID) -> dict[str, Any]:
    app = (await db.execute(select(LoanApplication).where(LoanApplication.id == application_id))).scalar_one_or_none()
    if app is None:
        raise HTTPException(status_code=404, detail="Application not found")
    project = (await db.execute(select(Project).where(Project.id == app.project_id))).scalar_one()
    figures = app.self_reported_figures or {}
    summary = (
        await db.execute(select(EInvoiceSummary).where(EInvoiceSummary.loan_application_id == app.id))
    ).scalar_one_or_none()
    evidence = grading_fields(summary.features) if summary else None
    filings = (
        await db.execute(select(TaxFilingSummary).where(TaxFilingSummary.loan_application_id == app.id))
    ).scalar_one_or_none()
    statements = filings.statements if filings else None
    cic = (
        await db.execute(select(CicSummary).where(CicSummary.loan_application_id == app.id))
    ).scalar_one_or_none()
    vat = filings.vat if filings else None

    f: dict[str, Field] = {}

    # --- identity and the application itself --------------------------------
    f["industry"] = (
        _filled(project.industry, "project")
        if project.industry
        else _missing("the project has no industry")
    )
    # Derived from the stored headcount now, not stored itself: the bands are
    # the params file's to set.
    size = company_size_for_headcount(project.employee_count) if project.employee_count else None
    if size:
        f["company_size"] = _filled(size, "project", f"{project.employee_count} employees")
    elif project.employee_count:
        f["company_size"] = _missing(f"{project.employee_count} employees is outside every size band")
    else:
        f["company_size"] = _missing("no headcount on the company form (projects created before it was stored)")
    f["loan_size_vnd"] = _filled(int(app.requested_amount), "project")
    f["duration_months"] = (
        _filled(app.duration_months, "project")
        if app.duration_months
        else _missing("no loan term on the company form (applications created before it was stored)")
    )
    f["operating_months"] = (
        _filled(_months_since(project.incorporation_date, date.today()), "project", "from the incorporation date")
        if project.incorporation_date
        else _missing("no incorporation date")
    )

    # --- revenue --------------------------------------------------------------
    # Signed VAT declarations first: 24 consecutive months is the whole series
    # the engine needs, from signed evidence. The e-invoice export covers only
    # the latest 12.
    latest = evidence["monthly_revenue_latest"] if evidence else None
    if vat and vat["months_covered"] >= 24 and not vat["has_gaps"]:
        series = vat["monthly_revenue"][-24:]
        f["monthly_revenue"] = _filled(
            {"m1_m24": [m["revenue_vnd"] for m in series], "periods": [m["period"] for m in series]},
            "vat",
            "signed monthly VAT declarations" + ("" if vat["all_signed"] else " (some unsigned)"),
        )
    elif latest and len(latest) == 12:
        f["monthly_revenue"] = Field(
            {"m13_m24": latest, "periods": evidence["monthly_revenue_periods"], "m1_m12": None},
            "e-invoice",
            "partial",
            "the latest 12 months are observed; the engine needs the 12 before them too (signed VAT declarations)",
        )
    else:
        f["monthly_revenue"] = _missing(
            "needs 24 consecutive months; "
            + (f"the e-invoices cover {summary.months_covered}" if summary else "no e-invoice listings uploaded")
        )

    # --- costs ---------------------------------------------------------------
    # Cost of goods sold is a statutory line, so the signed statement wins.
    # No filing splits costs into fixed and variable (input-sources §6.1), so
    # those two follow a STATED POLICY, applied by the statement parser:
    # fixed = management expense + financial expense, variable = selling
    # expense (see app/uploads/parsers/statements.py). The wizard reads both from
    # the statements and does not let the applicant change them, so the
    # statements win here as well; the typed figure is only the fallback for an
    # application with no statements on file.
    if statements:
        f["cogs_y1"] = _filled(statements["cogs_y1"], "statements", f"FY{statements['fiscal_year']} income statement line 11")
    else:
        f["cogs_y1"] = _filled(figures["cogs_y1"], "typed") if figures.get("cogs_y1") is not None else _missing("not typed")
    for key in ("fixed_cost_y1", "variable_cost_excl_cogs_y1"):
        if statements:
            # `cost_rule` is what the parser applied; rows stored before it was
            # renamed carry the same idea under `split_method`.
            rule = (
                statements.get("cost_rule")
                or statements.get("split_method")
                or f"FY{statements['fiscal_year']} income statement"
            )
            f[key] = _filled(statements[key], "statements", rule)
        elif figures.get(key) is not None:
            f[key] = _filled(figures[key], "typed")
        else:
            f[key] = _missing("not typed")

    # --- customers: invoice-level data beats a typed guess --------------------
    for key in ("conc_top1_pct", "conc_top3_pct"):
        if evidence and evidence.get(key) is not None:
            f[key] = _filled(evidence[key], "e-invoice", "excludes sales to the company's own branches")
        elif figures.get(key) is not None:
            f[key] = _filled(figures[key], "typed")
        else:
            f[key] = _missing("optional — scores 0 when absent")
    f["crr"] = (
        _filled(evidence["crr"], "e-invoice", summary.features.get("crr_definition"))
        if evidence and evidence.get("crr") is not None
        else _missing("optional — needs 4+ months of e-invoices")
    )
    f["rri"] = _missing("no source defined (grading-input-sources Q5)")
    f["tcp"] = _missing("no source defined (grading-input-sources Q5)")
    # The engine reads a 0-100 SCORE (higher is better), not the share
    # withdrawn: converted by owner_withdrawal_score, as the band adapter does.
    withdrawn = None
    source = None
    if statements and statements.get("owner_withdrawal_pct") is not None:
        withdrawn, source = statements["owner_withdrawal_pct"], "statements"
    elif figures.get("owner_withdrawal_pct") is not None:
        withdrawn, source = figures["owner_withdrawal_pct"], "typed"
    if withdrawn is not None:
        f["owner_withdrawal"] = _filled(
            round(owner_withdrawal_score(withdrawn), 2),
            source,
            f"{withdrawn}% of profit withdrawn, as the engine's 0-100 score",
        )
    else:
        f["owner_withdrawal"] = _missing("optional — needs a profitable year's statements, or typed")

    # --- sector ---------------------------------------------------------------
    p = params()
    if project.industry in p.supported_industries:
        resolved = resolve_sector_inputs(project.industry, load_sector_reference(supported_industries=p.supported_industries))
        f["sector_cagr_pct"] = _filled(resolved["sector_cagr_pct"], "sector", "draft table, unreviewed")
        ai = dict(resolved["ai_scores"])
        f["ai_scores"] = Field(ai, "sector", "partial", "6 of 7: `founder` has no source, so the decision is AI_PENDING at best")
    else:
        f["sector_cagr_pct"] = _missing("industry is not one the engine supports")
        f["ai_scores"] = _missing("industry is not one the engine supports")

    # --- verification ---------------------------------------------------------
    # The engine reads cic_score on a 150-750 scale (params factor 20); CIC's
    # individual model prints 403-706, inside it. Whether an owner's PERSONAL
    # score may stand in for the company's is an open underwriting question,
    # so the note always says whose score it is.
    if cic and cic.score is not None:
        whose = "an individual's (usually the owner's)" if cic.subject_type == "individual" else "the company's"
        f["cic_score"] = _filled(
            cic.score,
            "cic",
            f"{whose} CIC score, rank {cic.rank}, scored {cic.scored_on}; unsigned PDF",
        )
    elif cic:
        f["cic_score"] = _missing("the CIC report was read but no score was found in it")
    else:
        f["cic_score"] = _missing("no CIC report uploaded")
    owner_ids = (
        await db.execute(select(ProjectOwnership.user_id).where(ProjectOwnership.project_id == project.id))
    ).scalars().all()
    kyb = None
    if owner_ids and project.tax_id:
        kyb = (
            await db.execute(
                select(GVerifyKybVerification)
                .where(
                    GVerifyKybVerification.user_id.in_(owner_ids),
                    GVerifyKybVerification.tax_code == project.tax_id,
                )
                .order_by(desc(GVerifyKybVerification.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
    if kyb and kyb.status in (STATUS_APPROVED, STATUS_REJECTED):
        f["kyc_aml_passed"] = _filled(kyb.status == STATUS_APPROVED, "kyb", f"GVerify {kyb.status}")
        f["fraud_flags"] = _filled([], "kyb")
    else:
        f["kyc_aml_passed"] = _missing(
            "no decided GVerify business verification for this tax code"
            + (f" (latest: {kyb.status})" if kyb else "")
        )
        f["fraud_flags"] = _missing("comes with the business verification")

    try:
        rate = await get_current_bank_rate(db)
        f["bank_rate_pct"] = _filled(float(rate.rate_pct), "config")
    except HTTPException:
        f["bank_rate_pct"] = _missing("no bank_rate_config row in force")

    # What stops a grade outright (engine R8 and NOT NULL columns), as opposed
    # to optional inputs that only lower it.
    fatal = ("industry", "company_size", "duration_months", "operating_months",
             "monthly_revenue", "cogs_y1", "fixed_cost_y1", "variable_cost_excl_cogs_y1",
             "cic_score", "kyc_aml_passed")
    blockers = [k for k in fatal if f[k].status != "filled"]

    return {
        "application_id": str(app.id),
        "gradeable": not blockers,
        "blockers": blockers,
        "fields": {k: asdict(v) for k, v in f.items()},
        "tax_filings": (
            {
                "parser_version": filings.parser_version,
                "tax_code": filings.tax_code,
                "fiscal_year": filings.fiscal_year,
                "vat_months": filings.vat_months,
                "signed": bool(statements.get("signed")),
                "interest_expense_vnd": statements.get("interest_expense_vnd"),
                "warnings": filings.warnings,
            }
            if filings
            else None
        ),
        "cic": (
            {
                "parser_version": cic.parser_version,
                "subject_type": cic.subject_type,
                "score": cic.score,
                "rank": cic.rank,
                "scored_on": cic.scored_on.isoformat() if cic.scored_on else None,
                "debt_total_vnd_million": cic.report.get("debt_total_vnd_million"),
                "debt_attention_vnd_million": cic.report.get("debt_attention_vnd_million"),
                "debt_bad_vnd_million": cic.report.get("debt_bad_vnd_million"),
                "negative_history": cic.report.get("negative_history"),
                "warnings": cic.warnings,
            }
            if cic
            else None
        ),
        "e_invoice": (
            {
                "parser_version": summary.parser_version,
                "seller_tax_code": summary.seller_tax_code,
                "period": f"{summary.period_start}–{summary.period_end}",
                "months_covered": summary.months_covered,
                "signed": summary.features.get("signed", False),
                "related_party_pct": summary.features.get("related_party_pct"),
                "total_revenue_vnd": summary.features.get("total_revenue_vnd"),
                "warnings": summary.warnings,
            }
            if summary
            else None
        ),
    }


async def sync_financials(db: AsyncSession, application_id: UUID) -> list[str]:
    """Write the financials row a score run grades, from `assemble`.

    Called by start_score_run() before it reads the row, so every run —
    including a re-run — grades the application's current evidence. Returns
    the blockers (empty when gradeable).

      * An operator-entered row (source "admin") is never overwritten.
      * Nothing is invented: a field `assemble` could not fill is written as
        NULL, which the engine reads as missing and answers INSUFFICIENT_DATA
        for (R8) — never a zero or a default.
      * industry, company_size, duration_months and operating_months are NOT
        NULL on the row, so without all four no row is written at all and the
        run reports no financials, as before.
    """
    row = (
        await db.execute(
            select(LoanApplicationFinancials).where(LoanApplicationFinancials.application_id == application_id)
        )
    ).scalar_one_or_none()
    if row is not None and row.source == "admin":
        return []

    data = await assemble(db, application_id)
    fields = data["fields"]

    def value(key):
        f = fields[key]
        return f["value"] if f["status"] == "filled" else None

    required = ("industry", "company_size", "duration_months", "operating_months")
    if any(value(k) is None for k in required):
        return data["blockers"]

    revenue = fields["monthly_revenue"]
    series = None
    if revenue["status"] == "filled":
        series = list(revenue["value"]["m1_m24"])
    elif revenue["status"] == "partial" and revenue["value"].get("m13_m24"):
        # The latest 12 months only: the older 12 stay NULL, which is how the
        # engine represents a missing month.
        series = [None] * 12 + list(revenue["value"]["m13_m24"])

    if row is None:
        row = LoanApplicationFinancials(application_id=application_id)
    row.source = "evidence"
    row.industry = value("industry")
    row.company_size = value("company_size")
    row.duration_months = value("duration_months")
    row.operating_months = value("operating_months")
    row.monthly_revenue_vnd = series
    row.cogs_y1_vnd = value("cogs_y1")
    row.fixed_cost_y1_vnd = value("fixed_cost_y1")
    row.variable_cost_excl_cogs_y1_vnd = value("variable_cost_excl_cogs_y1")
    row.conc_top1_pct = value("conc_top1_pct")
    row.conc_top3_pct = value("conc_top3_pct")
    row.crr = value("crr")
    row.rri = value("rri")
    row.tcp = value("tcp")
    row.owner_withdrawal = value("owner_withdrawal")
    row.cic_score = value("cic_score")
    row.kyc_aml_passed = value("kyc_aml_passed")
    row.fraud_flags = list(value("fraud_flags") or [])
    # Sector CAGR and the AI factor scores are resolved from the sector
    # reference table at scoring time; the overrides stay NULL.
    db.add(row)
    await db.flush()
    return data["blockers"]
