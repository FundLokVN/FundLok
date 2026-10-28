# Spec: CIC Event Log — Log Now, Send Later

| Field | Value |
|---|---|
| **Status** | DRAFT |
| **Owner** | Edward |
| **Implementer(s)** | Edward (`app/direct_transfer/cic.py`) |
| **Module** | `app/direct_transfer/` |
| **Version** | 0.1 |
| **Date** | 2026-10-28 |
| **Related ADR** | ADR-009, ADR-004 |
| **Depends on** | `funding-window.md`, `repayment-collection.md`, `disputes.md`, `transfer-and-evidence.md` |
| **Source** | `Loc.md` "CIC events", constraints (28 Oct); `Loc2.md`; HANDOFF-04 P-7 |

---

## 1. Context & Goal

CIC is planned but not connected; its contract is signed only after SBV grants the certificate. It must be connected before the first live disbursement. CIC wants a real-time report per disbursement, daily outstanding balances and monthly reports, and corrections by resubmission. Build the module that **logs every reportable event from day one** so it can be switched on later. There is no sender.

**Goal:** an append-only `cic_events` table, idempotent writers at the four reportable moments, and a clear seam for a future sender.

---

## 2. Out of Scope

- Any transport, file format, encryption, fixed-IP tunnel, or HTTP client (Q-010).
- Monthly reports (derivable from the daily balances later).
- Mapping to CIC field codes (ADR-004 / the Decree 94 plan owns that).
- The CIC data pull for grading.

---

## 3. Data Model

### New table: `cic_events`

```
cic_events
├── id UUID PK
├── event_type TEXT NOT NULL CHECK IN ('ATTESTED','VOID','DAILY_BALANCE','MISSED_DAY_CORRECTION')
├── tranche_id UUID NULL → tranches(id), repayment_day_id UUID NULL → repayment_days(id), window_id UUID NOT NULL → funding_windows(id)
├── emit_key TEXT NOT NULL UNIQUE                 -- idempotency
├── payload JSONB NOT NULL                        -- fields as of emission (see rule 3)
├── occurred_at TIMESTAMPTZ NOT NULL              -- when the money/business event happened
├── sent_at TIMESTAMPTZ NULL                      -- NULL until a future sender transmits it
├── created_at TIMESTAMPTZ DEFAULT now()
```
**Append-only at the DB layer.** Trigger `trg_cic_events_append_only` (`BEFORE UPDATE OR DELETE`): DELETE always raises; UPDATE raises **unless** it changes only `sent_at` from NULL to a non-NULL value (the single permitted mutation, for the future sender). The app layer exposes no update or delete function other than `mark_sent`.

---

## 4. API Contract

Internal service functions only (no endpoints):

```python
async def emit_cic_event(db, *, event_type, window_id, emit_key, payload, occurred_at, tranche_id=None, repayment_day_id=None) -> tuple[CicEvent, bool]  # (event, created)
async def emit_attested(db, tranche, *, now)          # key attested:{tranche_id}
async def emit_void(db, tranche, *, now, reason)      # key void:{tranche_id}
async def emit_daily_balances(db, *, on_date, now)    # key daily:{tranche_id}:{YYYY-MM-DD}
async def emit_missed_day_correction(db, day, *, now, stage)   # key missed:{day_id}:{stage}, stage in {'reported','resolved_paid'}
async def pending_events(db, *, limit)                # sent_at IS NULL, oldest first
async def mark_sent(db, event_ids, *, sent_at)        # the only mutation; for the future sender
```

---

## 5. State Machine

`sent_at NULL -> timestamp` only. Nothing else changes.

---

## 6. Business Rules

1. **Follow the money (D-022):** `ATTESTED` is the real-time disbursement report, emitted when a tranche becomes `ATTESTED` (a correction follows if it is later voided); `VOID` is the correction when an attested/confirmed tranche is voided; `DAILY_BALANCE` is the outstanding principal per tranche per business day counting `PRESUMED_PAID` and `MATCHED` days as paid and excluding `REPORTED_MISSED`; `MISSED_DAY_CORRECTION` is emitted when a day is reported missing and again when it is resolved as paid.
2. **Corrections are new events, never edits** (a void is a second event, not a change to the attested one).
3. **Payload (as of emission, integer VND, ICT dates):** common: `{"window_id","tranche_id","contract_id","borrower_project_id","lender_user_id","agreement_ref": "<tranche reference_code>"}`; `ATTESTED`: `+ {"amount_vnd","annual_rate_pct","term_months","transferred_at"}`; `VOID`: `+ {"reason","amount_vnd"}`; `DAILY_BALANCE`: `+ {"date","outstanding_principal_vnd","unpaid_interest_vnd","paid_days","missed_days"}`; `MISSED_DAY_CORRECTION`: `+ {"due_date","stage","principal_vnd","interest_vnd"}`. The payload contains no free-text statement.
4. **Idempotent emit keys** make every writer safe to re-run; a second call with the same key returns the existing row and `created = False`.
5. **`sent_at` stays NULL** until connected; there is no sender and the module imports no HTTP client (test asserts).
6. **One agreement per investor per tranche** (Q-006 answered A): `agreement_ref` is the tranche reference.
7. **Must be connectable before the first live disbursement:** `pending_events` and `mark_sent` are the whole interface a sender needs.
8. **Tranche reporting before attestation:** events exist only from `ATTESTED` onward (reserved/signed tranches are not reportable).

---

## 7. Error Cases

| Scenario | Behaviour |
|---|---|
| Duplicate emit | returns existing row, no new row |
| Attempt to UPDATE any column but `sent_at` | DB trigger raises |
| `sent_at` set twice | DB trigger raises (only NULL to non-NULL permitted) |
| DELETE | DB trigger raises |
| Tranche not yet ATTESTED | `emit_attested` raises `ValueError` |

---

## 8. Acceptance Criteria

`tests/direct_transfer/test_cic_events.py`

- [ ] `test_attestation_emits_event_once_idempotently`
- [ ] `test_void_emits_correction_event_and_keeps_original`
- [ ] `test_daily_balance_event_per_tranche_per_day_idempotent`
- [ ] `test_daily_balance_counts_presumed_paid_excludes_reported_missed`
- [ ] `test_missed_day_correction_emitted_on_report_and_on_resolution`
- [ ] `test_cic_events_delete_rejected_by_trigger`
- [ ] `test_cic_events_update_rejected_except_sent_at_null_to_value`
- [ ] `test_sent_at_defaults_null_and_pending_events_lists_them_and_mark_sent_once`
- [ ] `test_cic_module_has_no_http_client_or_sender`

---

## 9. Open Questions

| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | Can a real-time report be corrected after dispute, and within what period (Q-027)? | Lok (ask CIC) | — |
| 2 | Exact CIC field mapping for payloads | Edward / Loc | — |
| 3 | Is an unsettled `ATTESTED` (not yet SME-confirmed) tranche reportable in real time, or only at confirmation? Built: at attestation, with correction (Loc "report at attestation and correct if disputed") | Lok | — |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 0.1 | 2026-10-28 | Edward / Claude | Initial draft from HANDOFF-04 |
