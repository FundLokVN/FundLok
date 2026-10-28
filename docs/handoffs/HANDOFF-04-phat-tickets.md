# HANDOFF-04 — Phat's tickets (direct-transfer disbursement and repayment)

| Field | Value |
|---|---|
| **Status** | DRAFT (specs are DRAFT; shapes below are copied from them and are the contract) |
| **Date** | 2026-10-28 |
| **From** | Edward (backend, `app/direct_transfer/`) |
| **To** | Phat (routers in your modules, all React) |
| **Specs** | `docs/specs/direct_transfer/funding-window.md` (start here), `transfer-and-evidence.md`, `repayment-collection.md`, `disputes.md`, `fees.md`, `cic-event-log.md`, `ledger-amendment.md` |
| **ADR** | `docs/adr/009-direct-transfer-model.md` |

You need no other context. Rules that apply to every ticket:

- The backend is a set of **service functions** in `app/direct_transfer/` that take an `AsyncSession` and an injected `now`. Your router resolves the caller, checks the role, calls the function, and maps `DirectTransferError(status_code, detail)` to `{"detail": "<message>"}`. A resource that exists but is not the caller's returns **404**.
- Money is **integer VND** in JSON (no decimals, no strings). Timestamps are ISO-8601 with timezone. Dates are `YYYY-MM-DD` (ICT).
- Endpoints that create or change a financial record take an `Idempotency-Key` header (UUID); pass it straight to the service function's `idempotency_key`.
- File uploads: first `POST /evidence-files` (multipart, `purpose`, `file`) -> `{ "file_id": "uuid", "sha256": "string" }`; other calls refer to `file_id`. Accepted: PDF, PNG, JPEG, WEBP, max 10 MB.
- No money is held or moved by FundLok. Copy must never say FundLok lends, guarantees or promises a return; show returns as a range. Disclosures (Huy's text): FundLok is not a party, no guarantee, money moves directly between your accounts, partial funding, tax withheld.
- Do not edit `app/direct_transfer/` or `alembic/`; ask Edward. Service function names are given per ticket.
- `grep -rn PROVISIONAL docs app` shows rules that may change; build them as server-driven values (do not hard-code 2 hours, 14 days, 10M, 50%): read them from the API responses.

Statuses you will render (tranche): `RESERVED`, `SIGNED`, `ATTESTED`, `CONFIRMED`, `ACTIVE`, `EXPIRED`, `UNDER_REVIEW`, `VOID`. Window: `OPEN`, `FULL`, `CLOSING`, `REPAYING`, `FAILED`, `REPAID`, `CANCELLED`.

---

## Epic 0 — Save a bank account first (prerequisite for Epics 1 and 2)

Every investor and every SME must have an **approved** payment account before they can invest or open a window: the SME's account is the payee on the VietQR, and the investor's is the payer we check receipts against. No bank API exists, so staff approve each account from an uploaded bank statement or bank confirmation letter (Epic 3.1). Ask for the account at onboarding so approval is not what delays the first investment.

### 0.1 Bank dropdown
`GET /direct-transfer/banks?include_wallets=false` -> `[BankOut]`, service `list_banks(include_wallets=False)`. Static data, safe to cache for the session. Any signed-in user.

```json
{ "bin": "970407", "code": "TCB", "short_name": "Techcombank", "name": "Ngân hàng TMCP Kỹ thương Việt Nam",
  "kind": "BANK", "popular": true }
```
- Already ordered: the 12 `popular` banks first (Vietcombank, VietinBank, BIDV, Agribank, Techcombank, MB, ACB, VPBank, TPBank, Sacombank, HDBank, VIB), then the rest A-Z. Show the popular ones pinned and the rest searchable.
- `bin` is what you send back when saving the account. Never let the user type a BIN.
- `kind: "WALLET"` entries (MoMo, ZaloPay, MVAS) are excluded unless `include_wallets=true`. Leave them off: the evidence flow is bank accounts only for now. PROVISIONAL(Q-D17).
- Logos are not part of the data. VietQR's public CDN serves `https://cdn.vietqr.io/img/{code}.png` if you want them; that is a third-party dependency, your call.

### 0.2 Save an account
`POST /payment-accounts` (user), body below -> `PaymentAccountOut`, service `register_payment_account`. The router sets `holder_role` from the signed-in user (`SME` or `INVESTOR`).
```json
{ "holder_type": "INDIVIDUAL|ENTITY", "bank_bin": "970407", "account_number": "19036789012",
  "account_name": "NGUYEN VAN A", "evidence_file_id": "uuid" }
```
- `bank_bin` must be one from 0.1. The server fills the bank name from the list and **ignores any `bank_name` you send**.
- `account_name` is the name exactly as registered at the bank (upper-case, no accents is fine; the comparison folds accents and case). Tell the investor: **the name on the transfer receipt must match their KYC name**, so they must pay from this account.
- `holder_type`: `INDIVIDUAL` for a person, `ENTITY` for a company. It decides tax withholding on interest (individuals only), so ask clearly.
- `evidence_file_id`: upload the statement or bank confirmation first with `POST /evidence-files` (`purpose` = `BANK_STATEMENT` or `BANK_CONFIRMATION`; any other value is rejected). Staff cannot approve without it.
- Errors: `400 Unsupported bank`, `400 Invalid account number` (4 to 32 letters/digits), `409 ACCOUNT_EXISTS` (same bank + number already saved by this user).
- Response `status` starts `PENDING`.

### 0.3 My accounts and their status
`GET /payment-accounts` -> `[PaymentAccountOut]` (caller's own, oldest first), service `list_payment_accounts`.
```json
{ "id": "uuid", "holder_role": "INVESTOR", "holder_type": "INDIVIDUAL", "bank_bin": "970407", "bank_name": "Techcombank",
  "account_number": "19036789012", "account_name": "NGUYEN VAN A", "status": "PENDING|APPROVED|REJECTED",
  "rejection_reason": null, "created_at": "...", "approved_at": null }
```
Render three states: **Pending** (saved, waiting for staff; cannot be used yet), **Approved** (selectable when investing or opening a window), **Rejected** (show `rejection_reason`; the user saves a corrected account as a new entry).
- Gate: reserving a ticket (1.3) or opening a window (1.2) with an account that is not `APPROVED` returns `409 ACCOUNT_NOT_APPROVED`. Disable the invest and open-window buttons until the user has an approved account, and link them to this screen.
- A user may save several accounts and pick one per investment (`payment_account_id`).
- Not built: edit or delete. A wrong account is fixed by saving a new one.

---

## Epic 1 — Listing window (start here)

### 1.1 Listing page: progress and status
**Goal:** show confirmed, reserved and target amounts, days left, minimum ticket, capacity left and status.
**Endpoint:** `GET /funding/listings/{listing_id}/window` (any verified user). Service: `get_window_progress`.
```json
{ "id": "uuid", "listing_id": "uuid", "status": "OPEN", "target_vnd": 100000000, "min_raise_vnd": 50000000,
  "min_ticket_vnd": 10000000, "confirmed_vnd": 30000000, "reserved_vnd": 20000000, "capacity_vnd": 50000000,
  "annual_rate_pct": "16.000", "term_months": 4, "window_start": "ts", "window_end": "ts", "days_left": 9,
  "tranche_count": 5, "closing_started_at": null, "close_wait_until": null, "repayment_start_date": null }
```
**Accept:** progress bar uses `confirmed_vnd` and `reserved_vnd`; status chips for `OPEN`/`FULL`/`CLOSING`/`REPAYING`/`FAILED`; no other investor's identity or amount is shown; `capacity_vnd = 0` shows "Fully reserved" while `status = FULL`.

### 1.2 SME: sign the standing offer and open the window
**Endpoint:** `POST /funding/listings/{listing_id}/window` (SME, owner). Service: `open_funding_window`. Header `Idempotency-Key`.
```json
{ "payment_account_id": "uuid", "declared_outside_debt_vnd": 0, "declaration_accepted": true, "window_days": 14 }
```
**Response 201:** `FundingWindowOut` (as 1.1 with `confirmed_vnd: 0`).
**Errors (409):** `SME_HAS_LIVE_LISTING`, `SME_INVOICE_UNPAID`, `ACCOUNT_NOT_APPROVED`, `BORROWER_CAP_EXCEEDED`. Block the "publish" button when the SME has a live window or an unpaid invoice (use ticket 3.3).
**Accept:** the signing step calls the signer (mock today; vendor later); the page shows the standing-offer text and the full terms before signing; the SME payment account must be one approved by staff (`GET /payment-accounts`, `POST /payment-accounts`, see 3.1).

### 1.3 Investor: reserve a ticket
**Endpoint:** `POST /funding/windows/{window_id}/reservations` (INVESTOR). Service: `reserve_ticket`. Header `Idempotency-Key`.
```json
{ "amount_vnd": 10000000, "ack_risk_disclosure": true, "payment_account_id": "uuid" }
```
**201** `TrancheOut`: `{ "id","window_id","status":"RESERVED","amount_vnd","reference_code","hold_expires_at","signed_at":null,"attested_at":null,"confirm_deadline":null,"review_cause":null,"void_reason":null }`.
**Errors (409):** `CAPACITY_EXCEEDED`, `BELOW_MIN_TICKET`, `ALREADY_HAS_LIVE_TRANCHE`, `WINDOW_NOT_OPEN`, `INVESTOR_FEE_UNPAID`, `ACCOUNT_NOT_APPROVED`.
**Accept:** risk-disclosure checkbox required; countdown to `hold_expires_at` (use the server time offset); when it hits zero show the expired state and a "reserve again" action; the last ticket may be below the minimum when it equals `capacity_vnd` (the API decides, do not pre-validate).

### 1.4 Investor: sign the acceptance, then the QR screen
**Endpoints:** `POST /funding/tranches/{id}/accept` (service `accept_tranche`) then `GET /funding/tranches/{id}/payment`.
Accept returns `TrancheOut` with `status: "SIGNED"`. Payment returns:
```json
{ "bank_name": "string", "bank_bin": "970407", "account_number": "string", "account_name": "string",
  "amount_vnd": 10000000, "reference_code": "FLAB12CD34", "vietqr_payload": "000201…", "hold_expires_at": "ts",
  "warning": "string" }
```
**Accept:** QR image generated client-side from `vietqr_payload`; amount, reference, account number and name each have a copy button; show "pay from an account in your own name" and "do not change the amount or the memo"; keep the countdown visible.

### 1.5 Investor: receipt upload and status tracker
**Endpoint:** `POST /funding/tranches/{id}/receipt` (multipart `file`; header `Idempotency-Key`). Service: `submit_receipt`.
**202:** `{ "transfer_id": "uuid", "tranche_status": "SIGNED|ATTESTED|UNDER_REVIEW|EXPIRED", "check_status": "AWAITING_FIELDS|MATCHED|MISMATCH", "failure_codes": ["AMOUNT_MISMATCH"], "confirmation_level": "REPORTED|ATTESTED" }`.
`GET /funding/me/tranches` lists tranches for the tracker.
**Errors:** 409 `DUPLICATE_BANK_TXN` / `DUPLICATE_FILE` ("this receipt was already used"), 413, 415.
**Accept:** tracker steps Reserved -> Signed -> Attested -> Confirmed, with Under review / Expired / Void as side states and a plain-language line for each `failure_codes` value; a `SIGNED` tranche with a receipt and `check_status = AWAITING_FIELDS` shows "We are reading your receipt" (staff enter the fields in the MVP).

### 1.6 SME: tranche inbox, confirm or dispute
**Endpoints:** `GET /funding/windows/{window_id}/tranches` (SME, own window; service `list_window_tranches`), `POST /funding/tranches/{id}/confirm`, `POST /funding/tranches/{id}/dispute` body `{ "statement": "string", "file_id": "uuid|null" }`.
`list` item: `{ "id","amount_vnd","status","reference_code","investor_display":"A. N***","attested_at","confirm_deadline","receipt_file_url","review_cause" }`.
**Accept:** countdown to `confirm_deadline` with the text "silence counts as received"; dispute needs a statement and an optional bank statement upload; the confirm and dispute buttons are enabled only for `ATTESTED`. Errors: 400 invalid transition, 409 `DISPUTE_WINDOW_CLOSED`.

### 1.7 Window close views
**Endpoints:** `GET /funding/windows/{window_id}/close-summary`; `POST /funding/windows/{window_id}/close-early` (SME).
Close summary: `{ "status": "REPAYING|FAILED", "confirmed_total_vnd", "repayment_start_date", "tranches": [ { "id","amount_vnd","stub_days","stub_interest_vnd","status" } ], "refunds": [ { "tranche_id","amount_vnd","due_by","status" } ], "activation_fee_invoice_id" }`.
**Accept:** success view (start date, per-tranche stub interest, activation-fee notice "invoiced separately, not deducted"); below-50% view for the SME: a refund checklist, one row per tranche, with the investor's account details and an upload slot for each refund receipt (`POST /funding/refunds/{transfer_id}/receipt`, same shape as 1.5); the "close early" button is enabled only when the API says so (`409 BELOW_MIN_RAISE_FOR_EARLY_CLOSE` otherwise).

### 1.8 Disclosures
Static copy from Huy: FundLok is not a party, no guarantee, money moves directly, partial funding, tax withheld. Shown before reserving (1.3) and signing (1.4). No code dependency.

---

## Epic 2 — Today's payments (repayment MVP)

### 2.1 SME: today's due per investor
**Endpoint:** `GET /repayment/today?date=YYYY-MM-DD` (SME). Service: `todays_payments`.
```json
{ "date": "2026-11-20", "rows": [ { "repayment_day_id": "uuid", "tranche_id": "uuid", "kind": "INSTALMENT|STUB",
  "investor_display": "A. N***", "due_date": "2026-11-20", "principal_vnd": 119048, "interest_vnd": 6349,
  "gross_vnd": 125397, "tax_withheld_vnd": 317, "net_vnd": 125080, "status": "DUE",
  "payee": { "bank_name": "string", "bank_bin": "970407", "account_number": "string", "account_name": "string" },
  "reference_code": "FLAB12CD34", "vietqr_payload": "000201…" } ],
  "totals": { "gross_vnd": 1253968, "tax_withheld_vnd": 3170, "net_vnd": 1250798 } }
```
The SME pays the **net** amount to each investor (the SME declares and pays the withheld tax itself).
**Endpoint:** `POST /repayment/days/mark-paid` body `{ "repayment_day_ids": ["uuid"], "receipt_file_id": "uuid|null" }` (header `Idempotency-Key`). Service: `mark_days_paid`. Returns the updated rows.
**Accept:** one QR per investor row; a "mark all as paid" action; per-row status chips `DUE`, `PRESUMED_PAID`, `REPORTED_MISSED`, `MATCHED`; a day left unmarked after its due date still counts as paid unless the investor reports it (show that sentence).

### 2.2 Investor: schedule and "report missing"
**Endpoints:** `GET /repayment/tranches/{tranche_id}/schedule` (investor or SME of the window; service `tranche_schedule`) returns the rows above for every day; `POST /repayment/days/{id}/report-missing` body `{ "statement": "string|null", "file_id": "uuid|null" }` (INVESTOR; service `report_missing_day`).
**Accept:** the "report missing" button is available while `report_deadline` has not passed (countdown); after the deadline the API may still accept a late report into reconciliation (`status` unchanged, message from the API); show `REPORTED_MISSED` rows with "waiting for the SME's evidence".
**Errors:** 409 `REPORT_WINDOW_CLOSED`, 400 invalid state.

### 2.3 Standing-order instructions
Static content per bank (Techcombank first): how to create a daily standing order that skips weekends and holidays, and the warning that every extension changes the amount. No backend call.

### 2.4 SME monthly withholding report (data only; Huy owns the wording)
**Endpoint:** `GET /repayment/withholding-report?month=YYYY-MM` (SME). Service: `withholding_report`.
`{ "month": "2026-11", "rows": [ { "tranche_id","investor_display","investor_type":"INDIVIDUAL|ENTITY","interest_vnd","tax_withheld_vnd" } ], "totals": { "interest_vnd","tax_withheld_vnd" } }`

---

## Epic 3 — Staff (admin)

### 3.1 Payment-account approval (staff)
The user side (saving an account) is **Epic 0**. Staff queue: service `pending_payment_accounts` (the path is not fixed by the specs; suggested `GET /admin/direct-transfer/payment-accounts/pending`), then:
Staff: `POST /admin/direct-transfer/payment-accounts/{id}/decision` `{ "decision": "APPROVED|REJECTED", "reason": "string" }`. Service: `decide_payment_account`.
Show the account, the uploaded statement (`evidence_file_id`) and the holder's KYC name side by side. A reason is required for both outcomes.

### 3.2 Exceptions queue
`GET /admin/direct-transfer/exceptions` -> `[ { "kind": "AWAITING_FIELDS|RECEIPT_MISMATCH|LATE_RECEIPT", "tranche_id","transfer_id","amount_vnd","reference_code","failure_codes":[…],"parsed_fields":{…}|null,"file_url","timeline":[ evidence_log rows ] } ]`.
`POST /admin/direct-transfer/transfers/{id}/fields` `{ "amount_vnd","transferred_at","payer_name","payer_account","payee_account","memo","bank_txn_id","reason" }` (manual entry; runs the checks). Service: `staff_enter_receipt_fields`.
Decision on a mismatch or late receipt: `POST /admin/direct-transfer/disputes/{dispute_id}/decision` (see 3.3).
**Accept:** the viewer shows the file next to an editable field form; every action requires a reason; the timeline is the append-only log.

### 3.3 Dispute queue and decisions
`GET /admin/direct-transfer/disputes?status=open` -> `[ { "id","subject_type":"TRANCHE|REPAYMENT_DAY","subject_id","cause","status","required_next_party":"INVESTOR|SME|STAFF","response_deadline","evidence":[ { "party","statement","file_url","submitted_at" } ] } ]`.
`POST /admin/direct-transfer/disputes/{id}/decision` `{ "outcome": "RECEIVED|NOT_RECEIVED|PAID|NOT_PAID", "reason": "string" }`; `POST /admin/direct-transfer/disputes/{id}/bank-trace` `{ "reason": "string" }`. Services: `decide_dispute`, `escalate_to_bank_trace`.
Parties: `POST /disputes/{id}/evidence` `{ "statement": "string", "file_id": "uuid|null" }` (INVESTOR or SME, whoever is `required_next_party`). Service: `submit_dispute_evidence`. 409 `NOT_YOUR_TURN`.
**Accept:** both sides' statements side by side, whose turn it is, a deadline, decision form with mandatory reason.

### 3.4 Fee invoices and overdue blocks
`GET /fees/invoices` (own, SME or INVESTOR) -> `[ { "id","kind":"SME_ACTIVATION|INVESTOR_SERVICE|LATE_FEE","base_vnd","fee_vnd","vat_vnd","total_vnd","status":"ISSUED|PAID|OVERDUE|VOID","issued_at","due_at","paid_at","reference_code","vietqr_payload" } ]`; `GET /admin/direct-transfer/fees/blocks` -> SMEs and investors currently blocked; `POST /admin/direct-transfer/fees/invoices/{id}/mark-paid` `{ "bank_txn_id": "string", "reason": "string" }`. Service: `mark_invoice_paid`.
FundLok's fee account is FundLok's own revenue account, never a customer-money account; the QR is shown on the invoice view.

---

## Order of work suggested

0. **Epic 0** (save a bank account) first: nothing in Epics 1 and 2 works without an approved account.
1. Epic 1 tickets 1.1 to 1.5 (they unblock end-to-end testing against the mock signer and staff-entered receipts).
2. 3.1 and 3.2 (staff must approve accounts and enter receipt fields or nothing moves).
3. 1.6, 1.7, then Epic 2, then 3.3, 3.4.

## Things I need from you

- Confirm the router locations (`app/market/` for window/tranche endpoints, `app/admin/` for staff endpoints). Nothing in `app/direct_transfer/` imports them.
- `app/models/__init__.py` re-exports the new models (one shared-file change, in the PR for your OK).
- The legacy `place_order` endpoint should check `app.direct_transfer.compat.legacy_place_order_enabled()` and return 410 when false (B15). Default is enabled so the old tests keep passing.
