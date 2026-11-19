# ADR-011: Commit-then-Accept Funding Flow (replaces ADR-009 §2)

| Field | Value |
|---|---|
| **Status** | Accepted (Edward, 2026-11-14; Loc, Appendix M reply, 2026-11-14). Finalised when handbook v3.2 lands |
| **Date** | 2026-11-14 |
| **Deciders** | Edward Wong (CTO), Loc (CEO) |
| **Supersedes** | ADR-009 §2 (funding window: reserve, pay by VietQR and upload a receipt at commit; the loan is whatever is confirmed in the window). The rest of ADR-009 (no custody, direct transfer, evidence-based confirmation, fees) stands |
| **Related** | `docs/specs/direct_transfer/end-to-end-payment-flow.md` (4 Nov), `docs/handoffs/HANDOFF-06-appendix-m-remediation.md`, D-036 to D-052 in `docs/outstanding-issues.md` |

## Context

The pre-launch test (Appendix M, 14 Nov 2026, backend `main` @ `ece64a4`) showed that the code follows ADR-009 §2 and Phat's `repay_from_approval` (2 Nov): each lender holds a ticket for 2 hours, pays at commit, and each confirmed ticket becomes its own loan. The handbook (10.1 to 10.10), the signed Standing Offer (Art. 4) and the 4 Nov flow describe a different process. 18 steps failed and 21 were partial, mostly because of this gap. The 2,084 passing tests encode the old design.

## Decision

Adopt the 4 Nov commit-then-accept flow at launch.

1. **Commit, no money.** On Days 0 to 12 a lender makes a signed pledge. A lender may commit more than once to the same project. No payment hold and no payment at commit.
2. **The SME decides on Day 13, by 15:00.** It accepts or declines the committed total. Silence counts as accept at 50% of target or more and as decline below 50%; the SME can override either way before the deadline. Below 50% with no manual accept: auto-decline, no loan, no money moves, no fee.
3. **Day 14: one contract per lender, then pay.** Each lender signs one loan contract for its total (OTP plus a logged click) and pays the SME directly before 15:00. The Lender Service Agreement is still signed at the first commitment.
4. **Pooled schedule, separate contracts.** All lenders share the same rate, start date, end date and payment day, but each lender keeps its own loan contract and its own claim. The loan is formed once, at the Day-14 cut-off, from the lenders who signed and paid.
5. **No-show.** A lender who has not signed and paid by 15:00 on Day 14 is locked and that lender's loan never takes effect. To unlock: re-eKYC plus the exact acknowledgement sentence (text and time logged). A second no-show in the lender's lifetime closes the account permanently. No lock with proof of force majeure or a bank outage (staff decide).
6. **Two-way check on every disbursement.** The lender uploads its transfer receipt and the SME confirms or disputes within 24 hours; silence counts as confirmed, with the window shown next to each auto-confirmation. Anything counted as received by silence can be disputed within 30 days with a bank statement.
7. **All deadlines count calendar days.** Daily repayments stay Monday to Friday (21 a month, 252 a year). Vietnamese public holidays are ignored everywhere for now: a holiday is a normal repayment day and no deadline moves for it. Recorded as technical debt (handbook 10.1 "a holiday has no instalment" returns later).
8. **Investors earn the full graded rate.** `investor_return_bps` is removed. The 2.5% yearly lender service fee is separate and invoiced.
9. **Stub interest disappears.** Everyone starts on the same day.

## Consequences

- Positive: matches the handbook and the signed contract; no refunds, unwanted partial loans or stub interest; one loan per lender per round, which is easier to explain and reconcile.
- Negative: a large change to the tranche state machine, a migration, and many rewritten tests. Lenders now sign and pay in one short window on Day 14, so the no-show rule carries real weight.
- Neutral: ledger legs still post on confirmation against the platform-level `EXTERNAL` account (D-026); no cash account appears.

## Out of scope

Default handling (M10), the backstop, recovery, fraud handling, the extension fee, and holiday handling. See HANDOFF-06 and the technical-debt list in `docs/outstanding-issues.md`.
