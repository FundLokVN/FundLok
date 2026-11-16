"""Setup: second-factor staff sessions for the seeded admin / system admin, and KYC/KYB verification mode MANUAL
(no GVerify keys locally, so staff approve each attempt). Re-run whenever the admin token expires (30 min)."""
import appdb
from e2e_lib import H, c, load, save, show

st = load()
st.update(appdb.mint_staff_sessions())
save(st)
with c() as cl:
    print("admin overview:", cl.get("/admin/overview", headers=H(st["admin"]["token"])).status_code)
    r = cl.put("/system/verification-mode", json={"mode": "MANUAL"}, headers=H(st["sysadmin"]["token"]))
    print("verification mode:", show(r, 120))
