# Spec: Day 14 — the Investor signs their loan contract with a Zalo code, then pays

| Field | Value |
|---|---|
| **Status** | DRAFT (built with the spec; needs Edward's review: the payment gate in `tranches.py`, the no-show and closure in `day14.py`, the waiver in `standing.py`) |
| **Owner** | Phat |
| **Module** | `app/market/day14_signing.py`, `app/market/commitments_router.py`, `app/market/investor_agreement/service.py` (`record_agreement`), `app/direct_transfer/day14.py`, `app/direct_transfer/tranches.py`, `app/direct_transfer/standing.py`, `app/admin/router.py` |
| **Version** | 1.0 |
| **Date** | 2026-11-18 |
| **Source** | Money flow FINAL v1.0 (Loc, locked 17 Nov 2026): section 1 Day 14 and "If an investor does not pay on disbursement day"; ops rule "Day-14 signing close and 15-minute QR" |
| **Depends on** | `day13-loan-contracts.md` (the contracts), `day14-transfer-rule.md` (QR, cut-offs, late money), `commit-then-accept-flow.md` v1.1 sections 4.4, 4.7, 4.8 |

---

## 1. Context & Goal

Money flow FINAL v1.0, Day 14: *"Each investor signs one loan contract for its total pledged (plus the investor service agreement the first time) with one OTP sent by Zalo. It then gets a VietQR with the SME's account, the exact amount and a reference, transfers before 15:00 and uploads the proof."* And: *"The account is locked at 15:00 and that investor's loan never takes effect. A second no-show closes the account for good."* Ops rule: *"Force majeure or a bank outage (loan contract Art. 5.3) is decided by staff on the investor's proof."*

The code goes through the Zalo OTP template FundLok already uses for the Investor Service Agreement (`zalo.send_otp`, `investor_agreement_codes` with `purpose = LOAN_CONTRACT`, added in S1).

## 2. Out of Scope

- The 15-minute QR, the 14:45 / 15:00 / 15:30 cut-offs and late money (built in #120/#121).
- Pooled loan formation (slice S4): until it lands, staff approval of each CONFIRMED ticket starts its own loan (`repay_from_approval`), as on LEGACY rounds.
- Top-up signing in the 7-day reopening (Phase 3).

## 3. Data Model

No migration: `window_loan_contracts.investor_signed_at / investor_signed_file_key / investor_signature` (Day-13 PR) and `investor_agreement_codes.purpose / window_id` (S1). `lender_standing.closed_at` (S1) is now set.

## 4. API Contract

Investor (the ticket's owner; anyone else 404):
- `GET /funding/tranches/{tranche_id}/contract?download=` → the PDF (the SME's signed copy once on file).
- `POST /funding/tranches/{tranche_id}/contract/code` → `200 { sent_to_masked, channel, expires_in_seconds: 300, resend_after_seconds: 60 }`.
- `POST /funding/tranches/{tranche_id}/contract` `{ "code": "123456", "click_to_sign": true }` → `201 InvestorTrancheOut` (`status: SIGNED`).
- `GET /funding/tranches/{tranche_id}/payment` → `409 CONTRACT_REQUIRED` while the commit-then-accept ticket is RESERVED.

| Status | Code | When |
|---|---|---|
| 409 | `TRANCHE_NOT_SIGNABLE` | not a RESERVED ticket on an AWAITING_PAYMENT commit-then-accept round |
| 409 | `CONTRACT_DEADLINE_PASSED` | from 14:45 on Day 14 |
| 409 | `CONTRACT_UNAVAILABLE` | no contract generated for this lender |
| 400 | `CLICK_REQUIRED` | `click_to_sign` not true |
| 409 / 429 / 400 / 502 / 503 | `PHONE_REQUIRED`, `CODE_COOLDOWN`, `CODE_DAILY_LIMIT`, `PHONE_NO_ZALO`, `CODE_NOT_SENT`, `CODE_UNAVAILABLE` | as for the agreement's code |
| 400 | `CODE_EXPIRED`, `CODE_INCORRECT`, `CODE_TOO_MANY_ATTEMPTS` | no live LOAN_CONTRACT code for this ticket, a wrong guess (counted) |

Staff (`require_admin`): `POST /admin/funding/lenders/{investor_id}/waive-lock` `{ window_id, reason: FORCE_MAJEURE | BANK_OUTAGE, note }` → `200 { investor_id, status, no_show_count }`. `400 WAIVER_REASON`, `400 NOTE_REQUIRED`, `409 NOT_LOCKED`.

## 5. State Machine

```
ticket RESERVED ──code + click (before 14:45)──► SIGNED ──receipt──► ATTESTED ──► CONFIRMED ──► ACTIVE
ticket RESERVED | SIGNED with no receipt ──15:30 decision──► EXPIRED (NO_SHOW): pledges VOID, contract VOID
lender GOOD ──no-show──► LOCKED ──re-eKYC + sentence──► GOOD
lender LOCKED ──staff waiver──► GOOD (miss not counted)
lender ──second counted no-show──► CLOSED (permanent; unlock refused LENDER_CLOSED)
```

## 6. Business Rules

1. One contract per lender for its total; one Zalo code plus a logged click signs it. The record keeps the code, the masked phone, the copy's SHA-256, the contract number and check code, the IP and the browser.
2. On the lender's first Day 14 the same code confirms the Investor Service Agreement (`record_agreement`); later, the ticket's Annex is recorded under the agreement on file.
3. The VietQR opens only after signing; signing and new QRs close at 14:45.
4. A no-show's loan never takes effect: its pledges and its contract are VOID (`NO_SHOW`), the lender is locked and the miss counted; the second counted miss closes the account.
5. A staff waiver (force majeure, bank outage; a note is required) lifts the lock and uncounts that round's miss.

## 7. Error Cases

See section 4.

## 8. Acceptance Criteria

`tests/market/test_day14_investor_signing.py`:
- [x] `test_payment_instructions_refused_until_contract_signed`
- [x] `test_contract_signing_needs_otp_and_click_and_logs_both`
- [x] `test_contract_signing_after_pay_deadline_is_refused`
- [x] `test_no_show_voids_its_pledges_and_contract_and_locks_the_lender`
- [x] `test_second_no_show_closes_the_lender_permanently`
- [x] `test_staff_waiver_lifts_lock_without_counting_the_miss`

`tests/market/test_commitments_routes.py`: `test_day14_contract_routes_and_the_staff_waiver_check_roles`.

## 9. Open Questions

| # | Question | Owner | Default used |
|---|---|---|---|
| 1 | Pooled formation (S4) before launch, or per-ticket loans started by staff approval? | Edward, Loc | Per-ticket until S4 (same rate, end date and payment day when approved the same day) |
| 2 | Should the investor's signed copy carry a printed "signed by code" line? | Loc | No: the record holds the code, the copy's hash and the time |
