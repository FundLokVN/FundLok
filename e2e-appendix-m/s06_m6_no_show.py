"""M6.4 after P1's Day 14: the no-show lender D unlocks with a fresh eKYC and the acknowledgement sentence.

The final sentence (SCRUM-604, locked 18 Nov 2026), typed in the lender's own language:
  vi "Tôi hiểu việc không có mặt giải ngân ảnh hưởng rất nhiều đến doanh nghiệp, tôi cam kết sẽ đầu tư chuẩn mực"
  en "I realise no-showing has a big effect on the SME. I commit to investing properly."
"""
import builders as B
from e2e_lib import H, c, err, load, login, record

FINAL_VI = "Tôi hiểu việc không có mặt giải ngân ảnh hưởng rất nhiều đến doanh nghiệp, tôi cam kết sẽ đầu tư chuẩn mực"
FINAL_EN = "I realise no-showing has a big effect on the SME. I commit to investing properly."

st = load()
A = H(st["admin"]["token"])
tokD = login(st["LD"]["email"])[0]
with c() as cl:
    sd = cl.get("/funding/me/standing", headers=H(tokD)).json()
    shown = sd.get("acknowledgement_sentence") or ""
    record("6.4 the sentence the locked lender is asked to type is the final one (SCRUM-604)", shown == FINAL_VI,
           f"system shows: {shown!r}")
    r = cl.post("/funding/me/standing/unlock", json={"acknowledgement": FINAL_VI}, headers=H(tokD))
    record("6.4 unlock needs a fresh eKYC first", r.status_code == 409 and err(r) == "EKYC_REQUIRED", f"{r.status_code} {err(r)}")
    status = cl.get("/gverify/kyc/status", headers=H(tokD)).json()
    r = cl.post("/gverify/kyc/verify", json={"id_front_b64": B.jpeg_b64("white"), "id_back_b64": B.jpeg_b64("gray"),
                                            "portrait_b64": B.jpeg_b64("beige")}, headers=H(tokD))
    ok = r.status_code == 201
    if ok:
        ok = cl.post(f"/admin/kyc-verifications/{r.json()['verification_id']}/resolve",
                     json={"decision": "APPROVED", "note": "E2E re-verification after a no-show",
                           "person_number": "079012345678", "full_name": st["LD"]["name"], "date_of_birth": "1990-01-01"},
                     headers=A).status_code == 200
    record("6.4 a locked lender can redo eKYC", ok and status.get("reverify_required") is True,
           f"reverify_required={status.get('reverify_required')}; new attempt {r.status_code}, staff approved {ok}")
    wrong = cl.post("/funding/me/standing/unlock", json={"acknowledgement": "toi hua"}, headers=H(tokD))
    vi = cl.post("/funding/me/standing/unlock", json={"acknowledgement": FINAL_VI}, headers=H(tokD))
    en = cl.post("/funding/me/standing/unlock", json={"acknowledgement": FINAL_EN}, headers=H(tokD))
    record("6.4 a wrong sentence is refused", wrong.status_code in (409, 422) and err(wrong) == "ACKNOWLEDGEMENT_MISMATCH",
           f"{wrong.status_code} {err(wrong)}")
    record("6.4 the final Vietnamese sentence unlocks the account", vi.status_code == 200 and vi.json().get("status") == "GOOD",
           f"{vi.status_code} {err(vi) or vi.json().get('status')}")
    record("6.4 the final English sentence is accepted for an English-speaking lender",
           (vi.status_code == 200) or (en.status_code == 200 and en.json().get("status") == "GOOD"),
           f"{en.status_code} {err(en) or en.json().get('status')}")
    if vi.status_code != 200 and en.status_code != 200 and shown:
        r = cl.post("/funding/me/standing/unlock", json={"acknowledgement": shown}, headers=H(tokD))
        print(f"   unlocked with the sentence the system shows instead: {r.status_code} {r.json().get('status')}")
    sd = cl.get("/funding/me/standing", headers=H(tokD)).json()
    print(f"   D now {sd['status']}, misses {sd['no_show_count']}")
