"""P1 Day 13 (M4.1, 4.2, 4.8, 3.9) and Day 14 (M5, M6.1, M6.9) on the commit-then-accept flow.

Time travel: the window's own deadlines are moved relative to the real clock (psql), and the Cloud Scheduler jobs
(funding-advance, acceptance-reminders, day14-transfer-reminders, day14-cutoff) are run in-process with the real
clock (appdb.run_job), because their routes need Google OIDC.

Cast: A 40M and B 30M pay on time; C 20M pays on time but uploads after 15:00 (6.9); D 10M never signs (6.1).
"""
import datetime as dt
import re

import appdb
import builders as B
import e2e_lib as L
from app.direct_transfer.vietqr import parse_tlv, verify_crc
from e2e_lib import H, c, err, load, login, record, save, show

M = 1_000_000
st = load()
A = H(st["admin"]["token"])
p1 = st["p1"]
win, listing = p1["window"]["id"], p1["window"]["listing_id"]
sme_key = p1["sme"]
sme = login(st[sme_key]["email"])[0]
tok = {k: login(st[f"L{k}"]["email"])[0] for k in "ABCD"}
P1_ACCT, P1_TAX = "0071000111222", "0311111112"


def window(cl):
    return cl.get(f"/funding/listings/{listing}/window", headers=A).json()


def contracts(cl):
    return cl.get(f"/funding/windows/{win}/loan-contracts", headers=H(sme)).json()


def ticket(cl, k):
    return [t for t in cl.get("/funding/me/tranches", headers=H(tok[k])).json() if t["window_id"] == win][0]


def sign_day14(cl, k, t):
    mark = L.log_mark()
    r = cl.post(f"/funding/tranches/{t['id']}/contract/code", headers=H(tok[k]))
    if r.status_code != 200:
        return r
    code = L.code_from_log(mark, t["reference_code"])
    return cl.post(f"/funding/tranches/{t['id']}/contract", json={"code": code, "click_to_sign": True}, headers=H(tok[k]))


# ------------------------------------------------------------------ Day 13: the commit phase ends
L.psql(f"update funding_windows set commit_closes_at = now() - interval '1 minute', "
       f"sme_deadline_at = now() + interval '2 hours', pay_deadline_at = now() + interval '3 hours', "
       f"no_show_at = now() + interval '3 hours 15 minutes', receipt_deadline_at = now() + interval '3 hours 45 minutes' "
       f"where id = '{win}'")
job = appdb.run_job("funding-advance")
with c() as cl:
    w = window(cl)
    lc = contracts(cl)
    rows = lc.get("contracts", [])
    amounts = sorted(r["amount_vnd"] for r in rows)
    record("4.8 at the end of Day 12 every loan contract is generated from the final pledge list, one per lender",
           w["status"] == "AWAITING_SME" and amounts == [10 * M, 20 * M, 30 * M, 40 * M]
           and all(r["status"] == "GENERATED" for r in rows),
           f"job {job}; window {w['status']}; contracts {[(r['contract_no'], r['amount_vnd'], r['status']) for r in rows]}")
    record("4.8 noon signing cut-off shown only below 50% (P1 is at 100%)", lc.get("signing_cutoff_at") is None,
           f"signing_cutoff_at={lc.get('signing_cutoff_at')}")
    a_row = [r for r in rows if r["amount_vnd"] == 40 * M][0]
    pdf = cl.get(f"/funding/windows/{win}/loan-contracts/{a_row['id']}/document", headers=H(sme))
    from io import BytesIO

    from pypdf import PdfReader
    text = "".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf.content)).pages) if pdf.status_code == 200 else ""
    record("4.8 each contract is the lender's own copy (number, amount, Disbursement Date)",
           a_row["contract_no"] in text and "40.000.000" in text,
           f"PDF {pdf.status_code}, {len(pdf.content)} bytes; number in text {a_row['contract_no'] in text}; "
           f"'40.000.000' in text {'40.000.000' in text}")
    # 3.9 / 3.8: the commit phase is over
    r = B.commit(cl, tok["A"], win, 10 * M, st["LA"]["account_id"])
    record("3.9 a new pledge after Day 12 is refused", r.status_code == 409, f"{r.status_code} {err(r)}")
    pledge_c = p1["pledges"]["C"][0]["id"]
    r = cl.post(f"/funding/commitments/{pledge_c}/withdraw", headers=H(tok["C"]) | B.idem())
    record("3.8 withdrawing after Day 12 is refused", r.status_code == 409, f"{r.status_code} {err(r)}")

