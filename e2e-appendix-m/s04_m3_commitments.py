"""M3.1-3.5 on P1 and M5.1 (payment instructions / VietQR)."""
import builders as B
from app.direct_transfer.vietqr import parse_tlv, verify_crc
from e2e_lib import H, c, load, login, record, save, show

st = load()
win, listing = st["p1"]["window"]["id"], st["p1"]["window"]["listing_id"]
tok = {k: login(st[f"L{k}"]["email"])[0] for k in "ABCDE"}
tr = st["p1"].setdefault("tranches", {})


def window(cl):
    return cl.get(f"/funding/listings/{listing}/window", headers=H(tok["A"])).json()


with c() as cl:
    r = B.reserve(cl, tok["A"], win, 40_000_000)
    tr["A"] = r.json()
    w = window(cl)
    record("3.1 A commits 40M: signed, no money moves, capacity left 60M",
           "PARTIAL" if r.status_code == 201 and w["capacity_vnd"] == 60_000_000 else False,
           f"status={tr['A'].get('status')} capacity={w['capacity_vnd']}; but A must pay by hold_expires_at="
           f"{tr['A'].get('hold_expires_at')} (2 h after committing)")
    pay = cl.get(f"/funding/tranches/{tr['A']['id']}/payment", headers=H(tok["A"])).json()
    payload = pay.get("vietqr_payload") or ""
    ok = (pay.get("amount_vnd") == 40_000_000 and pay.get("account_number") == "0071000111222"
          and bool(pay.get("reference_code")) and bool(payload) and verify_crc(payload))
    record("5.1 VietQR: SME account, exact amount, unique reference", ok,
           f"acct={pay.get('account_number')} amount={pay.get('amount_vnd')} ref={pay.get('reference_code')} "
           f"tag54={parse_tlv(payload).get('54') if payload else None}")
    record("5.1 instructions issued on Day 14", False, "issued immediately at commit")

    r = B.reserve(cl, tok["A"], win, 10_000_000)
    record("3.2 second commitment by A refused", r.status_code == 409, f"{show(r, 120)} {r.headers.get('X-Error-Code')}")
    r = B.reserve(cl, tok["E"], win, 9_000_000)
    record("3.3 9M refused (minimum 10M)", r.status_code == 409, f"{show(r, 120)} {r.headers.get('X-Error-Code')}")
    r = B.reserve(cl, tok["E"], win, 70_000_000)
    record("3.4 70M refused (above capacity left)", r.status_code == 409, f"{show(r, 120)} {r.headers.get('X-Error-Code')}")
    for k, amt in (("B", 30_000_000), ("C", 20_000_000), ("D", 10_000_000)):
        r = B.reserve(cl, tok[k], win, amt)
        tr[k] = r.json()
    w = window(cl)
    record("3.5 capacity 0, commitments close early", w["capacity_vnd"] == 0 and w["status"] == "FULL",
           f"status={w['status']} capacity={w['capacity_vnd']}")
    record("3.5 disbursement still on Day 14", "PARTIAL",
           f"window_end={w['window_end']}; there is no Day-14 disbursement, the round closes once every ticket is confirmed")
save(st)
