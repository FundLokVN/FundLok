# HANDOFF-04 — Direct-Transfer Disbursement & Repayment

| Field | Value |
|---|---|
| **Status** | FOR IMPLEMENTATION — **Phase B released 28 Oct (per-task spec gate, see §0.1)** |
| **Version** | 0.3 — 28 October 2026 |
| **Author** | Edward Wong (CTO), drafted with Claude |
| **Implementer** | Sonnet |
| **Inputs** | `Loc.md` (Lok's design, 27–28 Oct; converted from the claude.ai artifact), `Loc2.md` (Lok's 28 Oct decisions and asks to Edward) |
| **Supersedes** | ADR-008 (GPay, still *Proposed*), `docs/specs/payments/gpay-virtual-account.md`, and the omnibus parts of `docs/specs/payments/funding-flow.md` |
| **Baseline** | `main` + `decree94-release`; `docs/outstanding-issues.md` (updated 2026-10-22) |

> **How to read this.** Lok's documents are the product input. Where they conflict with the repo, §2 lists
> the conflict and the default you must use. Anything marked **PROVISIONAL(X-n)** is built behind config
> and must be greppable (`grep -rn PROVISIONAL`). Do **not** resolve an open question by guessing in code.

---

## 0.1 Decisions taken by Edward, 28 Oct 2026 (binding for this handoff)

Edward accepted every default in §2 and decided the three items that were his. No need to wait on Lok for
any of these; none is marked "Lok to confirm" any more except where §2 says IBL/legal.

| # | Decision | Effect |
|---|---|---|
| E1 | **S6 is superseded: the fee-free MVP is over.** Both fees are live: SME activation fee **3%** of confirmed principal (separate VAT invoice, due 7 days, never netted) and investor service fee **2.5%/yr** (monthly accrual + invoice). | `fee_invoices` and the blocks (no new listing while SME invoice unpaid; no new reservation while investor fee unpaid) are **on by default**. Rates stay config. Record in `docs/outstanding-issues.md` as a new D-0xx referencing S6. HANDOFF-03 T14 (fee taken from investor return) is **not** used for the new path. |
| E2 | **Ledger:** confirmed-transfer model. `transfers` is the evidence record; claim legs post only on CONFIRMED against the existing `EXTERNAL` account; **no `OMNIBUS_CASH` legs** on any new path (I-2). | P-3 specs it; existing ledger code and trigger are untouched. |
| E3 | **Stub interest:** separate dated obligation per tranche, due on the first instalment day, outside `T0`, counted in the cap test; basis `P × r × business_days/252` (I-10). | Facility schedule and INV-5 untouched. |
| E4 | State-machine additions accepted: `SIGNED → EXPIRED`, `CONFIRMED → VOID` before ACTIVE (I-22, I-23, §4.3). | |
| E5 | All other §2 defaults accepted: last-ticket remainder rule (I-6), "full" definition (I-7), late receipt to exceptions (I-8), start-date rule (I-9), withholding rounding and individual-only (I-16), disputed-at-close excluded + SME refunds (I-17), SME early close only at ≥50% confirmed, **no "extend once"** (I-24), cap source = own records + SME declaration (I-25), min ticket 10M, staff manual receipt entry, mock signer. | |
| E6 | Recommended positions in §6 accepted as the working rules. | Timers only; configurable. |

**Per-task spec gate (replaces the single Phase A → B gate).** Sonnet works in this order and may start a
Phase B task as soon as its spec (Phase A) is written and committed on the branch; Edward reviews specs and
code together in the same PR cycle. It does **not** wait for Phat or Lok. Code that touches shared files
(`app/lending/models.py`, `app/models/__init__.py`, `app/auth/`) still needs Phat's explicit OK in the PR.

**Still needed from outside before live money (not before the proof):** IBL answers (incl. the 20% cap on
the 3% fee, I-11), a remote-signing vendor, an e-invoice vendor, CIC connection, Viettel IDC storage
decision (I-14). The code ships with mocks/interfaces for all of these.