# 4.1 reminders: the job sends on the 3rd and the 1st ICT day before the deadline
deadline = dt.datetime.fromisoformat(w["sme_deadline_at"].replace("Z", "+00:00"))
t0 = L.utcnow_iso()
rem = appdb.run_job("acceptance-reminders", now=deadline - dt.timedelta(days=1))
logged = L.psql(f"select kind || ':' || channel || ':' || status from acceptance_reminders where window_id = '{win}' "
                f"order by channel").split()
mail = L.latest_mail(st[sme_key]["email"], since=t0, wait=8)
record("4.1 reminder 1 day before: in-app, Zalo and email (no SMS), each logged with channel and status",
       any(x.endswith("IN_APP:SENT") for x in logged) and any(x.endswith("EMAIL:SENT") for x in logged),
       f"job {rem}; logged {logged}; email to SME: {mail and mail.get('Subject')}; Zalo is SKIPPED until "
       "ZALO_ZBS_ACCEPTANCE_TEMPLATE_ID is set")

# ------------------------------------------------------------------ Day 13: accepting means signing
with c() as cl:
    r = cl.post(f"/funding/windows/{win}/decision", json={"decision": "ACCEPT"}, headers=H(sme) | B.idem())
    record("4.2 Accept before signing the contracts is refused", r.status_code == 409 and err(r) == "CONTRACTS_NOT_SIGNED",
           f"{r.status_code} {err(r)}")
    blank = cl.get(f"/funding/windows/{win}/loan-contracts/{a_row['id']}/document", headers=H(sme)).content
    r = cl.post(f"/funding/windows/{win}/loan-contracts/{a_row['id']}/signed",
                files={"file": ("unsigned.pdf", blank, "application/pdf")}, headers=H(sme))
    record("4.2 an unsigned copy is refused", r.status_code == 422, f"{r.status_code} {err(r)}")
signed = appdb.sme_sign_window_contracts(win, st[sme_key]["id"], P1_TAX)
B.set_phone(st[sme_key]["id"], "+84903111001")
with c() as cl:
    mark = L.log_mark()
    r = cl.post(f"/funding/windows/{win}/loan-contracts/guarantor-code", headers=H(sme))
    g_ok = False
    if r.status_code == 200:
        code = L.code_from_log(mark, "Guarantor code")
        g = cl.post(f"/funding/windows/{win}/loan-contracts/guarantor-signature", json={"code": code}, headers=H(sme))
        g_ok = g.status_code == 200 and bool(g.json().get("guarantor_signed_at"))
    lc = contracts(cl)
    record("4.2 SME signs every contract (enterprise signature, NEAC-checked); guarantor signs all by one Zalo code",
           len(signed) == 4 and lc.get("all_signed") and g_ok,
           f"SME signed {signed} (NEAC faked locally); guarantor code {r.status_code} {err(r)}, signed {g_ok}")
    r = cl.post(f"/funding/windows/{win}/decision", json={"decision": "ACCEPT"}, headers=H(sme) | B.idem())
    w = window(cl)
    tickets = {k: ticket(cl, k) for k in "ABCD"}
    record("4.2 Accept: pledges final, one ticket per lender for its total, P1 goes to Day 14",
           r.status_code == 200 and w["status"] == "AWAITING_PAYMENT" and w["sme_decision"] == "ACCEPTED"
           and {k: t["amount_vnd"] for k, t in tickets.items()} == {"A": 40 * M, "B": 30 * M, "C": 20 * M, "D": 10 * M},
           f"decision {r.status_code}; window {w['status']} / {w['sme_decision']}; "
           f"tickets {[(k, t['amount_vnd'], t['status']) for k, t in tickets.items()]}")

