"""Lenders A-F (individuals, approved eKYC and bank accounts) and one lender without eKYC (M1.4).
The Lender Service Agreement (Zalo OTP + signed PDF) is recorded with appdb.record_agreements."""
import appdb
import builders as B
from e2e_lib import c, load, record, save, show

NAMES = {"A": "LE VAN AN", "B": "PHAM THI BINH", "C": "HOANG VAN CUONG", "D": "VU THI DUNG", "E": "DO VAN EM",
         "F": "BUI THI PHUONG"}
ACCTS = {"A": "1010101010", "B": "2020202020", "C": "3030303030", "D": "4040404040", "E": "5050505050",
         "F": "6060606060"}
ids = []
for k, name in NAMES.items():
    u = B.onboard(f"L{k}", f"e2e.lender.{k.lower()}@example.com", name.title(), "INVESTOR",
                  bank={"holder_type": "INDIVIDUAL", "bank_bin": "970415", "account_number": ACCTS[k],
                        "account_name": name})
    ids.append(u["id"])
    st = load()
    st[f"L{k}"].update(name=name, account_number=ACCTS[k])
    save(st)
    print("lender", k, "ready")
nokyc = B.onboard("Lnokyc", "e2e.lender.nokyc@example.com", "Ngo Van Khong", "INVESTOR", kyc=False,
                  bank={"holder_type": "INDIVIDUAL", "bank_bin": "970415", "account_number": "9090909090",
                        "account_name": "NGO VAN KHONG"})
appdb.record_agreements(ids)

st = load()
with c() as cl:
    r = B.reserve(cl, nokyc["token"], st["p1"]["window"]["id"], 10_000_000)
    record("1.4 lender without approved eKYC cannot commit", r.status_code == 403, show(r, 200))
