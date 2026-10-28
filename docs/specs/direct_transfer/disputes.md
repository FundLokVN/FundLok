# Spec: Disputes — Queue, Two-Step Proof, Evidence, Staff Decision

| Field | Value |
|---|---|
| **Status** | DRAFT |
| **Owner** | Edward |
| **Implementer(s)** | Edward (`app/direct_transfer/disputes.py`, `admin.py`); Phat (staff and party screens) |
| **Module** | `app/direct_transfer/` |
| **Version** | 0.1 |
| **Date** | 2026-10-28 |
| **Related ADR** | ADR-009 |
| **Depends on** | `transfer-and-evidence.md`, `funding-window.md`, `repayment-collection.md`, `cic-event-log.md` |
| **Source** | `Loc.md` rule 7 and "Decisions and points to align"; `Loc2.md` points 1 to 3; HANDOFF-04 section 6 (E6, Q-026, Q-027) |

---

## 1. Context & Goal

Every confirmation rests on documents, so disagreements are settled on documents. A disbursement dispute (the SME says the money never arrived, or the receipt does not match) and a repayment dispute (the investor says a day was not paid) both follow a two-step proof and end in a **recorded staff decision**. If the statements conflict the payer asks the bank to trace the transfer (tra soat) and the bank's answer decides. FundLok judges nothing automatically.

**Goal:** specify the dispute and evidence tables, whose turn it is, deadlines, staff decisions (with who/why/when), outcomes on tranches and repayment days, and the CIC correction hooks.

---

## 2. Out of Scope

- Bank trace itself (a manual outside process; only recorded).
- Automatic judgement of statements; payment of damages or penalties.
- Legal escalation; arrears handling.
- Receipt-field editing (see `transfer-and-evidence.md`); this spec covers decisions after that.

---

## 3. Data Model

### New table: `disputes`

```
disputes
├── id UUID PK
├── subject_type TEXT NOT NULL CHECK IN ('TRANCHE','REPAYMENT_DAY')
├── tranche_id UUID NULL → tranches(id), repayment_day_id UUID NULL → repayment_days(id)
│   CHECK ((subject_type='TRANCHE' AND tranche_id IS NOT NULL) OR (subject_type='REPAYMENT_DAY' AND repayment_day_id IS NOT NULL))
├── window_id UUID NOT NULL → funding_windows(id)
├── cause TEXT NOT NULL CHECK IN ('RECEIPT_MISMATCH','SME_DISPUTE','LATE_RECEIPT','MISSED_DAY','LATE_REPORT')
├── raised_by UUID NULL → users(id)             -- NULL when raised by the system (mismatch)
├── status TEXT NOT NULL DEFAULT 'OPEN' CHECK IN ('OPEN','BANK_TRACE','DECIDED')
├── required_next_party TEXT NOT NULL CHECK IN ('INVESTOR','SME','STAFF')
├── response_deadline TIMESTAMPTZ NULL          -- for the required party (2 working days, placeholder)
├── outcome TEXT NULL CHECK IN ('RECEIVED','NOT_RECEIVED','PAID','NOT_PAID')
├── decided_by UUID NULL → users(id), decided_at TIMESTAMPTZ NULL, decision_reason TEXT NULL
├── bank_trace_reason TEXT NULL, bank_trace_at TIMESTAMPTZ NULL
├── created_at, updated_at TIMESTAMPTZ
```
Partial unique `(tranche_id) WHERE status <> 'DECIDED' AND subject_type='TRANCHE'`: one open dispute per tranche; same for `repayment_day_id`.

### New table: `dispute_evidence` (append-only by convention and trigger-free: rows are never updated; the same events are mirrored to `evidence_log`)

```
dispute_evidence
├── id UUID PK, dispute_id UUID NOT NULL → disputes(id)
├── party TEXT NOT NULL CHECK IN ('INVESTOR','SME','STAFF'), kind TEXT NOT NULL CHECK IN ('CLAIM','PROOF','COUNTER_PROOF','NOTE')
├── statement TEXT NULL, file_id UUID NULL → evidence_files(id), submitted_by UUID NOT NULL → users(id)
└── submitted_at TIMESTAMPTZ NOT NULL
```

