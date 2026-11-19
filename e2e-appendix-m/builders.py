"""Reusable builders: onboard a user through the API, and take an SME project from creation to staff approval."""
import base64
import io
import os
import re
import subprocess

from PIL import Image, ImageDraw

import e2e_lib as L
from e2e_lib import H, PW, c, idem, latest_mail, load, login, save, show


# --------------------------------------------------------------------------- files
def jpeg_b64(color: str) -> str:
    b = io.BytesIO()
    Image.new("RGB", (640, 400), color).save(b, "JPEG")
    return base64.b64encode(b.getvalue()).decode()


def png(text: str) -> bytes:
    img = Image.new("RGB", (720, 1280), "white")
    ImageDraw.Draw(img).text((40, 80), text, fill="black")
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def image_pdf(text: str) -> bytes:
    """A readable PDF without a text layer (fine for charter / registration)."""
    img = Image.new("RGB", (800, 1000), "white")
    ImageDraw.Draw(img).text((40, 40), text, fill="black")
    b = io.BytesIO()
    img.save(b, "PDF")
    return b.getvalue()


def cic_pdf(name: str, tax: str) -> bytes:
    """A CIC credit report PDF with a Vietnamese text layer (the CIC parser needs one). Built with fpdf2 in a
    throwaway uv environment so the backend venv stays untouched."""
    out = os.path.join(L.OUT, f"cic_{tax}.pdf")
    if not os.path.exists(out):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))}
        subprocess.run(["uv", "run", "-q", "--no-project", "--with", "fpdf2", "python",
                        os.path.join(L.HERE, "mkcic.py"), out, name, tax], check=True, env=env)
    with open(out, "rb") as f:
        return f.read()


def wizard_files(name: str, tax: str) -> dict:
    """The five documents the code requires (app/admin/service.py REQUIRED_DOCUMENT_TYPES), using the repo's
    own synthetic builders for the parsed formats."""
    from tests.uploads.einvoice_fixtures import Inv, listing_xlsx
    from tests.uploads.test_tax_filings import folder_zip, statements_xml, vat_months
    months = {}
    for i in range(12):
        y, m = (2025, 11 + i) if 11 + i <= 12 else (2026, i - 1)
        months[f"Einvoice/{y}-{m:02d}.xlsx"] = listing_xlsx(
            y, m, [Inv(number=k + 1, buyer=f"03{k:08d}", amount=150_000_000 + k, seller=tax) for k in range(3)])
    return {
        "legal_charter": ("dieu-le.pdf", "application/pdf", image_pdf("Dieu le cong ty")),
        "business_registration": ("dkkd.pdf", "application/pdf", image_pdf("Giay chung nhan DKDN")),
        "e_invoice_data": ("Einvoice Data.zip", "application/zip", folder_zip(months)),
        "tax_filings": ("HTKK.zip", "application/zip",
                        folder_zip(vat_months(mst=tax) | {"BCTC_2025.xml": statements_xml(mst=tax)})),
        "cic_report": ("cic.pdf", "application/pdf", cic_pdf(name, tax)),
    }


def upload(cl, tok: str, app_id: str, doc_type: str, filename: str, ctype: str, data: bytes, size: int | None = None):
    """init-upload -> PUT to the presigned URL (local MinIO) -> confirm. Returns (init_response, confirm_response|None)."""
    r = cl.post("/uploads/init-upload", json={"loan_application_id": app_id, "document_type": doc_type,
                                              "filename": filename, "content_type": ctype, "size": size or len(data)},
                headers=H(tok))
    if r.status_code >= 300:
        return r, None
    httpx_put = __import__("httpx").put(r.json()["upload_url"], content=data, headers={"Content-Type": ctype}, timeout=60)
    httpx_put.raise_for_status()
    conf = cl.post("/uploads/confirm", json={"loan_application_id": app_id, "file_keys": [r.json()["file_key"]]},
                   headers=H(tok))
    return r, conf


