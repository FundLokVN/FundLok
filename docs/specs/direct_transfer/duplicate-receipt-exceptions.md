# Spec: Duplicate transfer receipts go to the staff exceptions queue

| Field | Value |
|---|---|
| **Status** | DRAFT |
| **Owner** | Edward |
| **Implementer(s)** | Edward (service) / Phat (route and screen, when he wires the queue) |
| **Module** | `app/direct_transfer/`, `app/market/receipt_auto.py` |
| **Version** | 1.0 |
| **Date** | 2026-11-25 |
| **Related ADR** | ADR-009 |
| **Depends on** | `transfer-and-evidence.md` (rule 8 duplicates), `receipt-ocr.md`; Jira SCRUM-624 (AM-E19), Appendix M 5.5, handbook 10.10 |

No schema change, no migration, no ledger leg. Edward owns the direct-transfer service and Phat owns the OCR integration in `app/market/` and the admin routes and screen.

---

## 1. Context & Goal

An investor who uploads a receipt that FundLok has already seen is refused today, and that is correct: one file, and one bank transaction, funds at most one ticket. File-hash duplicates are refused with a 409. OCR can return a 202 with a rejected receipt for a reused bank transaction ID. Neither path adds a dedicated duplicate item to the exceptions queue today. Handbook 10.10 says a duplicate or edited screenshot fails the check **and goes to the staff exceptions queue**. A different investor sending someone else's receipt is exactly what staff should look at.

Where duplicates are detected today:

| Check | Where | What is kept |
|---|---|---|
| Same file bytes (SHA-256) | `store_evidence_file` -> `DUPLICATE_FILE` | An `evidence_log` row `DUPLICATE_REJECTED` on the TRANCHE with `{"reason": "DUPLICATE_FILE"}`. The file itself is not stored (rejected before storage). |
| Same bank transaction ID from staff entry | `apply_receipt_fields` -> `DUPLICATE_BANK_TXN` | The rejected transfer, stored file, and `DUPLICATE_REJECTED` evidence event on the transfer. |
| Same bank transaction ID from automatic OCR | `app/market/receipt_auto.py::run_system_check` | `SYSTEM_VERDICT` with `DUPLICATE_BANK_TXN` in its failure codes. When the denial is applied, the transfer is rejected and the upload returns 202. This path does not yet write `DUPLICATE_REJECTED`. |

The bank transaction ID checks also catch an **edited** copy of an already-used receipt, as long as the bank transaction id survives the edit: the bytes differ, the id is the same.

**Goal:** every `DUPLICATE_REJECTED` event appears in `list_exceptions` as a `DUPLICATE_RECEIPT` item that names the original receipt, until a staff member reviews it.

---

## 2. Out of Scope

- Detecting an edited image whose bank transaction id was also changed or removed. That needs image forensics, which `receipt-ocr.md` already puts out of scope.
- Duplicate checks on **repayment** receipts (SME to investor, `app/market/repayment_router.py`). Same refusal exists there; surfacing it is a separate ticket if wanted.
- Changing investor-facing responses or ticket states. Keep the existing 409 behavior for file and manual-field duplicates, and the existing 202 and verdict behavior for OCR uploads.
- Any automatic penalty on the investor (see Open Questions).
- The HTTP route and the screen for the queue. There is no `GET` route for the exceptions queue yet (only the service function `list_exceptions`); Phat wires it, and the review action below, with his other queue items.

---

## 3. Data Model

No schema change. Everything uses the existing append-only `evidence_log`.

`DUPLICATE_REJECTED` payload, extended so staff can find the original (new rows only):

| Source | Entity | Payload (added fields in bold) |
|---|---|---|
| DUPLICATE_FILE | TRANCHE | `reason: "DUPLICATE_FILE"`, **`original_file_id`**, **`original_transfer_id`**, **`original_tranche_id`** |
| DUPLICATE_BANK_TXN | TRANSFER | `bank_txn_id`, **`original_transfer_id`**, **`original_tranche_id`** |

New event `DUPLICATE_REVIEWED` (entity = the same entity as the `DUPLICATE_REJECTED` row it closes, actor = staff, `reason` = staff note), with payload `{"duplicate_event_id": "<evidence_log id>"}`.

Write the OCR `DUPLICATE_REJECTED` event once when a confident OCR result applies a denial and includes `DUPLICATE_BANK_TXN`. Use the existing transfer and the original transfer found by the duplicate check. Record it in the upload transaction with the verdict. Idempotent upload replays must not add a second event. Suggested verdicts retain their existing staff-review path.