# ------------------------------------------------------------------ Day 14
t1 = L.utcnow_iso()
rem = appdb.run_job("day14-transfer-reminders", kind="DAY14_MORNING")
logged = L.psql(f"select r.kind || ':' || r.channel || ':' || r.status from transfer_reminders r join tranches t on "
                f"t.id = r.tranche_id where t.window_id = '{win}' order by 1").split()
mail = L.latest_mail(st["LA"]["email"], since=t1, wait=8)
record("5.x Day-14 reminder 'sign and pay by 14:45' (in-app, email, Zalo), logged",
       len([x for x in logged if x.endswith("SENT")]) >= 4,
       f"job {rem}; {len(logged)} rows {sorted(set(x.split(':', 1)[1] for x in logged))}; email: {mail and mail.get('Subject')}")
with c() as cl:
    r = cl.get(f"/funding/tranches/{tickets['A']['id']}/payment", headers=H(tok["A"]))
    record("5.1 no QR before the lender signs its contract",
           r.status_code == 409 and err(r) in ("CONTRACT_REQUIRED", "INVESTOR_AGREEMENT_REQUIRED"),
           f"{r.status_code} {err(r)} (a first-time lender is told to sign the agreement; it is signed with the contract)")
    # A: refusals first, then the real signature
    t = tickets["A"]
    mark = L.log_mark()
    cl.post(f"/funding/tranches/{t['id']}/contract/code", headers=H(tok["A"]))
    code = L.code_from_log(mark, t["reference_code"])
    r1 = cl.post(f"/funding/tranches/{t['id']}/contract", json={"code": code, "click_to_sign": False}, headers=H(tok["A"]))
    wrong = "000000" if code != "000000" else "111111"
    r2 = cl.post(f"/funding/tranches/{t['id']}/contract", json={"code": wrong, "click_to_sign": True}, headers=H(tok["A"]))
    r3 = cl.post(f"/funding/tranches/{t['id']}/contract", json={"code": code, "click_to_sign": True}, headers=H(tok["A"]))
    agreement = cl.get("/funding/me/investor-agreement", headers=H(tok["A"]))
    record("5.2 A signs one loan contract for its total with one Zalo code and a click (click and code both required)",
           r1.status_code == 400 and r2.status_code == 400 and r3.status_code == 201 and r3.json().get("status") == "SIGNED",
           f"no click -> {r1.status_code} {err(r1)}; wrong code -> {r2.status_code} {err(r2)}; right -> {r3.status_code} "
           f"{r3.json().get('status')}")
    record("5.2 the same code signs the Investor Service Agreement on the lender's first Day 14",
           agreement.status_code == 200 and bool((agreement.json() or {}).get("signed_at")),
           f"GET /funding/me/investor-agreement -> {show(agreement, 200)}")
    for k in "BC":
        r = sign_day14(cl, k, tickets[k])
        print(f"   {k} signs: {r.status_code} {r.json().get('status') if r.status_code < 300 else err(r)}")
    pays = {k: cl.get(f"/funding/tranches/{tickets[k]['id']}/payment", headers=H(tok[k])).json() for k in "ABC"}
    pa = pays["A"]
    issued = dt.datetime.fromisoformat(pa["qr_issued_at"])
    expires = dt.datetime.fromisoformat(pa["qr_expires_at"])
    payload = pa.get("vietqr_payload") or ""
    record("5.1 VietQR: SME account, exact amount, its own code (reference + 4 digits), valid 15 minutes",
           pa["account_number"] == P1_ACCT and pa["amount_vnd"] == 40 * M
           and re.fullmatch(re.escape(pa["reference_code"]) + r"\d{4}", pa["qr_code"] or "") is not None
           and abs((expires - issued).total_seconds() - 900) < 2 and verify_crc(payload) and pa["qr_code"] in payload,
           f"acct {pa['account_number']} amount {pa['amount_vnd']:,} code {pa['qr_code']} (ref {pa['reference_code']}) "
           f"valid {issued:%H:%M:%S}-{expires:%H:%M:%S}; pay deadline {pa.get('pay_deadline_at')}; tag54 "
           f"{parse_tlv(payload).get('54')}")
    r = cl.post(f"/funding/tranches/{tickets['A']['id']}/qr", headers=H(tok["A"]))
    L.psql(f"update payment_qrs set issued_at = issued_at - interval '2 minutes' where tranche_id = '{tickets['A']['id']}'")
    r2 = cl.post(f"/funding/tranches/{tickets['A']['id']}/qr", headers=H(tok["A"]))
    sup = L.psql(f"select count(*) filter (where superseded_at is not null) || '/' || count(*) from payment_qrs "
                 f"where tranche_id = '{tickets['A']['id']}'")
    record("5.1b a new QR has a new code and replaces the old one (not more than once a minute)",
           r.status_code == 429 and r2.status_code == 201 and r2.json()["qr_code"] != pa["qr_code"] and sup.startswith("1/"),
           f"at once -> {r.status_code} {err(r)}; after a minute -> {r2.status_code} new code {r2.json().get('qr_code')}; "
           f"superseded/total {sup}")
    pays["A"] = r2.json()

    # A and B pay inside their QR's 15 minutes; staff key in the receipts (OCR is off locally)
    rc = {}
    shots = {}
    for k in "AB":
        shots[k] = B.png(f"Chuyen tien {tickets[k]['amount_vnd']:,} VND {st['L' + k]['name']} noi dung {pays[k]['qr_code']}")
        rc[k] = cl.post(f"/funding/tranches/{tickets[k]['id']}/receipt",
                        files={"file": (f"{k}.png", shots[k], "image/png")}, headers=H(tok[k]) | B.idem()).json()
        inside = dt.datetime.fromisoformat(pays[k]["qr_issued_at"]) + dt.timedelta(minutes=1)
        B.staff_fields(cl, A, rc[k]["transfer_id"], amount=tickets[k]["amount_vnd"], when=inside, payer=st["L" + k],
                       payee_acct=P1_ACCT, memo=pays[k]["qr_code"], txn=f"FT14{k}{inside:%H%M%S}")
    after = {k: ticket(cl, k) for k in "AB"}
    record("5.2 on time = the receipt shows the current code and a time inside its 15 minutes -> Paid",
           all(after[k]["status"] == "ATTESTED" and after[k].get("payment_timing") == "ON_TIME" for k in "AB"),
           f"{[(k, after[k]['status'], after[k].get('payment_timing')) for k in 'AB']}")
    dup = cl.post(f"/funding/tranches/{tickets['C']['id']}/receipt", files={"file": ("dup.png", shots["A"], "image/png")},
                  headers=H(tok["C"]) | B.idem())
    record("5.5 a duplicate screenshot is refused", dup.status_code == 409, f"{dup.status_code} {err(dup)}")

