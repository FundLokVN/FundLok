"""M7.1, 7.2, 7.5, 7.7: the live P1 schedules (A, B, C after D's no-show) against the handbook 10.7 formulas.

Handbook 10.8 prints the figures for r = 15% exactly. Lenders now earn the project's full graded rate, which the
engine does not round to 15%, so the figures are recomputed here with P1's actual rate:
  I = P x r x M / 12 (nearest dong), T = P + I, A = floor(T / N0), last = T - A x (N0 - 1),
  interest part = floor(A x I / T), tax = 5% of it (nearest dong), transfer = A - tax.
"""
import datetime as dt
import json
from decimal import ROUND_HALF_UP, Decimal

import yaml

import e2e_lib as L
from e2e_lib import H, c, load, login, record, save

st = load()
tr = st["p1"]["tranches"]
P = {"A": 40_000_000, "B": 30_000_000, "C": 20_000_000}
holidays = set()
with open(f"{L.BACKEND}/app/repayment_schedule/data/vn_public_holidays.yaml", encoding="utf-8") as f:
    for year in (yaml.safe_load(f).get("years") or {}).values():
        for item in year.get("holidays") or []:
            holidays.add(str(item["date"]))
with c() as cl:
    w = cl.get(f"/funding/listings/{st['p1']['window']['listing_id']}/window", headers=H(st["admin"]["token"])).json()
r_pct = Decimal(str(w["annual_rate_pct"]))
N0 = 42
starts, ends = {}, {}
with c() as cl:
    for k, p in P.items():
        if k not in tr or tr[k].get("status") != "ACTIVE":
            record(f"7.1 {k}'s schedule", False, f"{k} has no active loan ({tr.get(k, {}).get('status')})")
            continue
        rows = cl.get(f"/repayment/tranches/{tr[k]['id']}/schedule", headers=H(login(st['L' + k]['email'])[0])).json()
        with open(f"{L.OUT}/schedule_{k}.json", "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=1, ensure_ascii=False)
        inst = [r for r in rows if r["kind"] == "INSTALMENT"]
        stub = [r for r in rows if r["kind"] != "INSTALMENT"]
        interest = int((Decimal(p) * r_pct / 100 * 2 / 12).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        total = p + interest
        daily = total // N0
        last = total - daily * (N0 - 1)
        i_part = daily * interest // total
        tax = int((Decimal(i_part) * Decimal("0.05")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        g = [r["gross_vnd"] for r in inst]
        starts[k], ends[k] = inst[0]["due_date"], inst[-1]["due_date"]
        record(f"7.1 {k}: {N0} instalments of {daily:,}, day-1 transfer {daily - tax:,} (r = {r_pct}%)",
               len(inst) == N0 and set(g[:-1]) == {daily} and inst[0]["net_vnd"] == daily - tax and not stub,
               f"system: {len(inst)} instalments, daily {sorted(set(g[:-1]))}, day-1 net {inst[0]['net_vnd']:,}"
               + (f", extra rows {[(s['kind'], s['due_date']) for s in stub]}" if stub else ""))
        record(f"7.7 {k}: last instalment {last:,} before tax", g[-1] == last, f"system {g[-1]:,} on {inst[-1]['due_date']}")
        if k == "A":
            d1 = inst[0]
            record(f"7.2 A's split: interest {i_part:,}, tax {tax:,}, principal {daily - i_part:,}",
                   (d1["interest_vnd"], d1["tax_withheld_vnd"], d1["principal_vnd"]) == (i_part, tax, daily - i_part),
                   f"system {d1['interest_vnd']:,} / {d1['tax_withheld_vnd']:,} / {d1['principal_vnd']:,}")
            dates = [r["due_date"] for r in inst]
            weekend = [d for d in dates if dt.date.fromisoformat(d).weekday() >= 5]
            hol = sorted(h for h in holidays if dates[0] <= h <= dates[-1] and dt.date.fromisoformat(h).weekday() < 5)
            record("7.5 Monday to Friday, public holidays included as normal instalment days",
                   not weekend and set(hol) <= set(dates), f"{dates[0]}..{dates[-1]}; weekday holidays in range {hol}")
record("6.1 / 10.1 one shared schedule: same first and last day for every lender",
       len(set(starts.values())) == 1 and len(set(ends.values())) == 1, f"starts {starts}; ends {ends}")
record("7.x handbook 10.8 figures (r = 15%)", "N/A",
       f"P1 graded at {r_pct}% and lenders earn the full graded rate (decision 17 Nov), so 10.8's 15% figures are "
       "replaced by the formula check above")
st["p1"]["schedule_end"] = ends
save(st)