# --------------------------------------------------------------------------- onboarding
def onboard(key: str, email: str, full_name: str, role: str, *, kyb_name: str | None = None, tax_code: str | None = None,
            bank: dict | None = None, kyc: bool = True) -> dict:
    """register -> verify email (Mailpit) -> consent (3 boxes + email OTP) -> role -> eKYC (MANUAL mode, staff
    approve) -> optional eKYB (staff approve) -> optional bank account (staff approve)."""
    st = load()
    if st.get(key, {}).get("onboarded"):
        st[key]["token"] = login(email)[0]
        save(st)
        return st[key]
    A = H(st["admin"]["token"])
    with c() as cl:
        t0 = L.utcnow_iso()
        r = cl.post("/auth/register", json={"email": email, "password": PW, "full_name": full_name})
        assert r.status_code == 201, show(r)
        mail = latest_mail(email, "verify", since=t0)
        token = re.search(r"verify-email\?token=([\w\-\.]+)", mail["Text"]).group(1)
        assert cl.get("/auth/verify-email", params={"token": token}).status_code < 300
        tok, me = login(email)
        t1 = L.utcnow_iso()
        assert cl.post("/consent/code", headers=H(tok)).status_code == 200
        code = re.findall(r"\b\d{6}\b", latest_mail(email, "confirmation code", since=t1)["Text"])[0]
        r = cl.post("/consent/accept", json={"version": L.CONSENT_VERSION, "terms": True, "data_processing": True,
                                             "data_sharing_tpv": True, "code": code}, headers=H(tok))
        assert r.status_code == 200, show(r)
        assert cl.patch("/users/me/role", json={"role": role}, headers=H(tok)).status_code == 200
        tok, me = login(email)
        if kyc:
            r = cl.post("/gverify/kyc/verify", json={"id_front_b64": jpeg_b64("white"), "id_back_b64": jpeg_b64("gray"),
                                                     "portrait_b64": jpeg_b64("beige")}, headers=H(tok))
            assert r.status_code == 201, show(r)
            r = cl.post(f"/admin/kyc-verifications/{r.json()['verification_id']}/resolve",
                        json={"decision": "APPROVED", "note": "E2E manual review",
                              "person_number": "0790" + str(abs(hash(email)) % 10**8).zfill(8),
                              "full_name": full_name.upper(), "date_of_birth": "1990-01-01"}, headers=A)
            assert r.status_code == 200, show(r)
        if kyb_name:
            r = cl.post("/gverify/kyb/verify", json={"document_b64": base64.b64encode(b"%PDF-1.4 kyb").decode(),
                                                     "document_type": "COMPANY", "tax_code": tax_code}, headers=H(tok))
            assert r.status_code == 201, show(r)
            r = cl.post(f"/admin/kyb-verifications/{r.json()['verification_id']}/resolve",
                        json={"decision": "APPROVED", "note": "E2E manual review", "business_name": kyb_name,
                              "tax_code": tax_code}, headers=A)
            assert r.status_code == 200, show(r)
        acct_id = None
        if bank:
            r = cl.post("/payment-accounts", json=bank, headers=H(tok))
            assert r.status_code == 201, show(r)
            acct_id = r.json()["id"]
            r = cl.post(f"/admin/payment-accounts/{acct_id}/decision",
                        json={"decision": "APPROVED", "note": "E2E looked up", "verified_holder_name": bank["account_name"],
                              "accept_difference": True}, headers=A)
            assert r.status_code == 200, show(r)
    st = load()
    st[key] = {"email": email, "id": me["id"], "token": tok, "role": role, "onboarded": True, "account_id": acct_id}
    save(st)
    return st[key]


def fill_kyb_like_provider(user_id: str, address: str, representative: str) -> None:
    """A manual KYB approval cannot record the legal representative or company address (defect, see M1.3), and
    signing refuses without them. Fill them as a live GVerify response would have."""
    L.psql("update gverify_kyb_verifications set company_address='%s', representatives='[{\"name\": \"%s\", "
           "\"position\": \"Giam doc\"}]'::jsonb where user_id='%s'" % (address, representative, user_id))


