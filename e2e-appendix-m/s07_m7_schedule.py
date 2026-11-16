"""M7.1, 7.2, 7.5, 7.7 and M6.3: compare the live P1 schedules with handbook 10.8."""
import collections
import datetime as dt
import json

import yaml

import e2e_lib as L
from e2e_lib import H, c, load, login, record, save

HB = {  # handbook 10.8: P, daily A, last instalment, interest/day, tax/day, transfer/day
    "A": (40_000_000, 976_190, 976_210, 23_809, 1_190, 975_000),
    "B": (30_000_000, 732_142, 732_178, 17_857, 893, 731_249),
    "C": (20_000_000, 488_095, 488_105, 11_904, 595, 487_500),
    "E": (10_000_000, 244_047, 244_073, 5_952, 298, 243_749),  # E holds D's 10M after the no-show
}
st = load()
tr = st["p1"]["tranches"]
holidays = set()
with open(f"{L.BACKEND}/app/repayment_schedule/data/vn_public_holidays.yaml", encoding="utf-8") as f:
    for year in (yaml.safe_load(f).get("years") or {}).values():
        for item in year.get("holidays") or []:
            holidays.add(str(item["date"]))
ends = {}
with c() as cl:
    for k, (P, a, last, i1, t1, n1) in HB.items():
        rows = cl.get(f"/repayment/tranches/{tr[k]['id']}/schedule", headers=H(login(st['L' + k]['email'])[0])).json()
        with open(f"{L.OUT}/schedule_{k}.json", "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=1, ensure_ascii=False)
        inst = [r for r in rows if r["kind"] == "INSTALMENT"]
        stub = [r for r in rows if r["kind"] != "INSTALMENT"]
        g = [r["gross_vnd"] for r in inst]
        ends[k] = inst[-1]["due_date"]
        label = "10M lender (D's place)" if k == "E" else k
        record(f"7.1 day-1 transfer {label} = {n1:,}", inst[0]["net_vnd"] == n1 and not stub,
               f"system {inst[0]['net_vnd']:,} (interest {inst[0]['interest_vnd']:,}, tax {inst[0]['tax_withheld_vnd']:,})"
               + (f"; extra first-period STUB row(s): {[(s['due_date'], s['net_vnd']) for s in stub]}" if stub else ""))
        record(f"7.x {label} daily instalment {a:,} on days 1-{len(inst) - 1}", set(g[:-1]) == {a}, f"system {sorted(set(g[:-1]))}")
        record(f"7.7 last instalment {label} = {last:,} before tax", g[-1] == last, f"system {g[-1]:,} on {inst[-1]['due_date']}")
        print(f"   {k}: {len(inst)} instalments {inst[0]['due_date']}..{inst[-1]['due_date']} interest "
              f"{collections.Counter(r['interest_vnd'] for r in inst[:-1]).most_common(2)} total tax "
              f"{sum(r['tax_withheld_vnd'] for r in inst):,}")
        if k == "A":
            d1 = inst[0]
            record("7.2 split for A: interest 23,809, tax 1,190, principal 952,381",
                   (d1["interest_vnd"], d1["tax_withheld_vnd"], d1["principal_vnd"]) == (23_809, 1_190, 952_381),
                   f"system {d1['interest_vnd']:,} / {d1['tax_withheld_vnd']:,} / {d1['principal_vnd']:,}")
            dates = [r["due_date"] for r in inst]
            weekend = [d for d in dates if dt.date.fromisoformat(d).weekday() >= 5]
            skipped = sorted(h for h in holidays if dates[0] <= h <= dates[-1] and dt.date.fromisoformat(h).weekday() < 5)
            record("7.5 public holiday: no instalment that day", not weekend and not set(skipped) & set(dates),
                   f"{len(dates)} dates {dates[0]}..{dates[-1]}; weekday holidays in range skipped: {skipped}")
record("6.3 top-up ends on the same day as A-C", len(set(ends.values())) == 1, f"end dates {ends}")
st["p1"]["schedule_end"] = ends
save(st)
