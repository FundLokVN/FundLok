# Spec: Day-14 transfer rule (15-minute QR codes, 14:45 close, late money becomes a top-up)

| Field | Value |
|---|---|
| **Status** | DRAFT (Phat, 2026-11-18). For Edward's review. The first implementation PRs follow the proposals in section 9 until Loc and Edward answer |
| **Owner** | Phat (this spec, the UI, the reminder job) |
| **Implementer(s)** | Edward: SCRUM-612 (`app/direct_transfer/`, migrations). Phat: SCRUM-630 (all UI, `app/market` routers, `app/scheduled_jobs` reminder) |
| **Module** | `app/direct_transfer/` (QR codes, cut-offs, lock, top-up pledge), `app/market/funding_router.py` (routes), `app/scheduled_jobs/` (reminder) |
| **Version** | 0.2 (review fixes: decision after 15:30, no-QR is late, server clock, re-verify) |
| **Date** | 2026-11-18 |
| **Related ADR** | ADR-009, ADR-011 |
| **Depends on** | `commit-then-accept-flow.md` (Day-14 contract, `pay_deadline_at`, lender lock 4.7), `receipt-ocr.md`, `transfer-and-evidence.md` |
| **Source** | Loc, 18 Nov 2026: "New Day-14 transfer rule" (ops rule, no contract change). Playbook v3.22 section 10.11. Edge cases: design debt D-16 |

> **Edward: please read sections 4 and 6 first and comment on the PR.** Field names follow `commit-then-accept-flow.md`. Anything you'd rather name or store differently is fine as long as the API gives the UI the instants and codes in section 4.

---

## 1. Context & Goal

On Day 14 (disbursement day) lenders sign and pay the SME directly. Loc's 18 Nov rule makes the payment window precise and checkable:
- Signing and QR codes close at **14:45 ICT**.
- Every QR carries **its own code** (contract reference + 4 digits) and is valid for **15 minutes**. The lender can get a new QR, with a new code, until 14:45.
- A transfer is **on time** when the receipt shows the QR's code and a transaction time inside that QR's 15 minutes. OCR checks both.
- Receipts can be uploaded until **15:30**.
- **Late money** (an expired code, or a transfer with no QR) is never returned by the SME. It becomes that lender's **top-up**:
  - the lender is locked at **15:00**;
  - the platform records a top-up pledge for the amount received;
  - after unlocking, the lender signs the top-up on any day of the 7-day reopening, before 15:00;
  - the SME confirms that the money already arrived.

**Goal:** give the UI everything it needs to show this without guessing: the current code, its expiry, the cut-off instants and the late-money outcome. Give the backend one exact definition of "on time".

What exists today (18 Nov):
- The UI counts 15 minutes per QR **in the browser** (`fundlok-front-end` `TransferDetails.tsx`, `QR_VALIDITY_MS`), capped at the 2-hour `hold_expires_at`. "Get a new QR" restarts the timer but keeps the same memo.
- Nothing on the server knows about QR codes, 14:45, 15:00 or 15:30.

## 2. Out of Scope

- Contract wording. Loc: "ops rules only, no contract change". The contracts use "top-up" for *new* lenders filling a shortfall (Standing Offer Art. 5); this rule uses it for the late payer's own money. That gap is design debt D-16.
- The D-16 edge cases (e.g. part on time and part late, two transfers, a wrong amount). They go to staff review for now.
- Building the commit-then-accept flow itself (HANDOFF-06 Phase 2). This spec adds to it.
- SMS. In-app, email and Zalo are covered in section 6, rule 10.

## 3. Data Model (proposed; Edward's tables and migration)

### New table: `payment_qrs` (append-only)
```
id              UUID         PK
tranche_id      UUID         NOT NULL → tranches(id)
code            TEXT         NOT NULL UNIQUE   -- tranche.reference_code + 4 random digits, e.g. FLAB12CD344821
issued_at       TIMESTAMPTZ  NOT NULL
expires_at      TIMESTAMPTZ  NOT NULL          -- min(issued_at + 15 min, window.pay_deadline_at)
superseded_at   TIMESTAMPTZ  nullable          -- set when the lender asks for a new QR
```
- No separator in `code`: some banks drop punctuation from the memo. The VietQR memo limit is 50 ASCII characters (`vietqr.py:17`), and 14 characters fit.
- `code` must be unique across `payment_qrs`, `tranches.reference_code` and fee invoices. Extend `refs.py:12-19`.

