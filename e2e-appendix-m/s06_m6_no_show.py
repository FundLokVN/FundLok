"""M6 on P1: D never pays (hold fast-forwarded), D commits again and misses again, E takes the 10M top-up through
proof -> staff fields -> SME silence (M5.4), then staff Advance closes the round."""
import datetime as dt

import builders as B
import e2e_lib as L
from e2e_lib import H, c, load, login, record, save, show

st = load()
A = H(st["admin"]["token"])
p1 = st["p1"]
win, listing = p1["window"]["id"], p1["window"]["listing_id"]
tr = p1["tranches"]
tokD, tokE = login(st["LD"]["email"])[0], login(st["LE"]["email"])[0]


def window(cl):
    return cl.get(f"/funding/listings/{listing}/window", headers=A).json()


def d_status(cl):
    return cl.get(f"/admin/users/{st['LD']['id']}", headers=A).json().get("status")


with c() as cl:
    # B and C were marked Paid in s05; the SME says nothing: fast-forward its deadline, staff press Advance
    L.psql("update tranches set confirm_deadline = now() - interval '1 minute' where id in ('%s', '%s')"
           % (tr["B"]["id"], tr["C"]["id"]))
    L.psql(f"update tranches set hold_expires_at = now() - interval '1 minute' where id = '{tr['D']['id']}'")
    cl.post(f"/admin/funding/rounds/{win}/advance", headers=A)
    w = window(cl)  # reading the window also expires lapsed holds
    t = [x for x in cl.get("/admin/funding/transfers", headers=A).json()["items"] if x["id"] == tr["D"]["id"]]
    record("6.1 no-show D locked at the cut-off", d_status(cl) != "ACTIVE",
           f"D ticket -> {t[0]['status'] if t else '?'}; D account status = {d_status(cl)}")
    record("6.1 D's loan never takes effect; loan goes ahead with A-C (90M)", w["confirmed_vnd"] == 90_000_000,
           f"confirmed_vnd={w['confirmed_vnd']}")
    record("6.2 shortfall reopens for 7 calendar days", "PARTIAL",
           f"capacity {w['capacity_vnd']:,} reopened until the original window_end {w['window_end']}")

    r = B.reserve(cl, tokD, win, 10_000_000)
    record("6.4 D must redo eKYC and type the acknowledgement sentence first", False,
           f"no unlock flow; D committed again straight away: {r.status_code}")
    record("3.2 one commitment per project (ever)", r.status_code >= 400,
           f"after D's first ticket lapsed, a second commitment on the same project -> {r.status_code}")
    if r.status_code == 201:
        L.psql(f"update tranches set hold_expires_at = now() - interval '1 minute' where id = '{r.json()['id']}'")
        window(cl)
    record("6.5 second miss closes D permanently", d_status(cl) != "ACTIVE", f"D account status = {d_status(cl)}")

    # E funds the 10M top-up
    r = B.reserve(cl, tokE, win, 10_000_000)
    tr["E"] = tE = r.json()
    r = cl.post(f"/funding/tranches/{tE['id']}/receipt",
                files={"file": ("E.png", B.png(f"E 10,000,000 {tE['reference_code']}"), "image/png")}, headers=H(tokE) | B.idem())
    now = dt.datetime.now(dt.timezone.utc)
    cl.post(f"/admin/funding/transfers/{r.json()['transfer_id']}/fields",
            json={"amount_vnd": 10_000_000, "transferred_at": now.isoformat(), "payer_name": st["LE"]["name"],
                  "payer_account": st["LE"]["account_number"], "payee_account": "0071000111222",
                  "memo": tE["reference_code"], "bank_txn_id": f"FTE{now:%H%M%S}01", "reason": "E2E"}, headers=A)
    # the SME stays silent: fast-forward its deadline, then staff press Advance (no scheduler runs in production)
    L.psql(f"update tranches set confirm_deadline = now() - interval '1 minute' where id = '{tE['id']}'")
    adv = cl.post(f"/admin/funding/rounds/{win}/advance", headers=A).json()
    e = [x for x in cl.get("/admin/funding/transfers", headers=A).json()["items"] if x["id"] == tE["id"]][0]
    record("5.4 SME silence -> tranche counted received and confirmed", e["status"] in ("CONFIRMED", "ACTIVE"),
           f"E -> {e['status']} after the SME deadline + staff Advance; no 30-day dispute text anywhere")
    record("6.3 top-up: N = working days left, same end date as A-C", "PARTIAL",
           "E paid within the same round and gets a full-term schedule (checked in s07); the code has no top-up formula")
    print("round:", {k: adv.get(k) for k in ("status", "confirmed_vnd", "repayment_start_date")}, "fee:", adv.get("fee_invoice"))
    p1["round"] = {k: adv.get(k) for k in ("status", "repayment_start_date")}
    p1["fee_invoice"] = adv.get("fee_invoice")
save(st)
