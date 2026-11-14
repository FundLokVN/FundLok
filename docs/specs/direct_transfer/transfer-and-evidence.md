# Spec: Transfers, Evidence, Receipt Checks and the Exceptions Queue

| Field | Value |
|---|---|
| **Status** | DRAFT |
| **Owner** | Edward |
| **Implementer(s)** | Edward (`app/direct_transfer/`); Phat (upload UI, staff screens, routers) |
| **Module** | `app/direct_transfer/` (`models.py`, `receipts.py`, `vietqr.py`, `evidence.py`, `storage.py`, `accounts.py`) |
| **Version** | 0.1 |
| **Date** | 2026-10-28 |
| **Related ADR** | ADR-009 |
| **Depends on** | `funding-window.md`, `disputes.md`, `ledger-amendment.md` |
| **Source** | `Loc.md` "Build spec" (objects, confirmation levels, receipt checks), `Loc2.md` "Manual document scanning" and "Dispute queue and evidence log"; HANDOFF-04 I-14, I-15 |

---

## 1. Context & Goal

FundLok has no bank API and no bank-data partner (D-018). Every confirmation therefore rests on documents the customer uploads (transfer receipts and bank statements) that FundLok reads, checks against the QR it issued, and staff review. The core object is the **transfer**: a party-to-party movement of money with its evidence and a **confirmation level**. Everything is recorded in an append-only evidence log so a dispute, a regulator or the Decree 94 audit can reconstruct what was known and when.

**Goal:** define `payment_accounts`, `transfers`, `evidence_files` and the append-only `evidence_log`; the VietQR payload; the receipt checks; duplicate rules; the exceptions queue; and the `ReceiptExtractor` and `EvidenceStorage` interfaces (with the MVP manual-entry extractor).

---

## 2. Out of Scope

- A real OCR, LLM or bank-data integration. **No receipt may be sent to an external AI/OCR service** (I-14, Q-025). Local OCR can be added behind `ReceiptExtractor` later. *Proposed (DRAFT, Q-033): local OCR and an automatic verdict in `receipt-ocr.md`, behind switches that are off.*
- Bank feeds, OAuth, SePay/Casso (ADR-009).
- The `VERIFIED` (bank-data match) level: reserved in the enum, never set.
- Dispute decisions (see `disputes.md`); tranche transitions (see `funding-window.md`).
- Receipt validity against the bank (a forged receipt without bank data cannot be detected; mitigations are in section 6).

---

## 3. Data Model

Money is integer VND (`BIGINT`). Timestamps `TIMESTAMPTZ`. UUID keys.

### New table: `payment_accounts` (I-15)

```
payment_accounts
├── id              UUID PK
├── user_id         UUID NOT NULL → users(id)
├── holder_role     TEXT NOT NULL CHECK IN ('SME','INVESTOR')
├── holder_type     TEXT NOT NULL CHECK IN ('INDIVIDUAL','ENTITY')   -- drives withholding (PROVISIONAL(Q-D6))
├── bank_bin        TEXT NOT NULL CHECK (bank_bin ~ '^[0-9]{6}$')    -- NAPAS BIN
├── bank_name       TEXT NOT NULL
├── account_number  TEXT NOT NULL CHECK (account_number ~ '^[0-9A-Za-z]{4,32}$')
├── account_name    TEXT NOT NULL                                     -- as registered at the bank
├── status          TEXT NOT NULL DEFAULT 'PENDING' CHECK IN ('PENDING','APPROVED','REJECTED')
├── evidence_file_id UUID NULL → evidence_files(id)                   -- bank statement / bank confirmation letter
├── approved_by UUID NULL → users(id), approved_at TIMESTAMPTZ NULL, rejection_reason TEXT NULL
├── created_at TIMESTAMPTZ
UNIQUE (user_id, bank_bin, account_number)
```
This is **not** `linked_accounts` (the mock banking table). Who verifies an account (D-031, PROVISIONAL(Q-D18); Edward and Loc 2026-10-30). The standard is **FundLok name = KYC (investor) or KYB (SME) name = bank account holder name**, for investors and SMEs alike. Money disbursed to an SME, refunds and disputes must lead to the verified party (AML).

