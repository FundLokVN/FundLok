"""M12: account closure statuses (12.2) and money-holding wording on the rendered public pages (12.3).
12.3 needs the frontend running on E2E_FRONTEND (default http://localhost:3000); it is skipped otherwise."""
import re

import httpx

import e2e_lib as L
from e2e_lib import H, c, load, record, show

st = load()
A = H(st["admin"]["token"])
with c() as cl:
    res = {s: cl.patch(f"/admin/users/{st['Lnokyc']['id']}/status", json={"status": s, "note": "E2E threats to an SME"},
                       headers=A).status_code for s in ("CLOSED", "BANNED", "SUSPENDED")}
    record("12.2 staff record lender threats -> account closed", "PARTIAL",
           f"status codes {res}: only ACTIVE / PENDING_KYC / SUSPENDED exist; no reason code; T&S has no Art. 4.4")

PAT = re.compile(r"(thu nợ|thu hộ|nạp tiền|rút tiền|ký quỹ|ví điện tử|escrow|wallet|custod\w*|deposit|withdraw\w*|"
                 r"collect(?:ed|ion)?\b|we hold|khấu trừ khi giải ngân|deducted from disbursement)", re.I)
hits = {}
try:
    for page in ("/", "/en", "/why-us", "/en/why-us", "/terms", "/en/terms", "/faq", "/en/faq"):
        r = httpx.get(L.FRONTEND + page, timeout=60)
        if r.status_code != 200:
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[\s\S]*?</\1>", " ", r.text)))
        found = [text[max(0, m.start() - 60):m.end() + 60] for m in PAT.finditer(text)]
        if found:
            hits[page] = found[:5]
    for page, found in hits.items():
        L.say(page)
        for f in found:
            L.say("    " + f)
    positive = {p: f for p, f in hits.items() if any(re.search(r"thu nợ|collected every", x, re.I) for x in f)}
    record("12.3 no escrow / wallet / collection / we-hold-your-money wording", not positive,
           f"collection wording on {list(positive)} (other hits are statements that FundLok does NOT hold money; review list above)")
except httpx.ConnectError:
    record("12.3 wording scan", "N/A", f"frontend not reachable at {L.FRONTEND}")
record("12.1 T&S change: users notified 30 days before it takes effect; can close account before", False,
       "no effective date / notice; changing the consent version forces immediate re-acceptance; no self-service "
       "account closure; T&S Art. 7 shows the placeholder '[30]'")
