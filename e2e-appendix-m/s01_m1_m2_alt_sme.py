"""M1.2-1.4 and M2.1-2.3 on a first SME ("P1-alt"), including the stuck-application reproduction:
an application submits with a required document missing and staff can then only approve or reject.

The M1.1 consent screen itself was checked by hand in the browser (see README, manual checklist).
"""
import builders as B
from e2e_lib import H, c, load, login, record, save, show

st = load()
A = H(st["admin"]["token"])

# M1: onboard an SME through the API (register, email link, consent OTP, role, eKYC + eKYB with staff approval)
sme = B.onboard("sme_alt", "e2e.sme.owner@example.com", "E2E SME Owner", "SME", kyb_name="CONG TY TNHH E2E P1",
                tax_code="0312345678")
tok = login(sme["email"])[0]
with c() as cl:
    kyc = cl.get("/gverify/kyc/status", headers=H(tok)).json()
    kyb = cl.get("/gverify/kyb/status", headers=H(tok)).json()
    record("1.2 eKYC verdict stored", "PARTIAL" if kyc["status"] == "APPROVED" else False,
           f"status={kyc['status']} via staff manual review (automatic GVerify OCR/face match needs vendor keys)")
    record("1.3 eKYB verdict stored", "PARTIAL" if kyb["status"] == "APPROVED" else False,
           f"status={kyb['status']} via staff manual review; company_address={kyb.get('company_address')} "
           "(the manual resolve form cannot record address or legal representative)")

    # M2.1 bounds
    base = {"legal_name": "CONG TY TNHH E2E P1", "tax_id": "0312345678", "industry": "Retail Trade", "employee_count": 12,
            "incorporation_date": "2021-03-01"}
    for amt, dur, label in ((19_999_999, 2, "below 20M"), (100_000_001, 2, "above 100M"), (100_000_000, 7, "7 months"),
                            (100_000_000, 0, "0 months")):
        r = cl.post("/sme/businesses", json=base | {"loan_application": {"requested_amount": amt, "duration_months": dur}},
                    headers=H(tok))
        record(f"2.1 refuse {label}", r.status_code == 422, show(r, 200))
    existing = [p for p in cl.get("/projects", headers=H(tok)).json() if p["tax_id"] == "0312345678"]
    if not existing:
        r = cl.post("/sme/businesses", json=base | {"loan_application": {"requested_amount": 100_000_000,
                                                                          "duration_months": 2, "purpose": "Working capital"}},
                    headers=H(tok))
        record("2.1 100M over 2 months accepted as DRAFT", r.status_code == 201, show(r, 200))
    proj = [p for p in cl.get("/projects", headers=H(tok)).json() if p["tax_id"] == "0312345678"][0]
    app_id = proj["loan_application"]["id"]
    st = load()
    st["p1_alt"] = {"project_id": proj["id"], "application_id": app_id}
    save(st)

    if proj["loan_application"]["status"] == "DRAFT":
        # M2.2 wrong type / content type / size
        r, _ = B.upload(cl, tok, app_id, "legal_charter", "charter.docx", "application/msword", b"x")
        record("2.2 wrong file type refused", r.status_code == 400, show(r, 200))
        r, _ = B.upload(cl, tok, app_id, "legal_charter", "charter.pdf", "image/png", b"x")
        record("2.2 wrong content-type refused", r.status_code == 400, show(r, 200))
        r, _ = B.upload(cl, tok, app_id, "cic_report", "cic.pdf", "application/pdf", b"%PDF", size=26 * 1024 * 1024)
        record("2.2 oversize refused", r.status_code == 400, show(r, 200))
        files = B.wizard_files("CONG TY TNHH E2E P1", "0312345678")
        # deliberately a CIC PDF without a text layer: refused at confirm, the document stays PENDING
        files["cic_report"] = ("cic.pdf", "application/pdf", B.image_pdf("CIC report, scanned"))
        for t, (fn, ct, data) in files.items():
            _, conf = B.upload(cl, tok, app_id, t, fn, ct, data)
            print("   upload", t, conf.status_code if conf is not None else "init failed")
        B.upload(cl, tok, app_id, "legal_charter", "dieu-le-v2.pdf", "application/pdf", B.image_pdf("v2"))
        docs = [p for p in cl.get("/projects", headers=H(tok)).json() if p["id"] == proj["id"]][0]["loan_application"]["documents"]
        charters = [d for d in docs if d["document_type"] == "legal_charter"]
        record("2.2 replacement overwrites", len(charters) == 1 and charters[0]["original_filename"] == "dieu-le-v2.pdf",
               f"{len(charters)} charter row(s), file {charters[0]['original_filename'] if charters else None}")
        statuses = {d["document_type"]: d["status"] for d in docs}
        r = cl.post(f"/loans/applications/{app_id}/submit", headers=H(tok))
        record("2.2 submit refused while a required document is missing", r.status_code >= 400,
               f"documents {statuses}; submit -> {show(r, 120)}")

    # M2.3 grading
    fin = {"industry": "Retail Trade", "company_size": "micro", "duration_months": 2, "operating_months": 60,
           "monthly_revenue_vnd": [256_250_000] * 24, "cogs_y1_vnd": 1_800_000_000, "fixed_cost_y1_vnd": 400_000_000,
           "variable_cost_excl_cogs_y1_vnd": 200_000_000, "conc_top1_pct": 30.0, "conc_top3_pct": 50.0, "crr": 60.0,
           "rri": 60.0, "tcp": 60.0, "owner_withdrawal": 50.0, "cic_score": 650, "kyc_aml_passed": True, "fraud_flags": []}
    if cl.put(f"/underwriting/applications/{app_id}/financials", json=fin, headers=A).status_code == 200:
        r = cl.post("/underwriting/score-runs", json={"application_id": app_id, "mode": None}, headers=A)
        if r.status_code == 201:
            run = r.json()
            grade, rate = run["grade"]["value"], run["pricing"]["interest_rate_pct"]
            bank = run["versions"]["bank_rate_pct"]
            exp = min(20, bank + (20 - bank) * (100 - grade) / 100)
            record("2.3 grade 0-100 stored with parameter version", 0 <= grade <= 100 and bool(run["versions"]["params"]),
                   f"grade={grade:.2f} params={run['versions']['params']}")
            record("2.3 rate = min(20%, bank + (20% - bank)(100 - grade)/100), never above 20%",
                   abs(exp - rate) < 0.01 and rate <= 20, f"rate={rate:.4f} expected={exp:.4f} bank={bank}")
            daily, total = int(run["pricing"]["target_daily_vnd"]), int(run["pricing"]["target_payment_vnd"])
            record("2.3 engine DRR uses 21 working days a month", total // (2 * 21) == daily,
                   f"target daily {daily:,} = total {total:,} / (2 x {round(total / daily / 2)})")
            cl.post(f"/underwriting/score-runs/{run['id']}/approve", headers=A)
    r = cl.post(f"/admin/applications/{app_id}/decision", json={"decision": "APPROVED"}, headers=A)
    record("2.2 staff approval refused while a document is missing", r.status_code == 409,
           f"{show(r, 150)} {r.headers.get('X-Error-Code', '')}; staff decisions are only APPROVED/REJECTED, so this "
           "application can no longer be corrected")