### Modified table: `funding_windows` (commit-then-accept columns, values for this rule)
```
  pay_deadline_at      Day 14, 14:45 ICT   -- was 15:00 in commit-then-accept-flow.md; signing and QR issue close here
+ no_show_at           TIMESTAMPTZ  nullable   -- Day 14, 15:00 ICT: lock and top-up pledge run here
+ receipt_deadline_at  TIMESTAMPTZ  nullable   -- Day 14, 15:30 ICT: last receipt upload
```
If you'd rather compute `no_show_at` and `receipt_deadline_at` from `pay_deadline_at` plus offsets in `config.py`, that's fine. The API must still return the instants (4.3).

### Top-up pledge
A pledge created by the platform for late money. Two options, Edward's choice:
- a `commitments` row with a new `kind` = `'LATE_MONEY_TOP_UP'` and `source_transfer_id` → `transfers(id)`;
- or a separate table.

The UI only needs what section 4.5 returns.

### New table: `transfer_reminders` (Phat's job writes it; the migration is Edward's)
```
tranche_id   UUID         NOT NULL → tranches(id)
kind         TEXT         NOT NULL CHECK IN ('DAY14_MORNING','DAY14_AFTERNOON')
channel      TEXT         NOT NULL CHECK IN ('IN_APP','EMAIL','ZALO')
status       TEXT         NOT NULL CHECK IN ('SENT','FAILED','SKIPPED')
sent_at      TIMESTAMPTZ  NOT NULL
error        TEXT         nullable
PRIMARY KEY (tranche_id, kind, channel)
```
`acceptance_reminders` can't be reused: it is per window, and its CHECK only allows `D_MINUS_3` and `D_MINUS_1`.

## 4. API Contract (proposed)

All instants are ISO 8601 with an offset. The UI always shows them in ICT, whatever the browser's time zone.

### 4.1 Payment instructions: `GET /funding/tranches/{tranche_id}/payment` (changed)
Returns the **current** QR. The first GET after signing issues the first QR. Later GETs return the latest QR, even if it has expired (the UI shows the expired state).
```json
{
  "bank_name": "Techcombank", "bank_bin": "970407",
  "account_number": "19036789012", "account_name": "CONG TY DELTA FOODS",
  "amount_vnd": 20000000,
  "reference_code": "FLAB12CD34",
  "qr_code": "FLAB12CD344821",
  "vietqr_payload": "…memo=FLAB12CD344821…",
  "qr_issued_at": "2026-12-02T13:10:00+07:00",
  "qr_expires_at": "2026-12-02T13:25:00+07:00",
  "pay_deadline_at": "2026-12-02T14:45:00+07:00",
  "receipt_deadline_at": "2026-12-02T15:30:00+07:00",
  "hold_expires_at": "2026-12-02T14:45:00+07:00",
  "server_time": "2026-12-02T13:12:05+07:00",
  "warning": "…"
}
```
- After `pay_deadline_at`: `409 PAY_DEADLINE_PASSED`. The UI then reads the deadlines from the window (4.3).
- Every new field is nullable, so `LEGACY` windows and older backends keep working.

### 4.2 New QR: `POST /funding/tranches/{tranche_id}/qr` (new)
INVESTOR; the tranche must be the caller's and `SIGNED`, with no receipt yet.
- Returns `201` with the 4.1 body for a **new** code. It sets `superseded_at` on the previous QR.
- Errors:
  - `409 PAY_DEADLINE_PASSED`: after 14:45.
  - `409 TRANCHE_NOT_PAYABLE`: not `SIGNED`, or a receipt is already in.
  - `429 QR_TOO_SOON`: less than 60 s since the last QR, to avoid code spam.

### 4.3 Window: `FundingWindowOut` (extends `commit-then-accept-flow.md` 4.2)
```json
{ "disbursement_date": "2026-12-02",
  "pay_deadline_at": "…14:45…", "no_show_at": "…15:00…", "receipt_deadline_at": "…15:30…" }
```
All are null for `LEGACY` windows (see question 2).

### 4.4 Receipt: `POST /funding/tranches/{tranche_id}/receipt` (changed)
- After `receipt_deadline_at`: `409 RECEIPT_DEADLINE_PASSED`.
- `InvestorTrancheOut` gains:
  - `payment_timing`: `"ON_TIME" | "LATE" | "PENDING_CHECK" | null`
  - `late_reason`: `"EXPIRED_CODE" | "NO_QR_CODE" | "AFTER_CUTOFF" | null`

### 4.5 Standing and top-up pledges (extends `GET /funding/me/standing`, `commit-then-accept-flow.md` 4.7)
```json
{ "status": "GOOD|LOCKED|CLOSED", "locked_at": "…", "unlock_requires": ["EKYC","ACKNOWLEDGEMENT"],
  "top_up_pledges": [
    { "id": "uuid", "window_id": "uuid", "listing_title": "…", "amount_vnd": 20000000,
      "recorded_at": "…15:00…", "reopening_ends_at": "…",
      "status": "AWAITING_UNLOCK | AWAITING_SIGNING | SIGNED | SME_CONFIRMED | VOID" } ] }
```

