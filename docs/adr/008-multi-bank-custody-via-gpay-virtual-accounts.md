# ADR-008: Payment Rails and Custody Model — FundLok Holds No Money

> **SUPERSEDED 2026-10-28 by `docs/adr/009-direct-transfer-model.md`.** Kept for history only.

> The filename still says "multi-bank custody via GPay virtual accounts" because the first draft (2026-10-14) was framed that way. It is kept so existing links work. The title above is current.

| Field | Value |
|---|---|
| **Status** | **SUPERSEDED by ADR-009 (2026-10-28).** Was Proposed. GPay and the FBO account are out (D-020); do not build against this ADR. |
| **Date** | 2026-10-14, revised 2026-10-22 |
| **Deciders** | Edward Wong (CTO), Loc (CEO), with Legal input on custody |
| **Refines** | ADR-003 (omnibus-first, escrow-compatible ledger seam). Does not supersede it yet. |
| **Related** | `docs/outstanding-issues.md` (Q-001, Q-002, Q-004, Q-013 to Q-016; D-011 to D-013), `docs/specs/payments/gpay-virtual-account.md`, `docs/specs/payments/funding-flow.md`, `docs/specs/ledger/ledger-foundation.md`, `fundlok-domain` skill §9 |

## Context

**The constraint (D-011).** Under the current legal structure FundLok must not hold money at any point from disbursement to repayment. Money moves directly between investors and SMEs. FundLok instructs and records. The ideal is a custodial bank that triggers the movements between the parties, but a bank will not take FundLok seriously until the platform has proven the end-to-end use case. The short-term goal (D-012) is therefore a usable platform across the major use cases, with transaction costs absorbed by FundLok.

**This matches the domain skill and contradicts the older documents.** The `fundlok-domain` skill says money never moves into a FundLok-owned account and that custodial partners are plural. ADR-002, ADR-003's launch assumption, the funding-flow leg table (`EXTERNAL → OMNIBUS_CASH → …`), HANDOFF-03 S1/S2/S5 and the money-flow diagram in `CLAUDE.md` all assume a pooled FundLok custodial account. The schema encodes it: `custodial_accounts` has `uq_single_platform_custodial`, and `resolve_custodial_account()` falls back to the single PLATFORM row.

**The rails on the table (2026-10-22):**

| Rail | Pros | Cons |
|---|---|---|
| 1. Direct bank or QR transfer between investor and SME | Fast, cheap, easy. | We cannot see the transfer, so we must find a way to validate it on the platform. |
| 2. GPay pay-in and pay-out (G Payment JSC, G-Group) | Automatic, traceable. | About 3,300 VND per successful transaction (unconfirmed). Unclear whether it can pay between third-party accounts without an account FundLok owns (Q-001). |
| 3. Others that give traceability and automation without FundLok holding money | — | To be proposed. Candidates are a bank-notification service on the receiving account, bank standing orders or direct-debit mandates, and a custodial bank (the target state). |

**A conflict that decides everything.** Loc's description (2026-10-13 and 10-16) is that GPay creates virtual accounts at each customer's own bank, the money stays at the customer's bank, the customer remains the legal holder "depending on the stage", and GPay executes payouts. Phat's reading is that GPay pay-in and pay-out must go through an account FundLok owns, which would break D-011. Until GPay answers, this ADR cannot be accepted.

**Cost sensitivity.** The fee is per transaction, so it grows with investors times payout days, and daily instalments are small. On a 100M VND loan with about 5.5M margin, 10 investors paid daily cost roughly 3.3M over about 100 business days and about 4.3M over 6 months. If each transfer bills twice (pay-in and pay-out) the loss is real. A 3,300 VND fee is about 3% of a 105,000 VND daily payout.

## Open facts

These are tracked in `docs/outstanding-issues.md`, which is the record. This ADR changes only when they are answered there.