Every dispute step also appends an `evidence_log` row (the append-only, trigger-protected record).

---

## 4. API Contract

Services: `open_tranche_dispute`, `open_missed_day_dispute` (called by tranche/repayment flows), `submit_dispute_evidence`, `escalate_to_bank_trace`, `decide_dispute`, `advance_dispute_deadlines` (job), `list_disputes`.

### `POST /disputes/{dispute_id}/evidence` — INVESTOR or SME party
`{ "statement": "string|null", "file_id": "uuid|null" }` (at least one). **200** `DisputeOut`. 409 `NOT_YOUR_TURN`, 404 not a party.

### `GET /disputes/{id}` — party or staff; `DisputeOut`:
```json
{ "id":"uuid","subject_type":"TRANCHE|REPAYMENT_DAY","subject_id":"uuid","cause":"SME_DISPUTE","status":"OPEN",
  "required_next_party":"INVESTOR","response_deadline":"ts","outcome":null,
  "evidence":[ { "party":"SME","kind":"CLAIM","statement":"string","file_url":"string|null","submitted_at":"ts" } ] }
```
Party views never show the counterparty's identity beyond the masked display name.

### Staff
- `GET /admin/direct-transfer/disputes?status=open`
- `POST /admin/direct-transfer/disputes/{id}/decision` `{ "outcome": "RECEIVED|NOT_RECEIVED|PAID|NOT_PAID", "reason": "string (required)" }`
- `POST /admin/direct-transfer/disputes/{id}/bank-trace` `{ "reason": "string (required)" }`

**Errors:** 400 invalid outcome for the subject (`RECEIVED`/`NOT_RECEIVED` for tranches, `PAID`/`NOT_PAID` for repayment days); 404; 409 `NOT_YOUR_TURN`, `DISPUTE_ALREADY_DECIDED`, `CAPACITY_EXCEEDED` (reviving a late receipt with no capacity), `DISPUTE_WINDOW_CLOSED`; 422 missing reason.

---

## 5. State Machine

```
OPEN ──[evidence steps]──► OPEN ──[staff: bank trace]──► BANK_TRACE ──[staff decision]──► DECIDED
OPEN ──[staff decision]──► DECIDED
```
Required next party:

| Cause | Order of proof |
|---|---|
| `SME_DISPUTE` (disbursement, money said not received) | SME claim (given at dispute) -> **INVESTOR** bank statement first -> **SME** own statement -> STAFF |
| `MISSED_DAY` (repayment, investor says not received) | INVESTOR claim (the report) -> **SME** bank statement first -> **INVESTOR** own statement -> STAFF |
| `RECEIPT_MISMATCH`, `LATE_RECEIPT`, `LATE_REPORT` | STAFF immediately (the evidence is the receipt/report already on file) |

---

## 6. Business Rules

