# Spec: Commit-then-accept funding flow

| Field | Value |
|---|---|
| **Status** | **IMPLEMENTED** (build slices S1 to S6 merged; Loc's end-to-end Appendix M re-run on `main` 6977af7 passed, 2026-11-21). Provisional until handbook v3.2 (D-052); Loc's open points are in section 9 |
| **Owner** | Edward |
| **Implementer(s)** | Edward's modules: a Claude Sonnet implementer (HANDOFF-06 Phase 2). Phat: routers in `app/market`, all UI, notification wording (D-051) |
| **Module** | `app/direct_transfer/` (backend), `app/market/funding_router.py` and `app/market/funding.py` (Phat's routers, calling the service functions here), `app/market/investor_agreement/` (shared, section 3) |
| **Version** | 1.4 (unlock sentence: the two locked sentences, language stored, SCRUM-612; 1.3 was late Day-14 tickets after formation, rules 8 and 9; 1.2 was status only; 1.1 is the Money flow FINAL v1.0 amendment, see the box below) |
| **Date** | 2026-11-15 |
| **Related ADR** | ADR-011 (this spec implements it), ADR-009 |
| **Depends on** | `end-to-end-payment-flow.md` (4 Nov design), `funding-window.md`, `transfer-and-evidence.md`, `fees.md`, `ledger-amendment.md`, D-036 to D-057, `docs/appendix-m/appendix-m-remediation-decisions.md` (A1 to A7) |
| **Replaces** | The pay-at-commit behaviour of ADR-009 section 2 and `funding-window.md` rules on the 2-hour hold, stub interest and `repay_from_approval` for windows created under this flow |

> **Money flow FINAL v1.0 (Loc, locked 17 Nov 2026) prevails over this spec where they differ.** Version 1.1 applies it: (a) a pledge is pressing Confirm and signs nothing, so the Lender Service Agreement is **not** a gate at commit; it is signed with the loan contract, by one Zalo OTP, on the lender's first Day 14; (b) a lender may **withdraw** a pledge until the end of Day 12; (c) pledges close early when the target is reached, a withdrawal reopens them, and **Day 13 and Day 14 never move** (open questions 1 and 2 answered); (d) Day 14 follows the Day-14 transfer rule (`day14-transfer-rule.md`): signing and new QRs close at 14:45 (`pay_deadline_at`), the lock is at 15:00 (`no_show_at`), receipts close at 15:30 (`receipt_deadline_at`).

> **For Phat: read sections 4 (API contract) and 5 (state machine) first.** Section 3 lists what changes in the database. Every endpoint below is in the `/funding` router you own; the service functions behind them are Edward's. Anything you disagree with: comment on the PR before building.

---

## 1. Context & Goal

The code follows ADR-009 section 2: a lender reserves a ticket, signs and pays within 2 hours, and each confirmed ticket becomes its own loan. The handbook (10.1 to 10.10), the signed Standing Offer and Loc's 4 Nov design describe a different flow, and the Appendix M test (14 Nov) failed 18 steps because of the gap. Loc confirmed the 4 Nov flow for launch on 14 and 15 Nov (D-036, D-037).

**Goal:** a lender commits with no payment during Days 0 to 12; the SME accepts or declines by 15:00 on Day 13; on Day 14 each lender signs one loan contract for its total and pays the SME before 15:00; one pooled schedule starts for everyone who paid.

**Design rule that keeps the change small.** A *commitment* is a new, earlier object. A *tranche* stays what it is (one per lender per window, money-bearing, with the existing states RESERVED → SIGNED → ATTESTED → CONFIRMED → ACTIVE), but it is now **created at acceptance** and holds the lender's total. Ledger posting, receipts, disputes, CIC events and repayment days all key on the tranche and need no change.

## 2. Out of Scope

- Late status and late interest (`late-status.md` and Phase 3), automatic extension (Phase 3).
- The no-show shortfall reopening for 7 days and the top-up schedule (Phase 3, gap register B6).
- Account CLOSED status for misconduct (Phase 3), T&S and contract wording (Phat, D-043).
- A real lender signature provider (the mock signer stays; OTP plus a logged click is the launch method, A7).
- SMS (D-056): only the sender interface and a mock.
- Public holidays: ignored everywhere (D-046). Weekends do not move Day 13 or Day 14 (A2).
- Removing the legacy flow (section 9, question 8).

## 3. Data Model

All changes ship in one new Alembic revision off the current single head. Existing rows keep working: windows created before this change get `flow = 'LEGACY'` and keep the old behaviour until the legacy path is removed.

### New table: `commitments`

```
commitments
├── id                     UUID         PK
├── window_id              UUID         NOT NULL → funding_windows(id)
├── investor_id            UUID         NOT NULL → users(id)
├── investor_account_id    UUID         nullable → payment_accounts(id)   -- the account the lender will pay from
├── investor_type          TEXT         NOT NULL, CHECK IN ('INDIVIDUAL','ENTITY')
├── amount_vnd             BIGINT       NOT NULL, CHECK > 0
├── status                 TEXT         NOT NULL, default 'PLEDGED', CHECK IN ('PLEDGED','FOLDED','VOID')
├── void_reason            TEXT         nullable   -- DECLINED, AUTO_DECLINED, NO_SHOW, WINDOW_CANCELLED
├── tranche_id             UUID         nullable → tranches(id)           -- set when folded at acceptance
├── idempotency_key        TEXT         nullable, UNIQUE
├── request_fingerprint    TEXT         nullable
├── pledge_file_key        TEXT         NOT NULL   -- the signed pledge document
├── pledge_sha256          TEXT         NOT NULL
├── pledge_certificate     JSONB        NOT NULL
├── committed_at           TIMESTAMPTZ  NOT NULL
└── created_at             TIMESTAMPTZ  default now()
Index: ix_commitments_window_status (window_id, status); ix_commitments_window_investor (window_id, investor_id)
```
No unique index on (window, investor): a lender may commit several times (D-039, A6).

### Modified table: `funding_windows`

```
+ flow                TEXT         NOT NULL, default 'LEGACY', CHECK IN ('LEGACY','COMMIT_THEN_ACCEPT')   -- new windows are COMMIT_THEN_ACCEPT
+ commit_closes_at    TIMESTAMPTZ  nullable   -- end of the commitment phase
+ sme_deadline_at     TIMESTAMPTZ  nullable   -- SME decision deadline (Day 13, 15:00 ICT)
+ pay_deadline_at     TIMESTAMPTZ  nullable   -- lenders sign and pay by this time (Day 14, 15:00 ICT)
+ sme_decision        TEXT         nullable, CHECK IN ('ACCEPTED','DECLINED','AUTO_ACCEPTED','AUTO_DECLINED')
+ sme_decided_at      TIMESTAMPTZ  nullable
+ sme_decided_by      UUID         nullable → users(id)
```
- `ck_funding_windows_status` gains `AWAITING_SME` and `AWAITING_PAYMENT`. `FULL` stays allowed but a `COMMIT_THEN_ACCEPT` window never enters it (section 5).
- The two partial unique indexes `uq_funding_windows_one_live_per_project` and `uq_funding_windows_one_live_per_sme` have `WHERE status IN (...)`. **Both must be dropped and recreated** with the live set `OPEN, FULL, CLOSING, AWAITING_SME, AWAITING_PAYMENT, REPAYING`. The code constants `WINDOW_LIVE_STATES` and the status tuples in `windows.py:32,82,308` change the same way.
- `window_end` stays NOT NULL and equals `commit_closes_at` for new windows.

### Modified table: `tranches`
One new column, `dispute_until TIMESTAMPTZ nullable` (migration `a4d2c7e9b130`): `confirmed_at + 30 days` when the ticket is confirmed automatically (24-hour silence, or the system's own receipt check), else null (section 4.6). Meaning changes for `COMMIT_THEN_ACCEPT` windows:
- created at acceptance, one per lender with pledged commitments, `amount_vnd` = the sum of the lender's folded commitments;
- `hold_expires_at` = `window.pay_deadline_at` (not 2 hours);
- the `acceptance_*` columns hold the Day-14 **loan contract** signature (the pledge is on `commitments`);
- `stub_*` columns stay 0: **no stub interest in this flow** (ADR-011 item 9; supersedes D-027 for these windows);
- no-show: state `EXPIRED`, reason recorded in `evidence_log` (`NO_SHOW`). The existing late-receipt path (`EXPIRED → UNDER_REVIEW`, dispute `LATE_RECEIPT`) is how staff waive a lock for force majeure.

### New table: `lender_standing`

```
lender_standing
├── investor_id            UUID         PK → users(id)
├── status                 TEXT         NOT NULL, default 'GOOD', CHECK IN ('GOOD','LOCKED','CLOSED')
├── no_show_count          INT          NOT NULL, default 0
├── locked_at              TIMESTAMPTZ  nullable
├── locked_window_id       UUID         nullable → funding_windows(id)
├── unlocked_at            TIMESTAMPTZ  nullable
├── closed_at              TIMESTAMPTZ  nullable
└── updated_at             TIMESTAMPTZ  default now()
```
`CLOSED` here is the permanent closure after a second no-show; the staff misconduct status (C19) is separate and later.

### New table: `lender_unlock_acknowledgements` (append-only)

```
├── id UUID PK · investor_id UUID NOT NULL · locked_window_id UUID NOT NULL
├── sentence_text TEXT NOT NULL      -- the text the lender typed, as typed
├── sentence_version TEXT NOT NULL   -- 'unlock-v2' from 1.4 (v1 rows are the old placeholder)
├── sentence_language TEXT NULL      -- 'vi' | 'en': which locked sentence it matched; NULL on v1 rows
├── ekyc_verification_id UUID NOT NULL   -- the re-eKYC that allowed the unlock
├── acknowledged_at TIMESTAMPTZ NOT NULL · ip_address TEXT · user_agent TEXT
```

### New table: `acceptance_reminders`

```
├── window_id UUID NOT NULL · kind TEXT CHECK IN ('D_MINUS_3','D_MINUS_1') · channel TEXT CHECK IN ('IN_APP','ZALO','EMAIL','SMS')
├── status TEXT CHECK IN ('SENT','FAILED','SKIPPED') · sent_to TEXT nullable · error TEXT nullable · sent_at TIMESTAMPTZ
└── PK (window_id, kind, channel)
```

### Modified table: `investor_agreement_codes` (Phat's module; shared)
The Lender Service Agreement is now signed at the **first commitment**, when no tranche exists yet, and the Day-14 contract needs a code too.
```
~ tranche_id   UUID   -- becomes nullable
+ window_id    UUID   nullable → funding_windows(id)
+ purpose      TEXT   NOT NULL, default 'SERVICE_AGREEMENT', CHECK IN ('SERVICE_AGREEMENT','LOAN_CONTRACT')
```
`purpose = 'LOAN_CONTRACT'` requires `tranche_id`. `_check_can_sign` (`investor_agreement/service.py:275`) drops its RESERVED/SIGNED and hold-expiry test for `SERVICE_AGREEMENT`.

### Notifications (`notifications/models.py` `NOTIFICATION_EVENTS`)
Add: `COMMITMENT_RECEIVED`, `ACCEPTANCE_DUE_3D`, `ACCEPTANCE_DUE_1D`, `WINDOW_ACCEPTED`, `WINDOW_DECLINED`, `CONTRACT_SIGNING_OPEN`, `LENDER_LOCKED`, `TRANCHE_CONFIRMED`, `LOAN_FORMED`. A new event not in the frozenset raises `ValueError`.

### Alembic migration
`alembic/versions/<hash>_commit_then_accept_flow.py`, chained off the current head. `downgrade` refuses if any `COMMIT_THEN_ACCEPT` window exists.

## 4. API Contract

All routes are under `/funding` (`app/market/funding_router.py`). Investor routes keep the existing gate `_require_eligible_investor` (verified email, approved KYC, approved bank account). Errors are `{"detail": "<message>"}` with the code in the `X-Error-Code` header, as today. Money is integer VND. Times are ISO 8601 with offset (ICT).

### 4.1 Lender: commit (replaces `POST /windows/{id}/reservations` for new windows)

`POST /funding/windows/{window_id}/commitments` · **Auth:** INVESTOR · **Header:** `Idempotency-Key: <uuid>` (required)

```json
// request
{
  "amount_vnd": 20000000,            // required; at least min_ticket_vnd (10M) or exactly the remaining capacity
  "ack_risk_disclosure": true,       // required, must be true
  "terms_version": "string",         // required, the current INVESTOR_TERMS_VERSION
  "payment_account_id": "uuid"       // required, an APPROVED account of the lender
}
// response 201
{
  "id": "uuid", "window_id": "uuid", "amount_vnd": 20000000, "status": "PLEDGED",
  "committed_at": "2026-11-02T09:30:00+07:00",
  "my_committed_total_vnd": 30000000,      // all PLEDGED commitments of this lender in this window
  "window_committed_total_vnd": 60000000,
  "window_target_vnd": 100000000
}
```

| Status | Code | Condition |
|---|---|---|
| 409 | `WINDOW_NOT_OPEN` | window not in `OPEN` (the commitment phase has ended) |
| 409 | `LENDER_LOCKED` | no-show lock (unlock first) |
| 409 | `LENDER_CLOSED` | permanently closed after a second no-show |
| 409 | `INVESTOR_FEE_UNPAID` | an investor fee invoice is more than 7 days past issue |
| 409 | `BELOW_MIN_TICKET` | below the minimum and not exactly the remaining capacity |
| 409 | `WINDOW_FULL` | the pledges already reach the target (it reopens if a lender withdraws before the end of Day 12) |
| 409 | `CAPACITY_EXCEEDED` | above remaining capacity |
| 409 | `BORROWER_CAP_EXCEEDED` | unchanged check at window opening only; not re-run per commitment |
| 400 | `RISK_ACK_REQUIRED`, `TERMS_OUTDATED`, `ACCOUNT_NOT_APPROVED` | as today |
| 409 | `IDEMPOTENCY_CONFLICT` | same key, different request |

Removed for this flow: `ALREADY_HAS_LIVE_TRANCHE`, `HOLD_EXPIRED`, `INVESTOR_AGREEMENT_REQUIRED` (v1.1). When the commitments reach the target the window stays `OPEN` but takes no more pledges; it moves to `AWAITING_SME` only at `commit_closes_at`.

`GET /funding/me/commitments?window_id=<uuid>` · INVESTOR · `200 list[CommitmentOut]` (the object above, newest first).

`POST /funding/commitments/{commitment_id}/withdraw` · INVESTOR (the pledge's owner) · `200 CommitmentOut` with `status: "VOID"` (`void_reason = WITHDRAWN`). Allowed while the window is `OPEN` and before `commit_closes_at`; a replay returns the withdrawn pledge. `404` for someone else's pledge, `409 WITHDRAW_CLOSED` after the end of Day 12, `409 COMMITMENT_NOT_PLEDGED` otherwise.

### 4.2 Window progress (extends `FundingWindowOut`, `GET /funding/listings/{id}/window`)

New fields, all nullable for `LEGACY` windows:
```json
{
  "flow": "COMMIT_THEN_ACCEPT",
  "status": "OPEN | AWAITING_SME | AWAITING_PAYMENT | CLOSING | REPAYING | FAILED | REPAID | CANCELLED",
  "commit_closes_at": "iso", "sme_deadline_at": "iso", "pay_deadline_at": "iso",
  "committed_total_vnd": 60000000, "committed_pct": 60,
  "sme_decision": "ACCEPTED | DECLINED | AUTO_ACCEPTED | AUTO_DECLINED | null",
  "sme_decided_at": "iso | null"
}
```

### 4.3 SME: decide (new)

`POST /funding/windows/{window_id}/decision` · **Auth:** SME (the window's owner) · **Header:** `Idempotency-Key` (required)
```json
{ "decision": "ACCEPT" }      // or "DECLINE"
```
`200 FundingWindowOut`. `ACCEPT` below 50% of target is allowed (manual accept, A3).

| Status | Code | Condition |
|---|---|---|
| 409 | `DECISION_NOT_OPEN` | window not in `AWAITING_SME` (commit phase still running, or already decided) |
| 409 | `DECISION_DEADLINE_PASSED` | after `sme_deadline_at` (the automatic result stands) |
| 403 | — | not the window's SME |

### 4.4 Lender: Day-14 contract (new; replaces signing at commit)

All three: INVESTOR, and the tranche must be the caller's, state `RESERVED`, window `AWAITING_PAYMENT`, now ≤ `pay_deadline_at`.

- `GET /funding/tranches/{tranche_id}/contract` → the loan contract PDF for the lender's total (one contract, all folded commitments listed).
- `POST /funding/tranches/{tranche_id}/contract/code` → `200 { "sent_to_masked": "090***1234", "expires_in_seconds": 300, "resend_after_seconds": 60 }`. Same OTP rules as the Lender Service Agreement (5 min, 60 s cooldown, 5 per day, 5 attempts).
- `POST /funding/tranches/{tranche_id}/contract` (201):
```json
{ "code": "123456", "click_to_sign": true }     // both required
```
`201 InvestorTrancheOut` with `status: "SIGNED"`, `contract_signed_at`, `pay_by` (= `pay_deadline_at`). Writes the signature evidence (OTP verified, click, time, IP, user agent). Errors: `CODE_INVALID`, `CODE_EXPIRED`, `CODE_COOLDOWN`, `CODE_DAILY_LIMIT`, `PHONE_REQUIRED`, `PHONE_NO_ZALO`, `CODE_NOT_SENT`, `CONTRACT_DEADLINE_PASSED` (409), `TRANCHE_NOT_SIGNABLE` (409).

### 4.5 Lender: pay (changed gate only)

`GET /funding/tranches/{id}/payment` returns the VietQR (SME account, exact amount, unique reference) only once the tranche is `SIGNED`; before that `409 CONTRACT_REQUIRED` (was `INVESTOR_AGREEMENT_REQUIRED`). Payload unchanged. `POST /funding/tranches/{id}/receipt` is unchanged; a receipt after `pay_deadline_at` goes to the existing `LATE_RECEIPT` review.

### 4.6 SME: receipt check (new route for the existing service function)

`POST /funding/tranches/{tranche_id}/sme-receipt` · SME · multipart `file` (PDF/PNG/JPEG/WEBP, ≤ 10 MB) · `202 SmeTicketOut`
Stores the SME's own screenshot of the money arriving. When nothing can be read off it (OCR off, or an unsure read) the upload confirms on the SME's word, as pressing Received would. If amount, reference and bank transaction id match the lender's, the tranche goes `ATTESTED → CONFIRMED`; otherwise `UNDER_REVIEW` with a `RECEIPT_MISMATCH` dispute. Silence for 24 calendar hours after the lender's receipt is checked counts as received (`CONFIRMED`). `POST /funding/tranches/{id}/report-missing` (SME "Not received") stays as the dispute route and must be raised within the same 24 hours.

`POST /funding/tranches/{tranche_id}/dispute` · SME · multipart `statement` (text) and `file` (the bank statement; PDF/PNG/JPEG/WEBP, ≤ 10 MB; there is no separate evidence upload for the SME) → `202 SmeTicketOut`. For a tranche confirmed by silence: allowed until `confirmed_at + 30 days`; opens a dispute for staff. After that: `409 DISPUTE_WINDOW_CLOSED`. Each auto-confirmation exposes `dispute_until` (ISO) on `InvestorTrancheOut` and `SmeTicketOut` so the UI can show it beside the confirmation (A5).

### 4.7 Lender lock (new)

- `GET /funding/me/standing` → `200 { "status": "GOOD|LOCKED|CLOSED", "no_show_count": 0, "locked_at": null, "unlock_requires": ["EKYC", "ACKNOWLEDGEMENT"], "acknowledgement_sentence": "…" | null, "acknowledgement_sentences": {"vi": "…", "en": "…"} | null }`. `acknowledgement_sentence` stays (the Vietnamese one, which prevails) so the current screen keeps working; `acknowledgement_sentences` carries both, and the screen shows the one in the lender's language.
- `POST /funding/me/standing/unlock` → `{ "acknowledgement": "<sentence>" }` → `200` standing. Requires an `APPROVED` eKYC completed after `locked_at`, and the text matching **either** locked sentence. The language is whichever sentence matched (the request has no language field). Errors: `EKYC_REQUIRED`, `ACKNOWLEDGEMENT_MISMATCH` (wrong text is refused and nothing is stored), `NOT_LOCKED`, `LENDER_CLOSED`.
- **The two sentences** (locked by Loc 18 Nov 2026, SCRUM-604; version `unlock-v2`):
  - vi: "Tôi hiểu việc không có mặt giải ngân ảnh hưởng rất nhiều đến doanh nghiệp, tôi cam kết sẽ đầu tư chuẩn mực"
  - en: "I realise no-showing has a big effect on the SME. I commit to investing properly."
- **Matching** (one function, so the rule changes in one place): PROVISIONAL(Q-1.4): the text is compared after trimming, collapsing runs of spaces and ignoring case and accents, as the unlock has always been compared. Strict exact matching is the alternative, waiting on Loc's answer (section 9, question 10).
- **Recorded on success** (`lender_unlock_acknowledgements`): the text as typed, the language, the version, the time, the eKYC used, IP and user agent. Nothing is recorded on a refusal.

### 4.8 Staff (`/admin`, `require_admin`)

- `POST /admin/funding/rounds/{window_id}/advance` unchanged: it runs the idempotent `advance_window` (section 6 rule 12) for that round.
- `POST /admin/funding/lenders/{investor_id}/waive-lock` → `{ "window_id": "uuid", "reason": "FORCE_MAJEURE|BANK_OUTAGE", "note": "string (required)", "evidence_file_id": "uuid|null" }` → `200`. Lifts the lock, does not count the miss, audited with the staff member.
- Existing review routes (`/admin/funding/transfers/...`, `/admin/funding/reviews/{id}/decision`) are unchanged.

### 4.9 Removed or refused for `COMMIT_THEN_ACCEPT` windows
`POST /funding/windows/{id}/reservations` → `409 USE_COMMITMENTS`. `POST /funding/windows/{id}/close-early` stays for `LEGACY` only; here the SME decides instead (`409 USE_DECISION`).

## 5. State Machine

### Window (`funding_windows.status`, `COMMIT_THEN_ACCEPT`)

```
OPEN ──[commitments reach target, or commit_closes_at]──────────► AWAITING_SME
AWAITING_SME ──[SME ACCEPT, or auto-accept at sme_deadline_at when committed ≥ 50%]──► AWAITING_PAYMENT
AWAITING_SME ──[SME DECLINE, or auto-decline at sme_deadline_at when committed < 50%]─► FAILED   (close_reason DECLINED | AUTO_DECLINED)
AWAITING_PAYMENT ──[pay_deadline_at]────────────────────────────► CLOSING
CLOSING ──[no tranche pending, or close_wait_until]─────────────► REPAYING   (or FAILED if no tranche became ACTIVE)
OPEN | AWAITING_SME | AWAITING_PAYMENT ──[staff]───────────────► CANCELLED
REPAYING ──[last day settled]──────────────────────────────────► REPAID
```
`FULL` is not used. On `FAILED` by decline: every commitment is `VOID` (`DECLINED` or `AUTO_DECLINED`), no tranche exists, no money moved, no fee is issued, the listing goes `CANCELLED`, and every lender is notified (`WINDOW_DECLINED`). The one-borrower rule then closes the project (`one-borrower.md` rule 3), so the SME may re-apply straight away.

### Commitment
```
PLEDGED ──[window accepted]──► FOLDED   (tranche_id set)
PLEDGED ──[window declined / auto-declined / cancelled]──► VOID
FOLDED  ──[tranche EXPIRED as a no-show]──► VOID   (void_reason NO_SHOW)
```

### Tranche (unchanged names; new triggers)
```
(created at acceptance) RESERVED ──[lender signs the Day-14 contract]──► SIGNED
SIGNED ──[receipt matches: OCR or staff]──► ATTESTED
ATTESTED ──[SME receipt matches, or 24 h silence, or staff]──► CONFIRMED
CONFIRMED ──[loan formation at close]──► ACTIVE
RESERVED | SIGNED (no receipt) ──[pay_deadline_at]──► EXPIRED   (NO_SHOW: lock, commitments VOID)
SIGNED | ATTESTED ──[mismatch / SME dispute]──► UNDER_REVIEW ──► CONFIRMED | VOID
EXPIRED ──[late receipt]──► UNDER_REVIEW   (LATE_RECEIPT; staff may waive the lock)
```
A tranche with a receipt submitted before the deadline is **not** a no-show, even if unreviewed.

### Lender standing
```
GOOD ──[no-show]──► LOCKED ──[re-eKYC + exact acknowledgement]──► GOOD
LOCKED ──[staff waives: force majeure / bank outage]──► GOOD   (miss not counted)
GOOD | LOCKED ──[second counted no-show]──► CLOSED   (permanent)
```

## 6. Business Rules

1. **Timeline.** Day 0 is the ICT date the window opens. The commitment phase ends at the end of Day 12 (`commit_closes_at`), `sme_deadline_at` is 15:00 on Day 13, and on Day 14 `pay_deadline_at` is 14:45, `no_show_at` 15:00 and `receipt_deadline_at` 15:30 (Day-14 transfer rule). All are calendar days (D-046). A2: weekends do not move them. Reaching the target early closes pledges but moves no deadline (Money flow FINAL v1.0).
2. **Commit (A6).** Minimum 10M per commitment, or exactly the remaining capacity. Total of PLEDGED commitments ≤ target. Several commitments per lender are allowed. No money moves, no payment details are shown. Each commitment stores a pledge record through the platform signer (evidence of the Confirm; the lender signs nothing). A pledge may be withdrawn until the end of Day 12; a withdrawal frees its amount and reopens a full round. The Lender Service Agreement is signed once, with the Day-14 loan contract, by the same Zalo OTP (v1.1).
3. **Gates at commit.** Eligible investor, approved payment account, risk acknowledgement, current terms, `lender_standing.status = GOOD`, no investor fee invoice more than 7 days past issue (D-047), the SME is not the investor.
4. **Accept (A3).** An explicit SME `ACCEPT` before `sme_deadline_at` always wins, at any percentage. Silence at the deadline: committed ≥ 50% of target counts as `AUTO_ACCEPTED`, below 50% as `AUTO_DECLINED`. An explicit `DECLINE` before the deadline always wins. After the deadline the automatic result stands.
5. **At acceptance** the system creates one tranche per lender with PLEDGED commitments: amount = their sum, a unique reference code, `hold_expires_at = pay_deadline_at`, state `RESERVED`; the commitments become FOLDED; every lender is notified (`CONTRACT_SIGNING_OPEN`). `s`, the rate and the term stay as in the offer even if the round is not full (A4).
6. **Day-14 contract (A1b, A7).** One loan contract per lender for its total; signed with an OTP code plus a logged click; the VietQR appears only after signing. The Lender Service Agreement is not re-signed.
7. **No-show (A6).** At `pay_deadline_at` every tranche still `RESERVED`, or `SIGNED` with no receipt, becomes `EXPIRED` (`NO_SHOW`); its commitments become VOID; the lender is `LOCKED` and `no_show_count` increases. At the second counted no-show the lender is `CLOSED` permanently. A staff waiver (force majeure or bank outage) lifts the lock without counting the miss. Shortfall reopening and top-up are Phase 3.
8. **SME receipt check (A5).** The SME has 24 calendar hours from the moment a lender's receipt is checked to confirm (by uploading its own receipt), dispute, or stay silent (counts as received). A 30-day dispute with a bank statement stays open after any confirmation by silence; staff decide under Finance's interim policy, then the bank's trace decides. The silence lapse runs on every `funding-advance` run for every round, the loan formed or not (`cloud-scheduler-jobs.md`). A ticket confirmed after the loan formed without it (it was still `ATTESTED` at formation, so `excluded_at_close`), whether by silence, by the SME or by staff approving it, never joins: it becomes `VOID` (`EXCLUDED_AT_CLOSE`) and the SME owes the lender a tracked refund (PROVISIONAL(Q-D7), as for a review decided after formation). A ticket confirmed before formation is left as it is and joins the loan.
9. **Loan formation.** At `CLOSING`, once no tranche is pending (or `close_wait_until`, default 2 calendar days after `pay_deadline_at`; open question 6), one pooled facility forms from the CONFIRMED tranches: same rate, start date, end date and payment day for every lender; each lender keeps its own tranche, holding and contract. Start date = the first working day after formation (Monday to Friday). **No stub interest.** The loan still forms if the confirmed total is below 50% after no-shows (A4; open question 5). If no tranche is CONFIRMED the window is `FAILED`, no fee. A lender locked at 15:00 on Day 14 whose ticket joins the loan paid in time, so formation unlocks them (`day14-transfer-rule.md` rule 6), unless another round locked them or something else on this round still holds them.
10. **Fees.** SME 3% activation invoice is issued at formation on the confirmed total (unchanged, `fees.py`); never on a window that failed. Investor fee formula unchanged.
11. **Ledger.** Unchanged. Claim legs post when a tranche becomes ACTIVE, from `tranche.amount_vnd` (final at that point). No cash account, no leg before ACTIVE.
12. **Jobs are idempotent and time-driven.** One function, `advance_window(window_id, now)`, performs every due transition for a window: end of the commit phase, the SME auto-decision, the no-show cut-off, the 24-hour silence lapse, the `CLOSING` finalize. The Cloud Scheduler endpoints (D-049) and the staff Advance button both call it; running it twice changes nothing. The 2-hour `expire_holds_job` does not apply to this flow.
13. **Reminders (D-056).** 3 days and 1 day before `sme_deadline_at`, the SME is reminded by in-app, Zalo and email (SMS only through the mock sender), each logged in `acceptance_reminders`. A reminder is skipped, and logged as such, if the window is already decided.
14. **Notifications** to lenders: commitment received, window accepted or declined, signing open, locked, confirmed, loan formed. Each channel send is logged.
15. **Audit.** Every commitment, decision, signature, lock, unlock and waiver writes `evidence_log` and an audit row with the actor and time.
16. **Idempotency.** Commit, decision, contract signing and unlock accept `Idempotency-Key`; a replay returns the first result.

## 7. Error Cases

| Scenario | System behaviour | Response |
|---|---|---|
| Lender commits after the commit phase | Refused | 409 `WINDOW_NOT_OPEN` |
| Two commitments at the same moment fill the target | Window row locked; the second gets the remaining capacity or `CAPACITY_EXCEEDED` | 201 or 409 |
| SME never decides, 60% committed | Auto-accept at `sme_deadline_at` | window `AWAITING_PAYMENT`, `sme_decision = AUTO_ACCEPTED` |
| SME never decides, 40% committed | Auto-decline, commitments VOID, no fee | window `FAILED` |
| SME accepts at 40% before the deadline | Allowed | window `AWAITING_PAYMENT` |
| SME declines at 90% | Declined; no tranche, no money | window `FAILED` |
| Lender signs after 15:00 on Day 14 | Tranche already `EXPIRED` | 409 `CONTRACT_DEADLINE_PASSED` |
| Lender uploads a receipt at 14:59, unreviewed at 15:00 | Not a no-show | tranche stays `SIGNED`/`ATTESTED` |
| Lender uploads a receipt after the deadline | `EXPIRED → UNDER_REVIEW` (`LATE_RECEIPT`); staff may waive the lock | 202 |
| Lender with a lock tries to commit | Refused | 409 `LENDER_LOCKED` |
| Same lender no-shows twice | Second miss closes the account | `CLOSED`; 409 `LENDER_CLOSED` thereafter |
| `advance_window` runs twice, or the scheduler and staff overlap | Second run finds nothing due | no change |
| Two tranches' receipts are both unresolved at `close_wait_until` | Those tranches are excluded and the loan forms without them (existing `excluded_at_close`) | — |
| An excluded tranche is confirmed after formation (its 24 hours lapse, or staff approve it) | `VOID` (`EXCLUDED_AT_CLOSE`); a refund transfer the SME owes is recorded (rule 8) | — |
| A dispute raised on day 29 after silence-confirmation | Opens for staff | 202 |
| A dispute raised on day 31 | Refused | 409 `DISPUTE_WINDOW_CLOSED` |
| Window created before this change | `flow = LEGACY`, old behaviour | unchanged |

## 8. Acceptance Criteria

`tests/direct_transfer/test_commit_then_accept.py` unless noted. Each line is a pytest name.

- [ ] `test_commitment_moves_no_money_and_creates_no_tranche`
- [ ] `test_a_lender_can_commit_several_times_to_one_window`
- [ ] `test_commitment_below_min_ticket_refused_unless_exact_remainder`
- [ ] `test_commitments_above_capacity_refused`
- [ ] `test_a_full_round_takes_no_more_pledges_until_a_withdrawal_reopens_it`
- [ ] `test_lender_can_withdraw_a_pledge_until_the_end_of_day_12`
- [ ] `test_window_moves_to_awaiting_sme_at_commit_closes_at`
- [ ] `test_commitment_needs_no_signed_service_agreement` (`tests/market/`)
- [ ] `test_locked_lender_cannot_commit`
- [ ] `test_lender_with_fee_invoice_unpaid_after_seven_days_cannot_commit`
- [ ] `test_sme_accept_below_fifty_percent_is_allowed_before_the_deadline`
- [ ] `test_silence_at_fifty_percent_or_more_auto_accepts`
- [ ] `test_silence_below_fifty_percent_auto_declines_with_no_loan_no_money_no_fee`
- [ ] `test_explicit_sme_decision_before_deadline_overrides_the_automatic_result`
- [ ] `test_sme_decision_after_deadline_is_refused`
- [ ] `test_acceptance_creates_one_tranche_per_lender_with_summed_amount`
- [ ] `test_acceptance_folds_commitments_and_sets_hold_to_pay_deadline`
- [ ] `test_payment_instructions_refused_until_contract_signed`
- [ ] `test_contract_signing_needs_otp_and_click_and_logs_both`
- [ ] `test_contract_signing_after_pay_deadline_is_refused`
- [ ] `test_unsigned_or_unpaid_tranche_expires_at_pay_deadline_as_no_show`
- [ ] `test_receipt_uploaded_before_deadline_is_not_a_no_show`
- [ ] `test_no_show_locks_the_lender_and_voids_its_commitments`
- [ ] `test_second_no_show_closes_the_lender_permanently`
- [ ] `test_unlock_needs_fresh_ekyc_and_the_exact_acknowledgement`
- [ ] `test_staff_waiver_lifts_lock_without_counting_the_miss`
- [ ] `test_sme_receipt_match_confirms_the_tranche`
- [ ] `test_silence_for_24_hours_confirms_the_tranche`
- [ ] `test_dispute_after_silence_is_open_for_30_days_with_bank_statement`
- [ ] `test_loan_forms_once_pooled_with_same_rate_start_end_and_payment_day`
- [ ] `test_each_lender_keeps_its_own_tranche_and_contract`
- [ ] `test_no_stub_interest_in_commit_then_accept_windows`
- [ ] `test_loan_still_forms_when_confirmed_total_is_below_fifty_percent_after_no_shows`
- [ ] `test_window_with_no_confirmed_tranche_fails_with_no_fee`
- [ ] `test_sme_activation_invoice_issued_at_formation_on_confirmed_total`
- [ ] `test_ledger_claims_post_only_when_tranche_becomes_active`
- [ ] `test_a_ticket_attested_after_the_loan_formed_is_voided_with_a_refund_when_its_24_hours_pass`
- [ ] `test_staff_approving_a_ticket_attested_after_the_loan_formed_voids_it_with_a_refund`
- [ ] `test_a_lender_locked_at_1500_whose_receipt_joins_the_loan_is_unlocked_when_it_forms`
- [ ] `test_a_failed_lapse_is_counted_and_the_rounds_still_advance`
- [ ] `test_advance_window_is_idempotent_and_time_driven`
- [ ] `test_acceptance_reminders_sent_once_per_channel_and_logged`
- [ ] `test_reminder_skipped_when_window_already_decided`
- [ ] `test_legacy_windows_keep_the_old_behaviour`
- [ ] `test_reservations_route_refused_for_commit_then_accept_windows`
- [ ] `test_migration_recreates_live_window_indexes_with_new_states`
- [ ] `test_commitment_idempotency_key_replays_first_result`
- [ ] `test_rejects_wrong_role_on_every_new_route`

## 9. Open Questions

| # | Question | Owner | Default used in this spec |
|---|---|---|---|
| 1 | If commitments reach the target before Day 12, do the Day-13 and Day-14 deadlines stay fixed or move earlier? | Loc | **Answered (Money flow FINAL v1.0): fixed.** Pledges close early; Day 13 and Day 14 do not move |
| 2 | May a lender withdraw a pledge before the SME decides? | Loc | **Answered (Money flow FINAL v1.0): yes, until the end of Day 12** |
| 3 | The exact acknowledgement sentence for unlocking (Vietnamese and English), and its version | Loc | **Answered (SCRUM-604, 18 Nov 2026):** the two sentences in 4.7, version `unlock-v2` |
| 4 | How is each pledge signed: the acceptance document through the signer as today, or an OTP per commitment? | Loc, Edward | As today (no OTP per commitment); OTP only for the service agreement and the Day-14 contract |
| 5 | If no-shows leave the confirmed total below 50%, does the loan still form? | Loc | Yes (A4) |
| 6 | How long does `CLOSING` wait for disputes after `pay_deadline_at`? | Edward, Loc | `close_wait_days = 2` calendar days |
| 7 | Is Day 0 the day the window opens, or the day staff publish it? | Loc | The ICT date `window_start` falls on |
| 8 | When is the `LEGACY` flow removed? | Edward | After dev data is cleared; the `flow` column and legacy branches are then dropped (technical debt) |
| 9 | Phat: does the SME's decision screen fit before `commit_closes_at` (read-only) or only after? | Phat | Only after the commit phase |
| 10 | Unlock sentence matching: tolerant (case, accents, spacing ignored, as today) or strictly exact? | Loc | **Answered 2026-11-27 (Loc): tolerant.** Case, accents and spacing are ignored; the build already does this |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-11-15 | Edward / Claude | First draft, from D-036 to D-057 and the code inventory of 2026-11-15 |
| 1.1 | 2026-11-18 | Phat / Claude | Money flow FINAL v1.0: no service agreement at commit (signed on Day 14), withdrawal until the end of Day 12, fixed deadlines (`WINDOW_FULL`), Day-14 cut-offs 14:45 / 15:00 / 15:30; `FUNDING_WINDOW_FLOW` switches new windows to this flow |
| 1.2 | 2026-11-21 | Edward / Claude | Status DRAFT to IMPLEMENTED; no behaviour change |
| 1.3 | 2026-11-25 | Claude (for Edward's approval) | After the pooled loan forms: an excluded ticket confirmed later (24-hour lapse on every `funding-advance` run, or staff approval) is voided with a refund (rule 8); formation unlocks a lender locked at 15:00 whose ticket joins (rule 9) |
| 1.4 | 2026-11-26 | Edward / Claude | Unlock sentence (SCRUM-612): the two sentences Loc locked on 18 Nov, either accepted, language stored (`sentence_language`, version `unlock-v2`); matching rule flagged for Loc (question 10) |
