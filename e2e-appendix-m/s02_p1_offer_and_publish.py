"""P1 (100M, 2 months): application to approval, the offer as the SME sees it (M2.4), the signing step (M2.5),
then publish and open the funding window.

Grade <= 62.5 gives a business rate >= 15%; lenders are capped at 2.5% x 12 / M = 15% for M = 2, which reproduces
the handbook 10.8 investor figures.
"""
import io

from pypdf import PdfReader

import builders as B
import e2e_lib as L
from e2e_lib import H, c, load, login, record, save, show

st = load()
A = H(st["admin"]["token"])
sme = B.onboard("sme1", "e2e.p1b.owner@example.com", "Tran Thi P Mot", "SME", kyb_name="CONG TY TNHH P1 E2E",
                tax_code="0311111112")
p1 = B.make_project("sme1", "CONG TY TNHH P1 E2E", "0311111112", 100_000_000, 2, 256_250_000, min_rate=15.0)
a = p1.get("approval_seen_by_sme") or {}
keys = ",".join(a.keys())
record("2.4 offer shows loan size, rate, duration, daily instalment",
       all(k in a for k in ("reference_rate_pct", "duration_months", "estimated_daily_repayment_vnd")), keys)
record("2.4 offer shows DRR", "daily_repayment_rate" in a or "drr" in keys.lower(), keys)
record("2.4 offer shows revenue share s", "revenue_share" in keys.lower(), keys)
record("2.4 offer shows guarantor", "guarantor" in keys.lower(), keys)

tok = login(sme["email"])[0]
app_id = p1["application_id"]
with c() as cl:
    r = cl.put(f"/loans/applications/{app_id}/guarantor-address", json={"address": "12 Le Loi, Quan 1, TP Ho Chi Minh"},
               headers=H(tok))
    print("guarantor address:", r.status_code)
    j = cl.get(f"/loans/applications/{app_id}/listing-documents", headers=H(tok)).json()
    print("signing blanks after MANUAL KYB:", {k: j[k]["missing_details"] for k in ("service_agreement", "standing_offer")})
    fake = (b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n9 0 obj\n<< /Type /Sig /Filter /Adobe.PPKLite "
            b"/SubFilter /ETSI.CAdES.detached /ByteRange [0 120 4216 380] /Contents <3082> >>\nendobj\n%%EOF\n")
    r = cl.post(f"/loans/applications/{app_id}/listing-documents/service-agreement",
                files={"file": ("hop-dong.pdf", fake, "application/pdf")}, headers=H(tok))
    record("2.5 signing through the platform refuses an unsigned / blank document", r.status_code >= 400,
           f"{show(r, 200)} {r.headers.get('X-Error-Code', '')}")
    if "LEGAL_REPRESENTATIVE" in str(j):
        record("1.3 manual KYB approval lets the SME sign", False,
               "LEGAL_REPRESENTATIVE (and COMPANY_ADDRESS) stay blank after a manual KYB approval; no staff field to set them")

# what a live KYB response would have carried, so the documents can be generated
B.fill_kyb_like_provider(sme["id"], "45 Nguyen Hue, Quan 1, TP HCM", "TRAN THI P MOT")
account = {"holder_type": "ENTITY", "bank_bin": "970436", "account_number": "0071000111222",
           "account_name": "CONG TY TNHH P1 E2E"}
window = B.open_window("sme1", p1, account)
with c() as cl:
    so = cl.get(f"/loans/applications/{app_id}/standing-offer", headers=H(tok))
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(so.content)).pages)
    with open(f"{L.OUT}/p1_standing_offer.txt", "w", encoding="utf-8") as f:
        f.write(text)
    record("2.5 standing offer states revenue share s", "................%" not in text,
           "Standing Offer prints 'Revenue share for extensions (s) ................%'" if "................%" in text else "filled")
    record("2.5 sign in one step (SME agreement + standing offer), guarantor signs", "PARTIAL",
           "three separate CA-signed PDF uploads, each verified with NEAC; not executable locally (rows attached as "
           "conftest.attach_signed_listing_documents does)")
record("4.7 / 10.3 Day-13 15:00 deadline and Day-14 disbursement exist", False,
       f"window_start={window['window_start']} window_end={window['window_end']} (open time + 14 days, no 15:00 anchor)")
st = load()
st["p1"] = p1 | {"sme": "sme1", "window": window}
save(st)