# ------------------------------------------------------------------ 14:45 and 15:00
# Fast-forward past 14:45: move the deadlines AND every QR of the round 30 minutes back, so the QRs were issued before
# 14:45 (as they would be on a real Day 14) and C's transfer time below falls inside its QR and before 14:45.
L.psql(f"update payment_qrs q set issued_at = q.issued_at - interval '30 minutes', expires_at = q.expires_at - interval "
       f"'30 minutes' from tranches t where t.id = q.tranche_id and t.window_id = '{win}'")
L.psql(f"update tranches set reserved_at = reserved_at - interval '1 hour' where window_id = '{win}'")  # tickets existed
L.psql(f"update funding_windows set pay_deadline_at = now() - interval '2 minutes', "
       f"no_show_at = now() - interval '1 minute' where id = '{win}'")
pays["C"]["qr_issued_at"] = (dt.datetime.fromisoformat(pays["C"]["qr_issued_at"]) - dt.timedelta(minutes=30)).isoformat()
with c() as cl:
    r = cl.post(f"/funding/tranches/{tickets['D']['id']}/contract/code", headers=H(tok["D"]))
    rq = cl.get(f"/funding/tranches/{tickets['C']['id']}/payment", headers=H(tok["C"]))
    record("5.1a after 14:45: no signing and no QR", r.status_code == 409 and rq.status_code == 409,
           f"D asks for a code -> {r.status_code} {err(r)}; C asks for the QR -> {rq.status_code} {err(rq)}")
