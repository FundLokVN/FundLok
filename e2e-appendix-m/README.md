# Appendix M end-to-end run (FundLok handbook v3.22, Money flow FINAL v1.0)

Scripts that drive the handbook's Appendix M test script against a **local** FundLok stack, through the real API, and record a PASS / FAIL / PARTIAL result for every step. They produced the pre-launch test report of 14 Nov 2026 (`reference/results-2026-11-15.jsonl`, the old pay-at-commit flow) and, rewritten for the commit-then-accept flow (pledges, Day-13 signing and decision, Day-14 contract code, 15-minute QR, 14:45 / 15:00 / 15:30, late money as a top-up pledge), the run of 19 Nov 2026 on backend `main` @ `1722dd1` (`reference/results-2026-11-19.jsonl`). A clean run takes about 8 minutes.

> **Local, throwaway database only.** The run creates accounts and projects, moves deadlines directly in the database to fast-forward time, and runs worker jobs with a fake clock. Never point it at a shared, staging or production database.

## Prerequisites

1. Docker services from the backend's `docker-compose.yml`: `postgres` (port 5433), `mailpit` (1025/8025) and `minio` (9000). Redis is not needed.
2. A **fresh** database, migrated and seeded:
   ```bash
   docker compose up -d postgres mailpit minio
   DATABASE_URL=postgresql+psycopg2://fundlok:fundlok@localhost:5433/fundlok_e2e alembic upgrade head
   docker compose exec -T postgres psql -U fundlok -d fundlok_e2e -v ON_ERROR_STOP=1 < scripts/seed_data.sql
   ```
   The seed provides `admin@fundlok.vn` and `sysadmin@fundlok.vn`.