1. **Burden of proof (E6, Q-026):** the payer proves first with a bank statement, then the counterparty responds with its own. Disbursement: investor, then SME. Repayment: SME, then investor. If the statements conflict or money is lost, staff may start a **bank trace** by the payer; the bank's answer is recorded as a `NOTE` and decides.
2. **Whose turn:** `required_next_party` is stored; `submit_dispute_evidence` by anyone else is `NOT_YOUR_TURN`. After the second proof the dispute is `STAFF`'s turn.
3. **Deadline:** each party step has `response_deadline = end of the 2nd working day` (placeholder). `advance_dispute_deadlines(now)` moves a dispute whose required party has not answered to `STAFF` and logs `NO_RESPONSE`; it never auto-decides.
4. **Decision record:** `decide_dispute` requires a non-empty `reason`, records `decided_by`, `decided_at`, `outcome` and mirrors a `DISPUTE_DECISION` row to `evidence_log` (who, why, when). No update or delete path for evidence.
5. **Tranche outcomes:** `RECEIVED`: `UNDER_REVIEW -> CONFIRMED` if the tranche is not `excluded_at_close`; for cause `LATE_RECEIPT` the capacity is re-checked first (`CAPACITY_EXCEEDED` otherwise). If the tranche is `excluded_at_close` (PROVISIONAL(Q-D7)) it does **not** join the loan: it becomes `VOID(EXCLUDED_AT_CLOSE)` and a tracked `REFUND` transfer is created. `NOT_RECEIVED`: `UNDER_REVIEW -> VOID(NOT_RECEIVED)`; no refund (no money arrived); CIC `VOID` correction if an `ATTESTED` event was logged.
6. **Repayment-day outcomes:** `PAID`: row -> `MATCHED` (settled and ledger-posted as in `repayment-collection.md`), CIC `MISSED_DAY_CORRECTION` (resolved); `NOT_PAID`: row stays `REPORTED_MISSED` (no arrears state in MVP), dispute `DECIDED`.
7. **CIC corrections are new events, never edits** (`cic-event-log.md`, D-022). The correction hook is called by `decide_dispute` and `open_missed_day_dispute`.
8. **Late disputes vs CIC (Q-027):** until CIC answers whether a resubmission window exists, corrections are emitted at any time; internal acceptance of late reports is capped (`repayment-collection.md` rule 8).
9. **Dispute window for the SME on disbursement:** the SME can open `SME_DISPUTE` only from `ATTESTED` and before `confirm_deadline`; a frozen tranche (UNDER_REVIEW) has no schedule and no fee until decided.
10. **Visibility:** parties see only their own subject's dispute; staff see all; a resource the caller is not party to returns 404.
11. **Language:** no accusatory wording in notifications; ask what happened.

---

## 7. Error Cases

| Scenario | Behaviour | Response |
|---|---|---|
| Wrong party submits | refused | 409 `NOT_YOUR_TURN` |
| Both parties silent past the deadline | dispute moves to STAFF, logged | — |
| Staff decide without a reason | refused | 422 |
| Decide an already decided dispute | refused | 409 `DISPUTE_ALREADY_DECIDED` |
| `RECEIVED` on a late receipt with no capacity | refused; staff may choose `NOT_RECEIVED` (SME refunds) | 409 `CAPACITY_EXCEEDED` |
| Outcome not valid for the subject type | refused | 400 |
| Open a second dispute on the same tranche | partial unique index | 409 |

---

## 8. Acceptance Criteria

`tests/direct_transfer/test_disputes.py`

- [ ] `test_sme_dispute_creates_dispute_with_investor_first`
- [ ] `test_missed_day_dispute_has_sme_first`
- [ ] `test_wrong_party_cannot_submit_evidence`
- [ ] `test_two_step_proof_then_staff_turn`
- [ ] `test_deadline_lapse_moves_turn_to_staff_without_deciding`
- [ ] `test_decision_requires_reason_and_records_decider`
- [ ] `test_decision_mirrors_to_append_only_evidence_log`
- [ ] `test_received_outcome_confirms_tranche`
- [ ] `test_not_received_outcome_voids_tranche_without_refund`
- [ ] `test_received_after_exclusion_voids_with_refund_transfer`
- [ ] `test_late_receipt_revival_rechecks_capacity`
- [ ] `test_paid_outcome_matches_repayment_day`
- [ ] `test_not_paid_outcome_keeps_reported_missed`
- [ ] `test_bank_trace_escalation_recorded`
- [ ] `test_corrections_are_new_cic_events_not_edits`
- [ ] `test_one_open_dispute_per_subject`
- [ ] `test_outsider_gets_404`
- [ ] `test_invalid_outcome_for_subject_type_rejected`

---

## 9. Open Questions

| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | Burden-of-proof order and the bank-trace rule (Q-026, IBL, Huy review policy) | Lok / Huy / Edward | — |
| 2 | Whether the dispute window should stay open several days (Q-026) | Lok / Edward | — |
| 3 | CIC correction window (Q-027) | Lok | — |
| 4 | Who may decide (single staff vs dual control) | Huy | — |
| 5 | Response deadline length per step | Lok | — |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 0.1 | 2026-10-28 | Edward / Claude | Initial draft from HANDOFF-04 |
