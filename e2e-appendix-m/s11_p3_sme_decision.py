"""P3 (100M): the SME decision rules (M4). C and F commit and pay 20M each (40M, below 50%); the SME looks for an
accept / decline action, tries to accept 40M manually, then stays silent until the window ends."""
import datetime as dt

import appdb
import builders as B
import e2e_lib as L
from e2e_lib import H, c, load, login, record, save, show

B.onboard("sme3", "e2e.p3.owner@example.com", "Vo Van P Ba", "SME", kyb_name="CONG TY TNHH P3 E2E", tax_code="0333333333")
p3 = B.make_project("sme3", "CONG TY TNHH P3 E2E", "0333333333", 100_000_000, 2, 256_250_000)
w = B.open_window("sme3", p3, {"holder_type": "ENTITY", "bank_bin": "970436", "account_number": "0071000333444",
                                "account_name": "CONG TY TNHH P3 E2E"})
st = load()
A = H(st["admin"]["token"])
sme = login("e2e.p3.owner@example.com")[0]
with c() as cl:
    ids = []
    for k in "CF":
        tok = login(st[f"L{k}"]["email"])[0]
        t = B.reserve(cl, tok, w["id"], 20_000_000).json()
        r = cl.post(f"/funding/tranches/{t['id']}/receipt",
                    files={"file": (f"p3{k}.png", B.png(f"P3 {k} {t['reference_code']}"), "image/png")}, headers=H(tok) | B.idem())
        now = dt.datetime.now(dt.timezone.utc)
        cl.post(f"/admin/funding/transfers/{r.json()['transfer_id']}/fields",
                json={"amount_vnd": 20_000_000, "transferred_at": now.isoformat(), "payer_name": st[f"L{k}"]["name"],
                      "payer_account": st[f"L{k}"]["account_number"], "payee_account": "0071000333444",
                      "memo": t["reference_code"], "bank_txn_id": f"FTP3{k}{now:%H%M%S}", "reason": "E2E"}, headers=A)
        ids.append(t["id"])
    L.psql("update tranches set confirm_deadline = now() - interval '1 minute' where id in ('%s')" % "','".join(ids))
    cl.post(f"/admin/funding/rounds/{w['id']}/advance", headers=A)
    ww = cl.get(f"/funding/listings/{w['listing_id']}/window", headers=A).json()
    print("P3 after both confirmed:", {k: ww[k] for k in ("status", "confirmed_vnd", "capacity_vnd")})

    decision_routes = [p for p in appdb.openapi_paths() if any(x in p for x in ("accept", "decline", "cancel", "decision"))]
    record("4.2 SME accepts (accept action)", False, f"no SME accept route; routes matching accept/decline/cancel/decision: {decision_routes}")
    record("4.3 SME declines with a 'Decline / Khong chap nhan' button; project ends, lenders told, no fee", False,
           "no decline route and no button")
    r = cl.post(f"/funding/windows/{w['id']}/close-early", headers=H(sme) | B.idem())
    record("4.6 SME accepts manually with 40M committed -> loan of 40M goes ahead", r.status_code < 300,
           f"close-early (the only accept-like action) -> {show(r, 100)} {r.headers.get('X-Error-Code')}")

    L.psql(f"update funding_windows set window_end = now() - interval '1 minute' where id = '{w['id']}'")
    adv = cl.post(f"/admin/funding/rounds/{w['id']}/advance", headers=A).json()
    trs = [x["status"] for x in cl.get("/admin/funding/transfers", headers=A).json()["items"] if x["window_id"] == w["id"]]
    fees = [i["total_vnd"] for i in cl.get("/admin/funding/fee-invoices", headers=A).json() if i["window_id"] == w["id"]]
    record("4.5 silent with 40M (<50%) -> auto-declined, lenders told, no money moved, no fee",
           adv.get("status") != "REPAYING" and not fees, f"window -> {adv.get('status')}, tickets {trs}, SME fee invoices {fees}")
    record("4.4 silent with exactly 50M -> auto-accepted", False,
           "the 50% rule only runs on the dormant repay_from_approval=False branch; live config forms loans from any amount")
    record("4.1 reminders 3 days and 1 day before the deadline (in-app, SMS, Zalo), logged", False,
           "no acceptance deadline and no acceptance reminders; reminder logging exists only for daily repayment reminders")
    record("4.7 accept/decline at 15:01 on Day 13 refused", False, "no Day-13 15:00 deadline exists")
st = load()
st["p3"] = p3 | {"window": w}
save(st)