3. The API on <http://localhost:8000>, started with **`../venv/Scripts/python serve_e2e.py`** (launch config `api-e2e`). It uses the throwaway database `fundlok_e2e`, `FUNDING_WINDOW_FLOW=COMMIT_THEN_ACCEPT`, FundLok's fee account (`FUNDLOK_FEE_*`) and `PHONE_OTP_CHANNEL=log`, so the Zalo codes (Day-13 guarantor, Day-14 contract) go to `logs/api.log`, where the scripts read them. No real phone gets a message. Create the database once: `docker exec repo-postgres-1 psql -U fundlok -d postgres -c "CREATE DATABASE fundlok_e2e OWNER fundlok;"`. `e2e_lib` refuses to run against any other database.
4. Optional: the frontend on <http://localhost:3000>, used only by the wording scan in `s13`.
5. Python: the backend's own virtualenv. It already has httpx, Pillow, pypdf, PyYAML and the app's dependencies.
6. [`uv`](https://docs.astral.sh/uv/) on PATH. `builders.cic_pdf` uses it to build a CIC report PDF with a Vietnamese text layer (fpdf2) in a throwaway environment.

Environment overrides, all optional: `FUNDLOK_BACKEND` (default `..`), `DATABASE_URL` (default the compose Postgres), `E2E_API`, `E2E_FRONTEND`, `E2E_MAILPIT`, `E2E_PG_CONTAINER` (default `repo-postgres-1`; use `docker ps` to find yours).

## Run

```bash
cd e2e-appendix-m
../venv/Scripts/python run_all.py --fresh --reset-db   # wipe, migrate and seed fundlok_e2e, then run everything
```

- `--fresh` clears `out/`. Use it together with a freshly migrated and seeded database: the scripts reuse fixed emails and tax codes.
- `--from s07` resumes from a given script. Each script reads and writes `out/state.json`.
- `run_all.py` refreshes the staff session before every script, because admin access tokens last 30 minutes.
- Output:
  - `out/results.jsonl`: one line per check (`step`, `result`, `note`, `at`).
  - `out/schedule_*.json`: the live P1 repayment schedules.
  - `out/p1_standing_offer.txt`: text of the generated Standing Offer.
  - A summary printed at the end.

## What each script covers (19 Nov 2026 rewrite)

| Script | Appendix M | What it does |
|---|---|---|
| `s03_lenders.py` | 1.4 | Lenders A-F with approved eKYC, bank accounts and a verified mobile; no agreement pre-recorded (it is signed on the first Day 14) |
| `s04_m3_commitments.py` | 3.1-3.7, 3.10 | Pledges on P1 (A40 B30 C20 D10), withdrawal, minimum, capacity, full window, reopening after a withdrawal; P2 last ticket; sets up P3-P5 (new SMEs) |
| `s05_m5_disbursement.py` | 3.8-3.9, 4.1, 4.2, 4.8, 5.x, 6.1, 6.9 | P1: end of Day 12 (contracts), reminders, Accept needs signing (NEAC faked), guarantor Zalo code, Day-14 reminders, contract code, QR with its own code, new QR, on-time receipts, 14:45 / 15:00 / 15:30, SME receipt and silence, pooled loan, SME fee |
| `s06_m6_no_show.py` | 6.4 | D's unlock: fresh eKYC, then the final sentence (vi and en) |
| `s07_m7_schedule.py` | 7.1, 7.2, 7.5, 7.7 | Each lender's schedule against the handbook 10.7 formulas at P1's actual rate |
| `s10_m4_decisions.py` | 4.3-4.8 | P2 Decline, P3 silence at 70%, P4 manual accept at 40% (noon cut-off shown), P5 silence at 40%, a decision after the deadline |
| `s11_m6_p3_day14.py` | 6.5-6.8, 6.10 | P3 Day 14: E on time, F late money (expired code) -> top-up pledge, D's second no-show -> closed, loan below 50% goes ahead, staff waiver |

The 14 Nov table below is kept for the scripts that did not change (s01, s02, s08, s09, s12-s14 were adjusted, not rewritten).

## What each script covered on 14 Nov

| Script | Appendix M | What it does |
|---|---|---|
| `s00_staff_sessions.py` | setup | Second-factor sessions for the seeded staff (see "What is bypassed"), and KYC/KYB verification mode set to MANUAL |
| `s01_m1_m2_alt_sme.py` | 1.2–1.3, 2.1–2.3 | SME onboarding through the API; loan size and duration bounds; wrong type, content type and size; replacing a file; a CIC PDF without a text layer, then **submit succeeds with a document missing** and staff can only approve or reject; grade, rate formula and parameter version; engine DRR days |
| `s02_p1_offer_and_publish.py` | 2.4–2.5, 1.3 | P1 (100M, 2 months, grade ≤ 62.5 so lenders get 15%); offer fields; blanks left by a manual KYB; refusal of an unsigned document; revenue share s on the Standing Offer; publish and open the funding window |
| `s03_lenders.py` | 1.4 | Lenders A–F with approved eKYC and bank accounts, plus a lender without eKYC who is refused |
| `s04_m3_commitments.py` | 3.1–3.5, 5.1 | A 40M (2-hour payment hold), VietQR contents and CRC, one live commitment per lender, 10M minimum, capacity limit, filling P1 |
| `s05_m5_disbursement.py` | 5.2, 5.3, 5.5 | Transfer proofs; duplicate screenshot; staff key in fields (B, C become ATTESTED, "Paid"); A's transfer time falls before its commitment, so it goes to review; the SME cannot upload a receipt; the SME's 2-working-day deadline; a staff review decision confirms A with no SME step |
| `s06_m6_no_show.py` | 6.1–6.5, 5.4 | SME silence plus staff Advance confirms B and C; D's hold lapses (no lock); D commits again and misses again; E takes the 10M top-up through proof, fields and SME silence; the round closes and the SME fee is issued |
| `s07_m7_schedule.py` | 7.1, 7.2, 7.5, 7.7, 6.3 | Compares every live schedule with handbook 10.8 (daily, last, interest, tax, transfer) and checks holidays and end dates |
| `s08_m7_repayment_days.py` | 7.1, 7.3, 7.4, 7.6 | Moves the first two due dates back to the last two weekdays; Today's payments; paying two days at once; the lender sees and confirms; report deadlines; "Not received"; oldest-first allocation |
| `s09_m5_fee_m2_6_second_project.py` | 5.6, 5.7, 2.6 | SME fee amount, due date, QR and late interest; with P1 repaying, the same SME tries the same business and another business again (refused since the one-borrower rule, d2ea9ee) |
| `s10_p2_last_ticket.py` | 3.6, 3.7 | P2 at 45M: four 10M tickets, then F's 10M, 4M and 5M |
| `s11_p3_sme_decision.py` | 4.1–4.7 | P3 at 100M with 40M confirmed; looks for accept/decline routes; manual accept; silence until the window ends gives REPAYING plus a fee |
| `s12_m8_month_end.py` | 8.1–8.3, 7.3 | Runs the **undeployed** worker jobs with a month-end clock: presumed-paid days, the lender fee against the formula, lender visibility, the unpaid lender-fee block |
| `s13_m12_platform_rules.py` | 12.1–12.3 | Account statuses available to staff; scans the rendered public pages for collection and custody wording |
| `s14_manual_and_static.py` | 1.1, 2.2, 2.4, 5.8, 6.6, M9–M11 | Records the outcome of the manual browser checks below and of the not-built sections |

Shared modules:
- `e2e_lib.py`: paths, HTTP, state and results, Mailpit, `psql` time travel.
- `builders.py`: onboarding, documents, project to approval, opening a window, reserving.
- `appdb.py`: the in-process workarounds listed below.
- `mkcic.py`: builds the CIC PDF.

## What is bypassed, and why

Local machines lack some external services. Each workaround uses the app's own code or the same approach as `conftest.py`. Nothing in the product is patched.

| Step | Why it can't run locally | Workaround |
|---|---|---|
| Staff sign-in with TOTP | The local `.env` has no `TOTP_ENCRYPTION_KEY`, so 2FA enrolment returns 503 | `appdb.mint_staff_sessions`: `app.auth.service.create_token_pair(..., second_factor="otp")`, as `conftest._second_factor_session` does |
| eKYC / eKYB | No GVerify keys | Verification mode MANUAL, then staff approve each attempt through the admin API |
| Legal representative and company address for signing | A manual KYB approval cannot record them (a finding in itself) | `builders.fill_kyb_like_provider` writes them into the KYB row, as a live GVerify response would |
| SME signing (Service Agreement, Standing Offer, Loan Contract) | Needs CA-signed PDFs verified with NEAC | `appdb.attach_and_activate`: `conftest.attach_signed_listing_documents`, then the real `activate_project_if_approved` |
| Lender Service Agreement | Needs a Zalo OTP and a signed PDF | `appdb.record_agreements`, as `conftest.make_eligible_investor` does |
| Receipt OCR | Off locally | Staff key in the transfer fields through the admin API |
| Day-13 SME signature on each lender's contract | NEAC cannot verify a test certificate | `appdb.sme_sign_window_contracts`: the app's own `day13_contracts.record_sme_signed`, NEAC faked as `conftest.fake_neac` does |
| Cloud Scheduler jobs (funding-advance, acceptance and Day-14 reminders, day14-cutoff) | The `/internal/jobs` routes need Google OIDC | `appdb.run_job`: the same service functions, in-process, real clock |
| Zalo codes | No Zalo sends in a test | `PHONE_OTP_CHANNEL=log` (DEBUG only); scripts read the code from `logs/api.log` |
| Day 13 / Day 14 / 24 h / 7 days | No clock override in the app | `psql` updates to `tranches.hold_expires_at`, `tranches.confirm_deadline`, `funding_windows.window_end` and `repayment_days.due_date`, followed by the staff Advance route, or `appdb.run_jobs` with a fixed clock |

## Manual checklist (browser)

These were checked by hand in the original run. `s14` records them, so edit it if your outcome differs.

1. **M1.1 consent screen**: register at `/register`, open the verification link from Mailpit (<http://localhost:8025>) and log in. Expect three separate boxes, the "send code" button disabled until all three are ticked, and the OTP by email. A wrong code is refused. Look for the "FundLok does not collect debts" disclosure (absent) and the "[30]" placeholder in Art. 7 (present).
2. **SME dashboard** (log in as `e2e.p1b.owner@example.com`, password in `e2e_lib.PW`): there is no Accept / Decline ("Không chấp nhận") control, and no 30-day dispute text. The offer total (business rate) and the repayment plan total (lender rate) differ on the same screen.
3. **Lender dashboard** (`e2e.lender.c@example.com`): after `s12`, "Đã trả cho bạn" counts presumed-paid days the SME never paid.

## Reading the results

- A **FAIL** means the system differs from the handbook text, not necessarily that the code is broken. The backend's own suite passes (2,084 tests): the code follows its specs, which describe a pay-at-commit flow (`app/direct_transfer/config.py`: `hold_hours=2`, `repay_from_approval=True`), while Appendix M describes commit, then SME decision on Day 13, then transfer on Day 14.
- Steps M9–M11 are marked "not built" in the handbook itself and are recorded as NOT BUILT.

## Reset

Wipe and re-create the throwaway database, then repeat Prerequisites step 2:

```bash
../venv/Scripts/python run_all.py --fresh --reset-db   # only ever touches fundlok_e2e
```

## Reference and related documents

- `reference/results-2026-11-15.jsonl`: the latest result per check from the run on `main @ 81a5f2e` (15 Nov 2026), for comparing your own run.
- Pre-launch test report (14 Nov): https://claude.ai/artifact/PvJVn4odt7p6nRNABXxQz6
- Decision log (Loc, 15 Nov, final): https://claude.ai/artifact/N6WYiSLMiEJHzkm5Rvq54b
- Gap register (target vs system, plan corrections): https://claude.ai/artifact/FpzgnyhVRdz9c5GV48wBc1
- Source: FundLok Master Operating Handbook v3.1, Appendix M (v3.2 changes in progress, owned by Loc).

After Phase 2 (the Day-13 / Day-14 flow) the M3–M6 scripts (`s04`–`s06`, `s11`) need updating: they currently exercise pay-at-commit.
