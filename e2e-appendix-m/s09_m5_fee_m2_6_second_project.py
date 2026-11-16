"""M5.6 / 5.7 (SME activation fee) and M2.6 (a second project while P1 is unpaid).

Since d2ea9ee (docs/specs/projects/one-borrower.md) an SME account may have one project in progress, one live
listing, and projects only for the business its KYB verified. Decision B9 (15 Nov): limits apply per legal entity,
each entity underwritten separately, so a second business borrows through its own account.
"""
from e2e_lib import H, c, latest_mail, load, login, record, show

st = load()
A = H(st["admin"]["token"])
sme_key = st["p1"]["sme"]
tok = login(st[sme_key]["email"])[0]

with c() as cl:
    invoices = cl.get("/admin/funding/fee-invoices", headers=A).json()
    inv = [i for i in invoices if i["window_id"] == st["p1"]["window"]["id"]][0]
    record("5.6 one SME invoice = 3% x confirmed + VAT, due in 7 calendar days, not netted",
           (inv["fee_vnd"], inv["vat_vnd"], inv["total_vnd"]) == (3_000_000, 300_000, 3_300_000),
           f"fee {inv['fee_vnd']:,} + VAT {inv['vat_vnd']:,} = {inv['total_vnd']:,}; issued {inv['issued_at']} due {inv['due_at']}")
    mail = latest_mail(st[sme_key]["email"], "activation fee", since=inv["issued_at"].replace("Z", "+00:00")) or {}
    txt = (mail.get("HTML", "") + mail.get("Text", "")).lower()
    record("5.6 separate QR for the fee", "vietqr" in str(inv).lower() or "qr" in txt,
           "no QR in the invoice API or the email; the email says to reply for FundLok's account details")
    record("5.7 late interest on an unpaid SME fee (SME agreement 5.4: Civil Code Arts. 357, 468)", inv["late_fee_vnd"] > 0,
           f"late_fee_vnd={inv['late_fee_vnd']} (late_fee_bps_per_day=0)")

    # M2.6: the same SME, P1 repaying, tries the same business again and then another business
    base = {"legal_name": "CONG TY TNHH P1 E2E", "industry": "Retail Trade",
            "loan_application": {"requested_amount": 50_000_000, "duration_months": 2}}
    same = cl.post("/projects", json=base | {"tax_id": "0311111112"}, headers=H(tok))
    other = cl.post("/projects", json=base | {"legal_name": "CONG TY TNHH P1 CHI NHANH", "tax_id": "0311111113"},
                    headers=H(tok))
    record("2.6 no second project for the same business while P1 is unpaid", same.status_code >= 400,
           f"{same.status_code} {same.headers.get('X-Error-Code')}")
    record("2.6 this account cannot open a project for another business while P1 is unpaid", other.status_code >= 400,
           f"{other.status_code} {other.headers.get('X-Error-Code')} {show(other, 120)}")
    record("5.7 unpaid SME fee blocks the next listing only after the 7-day due date", "N/A",
           "not reachable in this run: the one-borrower rule refuses any new project while P1 repays; retest once a "
           "repaid business can apply again (one-borrower.md open question 1)")
