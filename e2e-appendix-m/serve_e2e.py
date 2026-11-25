"""Run the FundLok API for the Appendix M end-to-end run: LOCAL throwaway database only.

    ../venv/Scripts/python serve_e2e.py        # http://localhost:8000

What it changes for the run, through environment variables only (the product code is not patched):
  * DATABASE_URL        -> the throwaway database `fundlok_e2e` (override with E2E_DATABASE_URL);
  * FUNDING_WINDOW_FLOW -> COMMIT_THEN_ACCEPT, the Money flow FINAL v1.0 flow (production must set the same);
  * DEBUG=True + PHONE_OTP_CHANNEL=log -> one-time codes (Day 0 guarantor, Day 14 loan contract) are written to the
    log instead of being sent by Zalo, so no real phone gets a message. The scripts read them from logs/api.log.
"""
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)

os.environ["DATABASE_URL"] = os.environ.get(
    "E2E_DATABASE_URL", "postgresql+asyncpg://fundlok:fundlok@localhost:5433/fundlok_e2e"
)
os.environ["FUNDING_WINDOW_FLOW"] = "COMMIT_THEN_ACCEPT"
os.environ["DEBUG"] = "True"
os.environ["PHONE_OTP_CHANNEL"] = "log"
# FundLok's own account for fee invoices (AM-E14). Production must set the same three variables.
os.environ.setdefault("FUNDLOK_FEE_BANK_BIN", "970407")
os.environ.setdefault("FUNDLOK_FEE_ACCOUNT_NUMBER", "116889688")
os.environ.setdefault("FUNDLOK_FEE_ACCOUNT_NAME", "CONG TY CO PHAN FUNDLOK")
if "fundlok_e2e" not in os.environ["DATABASE_URL"] and not os.environ.get("E2E_I_KNOW_THIS_IS_THROWAWAY"):
    sys.exit("Refusing to start: the e2e run must use a throwaway database (fundlok_e2e).")

sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

LOGS = os.path.join(HERE, "logs")
os.makedirs(LOGS, exist_ok=True)
handler = logging.FileHandler(os.path.join(LOGS, "api.log"), encoding="utf-8")
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.INFO)

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=int(os.environ.get("E2E_PORT", "8000")), log_level="info")
