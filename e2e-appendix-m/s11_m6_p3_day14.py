"""P3 Day 14 (auto-accepted at 70% in s10; the SME and guarantor never signed):
  E 30M pays on time                      -> the loan goes ahead with E alone, below 50% (6.7, 6.8)
  F 30M pays with an expired QR code       -> late money: locked, a top-up pledge for the amount, never refunded (6.10)
  D 10M (unlocked in s06) misses again     -> account closed for good (6.5)
Then staff waive F's lock for a bank outage (6.6).
"""
import datetime as dt

import appdb
import builders as B
import e2e_lib as L
from e2e_lib import H, c, err, load, login, record

M = 1_000_000
st = load()
A = H(st["admin"]["token"])
p3 = st["sme3"]["project"]
win, listing, acct = p3["window"]["id"], p3["window"]["listing_id"], p3["acct"]
sme = login(st["sme3"]["email"])[0]
tok = {k: login(st[f"L{k}"]["email"])[0] for k in "DEF"}


def ticket(cl, k):
    return [t for t in cl.get("/funding/me/tranches", headers=H(tok[k])).json() if t["window_id"] == win][0]


def sign(cl, k, t):
    mark = L.log_mark()
    r = cl.post(f"/funding/tranches/{t['id']}/contract/code", headers=H(tok[k]))
    if r.status_code != 200:
        return r
    return cl.post(f"/funding/tranches/{t['id']}/contract",
                   json={"code": L.code_from_log(mark, t["reference_code"]), "click_to_sign": True}, headers=H(tok[k]))


with c() as cl:
    t = {k: ticket(cl, k) for k in "DEF"}
    copy = cl.get(f"/funding/tranches/{t['E']['id']}/contract", headers=H(tok["E"]))
    from io import BytesIO

    from pypdf import PdfReader
    text = "".join(p.extract_text() or "" for p in PdfReader(BytesIO(copy.content)).pages) if copy.status_code == 200 else ""
    record("4.4 the contract records silent acceptance ('Hoặc: không ký – ràng buộc theo Điều 4.2')",
           "không ký" in text.lower() or "khong ky" in text.lower(),
           f"E's copy {copy.status_code}, {len(copy.content)} bytes; 'không ký' in text: {'không ký' in text.lower()}")
    for k in "EF":
        r = sign(cl, k, t[k])
        print(f"   {k} signs: {r.status_code} {r.json().get('status') if r.status_code < 300 else err(r)}")
    pay = {k: cl.get(f"/funding/tranches/{t[k]['id']}/payment", headers=H(tok[k])).json() for k in "EF"}

# Day 14 afternoon: QRs moved 30 minutes back (issued on Day 14 morning), then 14:45 and 15:00 pass
L.psql(f"update payment_qrs q set issued_at = q.issued_at - interval '30 minutes', expires_at = q.expires_at - interval "
       f"'30 minutes' from tranches t where t.id = q.tranche_id and t.window_id = '{win}'")
L.psql(f"update tranches set reserved_at = reserved_at - interval '1 hour' where window_id = '{win}'")  # tickets existed
qr = dict(line.split("|") for line in L.psql(
    f"select t.investor_id || '|' || to_char(q.issued_at at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"+00:00\"') "
    f"|| ';' || to_char(q.expires_at at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"+00:00\"') || ';' || q.code "
    f"from payment_qrs q "
    f"join tranches t on t.id = q.tranche_id where t.window_id = '{win}'").splitlines())


def parse(k):
    issued, expires, code = qr[st[f"L{k}"]["id"]].split(";")
    return dt.datetime.fromisoformat(issued), dt.datetime.fromisoformat(expires), code


with c() as cl:
    for k, late in (("E", False), ("F", True)):
        issued, expires, code = parse(k)
        when = expires + dt.timedelta(minutes=3) if late else issued + dt.timedelta(minutes=2)
        rc = cl.post(f"/funding/tranches/{t[k]['id']}/receipt",
                     files={"file": (f"p3{k}.png", B.png(f"P3 {k} 30,000,000 {code}"), "image/png")},
                     headers=H(tok[k]) | B.idem()).json()
        B.staff_fields(cl, A, rc["transfer_id"], amount=30 * M, when=when, payer=st["L" + k], payee_acct=acct, memo=code,
                       txn=f"FTP3{k}{when:%H%M%S}")
    tE, tF = ticket(cl, "E"), ticket(cl, "F")
