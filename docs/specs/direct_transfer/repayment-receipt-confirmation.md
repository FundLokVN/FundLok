# Spec: Investor Repayment Receipt Confirmation

| Field | Value |
|---|---|
| **Status** | IMPLEMENTED |
| **Owner** | Phat |
| **Implementer(s)** | Phat (`app/market/repayment_receipts.py`, `app/market/repayment_router.py`, `app/admin/repayments.py`, frontend) |
| **Module** | `app/market/`, `app/admin/` |
| **Version** | 1.0 |
| **Date** | 2026-11-04 |
| **Related ADR** | ADR-009 |
| **Depends on** | `repayment-collection.md`, `transfer-and-evidence.md`, `disputes.md`, `ledger-amendment.md` |

---

## 1. Context & Goal

On the direct-transfer rail (ADR-009), an SME repays each investor directly into their bank account and uploads its bank transfer receipt (`POST /repayment/tranches/{tranche_id}/pay`). Rather than having FundLok operations staff approve or deny each repayment receipt, the investor who receives the payment checks their own bank statement and confirms that the repayment reached their account. Operations staff retain read-only visibility into repayment receipts and continue to resolve missed-day disputes raised from the repayment schedule.

**Goal:** move repayment receipt confirmation from admin approval to the receiving investor (`GET /repayment/receipts`, `POST /repayment/receipts/{receipt_id}/confirm`) and make the admin repayment receipt view read-only.

---

## 2. Out of Scope

- Changing the SME upload flow (`POST /repayment/tranches/{tranche_id}/pay`) or the investor's missed-day report flow (`POST /repayment/days/{day_id}/report-missing`).
- Admin dispute resolution (`POST /admin/direct-transfer/disputes/{id}/decide`), which remains staff-owned when an investor reports a day missing and does not subsequently confirm the receipt.
- Automatic bank-statement or Open API reconciliation.

---

## 3. Data Model

No schema or Alembic migration is required. Existing tables and columns are used:

- `transfers`: rows with `kind = 'REPAYMENT'` grouped by `evidence_file_id` (the receipt `UUID`). `check_status` transitions from `AWAITING_FIELDS` to `MATCHED` and `confirmation_level` to `CONFIRMED` on investor confirmation.
- `repayment_days`: each day paid by the receipt transitions to `status = 'MATCHED'`, with `matched_at` and `settled_at` set and `transfer_id` linked.
- `disputes`: if the investor had opened an unresolved report (`status != 'DECIDED'`) on a day covered by the receipt, confirming the receipt closes that dispute with `outcome = 'PAID'` (`decided_by = investor.id`, `actor_role = 'INVESTOR'`).
- `evidence_log`: `REPAYMENT_DAY_REPORTED` (`event = 'MATCHED'`) and `DISPUTE_DECISION` entries record `actor_role = 'INVESTOR'` and `actor_id = investor.id`.
- `audit_logs`: records `REPAYMENT_RECEIPT_CONFIRMED` on `TRANCHE` (included under `ACTIVITY_GROUPS["INVESTOR"]`).
- `notifications`: adds `REPAYMENT_RECEIPT_SUBMITTED` (sent to the investor on SME receipt upload) and `REPAYMENT_RECEIPT_CONFIRMED` (sent to the SME on investor confirmation).

---

## 4. API Contract

### `GET /repayment/receipts?status=AWAITING|ALL&limit=50`

**Auth:** Bearer JWT — role: `INVESTOR`

Returns receipts uploaded by SMEs for repayments to the authenticated investor, ordered with receipts awaiting confirmation first (`AWAITING_FIELDS`), then newest `submitted_at` first. Only receipts whose `REPAYMENT` transfers exclusively pay the authenticated investor are returned.

Each receipt also carries `system_check` (`receipt-ocr.md` section 10): what the system read off it, as advice (`MATCHES`, `DOES_NOT_MATCH` with `failure_codes`, or `UNSURE`), null when OCR was off. It never confirms a receipt: the investor does.

**Response 200** (`list[RepaymentReceiptOut]`)
```json
[
  {
    "id": "uuid",
    "tranche_id": "uuid",
    "reference_code": "FL8TKK1UAQ",
    "project_name": "ABC Co",
    "submitted_at": "2026-11-16T03:00:00Z",
    "status": "AWAITING_CONFIRMATION",
    "confirmed_at": null,
    "gross_vnd": 142023,
    "tax_withheld_vnd": 1148,
    "net_vnd": 140875,
    "days": [
      {
        "repayment_day_id": "uuid",
        "kind": "STUB",
        "due_date": "2026-11-16",
        "principal_vnd": 0,
        "interest_vnd": 20000,
        "gross_vnd": 20000,
        "tax_withheld_vnd": 1000,
        "net_vnd": 19000,
        "status": "PRESUMED_PAID"
      }
    ],
    "payee": {
      "bank_name": "Techcombank",
      "account_number": "19036789012",
      "account_name": "NGUYEN VAN A"
    },
    "payer": {
      "bank_name": "VietinBank",
      "account_number": "123123123123",
      "account_name": "ABC CO LTD"
    },
    "original_filename": "receipt.png",
    "content_type": "image/png",
    "open_report": false
  }
]
```

### `POST /repayment/receipts/{receipt_id}/confirm`

**Auth:** Bearer JWT — role: `INVESTOR`