### 4.6 Top-up signing (new; mirrors `commit-then-accept-flow.md` 4.4)
- `POST /funding/top-ups/{id}/contract/code` sends the one-time code. Same OTP rules as 4.4 there.
- `POST /funding/top-ups/{id}/contract` takes `{ "code": "123456", "click_to_sign": true }` and returns `201` with the pledge, `status: "SIGNED"`.
- Allowed only when standing is `GOOD`, within the reopening, and before 15:00 ICT on the day it's signed.
- Errors:
  - `409 LENDER_LOCKED`
  - `409 TOP_UP_SIGNING_CLOSED`: outside the reopening, or after 15:00.
  - the OTP errors from 4.4.

### 4.7 Notifications (`app/notifications/models.py`)
- `TRANSFER_DUE_TODAY` (new): to the lender on Day 14. `data`: `tranche_id`, `listing_title`, `amount_vnd`, `pay_deadline_at`.
- `TOP_UP_PLEDGE_RECORDED` (new): to the lender at 15:00. `data`: `pledge_id`, `amount_vnd`, `window_id`.
- `LENDER_LOCKED` (declared, unused today): to the lender at 15:00.

### 4.8 Reminder job (Phat): `POST /internal/jobs/day14-transfer-reminders`
Cloud Scheduler, Google OIDC, the same pattern as `/internal/jobs/repayment-reminders` (`app/scheduled_jobs/router.py:43-51`). Body: `{ "kind": "DAY14_MORNING" | "DAY14_AFTERNOON" }`. Idempotent per `transfer_reminders` primary key.

## 5. State Machine

**QR** (`payment_qrs`): `ISSUED` → `SUPERSEDED` (the lender asked for a new QR) or `EXPIRED` (`now ≥ expires_at`). These states are derived; there is no status column.

**Lender's Day 14 (ICT)**

| Time | Sign contract | Get / renew QR | Upload receipt | What the UI shows |
|---|---|---|---|---|
| before 14:45 | yes | yes, each valid 15 min | yes | QR, code, countdown, "Get a new QR" |
| 14:45 – 15:00 | no | no | yes | "Disbursement closed. Unlock your account to join the top-up" (once locked); receipt upload until 15:30 |
| 15:00 | — | — | yes | unpaid and late lenders are locked (nothing else yet) |
| after 15:30 (15:35 run) | — | — | no | decision: no receipt → no-show (counted); clean late money → top-up pledge; receipt proved in time → unlocked |
| after 15:30 | — | — | no | closed; pledge and unlock steps in the investments area |

## 6. Business Rules

