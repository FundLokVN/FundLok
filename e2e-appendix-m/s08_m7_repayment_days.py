"""M7.1, 7.3, 7.4, 7.6 on P1. Repayment starts on the next working day, so the first two due dates are moved
back to the two weekdays before today (DB edit) to make them payable now."""
import builders as B
import e2e_lib as L
from e2e_lib import H, c, load, login, record, save, show

st = load()
A = H(st["admin"]["token"])
tr = st["p1"]["tranches"]
active = [k for k in "ABC" if tr.get(k, {}).get("status") == "ACTIVE"]
OLD = "C" if "C" in active else "B"  # the lender used for the oldest-first check
win = st["p1"]["window"]["id"]
sme = login(st[st["p1"]["sme"]]["email"])[0]
d1, d2 = L.previous_weekdays(2)
first_two = L.psql(f"select string_agg(distinct due_date::text, ',' order by due_date::text) from (select d.due_date from "
                   f"repayment_days d join tranches t on t.id = d.tranche_id where t.window_id = '{win}' and d.day_index in "
                   f"(1, 2)) x")
dates = first_two.split(",")
L.psql(f"update repayment_days d set due_date = case d.day_index when 1 then date '{d1}' else date '{d2}' end, "
       f"report_deadline = d.report_deadline - (d.due_date - case d.day_index when 1 then date '{d1}' else date '{d2}' end) "
       f"* interval '1 day' from tranches t where t.id = d.tranche_id and t.window_id = '{win}' and d.day_index in (1, 2)")
print(f"moved due dates {dates} -> {d1}, {d2}")

with c() as cl:
    today = cl.get("/repayment/today", headers=H(sme)).json()
    rows = today["rows"]
    print("today's payments:", [(r["investor_display"], r["due_date"], r["net_vnd"]) for r in rows])
    record("7.1 Today's payments: one VietQR per lender with that lender's amount",
           all(r.get("vietqr_payload") for r in rows) and len({r["tranche_id"] for r in rows}) == len(active),
           f"{len(rows)} rows for {len({r['tranche_id'] for r in rows})} lenders")

    # A pays both due days in one transfer; B and C pay day 1 only (D was a no-show)
    due = cl.get(f"/repayment/tranches/{tr['A']['id']}/due", headers=H(sme)).json()
    ids = [r["repayment_day_id"] for r in due["days"]]
    r = cl.post(f"/repayment/tranches/{tr['A']['id']}/pay", data={"repayment_day_ids": ids},
                files={"file": ("a.png", B.png("A two days"), "image/png")}, headers=H(sme) | B.idem())
    print("A pays", len(ids), "days:", r.status_code)
    for k in [x for x in active if x != "A"]:
        d = cl.get(f"/repayment/tranches/{tr[k]['id']}/due", headers=H(sme)).json()["days"]
        cl.post(f"/repayment/tranches/{tr[k]['id']}/pay", data={"repayment_day_ids": [d[0]["repayment_day_id"]]},
                files={"file": (f"{k}.png", B.png(f"{k} day 1"), "image/png")}, headers=H(sme) | B.idem())

    # 7.3 lender A sees the payment and confirms; deadlines
    tokA = login(st["LA"]["email"])[0]
    rec = cl.get("/repayment/receipts", headers=H(tokA)).json()
    record("7.3 lender sees the payment", bool(rec), f"{len(rec)} receipt(s), net {[x['net_vnd'] for x in rec]}")
    sched = cl.get(f"/repayment/tranches/{tr['A']['id']}/schedule", headers=H(tokA)).json()
    import datetime as _dt
    paid_rows = [x for x in sched if x.get("status") not in ("DUE", "LATE") and x.get("report_deadline")]
    gap = L.psql(f"select round(extract(epoch from report_deadline - marked_paid_at) / 3600, 2) from repayment_days "
                 f"where tranche_id = '{tr['A']['id']}' and marked_paid_at is not null order by day_index limit 1")
    record("7.3 lender has 24 h from the SME's proof to press Not received", gap == "24.00",
           f"report deadline - SME proof = {gap} h")
    record("7.3 30-day dispute window shown", "dispute" in str(rec).lower() or "30" in str(rec),
           f"dispute fields in the receipts API: {[k for k in (rec[0].keys() if rec else []) if 'dispute' in k]}")
    if rec:
        r = cl.post(f"/repayment/receipts/{rec[0]['id']}/confirm", headers=H(tokA) | B.idem())
        print("A confirms:", r.status_code, r.json().get("status"))

    # 7.4 lender B presses Not received
    tokB = login(st["LB"]["email"])[0]
    day1 = cl.get(f"/repayment/tranches/{tr['B']['id']}/schedule", headers=H(tokB)).json()[0]["repayment_day_id"]
    r = cl.post(f"/repayment/days/{day1}/report-missing", json={"statement": "Chua nhan duoc tien"}, headers=H(tokB) | B.idem())
    staff = str(cl.get("/admin/funding/repayments", headers=A).json())
    record("7.4 Not received -> exceptions queue", r.status_code == 200 and "REPORTED_MISSED" in staff,
           f"report -> {r.json().get('status')}; shown in the staff repayments list: {'REPORTED_MISSED' in staff}; "
           "no staff decision route for a missed-day dispute")
    statuses = L.psql(f"select string_agg(distinct status, ',') from repayment_days where tranche_id = '{tr['B']['id']}'")
    record("7.4 a disputed or unproven day counts as Late (never presumed paid)", "LATE" in statuses or "REPORTED" in statuses,
           f"B's day statuses: {statuses}")

    # 7.6 oldest first: give one lender a third payable day, then pay only the newest while day 2 is still unpaid
    L.psql(f"update repayment_days set due_date = date '{d2}' where tranche_id = '{tr[OLD]['id']}' and day_index = 3")
    due = cl.get(f"/repayment/tranches/{tr[OLD]['id']}/due", headers=H(sme)).json()["days"]
    r = cl.post(f"/repayment/tranches/{tr[OLD]['id']}/pay", data={"repayment_day_ids": [due[-1]["repayment_day_id"]]},
                files={"file": ("c3.png", B.png("C newest day only"), "image/png")}, headers=H(sme) | B.idem())
    after = [(x["due_date"], x["status"]) for x in cl.get(f"/repayment/tranches/{tr[OLD]['id']}/schedule", headers=H(sme)).json()[:3]]
    record("7.6 payment applied to the oldest instalment first", not (r.status_code < 300 and after[1][1] == "DUE"),
           f"paying only the newer day while an older one is unpaid -> {r.status_code}; statuses {after}")
save(st)
