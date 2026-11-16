"""M8 (and M7.3's 'silence = received'): run the worker jobs that are NOT deployed in production
(app/direct_transfer/jobs.py repayment_day_job + invoice_job) once with a clock just after month end, then two days
later when the month's last days have settled."""
import datetime as dt

import appdb
import builders as B
import e2e_lib as L
from e2e_lib import H, c, load, login, record

st = load()
A = H(st["admin"]["token"])
tr = st["p1"]["tranches"]
today = L.ict_today()
month_start = today.replace(day=1)
next_month = (month_start + dt.timedelta(days=32)).replace(day=1)
for result in appdb.run_jobs([dt.datetime.combine(next_month, dt.time(0, 20), L.ICT),
                              dt.datetime.combine(next_month + dt.timedelta(days=2), dt.time(0, 20), L.ICT)]):
    print(result)

rows = L.psql(f"select due_date, status, marked_paid_at is not null from repayment_days where tranche_id = '{tr['A']['id']}' "
              f"and due_date < '{next_month}' order by due_date").splitlines()
unpaid_presumed = [r for r in rows if r.endswith("|PRESUMED_PAID|f")]
record("7.3 silence = received only after the SME uploaded proof", not unpaid_presumed,
       f"A: {len(unpaid_presumed)} of {len(rows)} days this month became PRESUMED_PAID with no SME payment or proof "
       "(repayment.presume_paid_days)")

acc = L.psql(f"select received_gross_vnd, fee_vnd from investor_fee_accruals where tranche_id = '{tr['A']['id']}'")
inv = L.psql(f"select fee_vnd, vat_vnd, total_vnd, due_at from fee_invoices where kind = 'INVESTOR_SERVICE' and "
             f"payer_user_id = '{st['LA']['id']}' order by issued_at desc limit 1")
if acc:
    received, fee = map(int, acc.split("|"))
    F = 40_000_000 * 250 * 2 // (12 * 10_000)  # P x f_L x M / 12, f_L = 2.5%
    expected = F * received // 41_000_000
    record("8.1 monthly lender fee = (F - charged) x received / balance at month start, before tax", "PARTIAL",
           f"A: fee {fee:,} on received {received:,} (formula gives {expected:,}); invoice {inv}; but 'received' includes "
           "presumed-paid days the SME never paid, and the job is not deployed")
else:
    record("8.1 monthly lender fee", False, "no accrual produced")

with c() as cl:
    tokA = login(st["LA"]["email"])[0]
    tx = cl.get("/funding/me/transactions", headers=H(tokA)).json()
    items = tx.get("items", tx) if isinstance(tx, dict) else tx
    fees = [x for x in items if "FEE" in str(x).upper()]
    record("8.1 lender can see and pay its fee invoice", bool(fees), f"{len(fees)} fee rows in the lender's transactions")
    # reopen P2 (full of unpaid holds) by lapsing its holds, so the fee check is what answers
    p2w = st["p2"]["window"]
    L.psql(f"update tranches set hold_expires_at = now() - interval '1 minute' where window_id = '{p2w['id']}' "
           "and status in ('RESERVED', 'SIGNED')")
    cl.get(f"/funding/listings/{p2w['listing_id']}/window", headers=A)
    r = B.reserve(cl, tokA, p2w["id"], 10_000_000)
    u = cl.get(f"/admin/users/{st['LA']['id']}", headers=A).json()
    record("8.3 lender fee unpaid > 7 days -> new commitments blocked only (decision B13); no late fee", "PARTIAL",
           f"new commitment -> {r.status_code} {r.headers.get('X-Error-Code')} from the moment of issue (not after 7 "
           f"days); account status {u.get('status')} (no lock)")
record("8.2 SME gets its monthly withholding figures", False,
       "repayment.withholding_report exists but has no API route and no screen; daily tax rows are correct")
