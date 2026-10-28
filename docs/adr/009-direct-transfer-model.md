# ADR-009: Direct-Transfer Custody Model — Funding Window, Evidence-Based Confirmation

| Field | Value |
|---|---|
| **Status** | Accepted (Edward, 2026-10-28; Lok's product design `Loc.md` / `Loc2.md`) |
| **Date** | 2026-10-28 |
| **Deciders** | Edward Wong (CTO), Loc (CEO); Legal (IBL) confirmation outstanding on the items in `docs/outstanding-issues.md` Q-018 to Q-025 |
| **Supersedes** | ADR-008 (payment rails and custody, never accepted); `docs/specs/payments/gpay-virtual-account.md`; the omnibus parts of `docs/specs/payments/funding-flow.md` for the new path |
| **Refines** | ADR-003 (the `custodial_account` indirection stays; no new path uses it for cash) |
| **Related** | `docs/handoffs/HANDOFF-04-direct-transfer-payments.md`, `docs/specs/direct_transfer/*`, `docs/outstanding-issues.md` (D-014 to D-030) |

## Context

FundLok must not hold money at any point from disbursement to repayment (D-011, reconfirmed by Lok 22 and 27 Oct). Decree 94 Art. 11.1.b requires disbursement and payment of principal, interest and fees to go through the customers' own bank accounts or e-wallets. GPay is out (no pay-in or pay-out between two accounts that are not FundLok's), and the FBO account is out ("not worth the risk"). We have no bank API and no bank-data partner, and no CIC connection yet (D-018, D-022).

ADR-002, ADR-003's launch assumption, `funding-flow.md`, the money-flow diagram in `CLAUDE.md` and ADR-008's candidate rails all assumed a pooled FundLok account (`OMNIBUS_CASH`). That assumption no longer holds for any new money path.

## Decision

1. **Money moves only between the investor's and the SME's own bank accounts.** FundLok issues a dynamic VietQR, collects receipts, records evidence, computes schedules and reports. FundLok never holds, receives or instructs money, and no code path posts to `OMNIBUS_CASH` or constructs an instruction to a FundLok-owned account.
2. **Funding window (Approach 2).** The SME signs one standing offer per listing; the listing is open for up to 14 days and closes early when full. Each investor reserves a ticket (2 hour hold), signs an acceptance, pays by VietQR and uploads the receipt. The loan is whatever is confirmed in the window, capped at the asked amount (at most 100M). Below 50% of target at close there is no loan and the SME refunds every tranche (D-014 to D-016).
3. **One investor, one loan contract = one `Tranche`.** `Contract` stays the loan container (one per listing) and `Holding` is created at close from confirmed tranches only (D-015).
4. **Evidence, not bank data.** The core object is the `Transfer`: from, to, amount, reference, rail, evidence and a **confirmation level** (Reported, Attested, Confirmed; Verified is reserved for a future bank-data partner). Every confirmation and dispute rests on uploaded receipts and statements reviewed by staff. Extraction and storage sit behind interfaces (`ReceiptExtractor`, `EvidenceStorage`); the MVP extractor is staff manual entry and no receipt is sent to an external OCR or AI service (D-018, Q-025).
5. **Append-only evidence.** `evidence_log` and `cic_events` are append-only at the application layer and by PostgreSQL trigger, like the ledger. Corrections are new rows.
6. **Ledger: confirmed-transfer model** (D-026). `transfers` is the evidence record. On confirmation the service posts claim legs against a platform-level `EXTERNAL` counterparty ("the rest of the world", defined in `funding-flow.md` section 3.3 but not previously implemented). No `OMNIBUS_CASH` legs on any new path. The existing ledger code and immutability trigger are untouched; only the account-type check gains `EXTERNAL`.
7. **Fees are collected afterwards and separately** (D-023, supersedes S6): SME activation fee 3% of confirmed principal on a VAT invoice, due 7 days, never netted from principal; investor service fee 2.5% a year, accrued monthly and invoiced. Unpaid invoices block new listings and new reservations.
8. **CIC follows the money** (D-022): every reportable event is logged now with an idempotent emit key and `sent_at` NULL; there is no sender.
9. **Stub interest** is a separate dated obligation per tranche, due on the first instalment day, outside `T0 = P + I`, counted in the cap test (D-027).
10. **Timers are ARQ jobs** wrapping idempotent service functions that take an injected clock (hold expiry, dispute lapse, window close, missed-day deadline, invoice due, fee accrual).

## Consequences

- Positive: a clean legal story (Art. 11.1.b), no payment-intermediary cost, no custody risk, partner-independent. The `rail` and `confirmation_level` fields let a later partner plug in without a schema change.
- Negative: confirmation is document-based and therefore forgeable; mitigated by amount, reference, payee and payer-name checks, bank-transaction-id and file-hash uniqueness, SME confirm-or-dispute and staff review. A refund after money has moved depends on the SME and is only tracked, not enforced. Repayment is manual: up to 840 transfers on a 100M, 4-month loan with 10 investors.
- Documents superseded: ADR-008, the GPay spec, `funding-flow.md` section 6 for the new path. `CLAUDE.md` (money-flow diagram, "Omnibus/custodial account" bullet, ADR index, Phase 1 checklist) is stale; the proposed edit is in `docs/handoffs/HANDOFF-04-claude-md-proposed-diff.md` for Edward to apply.
- Legal items still open before live money: IBL on the standing-offer wording (Q-019), traceability of receipts (Q-018), the 20% cap exposure of the 3% fee (Q-022) and of stub interest (Q-023), withholding (Q-024), data residency (Q-025). A remote-signing vendor, an e-invoice vendor, the CIC connection and the storage location decision are also needed; the code ships with mocks and interfaces only.

## Alternatives considered

- **GPay virtual accounts (ADR-008):** rejected by Lok (22-23 Oct); cannot pay between non-FundLok accounts.
- **Custodial or FBO account:** rejected ("not worth the risk"); revisit only with a signed bank (Q-017).
- **Scheduled co-signing (Approach 1):** dropped; adds no legal value (e-contracts may be signed at different times) and brings back no-shows.
- **Bank-data feed (SePay/Casso):** needs a licensed partner and SME consent; out of scope until one exists.
