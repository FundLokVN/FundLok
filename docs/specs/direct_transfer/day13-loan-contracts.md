# Spec: Day 13 — each Investor's loan contract, the SME's acceptance by signing, the guarantor, the reminders

| Field | Value |
|---|---|
| **Status** | DRAFT (built with the spec; needs Edward's review: migration, the decision gate in `commit_flow`) |
| **Owner** | Phat |
| **Module** | `app/market/day13_contracts.py`, `app/market/commitments_router.py`, `app/scheduled_jobs/acceptance.py`, `app/loans/agreement/loan_contract.py` (per-investor copy), `app/direct_transfer/models.py` (`WindowLoanContract`), `app/direct_transfer/commit_flow.py` (`decide_window(contracts_signed=…)`) |
| **Version** | 1.0 |
| **Date** | 2026-11-18 |
| **Source** | Money flow FINAL v1.0 (Loc, locked 17 Nov 2026): section 1 "Day 13 SME decision"; ops rule "Day-13 signing cut-off"; D-056 (reminders) |
| **Depends on** | `commit-then-accept-flow.md` v1.1 (S2), `docs/specs/loans/guarantor-zalo-otp.md` (the guarantor's Zalo code) |

---

## 1. Context & Goal

Money flow FINAL v1.0, Day 13: *"By 15:00 the SME accepts or declines the total pledged. Accepting means signing: the SME signs every investor's loan contract from the final pledge list with its enterprise digital signature, and the guarantor signs each by OTP sent by Zalo (its signature is not a condition of acceptance). If it stays silent, 50% of the target or more counts as accepted and the SME is bound by every loan contract without signing … less counts as declined. … The system reminds the SME (in-app, Zalo and email) 3 days and 1 day before the deadline and logs each reminder."*

Ops rule: *"The platform generates every loan contract PDF at 00:00 on Day 13 from the final pledge list. For a listing below 50% of target, the SME must finish signing by 12:00 so acceptance completes before 15:00. Staff log the completion time."*

## 2. Out of Scope

- The Investor's Day-14 signature on their copy (next PR, `day14` signing).
- Loan formation and fees (unchanged).
- A guarantor who is not the signing owner (see `guarantor-zalo-otp.md`).

## 3. Data Model

Migration `e3b9d7a2c418` (off `c5e8a1f3d920`):
```
window_loan_contracts
├── id UUID PK · window_id → funding_windows · investor_id → users      UNIQUE (window_id, investor_id)
├── contract_no TEXT UNIQUE          -- {application ref}-{seq:02d}/HĐCV-FL, seq = order of each lender's first pledge
├── amount_vnd BIGINT > 0            -- the lender's pledged total
├── status TEXT  GENERATED | SME_SIGNED | INVESTOR_SIGNED | VOID · void_reason TEXT
├── file_key, sha256, check_code, generated_at          -- the generated copy (evidence store)
├── sme_signed_file_key, sme_signed_sha256, sme_signed_at, sme_signature_check JSONB   -- NEAC verdict
└── investor_signed_at, investor_signed_file_key, investor_signature JSONB              -- Day 14
```
The guarantor's signature is a `guarantor_signatures` row (`WINDOW_LOAN_CONTRACTS`, subject = the window), listing every contract's number, check code and hash. Reminders use `acceptance_reminders` (S1).

The per-investor copy is Contract 5 filled for the lender (`loan_contract.render(..., copy=InvestorCopy)`): Lender name, ID, account; P, I, T, A and the last instalment; the Disbursement Date (Day 14); the contract number; a check code over the template's values and the lender's. Its signing clause says the SME signs digitally and the guarantor and the Investor by Zalo code (to be checked against Contract 5 FINAL v1.0).

## 4. API Contract

SME (the round's owner; another SME gets 404):

- `GET /funding/windows/{window_id}/loan-contracts` → `200 { window_id, status, pledged_vnd, target_vnd, sme_deadline_at, signing_cutoff_at, all_signed, guarantor_signed_at, guarantor_phone, contracts: [{ id, contract_no, lender_name (masked), amount_vnd, status, generated_at, sme_signed_at, investor_signed_at }] }`. `signing_cutoff_at` = 12:00 on Day 13 when pledged < 50% of target, else null.
- `GET /funding/windows/{window_id}/loan-contracts/{contract_id}/document?signed=&download=` → the PDF (generated, or the SME's signed copy).
- `POST /funding/windows/{window_id}/loan-contracts/{contract_id}/signed` (multipart `file`) → `200` the list above. Refusals: `409 CONTRACT_SIGNING_NOT_OPEN` (not AWAITING_SME), `409 DECISION_DEADLINE_PASSED`, `409 SIGNING_CUTOFF_PASSED` (below 50%, at or after 12:00), `409 SIGNING_ALREADY_SIGNED`, `413`, `415 SIGNING_NOT_PDF`, `422 SIGNING_UNSIGNED`, `422 SIGNING_WRONG_DOCUMENT` (another contract), `422 SIGNING_DOCUMENT_OUTDATED` (check code), NEAC's `422 SIGNING_SIGNATURE_INVALID`, `422 SIGNING_SIGNER_MISMATCH` (not the company's enterprise code), `503 NEAC_UNAVAILABLE`.
- `POST /funding/windows/{window_id}/loan-contracts/guarantor-code` → `200 { phone, channel, expires_in, resend_in }`; `POST …/guarantor-signature { code }` → `200` the list. Open in AWAITING_SME or AWAITING_PAYMENT once contracts exist. Code errors as in `guarantor-zalo-otp.md`; `409 GUARANTOR_ALREADY_SIGNED`, `409 CONTRACTS_UNAVAILABLE`.
- `POST /funding/windows/{window_id}/decision` (S2) now refuses `ACCEPT` with `409 CONTRACTS_NOT_SIGNED` until every contract is SME-signed. `DECLINE` needs nothing; it voids the contracts.

Scheduler (`require_scheduler`):
- `POST /internal/jobs/funding-advance` (00:00 and 15:00 ICT) → `{ job, advanced, contracts, failed }`; status 500 (same body) when `failed > 0`, so Cloud Scheduler retries.
- `POST /internal/jobs/acceptance-reminders` (09:00 ICT) → per channel `{ sent, failed, skipped }` for `in_app`, `email`, `zalo`, `sms`.

## 5. State Machine

```
OPEN ──00:00 Day 13──► AWAITING_SME  (contracts GENERATED, one per lender)
contract GENERATED ──SME signed copy, NEAC ok──► SME_SIGNED ──Day 14 Investor code──► INVESTOR_SIGNED
AWAITING_SME ──ACCEPT (all SME_SIGNED)──► AWAITING_PAYMENT
AWAITING_SME ──silence ≥ 50% at 15:00──► AWAITING_PAYMENT (contracts stay GENERATED: bound without signing)
AWAITING_SME ──DECLINE / silence < 50%──► FAILED (contracts VOID)
```

## 6. Business Rules

1. **Generation.** At 00:00 on Day 13 (`funding-advance`, or staff Advance) one contract per lender with PLEDGED commitments, for their total. Idempotent. A round that fails is rolled back and `funding-advance` answers 500, so Cloud Scheduler retries it within minutes. If every 00:00 attempt fails, the 15:00 run ends the commit phase and applies the silence rule in one step; the contracts are then made from the same pledges (now FOLDED into tickets) by that run or staff Advance, until 14:45 on Day 14, when signing closes.
2. **Accepting means signing.** An explicit ACCEPT needs every live contract SME-signed (enterprise signature, NEAC-checked, the right contract by number and check code).
3. **Noon cut-off.** Below 50% of target, signed uploads close at 12:00 on Day 13. The time signing completed is logged (`LOAN_CONTRACTS_SIGNING_COMPLETED`).
4. **Silence.** At 15:00 with 50% or more the round is AUTO_ACCEPTED and the SME is bound by every contract unsigned; below, AUTO_DECLINED.
5. **Guarantor.** One Zalo code signs every live contract; not a condition of acceptance.
6. **Reminders.** 3 days and 1 day before the Day-13 deadline (by ICT date), once per round, kind and channel: in-app (`ACCEPTANCE_DUE_3D/1D`), email, Zalo (`ZALO_ZBS_ACCEPTANCE_TEMPLATE_ID`: `company_name`, `amount`, `deadline`; skipped when unset or no verified phone), SMS skipped `NO_PROVIDER` (D-056). A decided round's reminders are logged `SKIPPED` (`DECIDED`). FAILED is retried by a later run.

## 7. Error Cases

See section 4.

## 8. Acceptance Criteria

`tests/market/test_day13_loan_contracts.py`:
- [x] `test_contracts_are_generated_at_the_end_of_day_12_one_per_lender_for_its_total`
- [x] `test_accepting_means_signing_every_lenders_contract`
- [x] `test_below_fifty_percent_signing_closes_at_noon_on_day_13`
- [x] `test_a_copy_of_another_contract_or_an_edited_one_is_refused`
- [x] `test_silence_at_fifty_percent_or_more_accepts_without_signatures`
- [x] `test_a_declined_round_voids_its_contracts`
- [x] `test_the_guarantor_signs_every_contract_with_one_zalo_code_and_it_is_not_a_condition`
- [x] `test_funding_advance_job_ends_the_commit_phase_and_generates_the_contracts`
- [x] `test_a_failed_midnight_run_is_counted_and_its_retry_makes_the_contracts`
- [x] `test_a_round_accepted_before_its_contracts_were_made_still_gets_them_until_14_45`
- [x] `test_acceptance_reminders_sent_once_per_channel_and_logged`
- [x] `test_reminder_skipped_when_window_already_decided`

`tests/market/test_commitments_routes.py`: `test_commit_decide_and_progress_over_the_api` (CONTRACTS_NOT_SIGNED), `test_day13_loan_contract_routes_are_the_owning_smes`.

## 9. Open Questions

| # | Question | Owner | Default used |
|---|---|---|---|
| 1 | Contract 5 FINAL v1.0's wording for each Investor's copy (signing clause, number format) | Loc | `INVESTOR_COPY_SIGNING`, `{ref}-{seq:02d}/HĐCV-FL` |
| 2 | The SME acceptance ZBS template (to register) | Phat | `company_name`, `amount`, `deadline`; Zalo skipped until set |
| 3 | One combined PDF for many lenders, so the SME signs once? | Loc | No: one contract, one signature each ("signs every investor's loan contract") |