- Every saved account starts `PENDING` ("waiting verification"). Nobody can reserve a ticket or open a window with it until it is `APPROVED`.
- The account name the user typed is **self-declared and proves nothing**. It only produces a hint: `name_match` (does the typed name equal the KYC/KYB name) and `expected_name`, and a `PAYMENT_ACCOUNT_NAME_MISMATCH` log entry when it differs.
- Staff verify it: they look up the holder name at the bank (today: type the bank and account number into a bank app's transfer screen, which shows the holder without moving money), compare it with the KYC/KYB name, and approve or reject. A written reason is required **either way**. To approve, staff record `verified_holder_name`, the name exactly as the bank shows it.
  - **INVESTOR**: `verified_holder_name` must equal the KYC name (`409 HOLDER_NAME_MISMATCH` otherwise; reject the account instead). With no KYC name on file: `409 KYC_NAME_MISSING`.
  - **SME**: the same, but a difference (an abbreviation, a trade name) can be approved with `accept_difference: true` and a reason saying why. The SME's KYB name is the newest APPROVED eKYB attempt's business name; the project's own `legal_name` is self-declared and not used. With no approved KYB there is nothing to compare, so approval also needs `accept_difference`.
- A decision is final (`409 ALREADY_DECIDED`). A rejected account number cannot be saved again for now (Q-030).
- Everything is in the append-only evidence log: who decided, when, the reason, the holder name seen, the expected name, whether they matched, and whether a difference was accepted.
- **Automatic verification (later, Q-029):** `name_lookup.BankNameLookup` returns the holder name from the bank. An account is auto-approved (`approved_by` NULL) only if the name the **bank** returns equals the KYC/KYB name. No provider is connected; the default `NoLookup` leaves every account to staff.
- Names are compared with the receipt check's normalization (`normalize_name`: case, accents, punctuation and spacing ignored). Anything else, such as "CTY" for "CONG TY", is a difference.
- `PaymentAccountOut` carries `name_match` (`true`, `false`, or `null` when there is no verified name) and `expected_name`. Both are computed on read, not stored; the staff queue carries them too.

### New table: `evidence_files`

```
evidence_files
├── id UUID PK, owner_user_id UUID NOT NULL → users(id)
├── purpose TEXT NOT NULL CHECK IN ('RECEIPT','BANK_STATEMENT','SME_STATEMENT','INVESTOR_STATEMENT','SIGNED_DOCUMENT','BANK_CONFIRMATION','DECLARATION','INVOICE','OTHER')
├── storage_key TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, size_bytes BIGINT NOT NULL, content_type TEXT NOT NULL, original_filename TEXT NULL
├── created_at TIMESTAMPTZ
UNIQUE INDEX uq_evidence_receipt_sha256 (sha256) WHERE purpose = 'RECEIPT'     -- one file funds one tranche
```

### New table: `transfers`

```
transfers
├── id UUID PK
├── kind TEXT NOT NULL CHECK IN ('DISBURSEMENT','REFUND','REPAYMENT','FEE_PAYMENT')
├── tranche_id UUID NULL → tranches(id), repayment_day_id UUID NULL, fee_invoice_id UUID NULL
├── from_account_id UUID NULL → payment_accounts(id)    -- payer
├── to_account_id   UUID NULL → payment_accounts(id)    -- payee (NULL for FundLok fee payments: FundLok's own account is a reference, not a payment_accounts row)
├── amount_vnd BIGINT NOT NULL CHECK (amount_vnd > 0)
├── reference TEXT NOT NULL                              -- the code carried in the memo
├── rail TEXT NOT NULL CHECK IN ('VIETQR','STANDING_ORDER','BATCH_UPLOAD','MANUAL')
├── confirmation_level TEXT NOT NULL CHECK IN ('REPORTED','ATTESTED','CONFIRMED','VERIFIED')
├── check_status TEXT NOT NULL CHECK IN ('AWAITING_FIELDS','MATCHED','MISMATCH','REJECTED','DUE')   -- DUE: a REFUND the SME still owes
├── failure_codes JSONB NOT NULL DEFAULT '[]'
├── evidence_file_id UUID NULL → evidence_files(id)
├── parsed_fields JSONB NULL                             -- ReceiptFields as extracted / entered
├── bank_txn_id TEXT NULL
├── transferred_at TIMESTAMPTZ NULL                      -- time on the receipt
├── reported_by UUID NULL → users(id)
├── created_at, updated_at TIMESTAMPTZ
UNIQUE INDEX uq_transfers_bank_txn_id (bank_txn_id) WHERE bank_txn_id IS NOT NULL AND kind IN ('DISBURSEMENT','REFUND')   -- a bank transaction funds at most one tranche
```
State history of a transfer (level changes, field entry, checks) is appended to `evidence_log`; the row itself holds only the current level.

### New table: `evidence_log` (append-only)

```
evidence_log
├── id UUID PK
├── entity_type TEXT NOT NULL      -- 'TRANCHE','TRANSFER','DISPUTE','WINDOW','REPAYMENT_DAY','FEE_INVOICE','PAYMENT_ACCOUNT'
├── entity_id UUID NOT NULL
├── event_type TEXT NOT NULL       -- see section 6 rule 14
├── actor_id UUID NULL, actor_role TEXT NULL     -- NULL actor = system/job
├── reason TEXT NULL               -- mandatory for staff overrides (who/why/when/approval)
├── payload JSONB NOT NULL DEFAULT '{}'    -- uploaded file id, extracted fields, from/to states, check results
├── file_sha256 TEXT NULL
├── occurred_at TIMESTAMPTZ NOT NULL
└── created_at TIMESTAMPTZ DEFAULT now()
```
**Append-only at the DB layer**: `BEFORE UPDATE OR DELETE` trigger `trg_evidence_log_append_only` raising an exception (same pattern as `ledger_immutability_guard`). Application code has no update or delete path.

### Interfaces (Python `Protocol`s)

```python
@dataclass(frozen=True)
class ReceiptFields:
    amount_vnd: int; transferred_at: datetime; payer_name: str; payer_account: str
    payee_account: str; memo: str; bank_txn_id: str; payee_name: str | None = None

class ReceiptExtractor(Protocol):
    async def extract(self, *, content: bytes, content_type: str) -> ReceiptFields | None: ...
        # None = could not extract; the transfer waits for staff entry

class ManualEntryExtractor:  # MVP: always returns None; staff enter the fields

class EvidenceStorage(Protocol):
    async def put(self, key: str, content: bytes, content_type: str) -> None
    async def get(self, key: str, *, max_bytes: int) -> bytes
# adapters: InMemoryStorage (tests), R2Storage (wraps app/utils/r2.py; storage location is Q-025)
```

### Alembic migration
One migration chained off the current head (hash revision id), created by `alembic revision --autogenerate` and hand-completed with the triggers and partial indexes. Revision id is recorded in the HANDOFF-04 report.

---

## 4. API Contract

### `POST /funding/tranches/{tranche_id}/receipt`  — investor uploads the receipt
**Auth:** INVESTOR (owner). **Service:** `submit_receipt`. `multipart/form-data`: `file` (image or PDF, max 10 MB). **Headers:** `Idempotency-Key` (replay returns the same transfer).
**202**
```json
{ "transfer_id": "uuid", "tranche_status": "SIGNED|ATTESTED|UNDER_REVIEW|EXPIRED",
  "check_status": "AWAITING_FIELDS|MATCHED|MISMATCH", "failure_codes": ["AMOUNT_MISMATCH"],
  "confirmation_level": "REPORTED|ATTESTED" }
```
With `ManualEntryExtractor` the result is `AWAITING_FIELDS` / `REPORTED` until staff enter the fields.

### `GET /funding/tranches/{tranche_id}/payment`  — QR screen data
**Auth:** INVESTOR (owner), tranche `SIGNED`.
```json
{ "bank_name": "string", "bank_bin": "970407", "account_number": "string", "account_name": "string",
  "amount_vnd": 10000000, "reference_code": "FLAB12CD34", "vietqr_payload": "string",
  "hold_expires_at": "ts", "warning": "Pay from an account in your own name; money sent above the ticket is not part of the loan." }
```

### Staff (admin) — `app/direct_transfer/admin.py`, Phat wires routers in `app/admin/`
- `GET /admin/direct-transfer/exceptions?status=open` -> `[ExceptionItem]` (transfers `AWAITING_FIELDS` or tranches `UNDER_REVIEW` cause `RECEIPT_MISMATCH` or `LATE_RECEIPT`), each with the file download URL, extracted fields, failure codes and the evidence timeline.
- `POST /admin/direct-transfer/transfers/{id}/fields` body `ReceiptFieldsIn` (staff manual entry) -> runs the checks -> `{check_status, failure_codes, tranche_status}`. Requires `reason` (who/why recorded).
- `POST /admin/direct-transfer/payment-accounts/{id}/decision` body `{ "decision": "APPROVED|REJECTED", "reason": "string" }`.
- Disputes: see `disputes.md`.

- `POST /evidence-files` (multipart `purpose`, `file`) -> `{ "file_id": "uuid", "sha256": "string" }` (stores via `EvidenceStorage`; same type and size limits).
- `POST /payment-accounts` (user registers an account, uploads a statement) -> `PaymentAccountOut`; `GET /payment-accounts` lists the caller's accounts.
- `POST /funding/refunds/{transfer_id}/receipt` (SME): a refund receipt, same shape and checks as the investor receipt, with payer = SME account and payee = the voided tranche's investor account.
- `GET /funding/windows/{window_id}/tranches` (SME of the window): tranche inbox rows with masked investor name.

**Errors:** 400 invalid transition / invalid file; 404 not found or not the caller's; 409 `DUPLICATE_BANK_TXN`, `DUPLICATE_FILE`, `HOLD_EXPIRED` (not used for receipts: late receipts are accepted, see rule 9), `TRANCHE_NOT_SIGNED`; 413 file too large; 415 unsupported type; 422 validation.

---

## 5. State Machine

Transfer `confirmation_level` only moves up: `REPORTED -> ATTESTED -> CONFIRMED` (`VERIFIED` reserved). `check_status`: `AWAITING_FIELDS -> MATCHED | MISMATCH`; `REJECTED` for duplicates. Tranche transitions that this spec triggers are defined in `funding-window.md` (2, 6, 12).

```
upload receipt          -> transfer REPORTED / AWAITING_FIELDS (or MATCHED/MISMATCH if the extractor returned fields)
checks pass             -> transfer ATTESTED, tranche SIGNED -> ATTESTED
checks fail             -> transfer MISMATCH,  tranche SIGNED -> UNDER_REVIEW (RECEIPT_MISMATCH)
SME confirms / lapses   -> transfer CONFIRMED, tranche ATTESTED -> CONFIRMED
```

---

## 6. Business Rules

1. **Reference code**: `FL` + 8 characters from `A-Z0-9` (36^8 space), generated from `secrets` (CSPRNG), no diacritics. The shortest memo among target banks is unchecked (Loc: "check before fixing the format"); the format is a constant `REFERENCE_LENGTH = 8`.
2. **VietQR payload** (NAPAS 247 / EMVCo MPM, TLV): `00 02 "01"`, `01 02 "12"` (dynamic, amount fixed), `38 LL` (`00 10 "A000000727"`, `01 LL` (`00 06 <BIN>`, `01 LL <account>`), `02 08 "QRIBFTTA"`), `53 03 "704"`, `54 LL <amount>`, `58 02 "VN"`, `62 LL` (`08 LL <memo>`), `63 04 <CRC>`; CRC is CRC-16/CCITT-FALSE (poly `0x1021`, init `0xFFFF`) over the whole payload including `6304`, uppercase hex. The memo (`08`) is the reference code. Pure functions with test vectors.
3. **Payer account**: must be the investor's own account (`from_account_id` is the investor's `APPROVED` account) and the payer name on the receipt must match the KYC name (rule 6).
4. **Receipt required fields**: amount, date and time, payer name, payer account, payee account, memo and the bank transaction ID. A receipt missing any of them is `MISSING_FIELD:<name>`.
5. **Exact amount**: `receipt.amount_vnd == tranche.amount_vnd` (`AMOUNT_MISMATCH`). The memo, with spaces and punctuation removed and upper-cased, must contain the reference code (`REFERENCE_MISSING`). The payee account number (digits compared, leading zeros preserved) must equal the SME's registered account (`PAYEE_MISMATCH`). The payer name must match the KYC name (`PAYER_NAME_MISMATCH`).
6. **Name matching** (PROVISIONAL(Q-D13)): both names are Unicode-NFKD folded, combining marks removed, `đ`/`Đ` -> `d`, upper-cased, non-alphanumerics replaced by single spaces, trimmed; equal strings match. Anything else is a mismatch and goes to staff. The KYC name is the latest approved `gverify_verifications.full_name` for the investor, else `users.full_name`.
7. **Transaction time**: the receipt's `transferred_at` must not be earlier than `tranche.reserved_at` (a transfer made before the reservation cannot carry this reference) and not later than `now + 5 minutes` (`TIME_OUT_OF_RANGE`).
8. **Duplicates**: a `bank_txn_id` already attached to a `DISBURSEMENT` or `REFUND` transfer, or a file whose SHA-256 already exists as a `RECEIPT`, is **rejected** (409 `DUPLICATE_BANK_TXN` / `DUPLICATE_FILE`); no new transfer row counts; a `DUPLICATE_REJECTED` log row is appended. Both are enforced by unique indexes, not application code alone. One bank txn or file funds at most one tranche.
9. **Late receipt**: if the tranche is `EXPIRED` (window not yet finalized) the receipt is accepted as `REPORTED`, the tranche goes `EXPIRED -> UNDER_REVIEW(LATE_RECEIPT)`, and staff may revive it only if capacity remains (PROVISIONAL(Q-D3)); otherwise the SME must refund (tracked `REFUND` transfer). A receipt for a `VOID` tranche or a finalized window is `409 TRANCHE_NOT_SIGNED`.
10. **Receipt while hold is live**: a `SIGNED` tranche with `receipt_submitted_at` set is exempt from hold expiry (it waits for staff).
11. **Exceptions queue**: contains transfers `AWAITING_FIELDS`, tranches `UNDER_REVIEW` with cause `RECEIPT_MISMATCH` or `LATE_RECEIPT`. Staff enter fields (manual entry) or decide the outcome. Every staff action requires a non-empty `reason` and records `actor_id`, time and role (overrides capture who, why, when, approval).
12. **Confirmation levels**: `REPORTED` (document uploaded) -> `ATTESTED` (fields match, checked by scan and staff) -> `CONFIRMED` (counterparty confirmed or window lapsed). `VERIFIED` is reserved.
13. **Evidence is stored once**: file bytes go to `EvidenceStorage` under `evidence/{purpose}/{sha256[:2]}/{file_id}`; the DB stores key, hash, size and content type. Accepted types: PDF, PNG, JPEG, WEBP; max 10 MB (configurable).
14. **Evidence log event types**: `RECEIPT_UPLOADED`, `FIELDS_ENTERED`, `CHECKS_RUN`, `DUPLICATE_REJECTED`, `TRANCHE_TRANSITION`, `CAP_CHECK`, `STUB_CAP_TEST`, `SME_CONFIRMED`, `SME_DISPUTED`, `DISPUTE_EVIDENCE`, `DISPUTE_DECISION`, `PAYMENT_ACCOUNT_DECISION`, `REPAYMENT_DAY_REPORTED`, `FEE_INVOICE_EVENT`, `REFUND_RECORDED`. Each row stores uploaded file id and hash, the extracted fields, each side's statements, the outcome and the decider.
15. **Storage and OCR location** (I-14): evidence stays behind `EvidenceStorage`; production location is open (Q-025). No receipt is sent outside the platform for extraction.
16. **Least privilege**: an investor sees only their own transfers; the SME sees receipts for its window's tranches; staff see all. Resources the caller does not own return 404.