# --------------------------------------------------------------------------- project to approval
WEAK = {"company_size": "micro", "operating_months": 30, "conc_top1_pct": 45.0, "conc_top3_pct": 75.0, "crr": 45.0,
        "rri": 45.0, "tcp": 45.0, "owner_withdrawal": 35.0, "cic_score": 560, "kyc_aml_passed": True, "fraud_flags": []}


def make_project(sme_key: str, name: str, tax: str, amount: int, months: int, monthly_revenue: int,
                 industry: str = "Retail Trade", min_rate: float | None = None) -> dict:
    """Create the project, upload the wizard documents, submit, staff financials + score run + lock, staff approval.
    Resumable: re-running picks up an existing project with the same tax code."""
    st = load()
    tok = login(st[sme_key]["email"])[0]
    A = H(st["admin"]["token"])
    out: dict = {}
    with c() as cl:
        existing = [p for p in cl.get("/projects", headers=H(tok)).json() if p["tax_id"] == tax]
        if not existing:
            r = cl.post("/projects", json={"legal_name": name, "tax_id": tax, "industry": industry, "employee_count": 12,
                                           "incorporation_date": "2021-03-01",
                                           "loan_application": {"requested_amount": amount, "duration_months": months,
                                                                "purpose": "Working capital"}}, headers=H(tok))
            assert r.status_code == 201, show(r)
        proj = [p for p in cl.get("/projects", headers=H(tok)).json() if p["tax_id"] == tax][0]
        out["project_id"], app_id = proj["id"], proj["loan_application"]["id"]
        out["application_id"] = app_id
        if proj["loan_application"]["status"] == "DRAFT":
            for t, (fn, ct, data) in wizard_files(name, tax).items():
                _, conf = upload(cl, tok, app_id, t, fn, ct, data)
                assert conf is not None and conf.status_code < 300, f"{t}: {show(conf) if conf else 'init failed'}"
            r = cl.post(f"/loans/applications/{app_id}/submit", headers=H(tok))
            assert r.status_code == 200, show(r)
        yr = monthly_revenue * 12
        fin = WEAK | {"industry": industry, "duration_months": months, "monthly_revenue_vnd": [monthly_revenue] * 24,
                      "cogs_y1_vnd": int(yr * 0.813), "fixed_cost_y1_vnd": int(yr * 0.0976),
                      "variable_cost_excl_cogs_y1_vnd": int(yr * 0.0488)}
        run = None
        for attempt in range(6):
            if cl.put(f"/underwriting/applications/{app_id}/financials", json=fin, headers=A).status_code != 200:
                break  # already decided / locked
            r = cl.post("/underwriting/score-runs", json={"application_id": app_id, "mode": None}, headers=A)
            if r.status_code != 201:
                break
            run = r.json()
            rate = run["pricing"]["interest_rate_pct"]
            print(f"   score attempt {attempt}: grade={run['grade']['value']:.2f} rate={rate:.4f} decision={run['decision']}")
            if run["decision"] == "APPROVED" and (min_rate is None or rate >= min_rate):
                break
            fin["cic_score"] -= 30
            fin["crr"] -= 5
            fin["rri"] -= 5
        if run:
            out.update(score_run_id=run["id"], grade=run["grade"]["value"],
                       business_rate_pct=run["pricing"]["interest_rate_pct"],
                       engine_target_daily=run["pricing"]["target_daily_vnd"])
            cl.post(f"/underwriting/score-runs/{run['id']}/approve", headers=A)
        r = cl.post(f"/admin/applications/{app_id}/decision", json={"decision": "APPROVED"}, headers=A)
        out["admin_decision"] = show(r, 200)
        proj = [p for p in cl.get("/projects", headers=H(tok)).json() if p["id"] == out["project_id"]][0]
        out["approval_seen_by_sme"] = proj["loan_application"].get("approval")
    return out


