"""M3 (pledges, Days 0-12) on P1 and P2, and the pledges that set up the Day-13 decision cases (P3-P5).

Money flow FINAL v1.0: a pledge is a press of Confirm. No OTP, nothing signed, no money moves. Several pledges per
lender; withdraw until the end of Day 12; the window reopens if a withdrawal takes the total below target.

Projects (each SME is its own business, one project per business):
  P1 100M  A 40 / B 30 / C 20 / D 10   handbook 10.8; Day 14 in s05-s06
  P2  45M  A, B, C, E 10 + F 5        last-ticket rule (3.6-3.7); then explicit Decline (4.3) in s10
  P3 100M  E 30 / F 30 / D 10 = 70%   silence >= 50% accepts (4.4); Day 14 late money + second no-show in s11
  P4 100M  E 20 / F 20 = 40%          manual accept below 50% (4.6) and the 12:00 signing cut-off (4.8)
  P5 100M  A 20 / B 20 = 40%          silence below 50% declines (4.5)
"""
import builders as B
from e2e_lib import H, c, err, load, login, record, save, show

M = 1_000_000
st = load()
p1w = st["p1"]["window"]
tok = {k: login(st[f"L{k}"]["email"])[0] for k in "ABCDEF"}
acct = {k: st[f"L{k}"]["account_id"] for k in "ABCDEF"}
pl = st["p1"].setdefault("pledges", {})


def window(cl, w):
    return cl.get(f"/funding/listings/{w['listing_id']}/window", headers=H(tok["A"])).json()


with c() as cl:
    # 3.1
    r = B.commit(cl, tok["A"], p1w["id"], 40 * M, acct["A"])
    j = r.json()
    pl["A"] = [j]
    w = window(cl, p1w)
    tickets = cl.get("/funding/me/tranches", headers=H(tok["A"])).json()
    record("3.1 A pledges 40M by pressing Confirm: pledge logged, no OTP, nothing signed, no money moves",
           r.status_code == 201 and j.get("status") == "PLEDGED" and not tickets and w["committed_total_vnd"] == 40 * M,
           f"{show(r, 160)}; A's tickets {len(tickets)}; committed {w['committed_total_vnd']:,}")
    # 3.2 + 3.8-like withdrawal on P1
    r2 = B.commit(cl, tok["A"], p1w["id"], 10 * M, acct["A"])
    total_after = r2.json().get("my_committed_total_vnd")
    wd = cl.post(f"/funding/commitments/{r2.json()['id']}/withdraw", headers=H(tok["A"]) | B.idem())
    w = window(cl, p1w)
    record("3.2 A pledges another 10M (several pledges allowed) and withdraws it before the end of Day 12",
           r2.status_code == 201 and total_after == 50 * M and wd.json().get("status") == "VOID"
           and w["committed_total_vnd"] == 40 * M,
           f"second pledge {r2.status_code} (A total {total_after:,}); withdraw -> {wd.json().get('status')}; "
           f"window committed {w['committed_total_vnd']:,}")
    r = B.commit(cl, tok["E"], p1w["id"], 9 * M, acct["E"])
    record("3.3 9M refused (minimum 10M)", r.status_code == 409 and err(r) == "BELOW_MIN_TICKET", f"{r.status_code} {err(r)}")
    r = B.commit(cl, tok["E"], p1w["id"], 70 * M, acct["E"])
    record("3.4 70M refused (above capacity left)", r.status_code == 409 and err(r) == "CAPACITY_EXCEEDED",
           f"{r.status_code} {err(r)}")
    for k, amt in (("B", 30), ("C", 20), ("D", 10)):
        pl[k] = [B.commit(cl, tok[k], p1w["id"], amt * M, acct[k]).json()]
    w = window(cl, p1w)
    rf = B.commit(cl, tok["F"], p1w["id"], 10 * M, acct["F"])
    record("3.5 target reached: pledges close early; Day 13 and Day 14 do not move",
           w["committed_total_vnd"] == 100 * M and rf.status_code == 409 and err(rf) == "WINDOW_FULL"
           and w["sme_deadline_at"] == p1w.get("sme_deadline_at", w["sme_deadline_at"]),
           f"committed {w['committed_total_vnd']:,}; F's 10M -> {rf.status_code} {err(rf)}; status {w['status']}; "
           f"SME deadline {w['sme_deadline_at']}; pay deadline {w['pay_deadline_at']}")
    # 3.10: a withdrawal reopens a full window until the end of Day 12
    c_pledge = pl["C"][0]
    wd = cl.post(f"/funding/commitments/{c_pledge['id']}/withdraw", headers=H(tok["C"]) | B.idem())
    rf = B.commit(cl, tok["F"], p1w["id"], 10 * M, acct["F"])
    ok_reopen = wd.status_code == 200 and rf.status_code == 201
    if rf.status_code == 201:
        cl.post(f"/funding/commitments/{rf.json()['id']}/withdraw", headers=H(tok["F"]) | B.idem())
    pl["C"] = [B.commit(cl, tok["C"], p1w["id"], 20 * M, acct["C"]).json()]
    w = window(cl, p1w)
    record("3.10 a withdrawal reopens a full window until the end of Day 12", ok_reopen and w["committed_total_vnd"] == 100 * M,
           f"C withdraws -> {wd.status_code}; F's 10M then -> {rf.status_code}; final list A40 B30 C20 D10 = "
           f"{w['committed_total_vnd']:,}")
    p1_window = w

