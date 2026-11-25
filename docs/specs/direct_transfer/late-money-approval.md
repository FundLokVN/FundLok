# Spec: Staff approve late money into the loan (before 15:30 on Day 14)

| Field | Value |
|---|---|
| **Status** | IMPLEMENTED (Edward, 2026-11-23). Loc's answers of 22 Nov applied |
| **Owner** | Edward |
| **Implementer(s)** | Edward: the service function, the state transition, the thin admin route glue and the tests (Phat reviews `app/admin/`). Phat: the button |
| **Module** | `app/direct_transfer/late_money.py` (new), `app/direct_transfer/tranche_state.py` (one transition), `app/admin/` (Phat's, calls the service) |
| **Version** | 1.2 |
| **Date** | 2026-11-21 |
| **Related ADR** | ADR-011, ADR-009 |
| **Depends on** | `day14-transfer-rule.md` (rules 5 to 7, 9), `commit-then-accept-flow.md`, `day14-investor-signing.md`, `transfer-and-evidence.md`, `ledger-amendment.md`, HANDOFF-07 task E3 |

---

## 1. Context & Goal

On Day 14, a lender's transfer that is right but late (an expired QR code, no QR, or after 14:45) is *late money*. Today the ticket goes `SIGNED → UNDER_REVIEW` (`review_cause = LATE_MONEY`), holds no capacity and never joins the loan. At the 15:35 run it becomes a `TopUpPledge` and the lender is locked. We launch **without top-up signing** (decided 2026-11-21), so that pledge can never be signed: the SME holds the lender's money and has no loan for it.

Loc's rule (21 Nov): staff can approve late money **into the loan before 15:30**, the receipt cut-off. The lender already signed their loan contract on Day 14 (payment is gated on signing), so the contract binds; only the payment was late.

**Goal:** one staff action that moves a clean late-money ticket into the round before 15:30, so the lender's loan forms with everyone who paid, and the money the SME holds is covered by a loan.

---

## 2. Out of Scope

- Top-up signing (`POST /funding/top-ups/{id}/contract…`, `day14-transfer-rule.md` 4.6). Post-launch.
- Money that staff do **not** approve by 15:30. It stays what it is today: the 15:35 run records a `TopUpPledge` and locks the lender. What happens to that money (a refund by the SME, off the platform) is Open Question 1.
- Late money that is not clean (receipt mismatch, an open dispute, a duplicate bank transaction). Those keep the existing dispute and staff-decision routes.
- Any change to the Day-14 cut-offs (14:45 QR close, 15:00 lock, 15:30 receipt deadline, 15:35 decision run).
- Any new ledger leg, fee or migration.

---

## 3. Data Model

No new table or column; no migration. `tranches.review_cause`, `evidence_log.event_type` and `transfers.late_reason` are free text or already hold the values used here.

One new tranche transition in `ALLOWED_TRANSITIONS` (`app/direct_transfer/tranche_state.py`):

```
("UNDER_REVIEW", "ATTESTED"): "13 staff approve late money before the receipt cut-off (late-money-approval.md)"
```

It applies only to a ticket with `review_cause = "LATE_MONEY"`: `transition_tranche` refuses `UNDER_REVIEW → ATTESTED` for any other cause (`INVALID_TRANSITION`). It sets `attested_at` and `confirm_deadline` and clears nothing else; the service function clears `review_cause` itself (below).

One new evidence event: `LATE_MONEY_APPROVED` on `entity_type = "TRANCHE"`, payload `{ "transfer_id", "amount_vnd", "late_reason", "approved_before": receipt_deadline_at }`.

---

## 4. API Contract

### 4.1 Service function (Edward)

```python
# app/direct_transfer/late_money.py
async def approve_late_money(
    db: AsyncSession, *, transfer_id: UUID, staff_id: UUID, reason: str, now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> Tranche
```

The caller commits. It locks the transfer, then the ticket, then the window, in that order (the order `_review_system_verdict` uses, so it never deadlocks with an upload or a staff decision).

### 4.2 Admin route (Phat wires it; it only calls 4.1)

`POST /admin/funding/transfers/{transfer_id}/approve-late-money`, admin only.

Request:
```json
{ "reason": "Bank statement shows the transfer at 14:52" }
```
Response `200`: `AdminInvestorTransferOut` (the existing ticket row; `status: "ATTESTED"`, `review_cause: null`).

### 4.3 Staff queue

`_needs_action()` in `app/admin/funding.py` gains `and_(Tranche.status == "UNDER_REVIEW", Tranche.review_cause == "LATE_MONEY")`, so a late-money ticket shows under `NEEDS_ACTION` while it can still be approved. `AdminInvestorTransferOut` already carries `review_cause`; add `can_approve_late_money: bool` (true while the preconditions of rule 1 hold) and `approve_late_money_until: datetime | null` (the round's `receipt_deadline_at`) so the button can show and disable itself.

### 4.4 Notifications

The lender: `EVENT_LATE_MONEY_APPROVED` (new, in-app), `data`: `window_id`, `listing_title`, `amount_vnd`. The SME: the existing "an investor paid" bell the normal approval already rings (`_notify_business_of_approval`).

---

## 5. State Machine

```
SIGNED --(receipt late, clean)--> UNDER_REVIEW[LATE_MONEY]
UNDER_REVIEW[LATE_MONEY] --(staff approve, before 15:30)--> ATTESTED   (new, transition 13)
UNDER_REVIEW[LATE_MONEY] --(15:35 run, not approved)-------> VOID      (LATE_MONEY_TOP_UP; TopUpPledge recorded; unchanged)
ATTESTED --(SME confirms, or 24 hours of silence)--> CONFIRMED         (unchanged)
```

After approval the ticket is an ordinary paid ticket: the SME's 24-hour receipt check applies (AM-E5), and the loan forms with it. The SME check is **not** bypassed.

---

## 6. Business Rules

1. **Preconditions** (all must hold, else the error in section 7):
   a. The ticket is `UNDER_REVIEW` with `review_cause = LATE_MONEY`.
   b. The latest receipt's `check_status` is `MATCHED` and its `payment_timing` is `LATE`.
   c. No open dispute on the ticket and no `TopUpPledge` for it.
   d. The window has Day-14 cut-offs (`window_cutoffs` is not None), is `OPEN` or `AWAITING_PAYMENT` (not yet closing), and `now < receipt_deadline_at` (15:30 ICT on Day 14). The server clock decides; there is no override.
   e. `capacity_left(window) >= tranche.amount_vnd`.
   f. `staff_id` is not the ticket's investor (the same rule as every receipt review).
   g. `reason` is not blank.
2. **Effect**, in one transaction: refuse a bank transaction that already funds another ticket (the same test as `receipt_auto.bank_txn_used_elsewhere`, repeated in `late_money.py` so `app/direct_transfer/` does not import from `app/market/`); set `tranche.receipt_submitted_at` if empty; `transition_tranche(UNDER_REVIEW → ATTESTED, actor_role="ADMIN", reason, confirm_deadline = now + sme_confirm_hours)`; set `tranche.review_cause = None`; set `transfer.confirmation_level = "ATTESTED"`; `refresh_window_fullness`; log `LATE_MONEY_APPROVED` and notify the lender. The admin wrapper `approve_late_money_transfer` (which has the IP) appends the audit row (`TRANCHE`, `LATE_MONEY_APPROVED`, actor, `reason`, `ip_address`), rings the business's bell and commits.
3. **The lender's lock** is not lifted by this function. The 15:35 run already releases a lender whose ticket is `ATTESTED`, `CONFIRMED` or `ACTIVE` and who is in no other open round (`day14.process_day14_window`, the `decide` branch). Approval therefore only has to leave the ticket in one of those states. A lender locked at 15:00 stays locked between approval and 15:35.
4. **No pledge is recorded** for an approved ticket: the 15:35 step records a pledge only for a ticket still `UNDER_REVIEW`.
5. **Loan formation** waits for the approved ticket like any `ATTESTED` ticket (`close._pending_tranches` counts it) and never later than `close_wait_until` (2 calendar days after the 14:45 pay deadline).
6. **Replay.** A second call for the same transfer finds the ticket no longer `UNDER_REVIEW` and answers `409 ALREADY_APPROVED` (when the log has `LATE_MONEY_APPROVED` for it) or `409 NOT_LATE_MONEY`. Nothing changes.
7. **Ledger.** No leg is posted by approval. The claim legs post at loan formation, on the existing path (`ledger-amendment.md`, `ledger_posting.py`). Edward approves, in writing, any PR touching `ledger_posting.py`; this spec needs none.

---

## 7. Error Cases

| Status | Code | When |
|---|---|---|
| 404 | `TRANSFER_NOT_FOUND` | no such transfer |
| 403 | (detail "You cannot review your own transfer") | staff is the ticket's investor |
| 409 | `NOT_LATE_MONEY` | precondition 1a, 1b or 1c fails (wrong status or cause, receipt not clean, open dispute, a pledge exists) |
| 409 | `ALREADY_APPROVED` | rule 6 |
| 409 | `LATE_MONEY_APPROVAL_CLOSED` | `now >= receipt_deadline_at`, or the window is not `AWAITING_PAYMENT` |
| 409 | `CAPACITY_EXCEEDED` | precondition 1e |
| 409 | `DUPLICATE_BANK_TXN` | the bank transaction already backs another ticket |
| 422 | `REASON_REQUIRED` | blank reason |

---

## 8. Acceptance Criteria (`tests/direct_transfer/test_late_money_approval.py`)

- `test_staff_approve_late_money_before_1530_moves_it_to_attested_with_a_24_hour_sme_check`
- `test_approved_late_money_counts_toward_the_round_once_the_sme_check_lapses`
- `test_approval_at_or_after_1530_is_refused_and_the_1535_run_records_the_pledge`
- `test_approved_late_money_records_no_top_up_pledge`
- `test_the_1535_run_unlocks_a_lender_whose_late_money_was_approved`
- `test_a_lender_locked_at_1500_stays_locked_until_the_1535_run`
- `test_approval_needs_a_clean_matched_receipt_and_no_open_dispute`
- `test_approval_is_refused_for_a_ticket_that_is_not_late_money`
- `test_approval_twice_changes_nothing_the_second_time`
- `test_staff_cannot_approve_their_own_transfer`
- `test_a_blank_reason_is_refused`
- `test_approval_is_audited_and_logged_with_the_staff_member_and_reason`
- `test_approval_never_bypasses_the_sme_receipt_check`
- `test_a_late_money_ticket_shows_under_needs_action_until_1530`
- `test_the_new_transition_is_only_allowed_for_a_late_money_ticket`

---

## 9. Open Questions

1. **Resolved (Loc, 22 Nov).** Approve into the loan (his option A); the SME does not refund, and no refund spec is written. Loc expects top-up signing to be the long-term way, but it is **not a launch blocker**: at launch staff push the investors and manage late money by hand, **even after 15:30**. After 15:30 this spec's approval is refused by design and the 15:35 run records the pledge, so that handling happens outside the platform (TD-06). If staff need to approve after 15:30 in the system, that is a new, small change to this spec, not built.
2. **Edward, 2026-11-21: approval keeps the SME's 24-hour receipt check; it does not skip it. Loc asked on 22 Nov what that check is; his own AM-E5 rule (SCRUM-610): after the lender's proof the SME has 24 calendar hours to upload its receipt or raise a concern, silence counts as received, and a loan takes effect only once confirmed.**
3. **Resolved (Edward, 2026-11-21):** 15:30 stays. Staff watch the `NEEDS_ACTION` queue from 14:45, and the 15:35 pledge path catches the rest.