---

## 0. What we are building

FundLok never holds money (D-011, confirmed again by Lok 22/27 Oct). Money moves investor bank → SME bank
(disbursement) and SME bank → investor bank (repayment). FundLok issues a VietQR, collects receipts,
records evidence, computes schedules and reports. **No bank API, no CIC connection** — every confirmation
runs on uploaded documents reviewed by staff.

Flow (Approach 2, "funding window"):

1. SME signs one standing offer per listing → listing OPEN for ≤14 days, closes early when full.
2. Investor **reserves** a ticket (≥ min ticket, ≤ capacity left; 2 h hold) → **signs** acceptance →
   pays by dynamic VietQR (SME's account, exact amount, reference `FL` + 8 chars) → **uploads receipt**.
3. FundLok parses the receipt; all checks pass → tranche **ATTESTED**, else exceptions queue.
4. SME has 2 working days to dispute; silence ⇒ **CONFIRMED**.
5. Window close: confirmed tranches (capped at target) form the loan; <50% of target ⇒ no loan, SME
   refunds every tranche, no fee. Otherwise one common repayment start date (first working day after
   close); early tranches earn stub interest at the same flat rate.
6. Repayment: manual. "Today's payments" screen, one VietQR per investor; a day counts paid unless the
   investor reports it missing by the deadline.

Rules already decided (Lok 28 Oct) — treat as constraints: Approach 2; standing offer; 14-day window;
cap = asked amount ≤100M; 50% minimum raise; one listing per SME until repaid in full; daily repayment on
working days (weekly settlement is **off**); SME dispute window 2 working days (placeholder); both fees on
credit; commission only on settled tranches; CIC = log now, send later.

---

## 1. Ownership and process (CLAUDE.md compliance)

- **Spec-driven, no exceptions.** Phase A produces specs and tickets; Phase B code starts only after
  Edward releases it in this file. Specs are reviewed via PR first.
- **Edward's modules (Sonnet may write):** `app/payments/`, `app/ledger/`, `app/contracts/`,
  `app/repayment_schedule/`, `app/compliance/`, `app/core/`, `app/utils/`, `alembic/`, `docs/`.
- **Phat's modules (Sonnet must NOT edit; write tickets/contracts only):** `app/market/`, `app/loans/`,
  `app/sme/`, `app/uploads/`, `app/admin/`, `app/users/router.py`, all React.
- **Shared (needs Edward + Phat agreement):** `app/lending/models.py`, `app/users/models.py`,
  `app/models/__init__.py`, `app/auth/`, OpenAPI contracts. Schema changes via Alembic only; new
  migrations chain off the current head with hash-based IDs (`alembic revision --autogenerate`).
- Put new backend code in a **new module `app/direct_transfer/`** (Edward-owned) rather than in
  `app/market/`, and expose service functions Phat's routers call. Models go in
  `app/direct_transfer/models.py` and are re-exported via `app/models/__init__.py` (**shared — flag in PR**).
- No UI code, ever. Every money-creating endpoint takes `Idempotency-Key`. All service functions
  `async def`; timers via ARQ, never `BackgroundTasks`. Errors `{"detail": ...}`. Tests under
  `tests/direct_transfer/`, fixtures only, acceptance criteria → test names 1:1.
- Use `search_fl_docs` before re-reading specs. Load the `fundlok-domain` skill before touching rates,
  schedules, fees or ledger.

---

## 2. Issues found — resolve or acknowledge before building

Severity: **B** = blocks the part named; **D** = has a default, proceed; **I** = FYI.

| # | Sev | Issue | Default for this handoff |
|---|---|---|---|
| I-1 | ~~B~~ resolved | `Loc.md` lost the three embedded diagrams. **Recovered 28 Oct from the live doc (rev 47) via the Docs connector.** The authoritative tranche machine (8 states, 9 transitions) is now §4.3 verbatim. This replaces my earlier invented machine (REPORTED/EXCEPTION/DISPUTED/REFUND_DUE/REFUNDED are **not** in Lok's design). | Build §4.3. The gaps it leaves are I-22…I-25. |
| I-2 | B (ledger) | **Ledger conflict.** `ledger-foundation.md`, `funding-flow.md` (ACCEPTED), CLAUDE.md money-flow and ADR-002/003 are built on an omnibus `OMNIBUS_CASH`. Lok's design has no FundLok cash account. Register Q-002 option A (confirmed-transfer model) is now effectively chosen. | Spec an amendment (§3 P-3). **Recommended:** `transfers` is the evidence record; ledger posts claim legs only on CONFIRMED using the existing `EXTERNAL` account as counterparty; **no `OMNIBUS_CASH` legs on any new path**. Existing ledger code/trigger untouched. Edward decides in the spec review. |
| I-3 | D | CLAUDE.md, ADR-002/003, ADR-008, GPay spec, memory notes `project_gpay_virtual_account` / `project_mb_bank_custodial` are stale. | P-1 marks them superseded; CLAUDE.md edits are Edward's (it is already modified/uncommitted — do not commit it). |
| I-4 | D | Register items now answered by Lok's design: Q-001 (GPay out), Q-004 (≈ option B + 50% minimum), Q-006 (A, one contract per investor), Q-013 (C), Q-014 (≈ A, with evidence-level field), Q-015 (A), Q-016 (min ticket B). | Add as new D-014+ with provenance `Lok, 2026-10-27/28, Loc.md`; mark Q-… superseded, don't edit DECIDED rows in place (register rule). |
| I-5 | D | **Name collision.** Existing `Contract` = one loan/listing (1:1 `Listing`, `Holding` unique per (contract, investor)). Lok's "loan contract" = one per investor ≙ a **Tranche**. | Keep `Contract` as the loan container. `Tranche` is the investor loan contract. Holding is created at window close from CONFIRMED tranches only (so existing distribution/share code keeps working). One live tranche per investor per listing. Don't rename anything in code. |
| I-6 | D | **Gap: last ticket below minimum.** Rule 2 says reserve ≥ min ticket, ≤ capacity. If remaining capacity < min ticket, nobody can ever fill it. | Allow a final reservation equal to exactly the remaining capacity when remaining < min_ticket. PROVISIONAL(Q-D3). |
| I-7 | D | **Gap: "full" is undefined.** Reserved-but-unpaid tickets consume capacity but are not funded; they can lapse and re-open capacity. | Capacity = target − Σ(live tranches: RESERVED, SIGNED, ATTESTED, CONFIRMED, DISPUTED). Listing flips to `FULL` (no new reservations) when capacity = 0, and to `CLOSING` → `CLOSED` once every live tranche is terminal or day 14 hits. A lapse while FULL re-opens it until day 14. PROVISIONAL(Q-D3). |
| I-8 | D | **Gap: receipt after hold expiry.** Money moved but the 2 h hold lapsed; capacity may already be re-reserved. | Late receipt → exceptions queue (`LATE_RECEIPT`). Staff may revive only if capacity remains; otherwise the SME must refund (tracked refund transfer). |
| I-9 | D | **Ambiguity: repayment start.** Summary says start = first working day after day 14/full; rule 8 says close waits up to 2 working days for open disputes, *then* start next working day. | Start date = first working day after `closed_at` where `closed_at` is when the wait ends. Config `close_wait_working_days=2`. PROVISIONAL(Q-D4). Stub-interest days = receipt date → start date. |
| I-10 | B (cap/legal) | **Stub interest vs flat-rate + 20% cap + "immutable origination total" (`T0 = P + I`).** Per-tranche stub interest changes the total and breaks pure pro-rata of the daily instalment. Lok put this to IBL (item 4) but didn't say where it lives in the math. | **Recommended:** stub interest is a **separate dated obligation per tranche** (due on the first instalment day), *not* folded into `T0`; facility schedule and INV-5 stay intact; the cap test must include it. Day-count basis `P × r × days/252` in business days (D23 basis). Spec P-4 documents both options; Edward picks. PROVISIONAL(Q-D5). |
| I-11 | B (legal) | **20% cap exposure of the 3% SME activation fee.** If a regulator treated it as interest, implied annualised cost = 16% + 3%×12/term: 4 mo → 25%, 1 mo → ~52%, 6 mo → 22%. v1.4 D24 keeps a fee outside the cap only with documented cost basis, separate agreement, VAT invoice, never deducted. (Lok cites the IBL 28 Sep letter; I have not seen it.) | Build it as separate invoice only (never netted). Run the INV-5 test **both ways** (with and without fee) and log both. Not an engineering blocker; flag to IBL/Lok. |
| I-12 | ~~D~~ decided | Conflict with S6 (HANDOFF-03: no fee during MVP). | **Decided E1: S6 superseded, fees live, invoicing on.** |
| I-13 | D | **E-invoice and remote-signing providers don't exist yet.** "Auto-issue e-invoice" and remote signing have no vendor (Minh integrating signing; quotes pending). | Provider interfaces + `MockSigner` / manual-invoice record. No vendor code. |
| I-14 | B (data residency) | Minh stores contracts/receipts/evidence **in Vietnam (Viettel IDC)**; CLAUDE.md says Cloudflare R2 + Cloud Run. Receipts carry bank/KYC PII; sending them to a foreign OCR/LLM API is a cross-border transfer. Q-008 (Decree 94 residency) is unanswered. | Storage and OCR behind interfaces. **Do not send real receipts to any external AI/OCR service.** MVP extractor = staff manual entry of parsed fields (the "scan" is staff-assisted); local OCR optional later. Storage adapter reuses R2 now. |
| I-15 | D | **SME account ownership is unverified** ("payee = SME's registered account" assumes someone proved it). No bank data. | Registered account captured at KYB with a bank statement / bank confirmation document, staff-approved. Account stored on a new `payment_accounts` record (don't reuse mock `linked_accounts`). |
| I-16 | D | **Withholding tax mechanics.** 317 VND/day is 5% of 6,349 interest, but rounds: 84×317 = 26,628 vs 26,667 total. Also 5% PIT applies to individual lenders; whether corporate investors are withheld is unstated. IBL item 6 still open. | Per-day floor, remainder on the final day (same rule as origination). `investor_type` field; withhold only for `INDIVIDUAL`. PROVISIONAL(Q-D6). Tax-withholding report spec'd but Huy owns its content. |
| I-17 | D | **Late/disputed tranche after close.** Lok: disputed tranche is frozen (no schedule, no fee) but doesn't say if it can join a loan already started. | Excluded from the loan if unresolved at close. If later resolved in the investor's favour it does **not** join; SME refunds that tranche (tracked). PROVISIONAL(Q-D7). |
| I-18 | D | Arithmetic in Loc.md checked and correct (1,253,968/day; 125,397/investor; 119,048 + 6,349; tax 317; fees 3,000,000 / 833,333; 840 transfers; 8.4M VIB; 44,000 signing). Uses 84 days = 4×21 ✔ (Q-011 resolved A). | Use as golden numbers for tests. |
| I-19 | D | Timers (hold expiry, dispute lapse, window close, report deadline, invoice due) need ARQ, but `app/scheduler` is a scaffold with no real jobs (CLAUDE.md "ARQ worker scaffold — deferred"). | Phase B builds the job layer; every timer is also an idempotent service function callable from tests (time injected, no sleeps). |
| I-20 | I | Lok's three alignment points (burden of proof, dispute-window length, late disputes vs CIC) don't block specs. | Recommended positions in §6. Edward closes them with Lok in one call. |
| I-22 | D | **State-machine gap: Signed has no expiry.** The diagram's only lapse is Reserved → Expired. An investor who signs but never uploads a receipt is stuck in Signed forever, holding capacity. | Add `Signed → Expired` on hold lapse (same 2 h hold, no receipt). Late receipt → I-8. PROVISIONAL(Q-D3). |
| I-23 | D | **State-machine gap: the below-50% / KYB-fail / fraud refund has no state.** The diagram's Void ("SME refunds") is reachable only from Under review. | Add `Confirmed → Void` (reason `BELOW_MIN_RAISE` / `KYB_FAIL` / `FRAUD`) before the loan is Active, with a tracked refund `transfer`. A refund after Active is out of MVP. PROVISIONAL(Q-D8). |
| I-24 | D | **Diagram vs text: who closes the window.** Diagram: "day 14, or earlier **if the SME closes it**". Text: closes early when **fully funded**. Loc's cons table also mentions "SME can close early or extend once". | Support all three: full ⇒ auto-close; SME early close (only if ≥50% confirmed); **extend once** is *not* built (no spec, not in Loc's rules). PROVISIONAL(Q-D4). |
| I-25 | D | **Diagram vs decision: "100M limit checked via CIC"** in the Check-cap step; CIC is not connected. | Use own records + SME signed declaration (Loc2). Log a `CAP_CHECK` evidence row with the basis; swap the source when CIC connects. |
| I-26 | I | The tranche machine has **Active** ("repaying daily") but the repayment-day statuses are separate (`due`, `presumed paid`, `reported missed`, `matched`). Tranche `Active → Settled/closed` is not drawn. | Tranche stays Active until the facility is SETTLED (`facility-state-machine.md`); no new tranche state. |
| I-21 | I | `Loc.md` is named `Lok.md` in the request; artifact link in `Loc2.md` was **not** fetched (Loc.md is the conversion). `Loc2.md` repeats its text twice. | No action. |

---

## 3. Phase A — documents (Sonnet does these now)

Each spec follows `docs/specs/_TEMPLATE.md` (9 required sections; Status `DRAFT`). Cite `Loc.md` sections.
Acceptance criteria must be testable and become test names.

| ID | Deliverable | File | Notes |
|---|---|---|---|
| P-0 | Register update: new D-014…D-0xx (Lok's decisions with provenance `Lok, 2026-10-28, Loc.md/Loc2.md`), supersede Q-001/004/006/013/014/015/016 (new Q-017+ referencing the old ones), new `Q-D3…Q-D7` for the PROVISIONAL items above, changelog row | `docs/outstanding-issues.md` | Follow the file's own rules. |
| P-1 | ADR-009 "Direct-transfer custody model" (supersedes ADR-008); mark ADR-008 and the GPay spec `SUPERSEDED`; CLAUDE.md **proposed diff only** in the PR body (Edward applies) | `docs/adr/009-direct-transfer-model.md` | ADR index row. |
| P-2 | `docs/specs/direct_transfer/transfer-and-evidence.md` — `transfers`, `evidence_files`, `evidence_log` (append-only + PG trigger like the ledger), confirmation levels Reported→Attested→Confirmed (Verified reserved), receipt parsing & checks, duplicate rules, exceptions queue, `ReceiptExtractor` and storage interfaces | | Edward's Q-Transfer / "manual document scanning". |
| P-3 | `docs/specs/direct_transfer/ledger-amendment.md` — amendment to ledger-foundation/funding-flow for the no-cash-account model (see I-2) | | Mark funding-flow §6 superseded for new path. |
| P-4 | `docs/specs/direct_transfer/funding-window.md` — Listing extensions, standing offer, tranche state machine (§4.3), capacity/hold, close logic, 50% rule + refund flow, common start date + stub interest (I-10), one-listing-per-SME lock, 100M cap check against own records + SME declaration, timers table, API contract | | The Phat-facing contract. |
| P-5 | `docs/specs/direct_transfer/repayment-collection.md` — repayment days per tranche, "today's payments" payload, VietQR per investor, missed-day report + deadline, tax withheld/net, standing-order/batch as later options, monthly withholding report data | | Reuses facility/schedule specs; MVP excludes arrears (CLAUDE.md MVP scope). |
| P-6 | `docs/specs/direct_transfer/disputes.md` — dispute queue, two-step proof, evidence log, staff decision record, CIC correction hook | | Positions in §6. |
| P-7 | `docs/specs/direct_transfer/cic-event-log.md` — append-only `cic_events` (ATTESTED, VOID, DAILY_BALANCE, MISSED_DAY_CORRECTION), idempotent emit keys, `sent_at` NULL until connected, **no sender** | | Must be connectable before first live disbursement. |
| P-8 | `docs/specs/direct_transfer/fees.md` — `fee_invoices` (SME 3%, due 7 d, late fee), `investor_fee_accruals` (monthly, pro-rata to amounts actually received), reservation block flags, invoice-provider interface | | See I-11/I-12. |
| P-9 | `docs/handoffs/HANDOFF-04-phat-tickets.md` — Phat's tickets (§5) with request/response shapes copied from the specs | | Zero chat context needed. |

**Phase A exit:** one PR per spec (or one docs PR), tests none. Open questions listed in each spec §9.
Edward + Phat review; Edward sets `Phase B released` below.

---

## 4. Phase B — build (gated; do not start until released)

### 4.1 Order of work (each = one PR, tests first)

| # | Task | Module | Depends on |
|---|---|---|---|
| B1 | Alembic migration: `tranches`, `transfers`, `evidence_files`, `evidence_log` (+ trigger), `payment_accounts`, `standing_offers`, `disputes`, `repayment_days`, `cic_events` (+ trigger), `fee_invoices`, `investor_fee_accruals`; `listings` gains `min_raise`, `window_end`, `closed_at`, `stub_*`, `status` values; one-listing-per-SME enforced by **partial unique index** (SME with a non-terminal loan) | `alembic/`, `app/direct_transfer/models.py` | P-2…P-8 accepted, I-1 resolved |
| B2 | `business_days` helpers for window/dispute/start-date (reuse `app/repayment_schedule/calendar.py`; ICT) | `app/direct_transfer/` | — |
| B3 | Reference code generator (`FL` + 8, CSPRNG, A–Z0-9, unique) and **VietQR (NAPAS 247 / EMVCo) payload builder with CRC16** — pure functions, test vectors | `app/direct_transfer/vietqr.py` | — |
| B4 | Receipt checks (pure): exact amount, memo carries reference, payee = SME registered account, payer name matches KYC (diacritic-folded, case-insensitive), required fields present, bank txn-ID uniqueness, file-hash uniqueness. `ReceiptExtractor` interface + `ManualEntryExtractor` | same | B1 |
| B5 | Reservation service: `SELECT … FOR UPDATE` on listing, capacity (I-7), min-ticket + last-ticket rule (I-6), borrower-total check (own records + SME declaration), 2 h hold, one live tranche per investor/listing, blocked if investor fee overdue, `Idempotency-Key` | same | B1, B2 |
| B6 | Signing: `Signer` interface + `MockSigner`; store signed file + hash + certificate fields; standing-offer record | same | B1 |
| B7 | Receipt submission → Attested/exceptions; SME confirm/dispute; auto-confirm on lapse; evidence log appends for every step | same | B4, B5 |
| B8 | Window close: auto-close on full/day-14, wait-for-disputes, 50% check, refund flow (`REFUND_DUE` tranche transfers), Holding creation, common start date, stub interest (per I-10), facility origination from confirmed total (reuse `originate_facility`), `fee_invoice` creation | same + `app/repayment_schedule/` | B7 |
| B9 | Repayment days per tranche (pro-rata split of the facility schedule; tax per I-16), "today's payments" read model, per-investor VietQR, missed-day report + deadline, reconciliation queue | same | B8 |
| B10 | Dispute queue service: two-step proof, staff decision, outcomes (CONFIRMED / VOID / REFUND), CIC correction events | same | B7 |
| B11 | CIC event log writers (no transport) at attestation, void, daily balance, missed-day correction | same | B7, B9 |
| B12 | Fee ledger: SME invoice at close, due/late/block-listing; investor monthly accrual + block-reservation; commission eligibility flag (settled tranches only) | same | B8 |
| B13 | ARQ jobs wrapping B5/B7/B8/B9/B12 timers (hold expiry, dispute lapse, window close, report deadline, invoice due) | `app/scheduler/` | B5–B12 |
| B14 | Admin/staff service layer for exceptions + disputes queues (Phat wires routers in `app/admin/`) | `app/direct_transfer/` | B7, B10 |
| B15 | Compatibility: `place_order` path deprecated behind a feature flag; old tests keep passing | — | B5 |

### 4.2 Hard correctness bars

- Golden numbers from Loc.md (I-18) reproduced to the đồng. Integer VND only (`money-integer-vnd.md`).
- Capacity never exceeds target under concurrent reservations (concurrency test with two sessions).
- A bank txn ID or file hash can fund at most one tranche.
- `evidence_log`, `cic_events` (and `transfers` state history) are append-only at app **and** DB level;
  corrections are new rows.
- No code path moves/holds money or posts to `OMNIBUS_CASH`. No GPay code.
- Time is injected everywhere; no `sleep`, no wall-clock in tests.

### 4.3 Tranche state machine (Lok's diagram, recovered verbatim) + two additions

States (8): `RESERVED` (ticket held 2 h) · `SIGNED` (offer accepted) · `ATTESTED` (CIC reported) ·
`CONFIRMED` (part of the loan) · `ACTIVE` (repaying daily) · `EXPIRED` (capacity freed) ·
`UNDER_REVIEW` (staff decides) · `VOID` (SME refunds).

Transitions (9, as drawn):

| # | From → To | Trigger |
|---|---|---|
| 1 | RESERVED → SIGNED | investor signs |
| 2 | SIGNED → ATTESTED | receipt matches |
| 3 | ATTESTED → CONFIRMED | SME confirms, or 2 working days lapse |
| 4 | CONFIRMED → ACTIVE | window closes |
| 5 | RESERVED → EXPIRED | hold lapses |
| 6 | SIGNED → UNDER_REVIEW | receipt mismatch |
| 7 | ATTESTED → UNDER_REVIEW | SME disputes |
| 8 | UNDER_REVIEW → CONFIRMED | received (staff decision) |
| 9 | UNDER_REVIEW → VOID | not received (SME refunds) |

Additions this handoff needs (not in the diagram — **Edward/Lok to confirm**, I-22/I-23):

| # | From → To | Trigger |
|---|---|---|
| 10 | SIGNED → EXPIRED | hold lapses with no receipt |
| 11 | CONFIRMED → VOID | below-50% raise, KYB fail or fraud, before ACTIVE (tracked refund transfer) |

Notes: "Reported" is a **confirmation level on the transfer** (document uploaded), not a tranche state.
Mismatches and dispute outcomes both live in `UNDER_REVIEW`; record the cause (`RECEIPT_MISMATCH` /
`SME_DISPUTE` / `LATE_RECEIPT`) on the review row, not as extra states. Terminal: EXPIRED, VOID, ACTIVE
(until the facility settles). Only CONFIRMED tranches join the loan at close.

### 4.4 Contract map (recovered)

Three parties, three relationships: FundLok–SME **platform service agreement** (3% fee; service mandate
only), FundLok–investor **investor service agreement** (2.5%/yr fee; servicing POA), and the SME–investor
**loan contract — one per investor per listing** (offer + drawdown + acceptance). This confirms I-5:
one live tranche per investor per listing.

---

## 5. Phat's tickets (content for P-9)

Each ticket: goal, endpoint + schema (from spec), acceptance criteria. Backend contract must exist first.

**Epic 1 — Listing window (start here)**
1. Listing page: progress (confirmed / reserved / target), days left, min ticket, capacity left, status.
2. SME: sign standing offer + drawdown (calls `Signer`), publish listing (blocked if prior loan unpaid).
3. Investor: reserve (amount, risk disclosure ack, 2 h countdown), expiry UX.
4. Investor: sign acceptance → QR screen (amount, reference, SME account, copy buttons, VietQR image).
5. Investor: receipt upload (image/PDF), status tracker (Reserved / Signed / Attested / Confirmed / Under review / Expired / Void).
6. SME: tranche inbox — confirm or dispute with statement upload; countdown to auto-confirm.
7. Window close views: success (start date, stub interest), below-50% refund checklist for SME + tracked refund receipts.
8. Disclosures (copy by Huy): FundLok not a party, no guarantee, money moves directly, partial funding, tax withheld.

**Epic 2 — Today's payments (repayment MVP)**
1. SME screen: today's due per investor (principal, interest, tax, net), one VietQR each, mark-as-paid + receipt optional.
2. Investor: schedule view; "report missing" before the deadline (countdown); statement upload for disputes.
3. Standing-order setup instructions per bank (static content, Techcombank first).

**Epic 3 — Staff (admin)**
1. Exceptions queue: receipt viewer + extracted-field editor (manual entry), approve/void.
2. Dispute queue: both sides' statements side by side, decision form, evidence log timeline.
3. Fee invoices and overdue blocks view.

---

## 6. Recommended positions on Lok's three alignment points (Edward to close)

1. **Burden of proof.** Works as a manual flow: payer proves first (disbursement → investor; repayment →
   SME), counterparty responds with its statement; conflict ⇒ bank trace (tra soát) by the payer; staff
   decision is recorded with outcome and decider. Model it as `dispute_evidence` rows with a
   `required_next_party` field so the queue shows whose turn it is, plus a response deadline (placeholder 2 wd).
2. **Dispute window length.** Keep the **instalment day counted as paid unless reported by the deadline**,
   but make the deadline **2 working days** (matching the disbursement window) rather than noon next day; a
   later report is accepted into reconciliation but never silently rewrites history.
3. **Late disputes vs CIC.** Corrections are **new events**, never edits: `cic_events` already append
   `VOID` / `MISSED_DAY_CORRECTION`. Ask CIC (Lok's item) whether a resubmission window exists; until
   answered, assume corrections are allowed at any time and cap internal late-report acceptance at 5
   working days. PROVISIONAL.

Also close with Lok: minimum ticket (10M vs 20M — a config value; default 10M), the cap/fee exposure (I-11), S6 (I-12).

---

## 7. Out of scope (do not build)

Bank feed/OAuth/SePay/Casso, GPay or any payment-initiation partner, CIC transport, arrears/lateness
handling, extension fee (D27), weekly settlement, automatic receipt OCR with external services, e-invoice
vendor integration, any frontend code, share conversion, Decree 94 reporting beyond the CIC event log.

## 8. Definition of done (Phase A)

- [ ] P-0…P-9 merged, each with all 9 spec sections and testable acceptance criteria.
- [ ] No spec contradicts ADR-009; stale docs marked superseded.
- [ ] `grep -rn PROVISIONAL docs` lists every item in §2 tagged PROVISIONAL.
- [x] State additions in §4.3 (#10, #11) accepted by Edward (E4); **Phase B released: ☑ 28 Oct 2026 (E1–E6, §0.1)**

## 9. Changelog

| Date | Change |
|---|---|
| 2026-10-28 | v0.1 created from Loc.md, Loc2.md and repo review. |
| 2026-10-28 | v0.3 Edward accepted all defaults; fees live (S6 superseded); ledger/stub-interest/state additions decided; Phase B released with a per-task spec gate (§0.1). |
| 2026-10-28 | v0.2 diagrams recovered from the live Lok doc (rev 47). §4.3 replaced with Lok's 8-state/9-transition machine; added §4.4 and I-22…I-26; I-1 resolved; Phat ticket status list aligned. |
