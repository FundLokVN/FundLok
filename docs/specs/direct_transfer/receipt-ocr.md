# Spec: Receipt OCR and the automatic verdict

| Field | Value |
|---|---|
| **Status** | DRAFT. Needs Edward's review: it changes how `transfer-and-evidence.md` confirms a transfer and touches ADR-009's "confirmations rest on uploaded documents reviewed by staff" (Q-033). Built behind two switches, both off |
| **Owner** | Phat (proposed) |
| **Implementer(s)** | Phat (backend in `app/uploads/`, `app/market/`, `app/admin/`; admin UI). No change to `app/direct_transfer/` code |
| **Module** | `app/uploads/parsers/bank_receipt.py`, `app/uploads/receipt_ocr.py`, `app/market/receipt_auto.py`, `app/market/funding_router.py`, `app/admin/funding.py` |
| **Version** | 0.1 |
| **Date** | 2026-11-14 |
| **Related ADR** | ADR-009 (point 4: evidence, not bank data; `ReceiptExtractor`) |
| **Depends on** | `transfer-and-evidence.md` (receipt checks, rules 3-9), `funding-window.md` (tranche states), D-031 (verified holder name) |
| **Decisions** | Phat, 2026-11-14: local OCR; a match confirms, a mismatch denies; an unsure read goes to staff; the loan waits for staff review; staff can reverse either verdict; the receiver's bank, account and holder name are checked |

---

## 1. Context & Goal

Every investor receipt is read by a person today (`ManualEntryExtractor`). That is slow, and a funding round waits on it. The receipt checks themselves already exist and are pure (`check_receipt`); only the reading is manual.

**Goal:** read every new receipt automatically on our own server, store what was read, check it against the payment FundLok issued and the business's verified account, and decide it: **accepted** (the ticket is confirmed) or **denied** (the investor uploads the right one). Anything the system is unsure of goes to staff as today. Staff can look again at any automatic verdict and approve or deny it, with an audit trail.

## 2. Out of Scope

- **Sending receipts outside the platform.** The engine is Tesseract, a local process in the API container; a bank's PDF is read from its own text layer. No external OCR or AI service is called (Q-025 still open for anything else).
- Refund receipts (SME to investor) and repayment receipts: still read by staff.
- Detecting edited or forged receipts. OCR reads what the image shows; a forged receipt that matches is accepted (as a forged one typed in by staff would be). Mitigations: staff review before the loan forms (rule 6), duplicate bank-transaction and file checks, the business's own dispute.
- Rasterising scanned PDFs (no text layer): left to staff.

## 3. Data Model

**No schema change.** Everything is stored in existing columns and the append-only `evidence_log`:

| Where | What |
|---|---|
| `evidence_log` `RECEIPT_OCR_READ` (entity TRANSFER, actor NULL, role `SYSTEM`) | The reading: engine, parser version, fields (amount, time, payer name and account, payee name, account and bank, memo, bank transaction id), each field's line confidence (0-100; null for exact PDF text), overall confidence, `confident`, notes |
| `evidence_log` `SYSTEM_VERDICT` | `verdict` (ACCEPTED, DENIED, NEEDS_REVIEW), `applied`, `suggested`, `failure_codes`, `review_reasons`, `receiver` (what was compared), engine, confidence; `reason` is a one-line summary |
| `evidence_log` `FIELDS_ENTERED`, `CHECKS_RUN` (role `SYSTEM`) | As for staff entry, when the system decided |
| `evidence_log` `SYSTEM_VERDICT_REVIEWED` (actor = staff) | `system_verdict`, `outcome` (APPROVED, DENIED), `overrides_system`; `reason` = staff's note |
| `transfers.parsed_fields`, `failure_codes`, `check_status`, `bank_txn_id`, `transferred_at` | Set when the system decided, as staff entry sets them (`parsed_fields` also carries `payee_bank`) |
| `audit_log` `TRANSFER_SYSTEM_ACCEPTED` / `_DENIED` / `_NEEDS_REVIEW` (actor NULL) | In the Transactions tab's activity log with staff's decisions |