L.psql(f"update funding_windows set pay_deadline_at = now() - interval '2 minutes', no_show_at = now() - interval "
       f"'1 minute' where id = '{win}'")
with c() as cl:
    record("6.10 a transfer with an expired QR code is late money (EXPIRED_CODE), held for review, not part of the loan",
           tF["status"] == "UNDER_REVIEW" and tF.get("payment_timing") == "LATE" and tE.get("payment_timing") == "ON_TIME",
           f"E {tE['status']} {tE.get('payment_timing')}; F {tF['status']} {tF.get('payment_timing')}")
appdb.run_job("day14-cutoff")
L.psql(f"update funding_windows set receipt_deadline_at = now() - interval '1 minute' where id = '{win}'")
cut = appdb.run_job("day14-cutoff")
with c() as cl:
    sF = cl.get("/funding/me/standing", headers=H(tok["F"])).json()
    sD = cl.get("/funding/me/standing", headers=H(tok["D"])).json()
    pledges = sF.get("top_up_pledges") or []
    record("6.10 late money: F locked; a top-up pledge for the 30M is recorded; nothing is refunded",
           sF["status"] == "LOCKED" and any(p["amount_vnd"] == 30 * M for p in pledges),
           f"job {cut}; F {sF['status']}; pledges {[(p['amount_vnd'], p['status']) for p in pledges]}")
    record("6.10 after unlocking, F signs the top-up on any day of the 7-day reopening; the SME confirms", "NOT BUILT",
           "the pledge is recorded but there is no route to sign it (POST /funding/top-ups/{id}/contract is not built; "
           "standing.py: Phase 3, gap B6)")
    r = cl.post("/funding/me/standing/unlock", json={"acknowledgement": "x"}, headers=H(tok["D"]))
    rp = B.commit(cl, tok["D"], st["sme4"]["project"]["window"]["id"], 10 * M, st["LD"]["account_id"])
    record("6.5 a second no-show closes the account for good", sD["status"] == "CLOSED" and err(r) == "LENDER_CLOSED",
           f"D {sD['status']} (misses {sD['no_show_count']}); unlock -> {r.status_code} {err(r)}; pledge -> {rp.status_code} {err(rp)}")

    # 6.7 / 6.8: the loan goes ahead with E alone (30%), though the SME and guarantor never signed
    cl.post(f"/funding/tranches/{t['E']['id']}/sme-receipt",
            files={"file": ("p3smeE.png", B.png("SME: 30,000,000 received"), "image/png")}, headers=H(sme) | B.idem())
    adv = cl.post(f"/admin/funding/rounds/{win}/advance", headers=A).json()
    w = cl.get(f"/funding/listings/{listing}/window", headers=A).json()
    if w["status"] == "CLOSING":
        L.psql(f"update funding_windows set close_wait_until = now() - interval '1 minute' where id = '{win}'")
        cl.post(f"/admin/funding/rounds/{win}/advance", headers=A)
        w = cl.get(f"/funding/listings/{listing}/window", headers=A).json()
    record("6.7 / 6.8 below 50% after a no-show and late money, the loan still goes ahead with whoever paid; "
           "the unsigned guarantor still guarantees it",
           w["status"] == "REPAYING" and w["confirmed_vnd"] == 30 * M,
           f"P3 {w['status']} confirmed {w['confirmed_vnd']:,}; E {ticket(cl, 'E')['status']}")

    # 6.6 staff waiver
    r = cl.post(f"/admin/funding/lenders/{st['LF']['id']}/waive-lock",
                json={"window_id": win, "reason": "BANK_OUTAGE", "note": "E2E: bank outage proven by statement"}, headers=A)
    sF = cl.get("/funding/me/standing", headers=H(tok["F"])).json()
    record("6.6 proof of a bank outage: staff lift the lock and the miss is not counted",
           r.status_code == 200 and sF["status"] == "GOOD" and sF["no_show_count"] == 0,
           f"waiver {r.status_code} {err(r)}; F {sF['status']} misses {sF['no_show_count']}; pledges "
           f"{[(p['amount_vnd'], p['status']) for p in sF.get('top_up_pledges') or []]}")