Existing `DUPLICATE_REJECTED` events have no `original_*` fields. List them with `original` = null. Historical OCR verdicts did not write this event and are outside the initial queue backfill.

---

## 4. API Contract

Service functions in `app/direct_transfer/admin.py` (Phat's routers call them):

### `list_exceptions(db)` (extended)

Adds items of this shape, one per `DUPLICATE_REJECTED` row not yet closed by a `DUPLICATE_REVIEWED` row:

```
{
  "kind": "DUPLICATE_RECEIPT",
  "duplicate_type": "FILE" | "BANK_TXN",
  "duplicate_event_id": uuid,
  "tranche_id": uuid,                 // the ticket the repeat upload was for
  "transfer_id": uuid | null,         // the REJECTED transfer (BANK_TXN only; FILE has none)
  "dispute_id": null,
  "investor_id": uuid,                // who uploaded the repeat
  "same_investor": bool,              // true when the original receipt belongs to the same investor
  "occurred_at": datetime,
  "amount_vnd": int,
  "reference_code": string,
  "failure_codes": ["DUPLICATE_FILE"] | ["DUPLICATE_BANK_TXN"],
  "file_key": string | null,          // BANK_TXN: the repeat file. FILE: null (never stored)
  "original": {                       // null on rows written before this change
    "tranche_id": uuid, "transfer_id": uuid, "investor_id": uuid,
    "reference_code": string, "file_key": string | null
  } | null,
  "timeline": [...]
}
```

Order: `same_investor = false` first (a possible stolen receipt), then oldest first.

### `review_duplicate_receipt(db, duplicate_event_id, *, actor, note)`

Appends `DUPLICATE_REVIEWED`, which removes the item from the queue. Staff only (the route's role check; the function refuses a non-staff actor with 403). `note` is required (min 1 character). Idempotent: lock the original duplicate event row, then look up an existing review before appending. A repeat or concurrent review returns the first result and creates no extra audit event.

Errors: 404 unknown event; 409 `NOT_A_DUPLICATE_EVENT` if the id is not a `DUPLICATE_REJECTED` row.

---

## 5. State Machine

None. Neither the tranche nor the transfer changes state. The queue item is derived: open until a `DUPLICATE_REVIEWED` row exists for it.

---

## 6. Business Rules

1. Keep the existing response, ticket state, and hold-clock behavior for each duplicate path.
2. Every `DUPLICATE_REJECTED` is queued, so repeat uploads by the same investor appear as separate items (each is one event); the screen may group them by `tranche_id`.
3. `same_investor = true` is shown but never auto-closed: a person re-sending their own receipt is usually harmless, but staff decide.
4. Queue items are visible to staff only and never contain the investor's name beyond what the existing queue items show.
5. Reviewing does not change money, tickets or ledger.

---

## 7. Error Cases

| Case | Status | `detail` |
|---|---|---|
| Review by a non-staff actor | 403 | `Staff only` |
| Unknown `duplicate_event_id` | 404 | `Not found` |
| Event is not a `DUPLICATE_REJECTED` row | 409 | `Not a duplicate event` |
| Review without a note | 422 | validation error |

---

## 8. Acceptance Criteria

Test files: `tests/direct_transfer/test_exceptions.py`, `tests/direct_transfer/test_evidence.py`, and `tests/market/test_receipt_auto_verdict.py`.

- `test_duplicate_file_is_listed_in_exceptions_with_original_receipt`
- `test_duplicate_bank_txn_is_listed_in_exceptions_with_original_receipt`
- `test_edited_copy_with_same_bank_txn_is_listed`
- `test_duplicate_from_other_investor_sorts_before_same_investor`
- `test_duplicate_same_investor_flagged_same_investor`
- `test_file_duplicate_still_returns_409_and_leaves_ticket_unchanged`
- `test_ocr_duplicate_still_returns_202_and_queues_original_receipt`
- `test_ocr_duplicate_upload_replay_does_not_queue_twice`
- `test_old_duplicate_row_without_original_is_listed_with_null_original`
- `test_review_duplicate_removes_it_from_queue_and_logs_reviewer_and_note`
- `test_review_duplicate_requires_staff_and_note`
- `test_concurrent_duplicate_reviews_append_once`
- `test_review_rejects_non_duplicate_event`

---

## 9. Open Questions

1. ~~**Investor consequence.**~~ **Answered (Loc, 26 Nov 2026): no automatic flag.** Staff review the queue item and use SUSPENDED or CLOSED (SCRUM-620) if it is fraud.
2. **Repayment receipts.** Surface duplicates there too? Proposal: separate ticket.
3. **Queue route.** Phat owns the queue GET and review POST in `app/admin/`.