Switches (environment, read per request): `RECEIPT_OCR_ENABLED` (read receipts at all), `RECEIPT_AUTO_VERDICT_ENABLED` (let a confident read decide; otherwise the verdict is only a suggestion), `RECEIPT_OCR_MIN_CONFIDENCE` (default 80), `RECEIPT_OCR_LANGUAGES` (`vie+eng`), `RECEIPT_OCR_PSM` (6), `RECEIPT_OCR_TIMEOUT_SECONDS` (20).

## 4. API Contract

### `POST /funding/tranches/{tranche_id}/receipt` (unchanged shape)
With OCR on, the 202 body can now also be `check_status: "MATCHED"`, `tranche_status: "CONFIRMED"`, `confirmation_level: "CONFIRMED"` (accepted), or `check_status: "REJECTED"` with `failure_codes` and `tranche_status: "SIGNED"` (denied: upload the right receipt while the hold lasts).

### `GET /admin/funding/transfers` and `/{tranche_id}`
- New filter `status=SYSTEM_DECIDED`: tickets whose receipt the system accepted or denied.
- `NEEDS_ACTION` also holds tickets the system accepted that no staff member has reviewed (their loan waits).
- `transfer.system_check` (null when OCR was off): `verdict`, `applied`, `suggested`, `summary`, `failure_codes`, `review_reasons`, `receiver` {`bank_read`, `bank_identified`, `bank_identified_bin`, `expected_bank`, `expected_bank_bin`, `account_read`, `expected_account`, `holder_read`, `expected_holder`}, `decided_at`, `engine`, `confidence`, `fields`, `field_confidence`, `notes`, `awaiting_review`, `reviewable`, `reviewed_at`, `reviewed_by`, `review_outcome`, `review_reason`, `overrides_system`.
- `expected.payee_holder_name`: the holder name the bank showed when the business's account was verified.

### `POST /admin/funding/transfers/{transfer_id}/decision` (unchanged shape: `{outcome, reason?}`)
On a receipt the system decided:

| System verdict | Staff | Result |
|---|---|---|
| ACCEPTED (ticket CONFIRMED) | APPROVED | Kept; the loan forms now (`repay_from_approval`) |
| ACCEPTED | DENIED | Ticket VOID (`NOT_RECEIVED`), its place freed. 409 `LOAN_STARTED` if the round was advanced or closed and the loan formed |
| DENIED (receipt REJECTED) | APPROVED | Received: SIGNED -> ATTESTED -> CONFIRMED and the loan forms; if the hold has lapsed (EXPIRED), as a late receipt settled RECEIVED (409 `CAPACITY_EXCEEDED`, `ROUND_CLOSED`). 409 `DUPLICATE_BANK_TXN` if its bank transaction funds another ticket |
| DENIED | DENIED | The rejection stands; nothing moves |

409 `SUPERSEDED` when the investor has uploaded a newer receipt (review that one), `ALREADY_DECIDED` once reviewed. A review is final. `TRANSFER_RECEIVED` / `TRANSFER_NOT_RECEIVED` audit rows carry `system_verdict` and `overrides_system`.

## 5. State Machine

No new states or transitions; the system uses the existing ones (`tranche_state.ALLOWED_TRANSITIONS`).

```
upload, OCR confident, all checks pass, receiver confirmed -> transfer MATCHED/CONFIRMED, tranche SIGNED -> ATTESTED -> CONFIRMED (no loan)
upload, OCR confident, any check fails                     -> transfer REJECTED, tranche stays SIGNED (receipt_submitted_at cleared)
anything else                                              -> transfer AWAITING_FIELDS (staff, as today)
staff approve ACCEPTED       -> form_investment_loan: CONFIRMED -> ACTIVE
staff deny ACCEPTED          -> CONFIRMED -> VOID (before ACTIVE)
staff approve DENIED         -> SIGNED -> ATTESTED -> CONFIRMED -> ACTIVE, or EXPIRED -> UNDER_REVIEW -> CONFIRMED -> ACTIVE
round advanced or closed     -> CONFIRMED tickets form their loans (existing behaviour), so an unreviewed acceptance can no longer be denied
```

