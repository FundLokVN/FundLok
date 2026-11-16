"""P2 (45M): four lenders x 10M, then F tries 10M / 4M / 5M (M3.6, M3.7)."""
import builders as B
from e2e_lib import H, c, load, login, record, save

B.onboard("sme2", "e2e.p2.owner@example.com", "Le Thi P Hai", "SME", kyb_name="CONG TY TNHH P2 E2E", tax_code="0322222222")
p2 = B.make_project("sme2", "CONG TY TNHH P2 E2E", "0322222222", 45_000_000, 2, 120_000_000)
w = B.open_window("sme2", p2, {"holder_type": "ENTITY", "bank_bin": "970436", "account_number": "0071000222333",
                                "account_name": "CONG TY TNHH P2 E2E"})
st = load()
A = H(st["admin"]["token"])
tok = {k: login(st[f"L{k}"]["email"])[0] for k in "ABCEF"}
with c() as cl:
    for k in "ABCE":
        B.reserve(cl, tok[k], w["id"], 10_000_000)
    ww = cl.get(f"/funding/listings/{w['listing_id']}/window", headers=A).json()
    record("3.6 four x 10M on P2 -> capacity left 5M", ww["capacity_vnd"] == 5_000_000, f"capacity={ww['capacity_vnd']:,}")
    r10, r4, r5 = (B.reserve(cl, tok["F"], w["id"], x) for x in (10_000_000, 4_000_000, 5_000_000))
    ww = cl.get(f"/funding/listings/{w['listing_id']}/window", headers=A).json()
    record("3.7 F: 10M and 4M refused, exactly 5M accepted, P2 full",
           r10.status_code == 409 and r4.status_code == 409 and r5.status_code == 201 and ww["capacity_vnd"] == 0,
           f"10M->{r10.status_code} {r10.headers.get('X-Error-Code')}, 4M->{r4.status_code} {r4.headers.get('X-Error-Code')}, "
           f"5M->{r5.status_code}; capacity={ww['capacity_vnd']} status={ww['status']}")
st["p2"] = p2 | {"window": w}
save(st)