# ---- P2: last-ticket rule
p2 = B.new_sme_project("sme2", "e2e.p2.owner@example.com", "Le Thi P Hai", "CONG TY TNHH P2 E2E", "0322222222",
                       45 * M, "0071000222333", monthly_revenue=120_000_000)
with c() as cl:
    for k in "ABCE":
        B.commit(cl, tok[k], p2["window"]["id"], 10 * M, acct[k])
    w = window(cl, p2["window"])
    record("3.6 four x 10M on P2 -> 5M left", w["committed_total_vnd"] == 40 * M, f"committed {w['committed_total_vnd']:,}")
    r10, r4, r5 = (B.commit(cl, tok["F"], p2["window"]["id"], x, acct["F"]) for x in (10 * M, 4 * M, 5 * M))
    w = window(cl, p2["window"])
    record("3.7 F: 10M and 4M refused, exactly 5M accepted, P2 full",
           r10.status_code == 409 and r4.status_code == 409 and r5.status_code == 201 and w["committed_total_vnd"] == 45 * M,
           f"10M -> {r10.status_code} {err(r10)}, 4M -> {r4.status_code} {err(r4)}, 5M -> {r5.status_code}; "
           f"committed {w['committed_total_vnd']:,}")

# ---- P3-P5: the Day-13 decision cases
plan = {
    "sme3": ("e2e.p3.owner@example.com", "Vo Van P Ba", "CONG TY TNHH P3 E2E", "0333333333", "0071000333444",
             (("E", 30), ("F", 30), ("D", 10))),
    "sme4": ("e2e.p4.owner@example.com", "Dang Thi P Bon", "CONG TY TNHH P4 E2E", "0344444444", "0071000444555",
             (("E", 20), ("F", 20))),
    "sme5": ("e2e.p5.owner@example.com", "Ly Van P Nam", "CONG TY TNHH P5 E2E", "0355555555", "0071000555666",
             (("A", 20), ("B", 20))),
}
for key, (email, owner, company, tax, account, pledges) in plan.items():
    p = B.new_sme_project(key, email, owner, company, tax, 100 * M, account)
    with c() as cl:
        for k, amt in pledges:
            r = B.commit(cl, tok[k], p["window"]["id"], amt * M, acct[k])
            assert r.status_code == 201, (key, k, show(r))
        w = window(cl, p["window"])
        print(f"   {key}: pledged {w['committed_total_vnd']:,} of {w['target_vnd']:,}")
st = load()
st["p1"]["pledges"] = pl
st["p1"]["window"] = p1_window
save(st)