## 6. Business Rules

1. **Local only.** Images are read by Tesseract (`vie+eng`) inside the API container; PDFs from their text layer. A reading never raises: an engine failure, timeout or unreadable file is a reading with a note, and the receipt waits for staff.
2. **Reading.** Fields are found by their Vietnamese or English labels (`bank_receipt.LABELS`), matched exactly first and fuzzily (similarity >= 0.88) only otherwise; the best label wins. A value that does not look like its field is skipped. Unlabelled, a single amount with a currency mark or a single date-and-time is used; two different ones are ambiguous. A time without seconds is the end of that minute.
3. **Confident** means: every required field (`receipts.REQUIRED_FIELDS`) read; none ambiguous; the payee account not masked (a masked payer account is fine: it is not compared); each required field's line confidence >= `RECEIPT_OCR_MIN_CONFIDENCE`. Only a confident read can decide.
4. **Checks.** `check_receipt` unchanged (amount exact, reference in the memo, payee account = the business's account, payer name = KYC name, time window), plus `DUPLICATE_BANK_TXN`, plus the receiver:
   - **Receiving bank**: identified in the NAPAS list from its short name, code or full name; another bank than the business's account is `PAYEE_BANK_MISMATCH`.
   - **Holder name**: against the name the bank showed when the business's account was verified (D-031, `verified_holder_name`), ignoring accents, case and punctuation, with the usual abbreviations expanded alike (CTY = CÔNG TY, CP = CỔ PHẦN, TNHH, MTV, ...) and a bank's truncation (a prefix of at least 18 characters) accepted; otherwise `PAYEE_NAME_MISMATCH`.
5. **Verdict.** Any failed check: **DENIED**. All checks pass and the receiving bank and holder name are both shown, identified and verified: **ACCEPTED**. Otherwise **NEEDS_REVIEW**, with the reason: the reading's notes, `PAYEE_BANK_NOT_SHOWN`, `PAYEE_BANK_UNRECOGNIZED`, `PAYEE_NAME_NOT_SHOWN`, `PAYEE_NAME_UNVERIFIED`, `LATE_RECEIPT` (a late receipt is always staff's), `AUTO_VERDICT_OFF`.
6. **Accepted, not yet lent.** An accepted ticket is CONFIRMED (it counts toward the round; the business's bell rings as for a staff approval and it is emailed as for any receipt) but its loan forms only when staff approve it, or when the round is advanced or closed. Until then staff can deny it.
7. **Denied.** The receipt is rejected as staff reject one: the ticket waits for a receipt again while its hold lasts, the bank transaction id is not claimed (a correct receipt of the same transfer can still come), the business is not told. The same file cannot be uploaded again (`DUPLICATE_FILE`).
8. **OCR runs before the ticket and round are locked** and is skipped on an idempotent replay.
9. **Staff review** is once per automatic verdict, needs no typing, and its note is optional (an empty one records "Approved/Denied after viewing the receipt"). Nobody reviews their own transfer.

## 7. Error Cases

| Scenario | Behaviour | Response |
|---|---|---|
| Tesseract missing or crashes, or times out | Reading with `OCR_FAILED:<why>`; staff | 202 `AWAITING_FIELDS` |
| Scanned PDF without text | `NO_TEXT`; staff | 202 `AWAITING_FIELDS` |
| Field misread with low confidence | `LOW_CONFIDENCE:<field>`; staff | 202 `AWAITING_FIELDS` |
| Amount misread confidently | Denied; staff can approve it | 202 `REJECTED` `AMOUNT_MISMATCH` |
| Paid to the same account number at another bank | Denied | 202 `REJECTED` `PAYEE_BANK_MISMATCH` |
| Business's account never verified at the bank | Staff | 202 `AWAITING_FIELDS`, `PAYEE_NAME_UNVERIFIED` |
| Staff deny an accepted ticket whose loan formed | Refused | 409 `LOAN_STARTED` |
| Staff review an old receipt after a newer upload | Refused | 409 `SUPERSEDED` |

## 8. Acceptance Criteria

`tests/uploads/test_bank_receipt_ocr.py`, `tests/market/test_receipt_auto_verdict.py`:

- [x] `test_a_typical_receipt_is_read_field_by_field`, `test_a_two_row_layout_reads_the_value_under_its_label`, `test_the_receiving_bank_printed_beside_the_account_number_is_read`, `test_amounts_in_every_format_banks_print`, `test_a_time_without_seconds_is_the_end_of_that_minute`
- [x] `test_the_payer_account_is_never_taken_for_the_payee_account`, `test_a_slightly_misread_label_still_matches_but_a_different_one_does_not`
- [x] `test_a_masked_payee_account_cannot_decide_the_receipt`, `test_a_field_read_two_different_ways_is_left_for_staff`, `test_two_different_unlabelled_amounts_are_ambiguous`, `test_a_missing_field_or_a_low_confidence_line_is_not_confident`
- [x] `test_a_missing_engine_sends_the_receipt_to_staff_instead_of_failing`, `test_an_unreadable_image_sends_the_receipt_to_staff`, `test_a_bank_pdf_is_read_from_its_text_layer_without_ocr`, `test_tesseract_reads_a_rendered_receipt` (runs where Tesseract with Vietnamese data is installed)
- [x] `test_with_ocr_off_every_receipt_waits_for_staff`
- [x] `test_a_matching_receipt_is_confirmed_automatically_and_its_loan_waits_for_staff`
- [x] `test_a_receipt_that_does_not_match_is_denied_and_the_investor_can_upload_another`
- [x] `test_an_unsure_read_is_left_for_staff_with_the_reading_shown`, `test_with_automatic_decisions_off_the_reading_is_only_a_suggestion`, `test_a_late_receipt_is_read_but_left_for_staff`, `test_a_replayed_upload_is_not_read_again`
- [x] `test_a_bank_transaction_that_already_funds_a_ticket_is_denied`
- [x] `test_banks_are_identified_however_the_receipt_names_them`, `test_holder_names_match_across_abbreviations_and_truncation`, `test_the_receiver_is_compared_and_shown_to_staff`, `test_a_receipt_paid_to_the_wrong_receiver_is_denied`, `test_a_receiver_the_receipt_does_not_confirm_is_left_for_staff`, `test_a_wrong_detail_denies_even_when_the_receiving_bank_is_not_shown`
- [x] `test_staff_see_the_verdict_and_the_ocr_evidence_and_the_ticket_waits_for_them`
- [x] `test_staff_approving_a_receipt_the_system_accepted_starts_its_loan`, `test_staff_denying_a_receipt_the_system_accepted_voids_the_ticket`
- [x] `test_staff_approving_a_receipt_the_system_denied_records_it_received`, `test_staff_can_approve_a_denied_receipt_after_the_hold_lapsed`, `test_staff_denying_a_receipt_the_system_denied_keeps_the_rejection`
- [x] `test_an_accepted_receipt_cannot_be_denied_once_its_loan_started`, `test_a_denied_receipt_replaced_by_a_newer_one_is_reviewed_through_the_newer_one`

## 9. Open Questions

| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | May a system verdict confirm a ticket without a person (ADR-009 point 4, `transfer-and-evidence.md` rule 12)? Q-033 | Edward, Loc | Built behind `RECEIPT_AUTO_VERDICT_ENABLED`, off |
| 2 | Is local OCR inside Q-025 option A ("local OCR later"), so it may run in production on the current host? | Minh, Loc | — |
| 3 | Accuracy on real receipts: labels and thresholds were tuned on rendered samples only. Run with `RECEIPT_OCR_ENABLED` on and automatic decisions off, compare suggestions with staff decisions, then switch decisions on | Phat | — |
| 4 | Should a holder-name mismatch with the right bank and account number deny, or go to staff? Built: deny (staff can reverse) | Phat | Phat, 2026-11-14: check the receiver's banking information |
| 5 | Refund and repayment receipts | Phat | Later |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 0.1 | 2026-11-14 | Phat / Claude | First draft, with the implementation behind switches |