Confirms that the repayment covered by `receipt_id` reached the investor's bank account.

**Response 200** (`RepaymentReceiptOut` with `status = "CONFIRMED"` and `confirmed_at` populated)

**Error Responses**

| Status | Code | Condition |
|---|---|---|
| 403 | — | Caller is not an `INVESTOR` |
| 404 | — | Receipt does not exist, has no `REPAYMENT` transfers, or any transfer on it pays another investor |
| 409 | `NOTHING_TO_CONFIRM` | Every transfer on the receipt is already confirmed/decided (`check_status != 'AWAITING_FIELDS'`) |

### Admin endpoints (`app/admin/router.py`)

- `GET /admin/funding/repayments` and `GET /admin/funding/repayments/{receipt_id}` and `GET /admin/funding/repayments/{receipt_id}/receipt-url` remain available for read-only staff inspection.
- `POST /admin/funding/repayments/{receipt_id}/decision` is removed.

---

## 5. State Machine

```
Transfer.check_status:
  AWAITING_FIELDS ──[investor POST /repayment/receipts/{id}/confirm]──► MATCHED (confirmation_level = CONFIRMED)

RepaymentDay.status:
  PRESUMED_PAID   ──[investor confirms receipt]───────────────────────► MATCHED (settled_at set, ledger posted)
  REPORTED_MISSED ──[investor confirms receipt]───────────────────────► MATCHED (open dispute closed as PAID, settled_at set, ledger posted)
```

---

## 6. Business Rules

1. **Payee isolation (404, not 403).** Only the investor whom every `REPAYMENT` transfer on a receipt pays may list or confirm that receipt. Any other investor receives `404 Receipt not found` so receipt IDs cannot be enumerated across investors; a receipt shared across multiple investors via the legacy `mark-paid` route is hidden from investor endpoints.
2. **Settlement and ledger posting.** Confirming a receipt calls `match_day(..., actor_role="INVESTOR")` for each waiting transfer whose day has no open dispute, posting the principal settlement legs to the ledger idempotently (`dt:repay:{day.id}:borrower` and `dt:repay:{day.id}:lender`) and closing the facility/window when all days are settled.
3. **Closing the investor's own open missing report.** If a day covered by the receipt has an open `Dispute` (`status != "DECIDED"`), confirming the receipt resolves that dispute via `decide_dispute(..., outcome="PAID", actor_role="INVESTOR")`, links `day.transfer_id`, and sets `transfer.confirmation_level = "CONFIRMED"`.
4. **Finality.** Confirmation is one-way and idempotent against double-clicks: once no transfer on the receipt has `check_status == "AWAITING_FIELDS"`, a subsequent confirm call returns `409 NOTHING_TO_CONFIRM`.
5. **Notifications.** Uploading a receipt (`POST /repayment/tranches/{tranche_id}/pay`) notifies the investor (`REPAYMENT_RECEIPT_SUBMITTED`). Confirming a receipt notifies the SME (`REPAYMENT_RECEIPT_CONFIRMED`).
6. **Admin read-only.** Staff can list and inspect repayment receipts and view the investor's `REPAYMENT_RECEIPT_CONFIRMED` entry under the `INVESTOR` activity group, but cannot approve or deny repayment receipts.

---

## 7. Error Cases

| Scenario | System behaviour | Response to caller |
|---|---|---|
| Another investor requests or confirms `receipt_id` | Ownership check fails | `404 {"detail": "Receipt not found"}` |
| Investor confirms an already-confirmed receipt | No transfer is in `AWAITING_FIELDS`; no state or audit change | `409 {"detail": "Nothing on this receipt waits for your confirmation"}` (`X-Error-Code: NOTHING_TO_CONFIRM`) |
| Receipt covers a day reported missing by the investor | Dispute is decided as `PAID` (`actor_role="INVESTOR"`), day becomes `MATCHED` and settled | `200 RepaymentReceiptOut` (`open_report: false`, `status: "CONFIRMED"`) |
| Admin calls removed `/admin/funding/repayments/{id}/decision` | Route no longer exists | `404` / `405` |
| OCR on (`receipt-ocr.md` section 10): the business's receipt clearly does not match the repayment | Upload refused before anything is stored or marked; days stay `DUE` | `422` (`X-Error-Code: RECEIPT_MISMATCH`, `details.failure_codes`) |

---

## 8. Acceptance Criteria

- [x] `test_the_investor_sees_the_receipt_paying_them_awaiting_their_confirmation`
- [x] `test_confirming_matches_and_settles_the_days_and_tells_the_business`
- [x] `test_only_the_investor_the_receipt_pays_can_confirm_it`
- [x] `test_a_receipt_is_confirmed_once`
- [x] `test_confirming_closes_the_investor_s_own_missing_report_as_paid`
- [x] `test_a_receipt_that_also_pays_another_investor_is_not_shown`
- [x] `test_staff_do_not_decide_repayment_receipts`
- [x] `test_a_receipt_the_investor_confirmed_shows_as_decided_for_staff`

---

## 9. Open Questions

None.

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-11-04 | Phat | Initial implementation spec |
| 1.1 | 2026-11-14 | Phat / Claude | `system_check` on each receipt; the upload can be refused with `RECEIPT_MISMATCH` (receipt-ocr.md section 10) |
