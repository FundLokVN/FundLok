"""M5.2, 5.3, 5.5 on P1: A-C upload transfer proof, D re-uses A's screenshot, staff key in the fields
('Paid' = ATTESTED), the SME's options, then a staff decision confirms A-C."""
import datetime as dt

import builders as B
from e2e_lib import H, c, load, login, record, save, show

st = load()
A = H(st["admin"]["token"])
tr = st["p1"]["tranches"]
tok = {k: login(st[f"L{k}"]["email"])[0] for k in "ABCD"}
sme_tok = login(st[st["p1"]["sme"]]["email"])[0]
rc = st["p1"].setdefault("receipts", {})

with c() as cl:
    shots = {}
    for k in "ABC":
        shots[k] = B.png(f"Chuyen tien {tr[k]['amount_vnd']:,} VND  {st['L' + k]['name']} -> CONG TY TNHH P1 E2E  "
                         f"noi dung {tr[k]['reference_code']}")
        r = cl.post(f"/funding/tranches/{tr[k]['id']}/receipt", files={"file": (f"{k}.png", shots[k], "image/png")},
                    headers=H(tok[k]) | B.idem())
        rc[k] = r.json()
        print("receipt", k, show(r, 160))

    # 5.5 duplicate screenshot
    dup = cl.post(f"/funding/tranches/{tr['D']['id']}/receipt", files={"file": ("dup.png", shots["A"], "image/png")},
                  headers=H(tok["D"]) | B.idem())
    record("5.5 duplicate screenshot fails the duplicate check", dup.status_code == 409,
           f"{show(dup, 120)} {dup.headers.get('X-Error-Code')}")
    d_entry = [x for x in cl.get("/admin/funding/transfers", headers=A).json()["items"] if x["id"] == tr["D"]["id"]]
    queued = bool(d_entry and (d_entry[0].get("transfer") or d_entry[0].get("review_cause") or d_entry[0].get("needs_action")))
    record("5.5 duplicate goes to the exceptions queue", queued,
           f"D's staff entry: transfer={d_entry[0].get('transfer') if d_entry else None}, "
           f"needs_action={d_entry[0].get('needs_action') if d_entry else None}")

    # 5.2 staff key in the fields read from each screenshot (receipt OCR is off locally). A's screenshot shows a
    # transfer time before A committed: the check must send it to review (exceptions queue).
    now = dt.datetime.now(dt.timezone.utc)
    for k in "ABC":
        when = now - dt.timedelta(hours=3) if k == "A" else now
        body = {"amount_vnd": tr[k]["amount_vnd"], "transferred_at": when.isoformat(), "payer_name": st["L" + k]["name"],
                "payer_account": st["L" + k]["account_number"], "payee_account": "0071000111222",
                "memo": tr[k]["reference_code"], "bank_txn_id": f"FT{k}{now:%H%M%S}01", "reason": "E2E: read from screenshot"}
        cl.post(f"/admin/funding/transfers/{rc[k]['transfer_id']}/fields", json=body, headers=A)
    items = {x["id"]: x for x in cl.get("/admin/funding/transfers", headers=A).json()["items"]}
    statuses = {k: items[tr[k]["id"]]["status"] for k in "BC"}
    record("5.2 tranches marked Paid after proof", all(s == "ATTESTED" for s in statuses.values()), f"{statuses}")
    a_item = items[tr["A"]["id"]]
    record("5.5 / 10.10 mismatched proof goes to the exceptions queue", a_item["status"] == "UNDER_REVIEW",
           f"A -> {a_item['status']}, cause {a_item.get('review_cause')}, "
           f"failure codes {(a_item.get('transfer') or {}).get('failure_codes')}")

    # 5.3 the SME's side
    r = cl.post(f"/funding/tranches/{tr['A']['id']}/receipt", files={"file": ("sme.png", B.png("SME receipt"), "image/png")},
                headers=H(sme_tok) | B.idem())
    rounds = cl.get("/funding/me/rounds", headers=H(sme_tok)).json()
    tick = [t for t in rounds[0]["tickets"] if t["id"] == tr["B"]["id"]][0]
    hours = None
    if tick.get("confirm_deadline"):
        hours = (dt.datetime.fromisoformat(tick["confirm_deadline"].replace("Z", "+00:00")) - now).total_seconds() / 3600
    record("5.3 SME uploads its receipt proof (two-way check)", r.status_code < 300,
           f"SME receipt upload -> {r.status_code}; the SME can only press 'Not received'")
    record("5.3/5.4 SME has 24 h", hours is not None and abs(hours - 24) < 1,
           f"SME deadline {tick.get('confirm_deadline')} = {hours:.1f} h away (2 working days)" if hours else "no deadline")

    # staff settle A's review as received: the ticket is confirmed with no SME step
    review = cl.get(f"/admin/funding/transfers/{tr['A']['id']}", headers=A).json().get("dispute") or {}
    r = cl.post(f"/admin/funding/reviews/{review.get('id')}/decision",
                json={"outcome": "RECEIVED", "reason": "E2E: bank statement checked"}, headers=A)
    a_after = cl.get(f"/admin/funding/transfers/{tr['A']['id']}", headers=A).json()["status"]
    record("5.3 tranche confirmed only after the SME's check", a_after not in ("CONFIRMED", "ACTIVE"),
           f"staff review decision ({r.status_code}) alone moved A to {a_after} with no SME step")
save(st)