def open_window(sme_key: str, project: dict, account: dict) -> dict:
    """Attach signed documents + activate (appdb), SME bank account (API, staff approve) -> funding window."""
    import appdb
    st = load()
    A = H(st["admin"]["token"])
    tok = login(st[sme_key]["email"])[0]
    activated = appdb.attach_and_activate(project["application_id"], st["admin"]["id"])
    with c() as cl:
        w = cl.get(f"/admin/projects/{project['project_id']}", headers=A).json().get("funding_window")
        if not w:
            r = cl.post("/payment-accounts", json=account, headers=H(tok))
            assert r.status_code == 201, show(r)
            r = cl.post(f"/admin/payment-accounts/{r.json()['id']}/decision",
                        json={"decision": "APPROVED", "note": "E2E bank lookup",
                              "verified_holder_name": account["account_name"]}, headers=A)
            assert r.status_code == 200, show(r)
            w = cl.get(f"/admin/projects/{project['project_id']}", headers=A).json().get("funding_window")
    print(f"   activated={activated} window={w and {k: w[k] for k in ('status', 'target_vnd', 'window_end')}}")
    return w


def reserve(cl, tok: str, window_id: str, amount: int):
    return cl.post(f"/funding/windows/{window_id}/reservations",
                   json={"amount_vnd": amount, "ack_risk_disclosure": True, "terms_version": L.TERMS_VERSION},
                   headers=H(tok) | idem())


# --------------------------------------------------------------------------- commit-then-accept (Money flow FINAL v1.0)
def commit(cl, tok: str, window_id: str, amount: int, account_id: str):
    """A pledge: press Confirm. No OTP, nothing signed, no money moves (standing offer Art. 3.1)."""
    return cl.post(f"/funding/windows/{window_id}/commitments",
                   json={"amount_vnd": amount, "ack_risk_disclosure": True, "terms_version": L.TERMS_VERSION,
                         "payment_account_id": account_id}, headers=H(tok) | idem())


def set_phone(user_id: str, phone: str) -> None:
    """A verified Vietnamese mobile on the account: the Day-0/Day-13 guarantor code and the Day-14 contract code go to
    it (Zalo; in this run they are written to logs/api.log instead)."""
    L.psql(f"update users set phone = '{phone}', phone_verified_at = now() where id = '{user_id}'")


def new_sme_project(key: str, email: str, owner: str, company: str, tax: str, amount: int, acct: str,
                    monthly_revenue: int = 256_250_000) -> dict:
    """Onboard an SME, take its project to approval and open its funding window (resumable)."""
    st = load()
    if st.get(key, {}).get("project"):
        return st[key]["project"]
    sme = onboard(key, email, owner, "SME", kyb_name=company, tax_code=tax)
    p = make_project(key, company, tax, amount, 2, monthly_revenue)
    fill_kyb_like_provider(sme["id"], "1 Le Duan, Quan 1, TP HCM", owner.upper())
    w = open_window(key, p, {"holder_type": "ENTITY", "bank_bin": "970436", "account_number": acct, "account_name": company})
    st = load()
    st[key]["project"] = p | {"window": w, "tax": tax, "sme": key, "acct": acct, "company": company}
    save(st)
    return st[key]["project"]


def staff_fields(cl, A: dict, transfer_id: str, *, amount: int, when, payer: dict, payee_acct: str, memo: str, txn: str):
    """Staff key in what the receipt shows (receipt OCR is off locally)."""
    return cl.post(f"/admin/funding/transfers/{transfer_id}/fields",
                   json={"amount_vnd": amount, "transferred_at": when.isoformat(), "payer_name": payer["name"],
                         "payer_account": payer["account_number"], "payee_account": payee_acct, "memo": memo,
                         "bank_txn_id": txn, "reason": "E2E: read from the screenshot"}, headers=A)
