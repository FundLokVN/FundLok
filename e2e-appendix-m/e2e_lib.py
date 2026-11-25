"""Shared helpers for the Appendix M end-to-end run against a LOCAL FundLok stack.

Every script in this folder imports this module first. On import it:
  * puts the backend repo on sys.path and makes it the working directory, so `app.*` imports read the
    backend's own .env (same SECRET_KEY as the running API);
  * points DATABASE_URL at the local compose Postgres unless you set it yourself.

NEVER point this at a shared, staging or production database: the run creates accounts, edits deadlines
directly in the database to fast-forward time, and runs worker jobs with a fake clock.

Run state (ids, tokens) is kept in out/state.json; every check is appended to out/results.jsonl.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.environ.get("FUNDLOK_BACKEND", os.path.join(HERE, "..")))
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://fundlok:fundlok@localhost:5433/fundlok_e2e")
os.environ.setdefault("FUNDING_WINDOW_FLOW", "COMMIT_THEN_ACCEPT")  # Money flow FINAL v1.0 (serve_e2e.py sets the same)
os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("PHONE_OTP_CHANNEL", "log")
os.environ.setdefault("FUNDLOK_FEE_BANK_BIN", "970407")
os.environ.setdefault("FUNDLOK_FEE_ACCOUNT_NUMBER", "116889688")
os.environ.setdefault("FUNDLOK_FEE_ACCOUNT_NAME", "CONG TY CO PHAN FUNDLOK")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
os.chdir(BACKEND)

import httpx  # noqa: E402

API = os.environ.get("E2E_API", "http://localhost:8000")
FRONTEND = os.environ.get("E2E_FRONTEND", "http://localhost:3000")
MAILPIT = os.environ.get("E2E_MAILPIT", "http://localhost:8025")
PG_CONTAINER = os.environ.get("E2E_PG_CONTAINER", "repo-postgres-1")
PG_USER = "fundlok"
PG_DB = os.environ["DATABASE_URL"].rsplit("/", 1)[-1].split("?")[0]
if PG_DB != "fundlok_e2e" and not os.environ.get("E2E_I_KNOW_THIS_IS_THROWAWAY"):
    raise SystemExit(f"Refusing to run against {PG_DB!r}: the e2e run edits and wipes data. Use fundlok_e2e.")
API_LOG = os.path.join(HERE, "logs", "api.log")  # written by serve_e2e.py

PW = "E2eTest#2026"          # password of every account this run creates (local test accounts only)
TERMS_VERSION = "2026-10-30"  # app/market/funding.py INVESTOR_TERMS_VERSION
CONSENT_VERSION = "tos-v0.3"  # app/consent/documents.py CURRENT_VERSION
ICT = dt.timezone(dt.timedelta(hours=7))

STATE = os.path.join(OUT, "state.json")
RESULTS = os.path.join(OUT, "results.jsonl")


# --------------------------------------------------------------------------- state / results
def say(text: str) -> None:
    """print, falling back to '?' for characters a non-UTF-8 console cannot show."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode(), flush=True)


def load() -> dict:
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save(st: dict) -> None:
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)


def record(step: str, ok, note: str) -> None:
    """ok: True -> PASS, False -> FAIL, or a label such as 'PARTIAL', 'N/A', 'NOT BUILT'."""
    result = "PASS" if ok is True else ("FAIL" if ok is False else str(ok))
    line = {"step": step, "result": result, "note": note, "at": dt.datetime.now(dt.timezone.utc).isoformat()}
    say(f"[{result}] {step}: {note}")
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- HTTP
def c() -> httpx.Client:
    return httpx.Client(base_url=API, timeout=60)


def H(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def idem() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def show(r: httpx.Response, n: int = 600) -> str:
    try:
        t = json.dumps(r.json(), ensure_ascii=False)
    except Exception:
        t = r.text
    return f"{r.status_code} {t[:n]}"


def login(email: str, pw: str = PW) -> tuple[str, dict]:
    """Password sign-in. Staff accounts need a second factor: use 00_staff_sessions.py for those."""
    with c() as cl:
        r = cl.post("/auth/login", json={"email": email, "password": pw})
        r.raise_for_status()
        return cl.cookies.get("access_token"), r.json()


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def latest_mail(to: str, subject_contains: str | None = None, wait: int = 15, since: str | None = None) -> dict | None:
    """Newest Mailpit message to `to` (optionally only those created at/after `since`, an ISO UTC time), so a rerun
    with the same addresses never picks up an older link or code."""
    deadline = time.time() + wait
    since_dt = dt.datetime.fromisoformat(since) - dt.timedelta(seconds=2) if since else None
    while time.time() < deadline:
        msgs = httpx.get(f"{MAILPIT}/api/v1/search", params={"query": f"to:{to}"}).json().get("messages", [])
        for m in msgs:  # newest first
            created = dt.datetime.fromisoformat(m["Created"].replace("Z", "+00:00"))
            if since_dt and created < since_dt:
                continue
            if subject_contains is None or subject_contains.lower() in m["Subject"].lower():
                return httpx.get(f"{MAILPIT}/api/v1/message/{m['ID']}").json()
        time.sleep(1)
    return None


# --------------------------------------------------------------------------- direct DB edits (time travel)
def psql(sql: str) -> str:
    """Run SQL in the local compose Postgres (used only to fast-forward deadlines and the like)."""
    res = subprocess.run(["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB, "-qAt", "-c", sql],
                         capture_output=True, text=True, check=True)
    return res.stdout.strip()


def ict_today() -> dt.date:
    return dt.datetime.now(ICT).date()


def previous_weekdays(n: int, before: dt.date | None = None) -> list[dt.date]:
    """The n weekdays before `before` (default today ICT), oldest first. Holidays are not excluded."""
    d = before or ict_today()
    out: list[dt.date] = []
    while len(out) < n:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return list(reversed(out))


# --------------------------------------------------------------------------- one-time codes (PHONE_OTP_CHANNEL=log)
def log_mark() -> int:
    """Current size of the API log, so a later read only sees codes issued after this point."""
    return os.path.getsize(API_LOG) if os.path.exists(API_LOG) else 0


def code_from_log(mark: int, needle: str, wait: int = 10) -> str:
    """The newest 6-digit code logged after `mark` on a line containing `needle` (e.g. a ticket reference)."""
    import re
    deadline = time.time() + wait
    while time.time() < deadline:
        if os.path.exists(API_LOG):
            with open(API_LOG, encoding="utf-8", errors="replace") as f:
                f.seek(mark)
                hits = [ln for ln in f.read().splitlines() if needle in ln and "PHONE_OTP_CHANNEL=log" in ln]
            if hits:
                m = re.search(r": (\d{6}) \(PHONE_OTP_CHANNEL=log\)", hits[-1])
                if m:
                    return m.group(1)
        time.sleep(0.5)
    raise RuntimeError(f"no code for {needle!r} in {API_LOG} (is the API running through serve_e2e.py?)")


def err(r) -> str:
    return r.headers.get("X-Error-Code", "")