cut = appdb.run_job("day14-cutoff")
with c() as cl:
    sd = cl.get("/funding/me/standing", headers=H(tok["D"])).json()
    sc = cl.get("/funding/me/standing", headers=H(tok["C"])).json()
    record("6.1 15:00: a lender with no transfer is locked", sd["status"] == "LOCKED" and sc["status"] == "LOCKED",
           f"job {cut}; D {sd['status']}, C (paid, receipt not yet uploaded) {sc['status']}")
    # 6.9: C paid inside its window and uploads the receipt between 15:00 and 15:30
    rcC = cl.post(f"/funding/tranches/{tickets['C']['id']}/receipt",
                  files={"file": ("C.png", B.png(f"C 20,000,000 {pays['C']['qr_code']}"), "image/png")},
                  headers=H(tok["C"]) | B.idem())
    inside = dt.datetime.fromisoformat(pays["C"]["qr_issued_at"]) + dt.timedelta(minutes=2)
    B.staff_fields(cl, A, rcC.json().get("transfer_id"), amount=20 * M, when=inside, payer=st["LC"], payee_acct=P1_ACCT,
                   memo=pays["C"]["qr_code"], txn=f"FT14C{inside:%H%M%S}")
L.psql(f"update funding_windows set receipt_deadline_at = now() - interval '1 minute' where id = '{win}'")
cut = appdb.run_job("day14-cutoff")
with c() as cl:
    sd = cl.get("/funding/me/standing", headers=H(tok["D"])).json()
    sc = cl.get("/funding/me/standing", headers=H(tok["C"])).json()
    tC, tD = ticket(cl, "C"), ticket(cl, "D")
    record("6.9 paid on time, receipt uploaded after 15:00 (before 15:30): on time, unlocked, no strike",
           tC["status"] == "ATTESTED" and sc["status"] == "GOOD" and sc["no_show_count"] == 0,
           f"C ticket {tC['status']} {tC.get('payment_timing')}; standing {sc['status']} misses {sc['no_show_count']}")
    record("6.1 15:30: no receipt -> no-show; D's loan never takes effect; locked and the miss counted",
           tD["status"] == "EXPIRED" and sd["status"] == "LOCKED" and sd["no_show_count"] == 1,
           f"job {cut}; D ticket {tD['status']}; standing {sd['status']} misses {sd['no_show_count']}")
    r = cl.post(f"/funding/tranches/{tickets['D']['id']}/receipt",
                files={"file": ("D.png", B.png("D late"), "image/png")}, headers=H(tok["D"]) | B.idem())
    record("5.x receipts close at 15:30", r.status_code == 409, f"{r.status_code} {err(r)}")
    if tC["status"] == "UNDER_REVIEW":
        # The system holds an after-14:45 upload for staff review; the only way through is a staff decision.
        r = cl.post(f"/admin/funding/transfers/{rcC.json().get('transfer_id')}/decision",
                    json={"outcome": "APPROVED", "reason": "E2E: transfer inside its QR window, uploaded at 15:05"},
                    headers=A)
        tC = ticket(cl, "C")
        sc = cl.get("/funding/me/standing", headers=H(tok["C"])).json()
        record("6.9 (manual path) staff approve C's receipt: the ticket counts and the lock is lifted",
               tC["status"] in ("ATTESTED", "CONFIRMED", "ACTIVE") and sc["status"] == "GOOD",
               f"staff decision {r.status_code} {err(r)}; C ticket {tC['status']}; standing {sc['status']}")

    # ---------------------------------------------------------------- the SME's check and loan formation
    r = cl.post(f"/funding/tranches/{tickets['A']['id']}/sme-receipt",
                files={"file": ("smeA.png", B.png("SME: 40,000,000 received"), "image/png")}, headers=H(sme) | B.idem())
    record("5.3 the SME's own receipt confirms A (two-way check)", r.status_code == 202 and r.json().get("status") in
           ("CONFIRMED", "ACTIVE"), f"{r.status_code} {r.json().get('status') if r.status_code < 300 else err(r)}")
    cl.post(f"/funding/tranches/{tickets['C']['id']}/sme-receipt",
            files={"file": ("smeC.png", B.png("SME: 20,000,000 received"), "image/png")}, headers=H(sme) | B.idem())
    L.psql(f"update tranches set confirm_deadline = now() - interval '1 minute' where id = '{tickets['B']['id']}'")
    adv = cl.post(f"/admin/funding/rounds/{win}/advance", headers=A).json()
    tick_b = [t for t in cl.get("/funding/me/rounds", headers=H(sme)).json()[0]["tickets"] if t["id"] == tickets["B"]["id"]]
    record("5.4 SME silence for 24 h counts as received; the 30-day dispute window is shown",
           bool(tick_b) and tick_b[0]["status"] in ("CONFIRMED", "ACTIVE") and bool(tick_b[0].get("dispute_until")),
           f"B -> {tick_b and tick_b[0]['status']}; dispute_until {tick_b and tick_b[0].get('dispute_until')}")
    w = window(cl)
    if w["status"] == "CLOSING":
        L.psql(f"update funding_windows set close_wait_until = now() - interval '1 minute' where id = '{win}'")
        adv = cl.post(f"/admin/funding/rounds/{win}/advance", headers=A).json()
        w = window(cl)
    loans = {k: ticket(cl, k) for k in "ABC"}
    record("6.1 the loan goes ahead with A-C (90M); one shared schedule",
           w["status"] == "REPAYING" and w["confirmed_vnd"] == 90 * M and all(t["status"] == "ACTIVE" for t in loans.values()),
           f"window {w['status']} confirmed {w['confirmed_vnd']:,}; repayment starts {w.get('repayment_start_date')}; "
           f"tickets {[(k, t['status']) for k, t in loans.items()]}")
    record("6.2 / 6.3 / 6.11 the 10M shortfall reopens for 7 days for top-ups", "NOT BUILT",
           "top-up signing in the 7-day reopening is Phase 3 (app/direct_transfer/standing.py, gap register B6): "
           "no route to pledge, sign or pay a top-up")
    fees = [i for i in cl.get("/admin/funding/fee-invoices", headers=A).json() if i["window_id"] == win]
    record("5.6 one SME fee invoice: 3% x confirmed disbursements + VAT, separate QR, due in 7 days",
           len(fees) == 1 and fees[0]["fee_vnd"] == 2_700_000,
           f"{[(i['fee_vnd'], i.get('vat_vnd'), i['total_vnd'], i.get('due_at')) for i in fees]}")
st = load()
st["p1"]["tranches"] = {k: t for k, t in loans.items()} | {"D": tickets["D"]}
st["p1"]["round"] = {"status": w["status"], "repayment_start_date": w.get("repayment_start_date")}
st["p1"]["fee_invoice"] = fees[0] if fees else None
save(st)