---

## 7. Error Cases

| Scenario | System behaviour | Response |
|---|---|---|
| Receipt re-uploaded (same bytes) | `DUPLICATE_FILE`; log row; nothing new counted | 409 |
| Same bank transaction ID on a second tranche | rejected, both tranches unaffected, log row | 409 `DUPLICATE_BANK_TXN` |
| Amount differs by 1 VND | `AMOUNT_MISMATCH`; tranche `UNDER_REVIEW` | 202 with failure codes |
| Payer name has different diacritics only | normalised, matches | 202 `MATCHED` |
| Payer name is a relative's | `PAYER_NAME_MISMATCH`; staff review; money return needs the SME | 202 with failure codes |
| Receipt for a tranche that is not the caller's | 404 | 404 |
| Receipt file not PDF/image or over 10 MB | rejected before storage | 415 / 413 |
| Staff enter fields on a transfer already `MATCHED` | rejected | 400 |
| Storage failure | nothing recorded; retry | 503 `{"detail":"Storage unavailable"}` |
| Extractor returns `None` | transfer `AWAITING_FIELDS`; exceptions queue | 202 |

---

## 8. Acceptance Criteria

Tests: `tests/direct_transfer/test_receipts.py` (pure checks), `test_vietqr.py`, `test_evidence.py`, `test_exceptions.py`.