1. A QR is valid from `issued_at` for 15 minutes, never past `pay_deadline_at` (14:45).
2. A new QR supersedes the previous one. Whether a superseded code still counts is open question 1.
3. **On time** = the receipt's memo, normalised (uppercase, spaces and punctuation removed), **equals** a valid QR code for that tranche, **and** the transaction time falls inside that QR's `[issued_at, expires_at]`. Today's check is a substring match (`receipts.py:134`), so `FLAB12CD34` + any 4 digits would pass; it must become an exact match.
4. OCR reads a time shown without seconds as the end of that minute (`bank_receipt.py:191-202`, `:59`). For rule 3, treat such a time as the whole minute `[hh:mm:00, hh:mm:59]` and count it on time if that minute overlaps the QR window.
5. Late money = a receipt that fails rule 3 (an expired or superseded code, no code, a ticket that never showed a QR, or paid after 14:45). It is never refunded by the SME. At 15:00 (`no_show_at`) the lender is locked. After the 15:30 receipt cut-off, a late receipt that matched in every other way (under review as `LATE_MONEY`, no open dispute) becomes a top-up pledge for the amount received; anything messier stays with staff (D-16).
6. A lender with no receipt at 15:00 is locked; if there is still none after 15:30 the ticket expires as a no-show (`commit-then-accept-flow.md` rule 7), counted once, with no pledge. A lender whose receipt, uploaded between 15:00 and 15:30, proves they paid in time is unlocked again.
7. A top-up pledge can be signed only after unlocking, on any day of the 7-day reopening, before 15:00 ICT. The SME then confirms the money already arrived, through the existing SME receipt/confirm path.
8. Receipts upload until 15:30 (`receipt_deadline_at`). After that the upload is refused, and the lender contacts support.
9. The ARQ worker isn't deployed, so the 15:00 lock and pledge need a trigger: a Cloud Scheduler endpoint in `app/scheduled_jobs` (Phat) calling Edward's service function, or a lazy check on each request. Pick one (question 6).
10. Reminders: on Day 14, at 08:00 (`DAY14_MORNING`) and 14:00 ICT (`DAY14_AFTERNOON`), to lenders whose tranche is `RESERVED`/`SIGNED` with no receipt, on windows whose `disbursement_date` is today.
    - **Channels:** in-app (`create_notification` + realtime), email, and Zalo. Zalo needs a new ZBS template (`company_name`, `amount`, `deadline` and a button to FundLok; deliberately no reference code, since a Day-14 transfer must use a QR's own code). Sending is off until `ZALO_ZBS_DAY14_TEMPLATE_ID` is set.
    - **Copy:** vi "Hôm nay là ngày giải ngân. Hãy ký và chuyển khoản trước 14:45." / en "Today is disbursement day: sign and pay by 14:45."

11. The app counts QR time on the server's clock (`server_time` in 4.1), not the device's.
12. Unlocking needs an eKYC approved after the lock. `GET /gverify/kyc/status` returns `reverify_required: true` while that is missing, and `POST /gverify/kyc/verify` accepts a new attempt from an approved investor exactly then.

## 7. Error Cases

| Status | Code | Where | Condition |
|---|---|---|---|
| 409 | `PAY_DEADLINE_PASSED` | GET payment, POST qr, sign contract | after `pay_deadline_at` (14:45) |
| 409 | `TRANCHE_NOT_PAYABLE` | POST qr | not `SIGNED`, or a receipt is already in |
| 429 | `QR_TOO_SOON` | POST qr | under 60 s since the last QR |
| 409 | `RECEIPT_DEADLINE_PASSED` | POST receipt | after `receipt_deadline_at` (15:30) |
| 409 | `LENDER_LOCKED` | top-up signing, new commitments | standing is `LOCKED` |
| 409 | `TOP_UP_SIGNING_CLOSED` | top-up signing | outside the reopening, or after 15:00 that day |

## 8. Acceptance Criteria

Frontend (Phat, Vitest + Playwright on the stub API):
- `test_transfer_shows_server_qr_code_and_countdown`: the memo row shows `qr_code`; the countdown runs to `qr_expires_at`.
- `test_transfer_falls_back_to_browser_15_minutes_without_qr_fields`: a `LEGACY` window or an old backend behaves as today.
- `test_new_qr_calls_endpoint_and_shows_new_code`.
- `test_new_qr_handles_qr_too_soon_and_pay_deadline_passed`.
- `test_after_pay_deadline_hides_qr_and_shows_disbursement_closed`.
- `test_receipt_upload_closes_at_receipt_deadline`, including the `RECEIPT_DEADLINE_PASSED` message.
- `test_times_render_in_ict_whatever_the_browser_time_zone`.
- `test_transfer_due_today_notification_says_sign_and_pay_by_1445`.

Backend (Edward, pytest):
- `test_qr_code_is_reference_plus_4_digits_and_unique`.
- `test_qr_expires_after_15_minutes_and_never_after_pay_deadline`.
- `test_new_qr_supersedes_previous`.
- `test_qr_refused_after_1445`.
- `test_on_time_needs_exact_code_and_time_inside_window`.
- `test_minute_only_receipt_time_counts_if_minute_overlaps`.
- `test_late_money_locks_lender_and_records_top_up_pledge_at_1500`.
- `test_no_transfer_locks_without_pledge`.
- `test_receipt_refused_after_1530`.
- `test_top_up_signable_only_after_unlock_before_1500_within_reopening`.

Reminder (Phat, pytest):
- `test_day14_reminder_targets_unpaid_lenders_on_disbursement_day`.
- `test_day14_reminder_sends_once_per_tranche_kind_channel`.
- `test_day14_reminder_skips_zalo_without_template_id`.

## 9. Open Questions

1. **Loc:** "the current code". If a lender pays with QR 1 inside its 15 minutes but has already asked for QR 2, is the payment on time? Proposal: yes. The code is real and the time is inside its window, so `superseded_at` doesn't make it late.
2. **Loc / Edward:** do the 14:45, 15:00 and 15:30 cut-offs apply to `LEGACY` windows, which are live today and use a 2-hour hold on any day? Proposal: per-QR codes and the 15 minutes apply to both flows. The cut-offs apply only where `pay_deadline_at` is set, i.e. `COMMIT_THEN_ACCEPT` windows.
3. **Loc:** reminder times on Day 14. Proposal: 08:00 and 14:00 ICT.
4. **Loc:** is unlocking still a new eKYC plus the undertaking sentence (`commit-then-accept-flow.md` 4.7, Contract 4 Art. 5.4)?
5. **Edward:** explicit `no_show_at` and `receipt_deadline_at` columns, or offsets in `config.py`?
6. **Edward:** the 15:00 lock and pledge trigger: Cloud Scheduler endpoint (rule 9) or lazy?
7. **Edward:** top-up pledge storage: `commitments.kind` or a new table?
