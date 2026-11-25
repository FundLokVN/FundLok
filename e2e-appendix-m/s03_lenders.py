"""Lenders A-F (individuals, approved eKYC and bank accounts, a Vietnamese mobile for the Zalo codes) and one lender
without eKYC (M1.4).

Money flow FINAL v1.0: the Lender Service Agreement is signed on the lender's FIRST Day 14, with the same Zalo code as
its first loan contract (no longer at the first commitment), so nothing is pre-recorded here."""
import builders as B
from e2e_lib import c, err, load, record, save, show

NAMES = {"A": "LE VAN AN", "B": "PHAM THI BINH", "C": "HOANG VAN CUONG", "D": "VU THI DUNG", "E": "DO VAN EM",
         "F": "BUI THI PHUONG"}
ACCTS = {"A": "1010101010", "B": "2020202020", "C": "3030303030", "D": "4040404040", "E": "5050505050",
         "F": "6060606060"}
for i, (k, name) in enumerate(NAMES.items(), start=1):
    u = B.onboard(f"L{k}", f"e2e.lender.{k.lower()}@example.com", name.title(), "INVESTOR",
                  bank={"holder_type": "INDIVIDUAL", "bank_bin": "970415", "account_number": ACCTS[k],
                        "account_name": name})
    B.set_phone(u["id"], f"+8490300000{i}")
    st = load()
    st[f"L{k}"].update(name=name, account_number=ACCTS[k])
    save(st)
    print("lender", k, "ready")
nokyc = B.onboard("Lnokyc", "e2e.lender.nokyc@example.com", "Ngo Van Khong", "INVESTOR", kyc=False,
                  bank={"holder_type": "INDIVIDUAL", "bank_bin": "970415", "account_number": "9090909090",
                        "account_name": "NGO VAN KHONG"})

st = load()
with c() as cl:
    r = B.commit(cl, nokyc["token"], st["p1"]["window"]["id"], 10_000_000, nokyc.get("account_id") or "")
    record("1.4 lender without approved eKYC cannot pledge", r.status_code == 403, f"{show(r, 200)} {err(r)}")