- [ ] `test_reference_code_format_and_uniqueness` — `^FL[A-Z0-9]{8}$`, CSPRNG source, collision retry
- [ ] `test_vietqr_crc16_known_vector` — CRC-16/CCITT-FALSE of `123456789` = `0x29B1`
- [ ] `test_vietqr_payload_structure_and_crc_round_trip` — parse TLVs back; fields equal inputs; CRC verifies
- [ ] `test_vietqr_payload_rejects_non_positive_amount_and_long_memo`
- [ ] `test_receipt_check_happy_path_matches`
- [ ] `test_receipt_check_amount_must_be_exact`
- [ ] `test_receipt_check_memo_must_carry_reference_ignoring_case_and_spaces`
- [ ] `test_receipt_check_payee_must_be_sme_registered_account`
- [ ] `test_receipt_check_payer_name_diacritic_folded_case_insensitive`
- [ ] `test_receipt_check_payer_name_mismatch_flags`
- [ ] `test_receipt_check_missing_required_fields_rejected`
- [ ] `test_receipt_check_time_before_reservation_flagged`
- [ ] `test_receipt_submission_matched_attests_tranche_and_logs`
- [ ] `test_receipt_submission_mismatch_moves_to_under_review`
- [ ] `test_manual_entry_extractor_leaves_transfer_awaiting_fields_and_hold_does_not_expire`
- [ ] `test_staff_field_entry_runs_checks_and_attests`
- [ ] `test_staff_action_requires_reason`
- [ ] `test_duplicate_bank_txn_id_rejected_and_unique_index_enforced`
- [ ] `test_duplicate_file_hash_rejected`
- [ ] `test_one_bank_txn_cannot_fund_two_tranches`
- [ ] `test_late_receipt_goes_to_exceptions_queue`
- [ ] `test_evidence_log_is_append_only_at_db_level` — UPDATE and DELETE raise
- [ ] `test_evidence_log_has_no_update_or_delete_function_in_service` (app layer)
- [ ] `test_payment_account_requires_staff_approval_before_use`
- [ ] `test_receipt_upload_rejects_wrong_content_type_and_size`
- [ ] `test_investor_cannot_submit_receipt_for_someone_elses_tranche_returns_404`
- [ ] `test_no_external_ocr_called` — extractor interface is the only extraction seam; default does not touch the network

---

## 9. Open Questions

| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | Is receipt plus SME confirmation traceable enough for Art. 11.1.b (Q-018)? | Legal | — |
| 2 | Where is evidence stored in production, and may a domestic OCR vendor be used (Q-025)? | Minh / Loc | — |
| 3 | Shortest memo length among target banks, to fix the reference format | Phat / Minh | — |
| 4 | Name matching beyond normalised equality (e.g. word order) (Q-D13) | Edward | — |
| 5 | Retention period for evidence and logs under Decree 94 (Q-008) | Loc | — |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 0.1 | 2026-10-28 | Edward / Claude | Initial draft from HANDOFF-04 |