- **Q-001:** can GPay pay customer to customer with no FundLok account in between, and who holds the money in transit?
- **Q-002:** the ledger model (this ADR's decision).
- **Q-004:** when does an investor's money move, given no escrow?
- **Q-003, Q-013 to Q-016:** fee tariff, rail per transfer, evidence needed for a direct transfer, payout cadence, investor cap.

## Options

### Option A — Confirmed-transfer ledger, rail-agnostic (leading)
The ledger records **confirmed party-to-party transfers** and the claims they create. There is no FundLok cash account.

- **A transfer** has: from party, to party, amount, purpose (funding, disbursement, repayment, fee), contract, reference code, rail, status, and an **evidence level**.
- **Evidence levels** record how it was confirmed: `PROVIDER_CONFIRMED` (a GPay notification or query), `BANK_EVIDENCED` (a bank-notification service on the receiving account), `ATTESTED` (sender's receipt plus receiver's confirmation in the app). Which level counts as traceable is Q-014.
- **Rails are adapters** that produce the same confirmed-transfer event: a GPay adapter, a direct-transfer adapter (reference code plus confirmation, with detection added later), and later a custodial-bank adapter. The ledger never imports provider types.
- **The ledger accounts that survive:** `LENDER` (claim), `BORROWER` (obligation), `UNEARNED_INTEREST`, `FL_REVENUE`, and the counterpart used for the parties' own banks. **`OMNIBUS_CASH` and `INVESTOR_CASH` have no meaning** here, because FundLok holds nothing and there is nothing to pool. The leg shapes belong in a revised `funding-flow.md`, not here.
- **Reconciliation** is per rail: our confirmed transfers against what the rail reports, daily, with a pre-payout check on any instruction FundLok sends. A mismatch holds further instructions on that account and is fixed by correcting entries, never by editing the ledger.
- **Rail per transfer.** The adapter chooses the rail for each transfer, so a hybrid (GPay for the few large funding and disbursement transfers, direct transfer for many small daily repayments) is configuration, not a redesign (Q-013).

### Option B — Pooled custodial accounts (ADR-003)
One or more custodial accounts through which FundLok instructs movements: a single omnibus/FBO account or one account per project, with investors and the SME as participants. The existing ledger design and funding-flow leg table apply.

- This is the **target state** (D-013) once a custodial bank signs.
- It is valid under D-011 only if the account is a custodial or FBO account FundLok controls but does not own, and Legal agrees that "instructing" is not "holding".
- It cannot be used now: no custodial bank has signed, and MB Bank is obsolete (D-009).

### Option C — One custodial account per virtual account (rejected)
With fewer than 10,000 users this creates thousands of accounts and per-account reconciliation for no legal or economic distinction. Reconsidered only if Q-001 shows each VA is a separately held, legally distinct account.

## Provisional direction (not a decision)

1. **Design the ledger on the logical model, contracts and participants, and treat each physical account or rail as a mapping.** Option B's custodial-account indirection from ADR-003 is the mapping layer. Moving from Option A to Option B is then an adapter and mapping change, not a ledger rewrite.
2. **Build now what does not depend on Q-001:** the rail-agnostic transfer record, the direct-transfer adapter with a reference code and two-sided confirmation, the GPay webhook storage, matching and mock client, and the evidence-level field.
3. **Build nothing ledger-side on a pooled assumption.** In particular, `record_funding`, `INVESTOR_CASH`, `EXTERNAL → OMNIBUS_CASH` and the single-PLATFORM fallback are not built until Q-001 and Q-002 are answered. None of them is implemented in `app/` today, so the cost of changing course is only specification.
4. **If Q-001 resolves to B** (money passes through a FundLok-owned account), GPay cannot carry payouts and the direct-transfer rail becomes the only automated option until a custodial bank signs. **If it resolves to C** (GPay's intermediary account), Legal must say whether that counts as FundLok holding the money before anything is built on it.

## Consequences

- **Funding flow changes.** Without escrow, money cannot wait for a funding target to be met. The pledge-then-pay flow in Q-004 option A replaces the accepted "pay first, then commit `INVESTOR_CASH`" assumption. This affects the order step (T10), the listing state machine and Phat's screens.
- **`funding-flow.md` (ACCEPTED) will need a new version** once Q-001 and Q-002 are answered. Nothing in it is implemented, so revising it is cheap now and expensive later.
- **GPay spec (`gpay-virtual-account.md`).** Its ledger-posting sections are suspended pending Q-001 and Q-002. Its storage, signature, idempotency, matching and mock-client sections stand.
- **Fees.** GPay's fee is a cost FundLok absorbs (D-006, D-012). It does not touch investor or borrower balances. Recording it needs an expense account, and the timing depends on whether GPay deducts it or invoices it (Q-003).
- **Compliance.** CIC and Decree 94 reporting are about per-borrower and per-agreement balances, not cash location, so the CIC plan is unaffected. The sandbox filing's custody description must match whichever option is accepted.
- **Stale documents to update once this ADR is decided:** ADR-002 and ADR-003 (a status note or supersession), the `CLAUDE.md` money-flow diagram and Tech Stack banking row, HANDOFF-03 S1, S2 and S5, and the MB Bank memory note. They are not edited by this revision.

## Rationale

- The expensive, hard-to-undo part is the ledger. A ledger that records confirmed transfers independent of rail survives a change of rail, including the move to a custodial bank that Edward and the CEO expect.
- Options that need a provider answer wait for that answer. The build that does not depend on it proceeds, so the chicken-and-egg problem with the custodial bank is not made worse by waiting.
- The cost profile of per-transaction fees argues against hard-wiring one rail. The adapter model lets cost and traceability be traded per transfer.
