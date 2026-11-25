"""M8 and M7.3/M10.1 at month end: run the repayment and invoice timers
with a clock just after month end, then two days later.

Checks: a day the SME never paid becomes LATE (never presumed paid); the lender fee counts only money received,
divided by the balance at the start of the month or on the Disbursement Date if later, rounded to the nearest dong.
"""
import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

import appdb
import e2e_lib as L
from e2e_lib import H, c, load, login, record

st = load()
tr = st["p1"]["tranches"]
today = L.ict_today()
next_month = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
for result in appdb.run_jobs([dt.datetime.combine(next_month, dt.time(0, 20), L.ICT),
                              dt.datetime.combine(next_month + dt.timedelta(days=2), dt.time(0, 20), L.ICT)]):
    print(result)

rows = L.psql(f"select due_date || '|' || status || '|' || (marked_paid_at is not null) from repayment_days "
              f"where tranche_id = '{tr['B']['id']}' and due_date < '{next_month}' order by due_date").splitlines()
unpaid = [r for r in rows if r.endswith("|false")]
record("7.3 / 10.1 a past day with no SME proof is LATE, never presumed paid",
       bool(unpaid) and all("|LATE|" in r for r in unpaid),
       f"B: {len(rows)} days this month; unpaid ones: {sorted(set(r.split('|')[1] for r in unpaid))}")

acc = L.psql(f"select received_gross_vnd || '|' || fee_vnd from investor_fee_accruals where tranche_id = '{tr['A']['id']}' "
             f"order by created_at desc limit 1")
inv = L.psql(f"select fee_vnd || '|' || vat_vnd || '|' || total_vnd || '|' || due_at from fee_invoices where kind = "
             f"'INVESTOR_SERVICE' and payer_user_id = '{st['LA']['id']}' order by issued_at desc limit 1")
if acc:
    received, fee = map(int, acc.split("|"))
    import json
    with open(f"{L.OUT}/schedule_A.json", encoding="utf-8") as f:
        total = sum(r["gross_vnd"] for r in json.load(f) if r["kind"] == "INSTALMENT")  # T: owed on the Disbursement Date
    F = int((Decimal(40_000_000) * Decimal("0.025") * 2 / 12).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    expected = int((Decimal(F) * received / total).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if total else None
    record("8.1 A's month-1 fee = F x instalments received (before tax) / balance on the Disbursement Date, nearest dong",
           expected is not None and fee == expected,
           f"received {received:,}; fee {fee:,}; formula {expected} (F {F:,}, balance {total:,}); invoice {inv}")
else:
    record("8.1 monthly lender fee", False, "no accrual produced")

with c() as cl:
    tokA = login(st["LA"]["email"])[0]
    tx = cl.get("/funding/me/transactions", headers=H(tokA)).json()
    items = tx.get("items", tx) if isinstance(tx, dict) else tx
    fees = [x for x in items if "FEE" in str(x).upper()]
    record("8.1 the lender can see its fee invoice", bool(fees), f"{len(fees)} fee rows in the lender's transactions")
record("8.3 a lender fee unpaid for 7 days blocks new pledges only", "N/A",
       "no round is open for pledges at this point of the run; covered by the backend's INVESTOR_FEE_UNPAID tests")
record("8.2 the SME gets its monthly withholding figures", "N/A", "screen check (AM-P16); not driven by this run")
