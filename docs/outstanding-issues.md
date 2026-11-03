# Outstanding Issues & Decisions

The shared record of **what is decided, what is pending, and who owns the answer.** Slack is the notification; this file is the record. If an answer exists only in Slack, it does not exist (CLAUDE.md, AI context rule).

| | |
|---|---|
| **Maintained by** | Edward (CTO) |
| **Contributors** | Loc (business/regulatory), Phat (integration/frontend), Minh (infra), and each person's Claude |
| **Last updated** | 2026-10-28 |

---

## How to use this file

**Adding a question.** Make it decision-shaped so the owner can reply with a letter. Each entry needs:

- **ID**: `Q-###`, never reused. Decided items move to the Decided table, keeping their ID.
- **Context**: two or three sentences in plain business language. No jargon the owner would have to look up.
- **Options**: 2–3, lettered, with the consequence of each.
- **Default**: what we will assume if nobody answers, so work is not blocked by silence.
- **Blocks**: what cannot proceed until it is answered. `Nothing` is a valid value.
- **Owner**: who actually knows. See below.
- **Provenance**: where the current answer or default comes from.

**Owners.**

| Owner tag | Answers |
|---|---|
| `Loc` | Business policy and regulatory decisions. |
| `Legal` | Legal interpretation (via Loc). |
| `GPay` | Facts about how GPay behaves: who holds funds, settlement timing, reversals, fees, negative balances. From GPay's documents or account manager, not from opinion. |
| `Phat` | Integration details and frontend contracts. |
| `Edward` | Architecture and backend design. |
| `Minh` | Infrastructure and hosting. |

**Provenance values.**

- `confirmed by <person>, <date>, <where>`: the owner said it. Only these are promoted into an ADR or spec.
- `from GPay docs, <link>`: read in the provider's documents.
- `inferred`: our assumption. Treat as provisional.

**Status values.**

| Status | Meaning | What work may do |
|---|---|---|
| `DECIDED` | Confirmed by the owner. | Build against it as a constraint. |
| `PENDING-NONBLOCKING` | Open, but independent work can continue. | Proceed with the default only where it is cheap to undo, and mark it `PROVISIONAL(Q-###)` in code and specs. |
| `PENDING-BLOCKING` | Open, and some work depends on it. | Defer only the work listed under **Blocks**. Everything else continues. |

A decision is blocking for a piece of work only if reversing it would force rework of a migration or schema, ledger legs or account types, an API contract with Phat, or a regulator-facing output. Ledger and money-leg work is never built on a provisional assumption.

**Answering.** Reply with the letter and any note. Then do **one** of the following, and the record is updated either way:

1. Edit this file and open a PR.
2. Tell Edward in Slack. He transcribes the answer here with provenance and merges the PR.

**Revisiting a decision.** If new information changes a `DECIDED` item, do not edit it in place. Add a new `Q-###` that references the old one, mark the affected work blocked, and change the ADR or spec only after the owner confirms.

**Finding unfinished work.** `grep -rnE "PENDING|PROVISIONAL" docs app tests`.

---

## Pending

### Q-001 — Can money move between customers without passing through a FundLok account? `SUPERSEDED`
> **Superseded 2026-10-28 by D-020 (residual question: Q-017)** (HANDOFF-04, Lok's Loc.md/Loc2.md and Edward's decisions E1-E6). Kept for history; do not build against the text below.

**Context.** The legal structure requires that FundLok holds no money at any point from disbursement to repayment (D-011). Loc understands GPay as virtual accounts at the customers' own banks, with GPay executing payouts directly between customers. Phat's reading is that GPay pay-in and pay-out must go through an account FundLok owns, which would break D-011. Both cannot be right.
**Options.**
- **A.** GPay pays customer to customer. FundLok only sends instructions and has no account in between. GPay is usable as planned.
- **B.** The money passes through an account FundLok owns or operates. This breaks D-011, so GPay could not be used for payouts.
- **C.** The money passes through GPay's own licensed intermediary account while in transit. Legal must decide whether that counts as FundLok holding it.

**Default.** Assume GPay payouts are unavailable. Hold all GPay ledger work. Build the parts that don't depend on the answer: the webhook, storage, matching and mock client, and the direct-transfer track (Q-013).
**Blocks.** GPay payouts, the ledger side of GPay, funding-flow T10 and T14, accepting ADR-008, GPay spec §3 ledger legs.
**Owner.** `GPay` for how it actually works (can payouts go between third-party accounts, and who holds the money in transit). `Legal`, via Loc, for whether option C counts as FundLok holding it.
**Provenance.** The two readings conflict. Loc, 2026-10-16, Slack: the VA money "physically sits at the customer's bank account. The legal holder is still the customer depending on the stage", and GPay executes payouts. Phat's reading, relayed by Edward 2026-10-22: pay-in and pay-out have to go through our account (unverified). Status: `inferred`.

### Q-002 — How should the ledger record money FundLok never holds? `SUPERSEDED`
> **Superseded 2026-10-28 by D-026** (Edward, decision E2, HANDOFF-04): option A, the confirmed-transfer model. Kept for history.

**Context.** Our ledger was designed around a pooled FundLok bank account (`OMNIBUS_CASH`, `INVESTOR_CASH` and similar). Under D-011 FundLok holds nothing, so the ledger must record claims and confirmed transfers between parties instead.
**Options.**
- **A.** Confirmed-transfer model. The ledger records each confirmed party-to-party transfer (who, to whom, amount, reference, which rail, how it was confirmed). There is no FundLok cash account. Every rail (GPay, direct transfer, later a custodial bank) produces the same kind of confirmed event, and a reconciliation job compares our records with what each rail reports.
- **B.** Pooled model, as in ADR-003: one or more custodial accounts through which FundLok instructs movements. Only valid once a custodial bank (omnibus/FBO or per-project) is signed.

**Default.** A is the working direction and B is the target state (D-013). No ledger changes until Q-001 is answered. The ledger is designed on the logical model (contracts and participants), with each physical account mapped onto it, so moving to B is an adapter and mapping change, not a rewrite.
**Blocks.** `docs/specs/payments/funding-flow.md` implementation (T10, T14), ADR-008.
**Owner.** `Edward`, after Q-001.
**Provenance.** inferred.

### Q-003 — What does GPay charge, and how is it billed? `PENDING-NONBLOCKING`
**Context.** The working figure is 3,300 VND per successful transaction, absorbed by FundLok for now (D-006, D-012). On a 100M VND loan with about 5.5M margin, daily payouts to 10 investors cost roughly 3.3M to 4.4M, depending on the term. We do not know whether one transfer is billed once or twice (pay-in and pay-out), or whether there are volume tiers or batch pricing.
**Options.**
- **A.** A flat fee per successful transaction, deducted from each settled amount.
- **B.** The same fee, invoiced monthly.
- **C.** A negotiated tier or monthly flat fee.

**Default.** Model the fee at 3,300 VND per transfer in margin reports. The fee path in the GPay spec (rule 15) stays configurable.
**Blocks.** Nothing.
**Owner.** `GPay`, via Loc.
**Provenance.** The 3,300 figure was stated by Edward on 2026-10-22; its source is unspecified and it is `inferred` until GPay's tariff arrives.

### Q-004 — When does an investor's money move? `SUPERSEDED`
> **Superseded 2026-10-28 by D-016 (residual question: Q-020)** (HANDOFF-04, Lok's Loc.md/Loc2.md and Edward's decisions E1-E6). Kept for history; do not build against the text below.

**Context.** With no FundLok account, money cannot wait in escrow until a loan is fully funded. If each investor pays the SME directly as they commit, the SME may receive part of the money without the funding target being met. Today an order is filled immediately with no payment step.
**Options.**
- **A.** Pledge, then pay. Investors commit a pledge. Once the target is met, each investor pays the SME directly within a set window. Unpaid pledges are released, and a rule decides whether the listing reopens or closes underfunded.
- **B.** Pay at commitment, directly to the SME. If the target is missed by the deadline, the SME must return the money. The investors carry SME credit risk on the refund.
- **C.** Hold the money with a third party (a custodial bank or GPay's intermediary account) until the target is met. Only possible if Q-001 resolves to C or a custodial bank is signed.

**Default.** A.
**Blocks.** The funding-flow order step (T10), the listing state machine, Phat's investor screens.
**Owner.** `Loc` (policy: window length, unpaid pledges, whether partial funding is allowed), with `Edward` and `Phat` on design.
**Provenance.** inferred.

### Q-005 — How does an SME payment map to a repayment? `PENDING-NONBLOCKING`
**Context.** Under the direct-transfer model each repayment transfer carries a reference code identifying the contract and instalment. If an SME has more than one loan at once, a payment must still be attributed to the right one. Separately, it is unclear whether the amount must equal the daily instalment exactly.
**Options.**
- **A.** Accept only the exact instalment amount. Anything else is held for an operator.
- **B.** Accept any positive amount and apply it to the oldest due instalment.
- **C.** Require a contract reference in the transfer.

**Default.** A, until the arrears policy is defined. Late and partial payments are outside the MVP.
**Blocks.** Nothing before repayment collection.
**Owner.** `Loc` (policy), `Edward` (design).
**Provenance.** inferred.

### Q-006 — Is each investor's loan a separate contract? `SUPERSEDED`
> **Superseded 2026-10-28 by D-015 (residual question: Q-019)** (HANDOFF-04, Lok's Loc.md/Loc2.md and Edward's decisions E1-E6). Kept for history; do not build against the text below.

**Context.** Loc expects each investor-to-SME relationship to be a separate contract, so 10 investors in one project means 10 contracts. This decides how many agreements we report to the credit bureau and what each contract number identifies. Loc will ask Legal.
**Options.**
- **A.** One contract per investor and SME pair.
- **B.** One contract per project with many investors.

**Default.** Model a "reportable agreement" as its own record, so either answer is cheap.
**Blocks.** The credit-bureau agreement number and lender fields.
**Owner.** `Legal`, via Loc.
**Provenance.** Loc, 2026-10-12, Slack: expected A, left open pending Legal (`inferred` until Legal answers).

### Q-007 — What do we report as "overdue interest" and days late? `PENDING-NONBLOCKING`
**Context.** The credit bureau wants a daily debt status and overdue interest, but the MVP has no arrears handling and no late-payment interest.
**Options.**
- **A.** Derive days-past-due and unpaid in-term interest for reporting only. No arrears states are added.
- **B.** Always report the lowest status and zero overdue interest until arrears exist.

**Default.** A.
**Blocks.** Reporting scope only, not the MVP happy path.
**Owner.** `Loc`.
**Provenance.** Raised 2026-10-12. Loc did not answer.

### Q-008 — Decree 94's own requirements `PENDING-NONBLOCKING`
**Context.** We have the credit-bureau decisions but not the text of Decree 94. Exposure caps, maximum loan term, data residency and retention periods come from it. The credit-bureau reporting does not need them.
**Options.**
- **A.** Obtain an English translation and include these in this release.
- **B.** Defer them to a later release.

**Default.** B. No exposure-cap enforcement is built and no customer or credit-bureau data is hard-deleted.
**Blocks.** The compliance module (exposure caps).
**Owner.** `Loc`.
**Provenance.** inferred.

### Q-009 — Which KYC/KYB vendor? `PENDING-NONBLOCKING`
**Context.** Didit is no longer used. Kalapa is being quoted.
**Options.** A. Kalapa. B. Another vendor. C. Keep the current GVerify flow.
**Default.** Verification stays behind a vendor-neutral interface. No new vendor-specific code.
**Blocks.** Nothing.
**Owner.** `Phat` and `Loc`.
**Provenance.** Loc, 2026-10-12, Slack.

### Q-010 — Hosting provider and fixed outbound address `PENDING-BLOCKING`
**Context.** The credit bureau only accepts connections from a registered fixed IP, over two carrier lines and an encrypted tunnel. Level 2 security is a prerequisite and starts with the cloud provider.
**Options.** Depends on the vendor Minh selects. A. A Vietnamese cloud with both. B. Another provider with a NAT gateway and leased lines.
**Default.** Nothing in the code depends on the provider.
**Blocks.** The credit-bureau pilot connection and the production worker. Not code.
**Owner.** `Minh`.
**Provenance.** Loc, 2026-10-12, Slack: pushing this first.

### Q-011 — 21 or 22 business days per month `PENDING-NONBLOCKING`
**Context.** The code computes maturity with 22 business days per month. The repayment spec (v1.4, D14) says 21. The end date reported to the credit bureau depends on it.
**Options.** A. 21, per the spec. B. 22, per the code.
**Default.** A.
**Blocks.** Persisting the maturity date.
**Owner.** `Edward`.
**Provenance.** inferred.

### Q-012 — Which document wins when they disagree? `PENDING-NONBLOCKING`
**Context.** `CLAUDE.md` shows money moving through a FundLok omnibus account. The domain skill (from the Handbook) says money never moves into a FundLok-owned account. Loc's latest answers side with the skill.
**Options.**
- **A.** The domain skill governs business rules. `CLAUDE.md` governs process and ownership.
- **B.** `CLAUDE.md` governs everything.

**Default.** A.
**Blocks.** Nothing, but `CLAUDE.md` and ADR-002 are stale until resolved.
**Owner.** `Edward`.
**Provenance.** inferred.

### Q-013 — Which payment rail for which transfer? `SUPERSEDED`
> **Superseded 2026-10-28 by D-020 (residual question: Q-017)** (HANDOFF-04, Lok's Loc.md/Loc2.md and Edward's decisions E1-E6). Kept for history; do not build against the text below.

**Context.** There are three ways to move money without FundLok holding it: a direct bank or QR transfer (free, but we must detect it), GPay (automatic, about 3,300 VND per transfer, on hold pending Q-001), and a future custodial bank. Daily repayments are many small transfers, where a flat fee hurts most (about 3% of a 105,000 VND payout).
**Options.**
- **A.** Hybrid. GPay for funding and disbursement (few, large transfers). Direct transfer with confirmation for daily repayments (many, small).
- **B.** GPay for everything, absorbing the cost for the pilot.
- **C.** Direct transfer for everything until a custodial bank is signed.

**Default.** A as the design. The adapter chooses the rail per transfer, so no rail is hard-wired.
**Blocks.** Nothing now. The direct-transfer track can start. The GPay track waits for Q-001.
**Owner.** `Loc` and `Edward`.
**Provenance.** inferred, from Edward's three proposed options on 2026-10-22.

### Q-014 — Is a reference code plus two-sided confirmation enough to count as traceable? `SUPERSEDED`
> **Superseded 2026-10-28 by D-018 (residual question: Q-018)** (HANDOFF-04, Lok's Loc.md/Loc2.md and Edward's decisions E1-E6). Kept for history; do not build against the text below.

**Context.** Direct transfers happen at the parties' own banks, which FundLok cannot see. We would prove each one with a unique reference code, the sender uploading a receipt, and the receiver confirming receipt in the app. An automated bank-notification service on the receiving account could be added later.
**Options.**
- **A.** The reference code plus two-sided confirmation is enough for the sandbox.
- **B.** Bank-sourced evidence (a notification service or GPay) is required for every transfer.
- **C.** Two-sided confirmation is enough below an amount threshold, and bank-sourced evidence is required above it.

**Default.** Build with an "evidence level" recorded on every transfer, so B or C can be switched on without a schema change.
**Blocks.** Nothing in the build. It affects whether the direct-transfer track can be used at go-live.
**Owner.** `Legal`, via Loc.
**Provenance.** inferred.

### Q-015 — Daily or weekly payout to investors? `SUPERSEDED`
> **Superseded 2026-10-28 by D-019** (HANDOFF-04, Lok's Loc.md/Loc2.md and Edward's decisions E1-E6). Kept for history; do not build against the text below.

**Context.** GPay's cost grows with investors times payout days. Settling weekly instead of daily would cut it roughly fivefold. The money cannot sit with FundLok in between, so the SME would have to pay from its own account on a weekly cadence.
**Options.**
- **A.** Daily payout, as in the current repayment spec.
- **B.** Daily accrual, weekly payout from the SME's own account. The SME owes the same total. This changes how we treat a late payment.
- **C.** Daily collection with weekly distribution. This needs a third party to hold the money, so it is excluded under D-011 unless a custodial bank is in place.

**Default.** A.
**Blocks.** The repayment collection design and fee estimates, not current work.
**Owner.** `Loc`.
**Provenance.** inferred.

### Q-016 — Should we cap investors per listing, or set a minimum ticket? `SUPERSEDED`
> **Superseded 2026-10-28 by D-025 (residual question: Q-021)** (HANDOFF-04, Lok's Loc.md/Loc2.md and Edward's decisions E1-E6). Kept for history; do not build against the text below.

**Context.** Transfer cost scales with the number of investors on a loan, and with small shares the flat fee is a large percentage of each payout.
**Options.**
- **A.** Cap the number of investors per listing (for example 5).
- **B.** Set a minimum ticket (for example 10M VND).
- **C.** No limit. FundLok absorbs the cost in the pilot (D-012).

**Default.** C, with the two limits available as configuration that is unset.
**Blocks.** Nothing.
**Owner.** `Loc`.
**Provenance.** inferred.

### Q-017 — Do we ever want a payment-initiation or bank-data partner? `PENDING-NONBLOCKING`
**Context.** Money now moves by direct transfer only (D-020). Automatic confirmation or SME payment push would need a licensed partner (bank-data feed, e-wallet, payment intermediary). Residual of Q-001 and Q-013.
**Options.** A. Revisit once the pilot shows the manual flow's cost. B. Never. C. Revisit when Decree 94 sandbox review asks for bank-sourced evidence.
**Default.** A. The transfer model carries a reserved `VERIFIED` confirmation level and a `rail` field, so a partner plugs in without a schema change.
**Blocks.** Nothing.
**Owner.** `Loc`.
**Provenance.** inferred; follows from `Loc.md` ("A bank feed is for later").

### Q-018 — Is a receipt plus SME confirmation (or a lapsed window) traceable enough for Art. 11.1.b? `PENDING-NONBLOCKING`
**Context.** Residual of Q-014. Every confirmation rests on uploaded documents reviewed by staff (D-018). IBL has not yet said whether this evidence is sufficient for the Decree 94 dossier.
**Options.** A. Sufficient for the sandbox. B. Bank-sourced evidence needed above an amount threshold. C. Needed for every transfer.
**Default.** A, with the evidence level recorded on every transfer so B or C is a policy switch.
**Blocks.** Live money, not the build.
**Owner.** `Legal`, via Loc (IBL question 2 in `Loc.md`).
**Provenance.** `Loc.md`, "For IBL".

### Q-019 — Does the standing offer form a separate valid contract with each accepting investor? `PENDING-NONBLOCKING`
**Context.** Residual of Q-006. Civil Code Art. 391.1, read literally, ends an offer at the first acceptance. The offer wording must say it is an offer to sign a separate contract with each accepting investor up to the target.
**Options.** A. IBL confirms the wording. B. IBL asks for per-investor countersignature (would reintroduce scheduling).
**Default.** A. One live tranche per investor per listing; the tranche is the investor's loan contract.
**Blocks.** Live money, contract set v0.1.
**Owner.** `Legal`, via Loc.
**Provenance.** `Loc.md`, appendix.

### Q-020 — Refund period and mechanics for a cancelled loan `PENDING-NONBLOCKING`
**Context.** Residual of Q-004. Below 50% raised, or on KYB fail or fraud, the SME refunds every tranche. FundLok cannot pull money back (IBL question 5).
**Options.** A. Fixed number of working days in the contract (default 2). B. Longer. Refunds are tracked as `transfers` of kind `REFUND` either way.
**Default.** A, configurable (`refund_working_days = 2`).
**Blocks.** Contract wording only.
**Owner.** `Legal`, `Loc`.
**Provenance.** inferred.

### Q-021 — Minimum ticket: 10M or 20M? `PENDING-NONBLOCKING`
**Context.** Residual of Q-016. At 10M a 100M loan can have 10 investors, so up to 840 manual transfers a day-cycle for the SME. 20M halves it.
**Options.** A. 10M. B. 20M.
**Default.** A (10M), a config value.
**Blocks.** Nothing.
**Owner.** `Loc`.
**Provenance.** Edward, 2026-10-28 (E5): accepted default 10M.

### Q-022 — Does the 3% SME activation fee count toward the 20% cap? `PENDING-BLOCKING`
**Context.** If a regulator treated the fee as interest, implied annualised cost is 16% + 3% x 12/term: 4 months about 25%, 1 month about 52%, 6 months about 22%. It is built as a separate VAT invoice, never netted from principal. `docs/specs/repayment_schedule/fixed-daily-repayment-mechanism-v1.4.md` D24 allows a fee outside the cap only with documented cost basis and a separate agreement.
**Options.** A. Outside the cap (separate service). B. Inside the cap; cap the fee. C. Lower the fee.
**Default.** A. The INV-5 test runs both ways (with and without the fee) and logs both results.
**Blocks.** Live money. Not engineering.
**Owner.** `Legal` (IBL), `Loc`.
**Provenance.** HANDOFF-04 I-11.

### Q-023 — Does stub interest fit the flat-rate disclosure and the 20% cap? `PENDING-BLOCKING`
**Context.** Early money earns interest from receipt to the common start date. It is built as a separate dated obligation per tranche, outside `T0` and counted in the cap test (D-027). IBL question 4.
**Options.** A. Separate obligation (built). B. Fold into `T0` (breaks the immutable origination total).
**Default.** A. If the cap test would fail, the stub is reduced to the headroom (PROVISIONAL(Q-D5)).
**Blocks.** Live money.
**Owner.** `Legal`, `Loc`.
**Provenance.** `Loc.md` IBL question 4; D-027.

### Q-024 — Withholding of 5% income tax on interest (IBL open item 6) `PENDING-NONBLOCKING`
**Context.** Paying directly makes the SME the payer. Rounding: per-day floor, remainder on the final instalment. Individuals only until IBL says otherwise (PROVISIONAL(Q-D6)).
**Options.** A. Individuals only. B. All investors. C. Per IBL guidance.
**Default.** A.
**Blocks.** The content of the SME's monthly withholding report (Huy owns it), not the data.
**Owner.** `Legal`, `Huy`.
**Provenance.** `Loc.md`, gaps table.

### Q-025 — Data residency and receipt OCR `PENDING-BLOCKING`
**Context.** Receipts carry bank and KYC personal data. Minh stores evidence in Vietnam (Viettel IDC). Sending receipts to a foreign OCR or LLM service would be a cross-border transfer. Related to Q-008.
**Options.** A. Staff manual entry now, local OCR later. B. Domestic OCR vendor. C. Foreign service (not allowed until Q-008 is answered).
**Default.** A. Storage and extraction sit behind interfaces; no receipt leaves the platform.
**Blocks.** Automatic extraction, production storage location.
**Owner.** `Minh`, `Loc`, `Edward`.
**Provenance.** HANDOFF-04 I-14.

### Q-026 — Burden of proof and dispute-window length (Lok's alignment points 1 and 2) `PENDING-NONBLOCKING`
**Context.** Disbursement dispute: the investor proves first with a bank statement, then the SME. Repayment dispute: reversed. A conflict goes to the bank trace (tra soat). Window for a missed-day report: 2 working days instead of noon next day.
**Options.** A. As proposed (built, E6). B. Different order or length.
**Default.** A. Deadlines are config.
**Blocks.** Nothing.
**Owner.** `Loc`, `Edward`, `Huy`.
**Provenance.** `Loc2.md`; HANDOFF-04 section 6.

### Q-027 — Late disputes and CIC corrections (Lok's alignment point 3) `PENDING-NONBLOCKING`
**Context.** Corrections are new CIC events, never edits (D-022). Whether CIC accepts a correction after a real-time report, and within what period, is unknown.
**Options.** A. Corrections accepted at any time; internal late-report acceptance capped at 5 working days (PROVISIONAL(Q-D14)). B. Shorter. 
**Default.** A.
**Blocks.** Nothing before CIC connects.
**Owner.** `Loc` (ask CIC).
**Provenance.** `Loc2.md`; HANDOFF-04 section 6.

### Q-029 — Which provider for bank account-holder name lookup (automating the staff check)? `PENDING-NONBLOCKING`
**Context.** Loc wants the manual check automated. Several providers return the holder name for a bank and account number through the NAPAS account-name inquiry (the bank list marks which banks support it: `lookupSupported`). It returns a name only: no balances or transactions, so it is not the bank-data access ruled out in D-018. `app/direct_transfer/name_lookup.py` has the hook (`BankNameLookup`); today it is `NoLookup`.
**Options.** A. A payment-QR provider with a lookup API (for example VietQR's). B. A bank-data aggregator (SePay, Casso) if one is chosen later. C. Stay manual until volume justifies it.
**Default.** C. The hook is built, nothing is connected.
**Blocks.** Nothing.
**Owner.** `Loc` (vendor, cost, terms), `Edward` (integration).
**Provenance.** Loc, 2026-10-30, Slack: "there should be a way to automate that process".

### Q-030 — Reviewing a rejected account instead of opening a new one `PENDING-NONBLOCKING`
**Context.** A decision is final, and the database allows one entry per user, bank and account number, so a rejected account number cannot be saved again, even when the problem was fixable. Loc wants, for the future, "a review system instead of them having to open a whole new account".
**Options.** A. Re-submission: the duplicate rule ignores REJECTED entries (a migration). B. Staff can reopen a rejected account for another look. C. Both, plus a "needs information" note.
**Default.** None built. Until then staff should leave an account PENDING and contact the user, rejecting only when it is final.
**Blocks.** Nothing.
**Owner.** `Edward` (design), `Phat` (screens).
**Provenance.** Loc, 2026-10-30, Slack.
---

## Provisional rules from HANDOFF-04 (`Q-D` series)

These are working rules built behind config and tagged `PROVISIONAL(Q-Dn)` in code and specs (`grep -rn "PROVISIONAL(Q-D" app docs tests`). IDs are local to HANDOFF-04, never reused; Q-D1 and Q-D2 are unused. Each is `PENDING-NONBLOCKING`, owner `Edward` unless stated, default = the rule below, provenance = `inferred` (Edward accepted the defaults on 2026-10-28, E5/E6, but they are not confirmed by Lok or Legal).

| ID | Provisional rule | Source |
|---|---|---|
| Q-D3 | Capacity, "full" and last-ticket: capacity = target minus live tranches (RESERVED, SIGNED, ATTESTED, CONFIRMED, UNDER_REVIEW); a final reservation equal to the remaining capacity is allowed when it is below the minimum ticket; `SIGNED -> EXPIRED` on hold lapse; a lapse while FULL reopens the window until day 14; late receipt after expiry goes to the exceptions queue and may be revived only if capacity remains (adds `EXPIRED -> UNDER_REVIEW`, cause `LATE_RECEIPT`) | I-6, I-7, I-8, I-22 |
| Q-D4 | Start date = first working day after `closed_at`, where `closed_at` is when the wait for open disputes ends (`close_wait_working_days = 2`); SME may close early only at 50% or more confirmed; "extend once" is not built | I-9, I-24 |
| Q-D5 | Stub interest is a separate dated obligation per tranche (E3), basis `P x r x business_days/252`, counted in the cap test; reduced to the cap headroom if it would breach 20% | I-10 |
| Q-D6 | Withholding: per-day floor, remainder on the final instalment; individuals only | I-16 |
| Q-D7 | A tranche disputed at close is excluded from the loan; if later resolved for the investor it does not join and the SME refunds it | I-17 |
| Q-D8 | `CONFIRMED -> VOID` before ACTIVE (reason `BELOW_MIN_RAISE`, `KYB_FAIL`, `FRAUD`) with a tracked refund transfer; no refund after ACTIVE in MVP | I-23 |
| Q-D9 | Ledger claim legs post when the tranche joins the loan (CONFIRMED to ACTIVE) rather than at CONFIRMED, so a pre-ACTIVE VOID needs no reversal; only principal claim legs are posted, interest, tax and fee obligations live in `repayment_days` and `fee_invoices` until an unearned-interest account is built; `EXTERNAL` is added to the account-type check by migration because it exists only on paper in `funding-flow.md` | I-2 |
| Q-D10 | VAT on the SME activation fee at 10% (config) | I-11, `Loc.md` |
| Q-D11 | SME late fee rate unset (0 per day, config) until the service agreement states it; the overdue block works regardless | `Loc.md` fee table |
| Q-D12 | Cross-platform exposure: SME's declared outside debt plus this target must not exceed 400M (sandbox figure in `Loc.md`) | I-25 |
| Q-D13 | Payer-name match: diacritic-folded, case-insensitive, whitespace-collapsed equality with KYC name; anything else goes to staff | `Loc.md` receipt checks |
| Q-D14 | Missed-day report deadline 2 working days; a later report within 5 working days is accepted into reconciliation without changing history, beyond that it is refused | section 6 |
| Q-D15 | Investor fee invoice due 7 days after issue; unpaid past due blocks new reservations; fee fixed at signing as `amount x rate x term/12` floored per tranche | `Loc.md` fee table |
| Q-D16 | "Repaid in full" for the one-listing lock means the facility is SETTLED and every instalment row is settled | `Loc.md` rule 1 |
| Q-D18 | Payment-account verification (D-031): every saved account starts PENDING ("waiting verification"); the typed account name is only a hint; staff record the holder name the bank shows and approve or reject with a reason; an investor's must equal the KYC name, an SME's may differ only if accepted explicitly with the reason; a bank-name lookup may automate it later (Q-029) | Edward and Loc, 2026-10-30 |

---

## Decided

| ID | Decision | Provenance |
|---|---|---|
| D-001 | Borrowers are registered LLCs and JSCs only. Individuals and households are out. Report the enterprise ID (CT002); customer type 1. | confirmed by Loc, 2026-10-12, Slack (relayed by Edward) |
| D-002 | Didit is no longer used. | confirmed by Loc, 2026-10-12, Slack |
| D-003 | Once connected to the credit bureau, pull directly (3,000 VND per pull, charged back in the disbursement fee) and remove self-submitted bureau reports. | confirmed by Loc, 2026-10-12, Slack |
| D-004 | Each daily instalment splits into principal and interest pro-rata. Investors are repaid in proportion to their holdings. | confirmed by Loc, 2026-10-13, Slack (relayed by Edward) |
| D-005 | A virtual account is per customer, per bank (applies if GPay virtual accounts are used; see Q-001). | confirmed by Loc, 2026-10-16, Slack |
| D-006 | GPay's fees are borne by FundLok for now (marketing expense), recorded at disbursement and repayment. Working figure is 3,300 VND per successful transaction (Q-003). | confirmed by Edward, 2026-10-14 |
| D-007 | GPay Virtual Account is cleared by Legal and is the payment method. | confirmed by Loc, 2026-10-13, Slack |
| D-008 | GPay also executes payouts to SMEs and investors. **Scope disputed, see Q-001.** | confirmed by Loc, 2026-10-16, Slack |
| D-009 | MB Bank is obsolete. | confirmed by Loc, 2026-10-16, Slack |
| D-010 | The end date reported is the extended date after an extension. Collateral flag is 0. Interest is reported as the in-term annual rate. | confirmed by Loc, 2026-10-12, Slack |
| D-011 | FundLok must not hold money at any point from disbursement to repayment. Money moves directly between investors and SMEs. FundLok instructs and records. | confirmed by Edward, 2026-10-22 (current legal structure; Loc to confirm the wording) |
| D-012 | Short term: prove the platform end to end across the major use cases, so a custodial bank takes us seriously. FundLok absorbs transaction costs in the pilot and reduces them later through a GPay agreement or a custodial bank. | confirmed by Edward, 2026-10-22 |
| D-013 | Working assumption: the target state is a custodial bank (a single omnibus/FBO account, or one per project with investors and the SME as participants). Timing unknown. | confirmed by Edward, 2026-10-22 as an assumption (Loc to verify) |
| D-014 | Disbursement is Approach 2, the funding window: invest any time within the window. Scheduled co-signing is dropped. (Answers Q-004, close to option B plus a 50% minimum.) | confirmed by Lok, 2026-10-28, Loc.md / Loc2.md |
| D-015 | Contracts form by standing offer: the SME signs one public offer per listing; each investor's signed acceptance is a separate loan contract, one tranche per investor per listing. `Contract` stays the loan container. (Answers Q-006, option A.) | confirmed by Lok, 2026-10-28, Loc.md |
| D-016 | Funding window: up to 14 days, closes as soon as the target is funded; target is the asked amount, at most 100M; nobody reserves beyond capacity; below 50% confirmed at close there is no loan and the SME refunds every tranche with no fee; one listing per SME until the previous loan is repaid in full; money above an accepted ticket is not a loan and not FundLok's liability. | confirmed by Lok, 2026-10-28, Loc.md / Loc2.md |
| D-017 | Repayment starts the first working day after close; early money earns stub interest at the same flat rate from receipt to the start date. Daily instalments on working days are the product promise. | confirmed by Lok, 2026-10-28, Loc.md |
| D-018 | No bank API and no bank-data partner: every confirmation and dispute runs on uploaded receipts and statements checked by staff. Evidence level is recorded on every transfer (Reported, Attested, Confirmed; Verified reserved). (Q-014, roughly option A.) | confirmed by Lok, 2026-10-28, Loc2.md |
| D-019 | Repayment frequency is daily on working days; weekly settlement is off. (Q-015, option A.) | confirmed by Lok, 2026-10-28, Loc.md |
| D-020 | Rail is direct transfer between the parties' own accounts only; GPay and the FBO account are out; no FundLok-held money at any point. (Resolves Q-001 and Q-013, option C.) | confirmed by Lok, 2026-10-22/23/27/28, Loc.md |
| D-021 | A repayment day counts as paid unless the investor reports it missing by the deadline. SME dispute window on disbursement is 2 working days (placeholder); silence counts as received. | confirmed by Lok, 2026-10-28, Loc2.md |
| D-022 | CIC follows the money: every reportable event is logged now (append-only) and sent once connected; corrections are new events. Borrower limits are checked on FundLok's own records plus the SME's signed declaration until CIC connects. | confirmed by Lok, 2026-10-28, Loc2.md |
| D-023 | Both fees are live. SME activation fee 3% of confirmed principal on a separate VAT invoice, due 7 days, never netted, unpaid blocks new listings. Investor service fee 2.5% a year, accrued monthly, invoiced, unpaid blocks new reservations. **Supersedes S6** (no fee during MVP) in HANDOFF-03; HANDOFF-03 T14 is not used on the new path. | confirmed by Edward, 2026-10-28 (E1) |
| D-024 | BD and introducer commission is paid only on settled tranches, once the dispute window has passed; final once paid. | confirmed by Lok, 2026-10-28, Loc2.md |
| D-025 | Minimum ticket 10M VND (config). (Q-016, option B.) | confirmed by Edward, 2026-10-28 (E5); Lok may revisit (Q-021) |
| D-026 | Ledger: confirmed-transfer model. `transfers` is the evidence record; claim legs post to the ledger on confirmation against an `EXTERNAL` counterparty; no `OMNIBUS_CASH` legs on any new path. Existing ledger code and trigger untouched. (Q-002, option A.) | confirmed by Edward, 2026-10-28 (E2) |
| D-027 | Stub interest is a separate dated obligation per tranche, due on the first instalment day, outside `T0`, counted in the cap test; basis `P x r x business_days/252`. | confirmed by Edward, 2026-10-28 (E3) |
| D-028 | Tranche state machine: Lok's 8 states and 9 transitions plus `SIGNED -> EXPIRED` and `CONFIRMED -> VOID` before ACTIVE. | confirmed by Edward, 2026-10-28 (E4) |
| D-029 | Edward accepted the remaining HANDOFF-04 section 2 defaults (I-6 to I-9, I-16, I-17, I-24, I-25), staff manual receipt entry, mock signer, and SME early close only at 50% or more confirmed. They are built behind config and tagged PROVISIONAL(Q-D3..Q-D16) until Lok or Legal confirm. | confirmed by Edward, 2026-10-28 (E5) |
| D-030 | Recommended positions on burden of proof, dispute-window length and late disputes vs CIC are the working rules (timers only, configurable). | confirmed by Edward, 2026-10-28 (E6); Lok to align (Q-026, Q-027) |
| D-031 | **Payment accounts are verified against the bank, for investors and SMEs alike.** Standard: FundLok name = KYC (investor) or KYB (SME) name = bank account holder name. Every saved account starts PENDING ("waiting verification"); nobody can invest or open a window until it is verified. The name the user types is only a hint (it is self-declared). Staff look up the holder name at the bank (today by typing the bank and account number into a bank app's transfer screen), record it, and approve or reject, with a written reason either way. An **investor** account is approved only if the bank's name equals the KYC name (otherwise reject: AML traceability, disputes and refunds must lead to the KYC-verified person). An **SME** difference (abbreviation, trade name) can be approved only with an explicit acceptance and the reason. A decision is final. Automation (a bank-name lookup returning the holder name) comes later (Q-029). Supersedes the earlier drafts of D-031 (approve on typed-name match) and Phat's #76 (no review). | confirmed by Edward and Loc, 2026-10-30, Slack. Loc: "the standard is FL name = KYC name = bank name" |
| D-032 | Accounts approved on save before verification existed (`approved_by` NULL, investors and SMEs): **leave them.** They are test data, not real accounts, so no migration is needed. Resolves Q-028 (option A). Before any real user data exists, delete the test data or re-verify any remaining account with `approved_by IS NULL`. | confirmed by Edward, 2026-10-30, Slack |
| D-035 | The account-verification task (HANDOFF-05, D-031) is **delegated to Phat end to end, backend and UI.** For this task only he may edit `app/direct_transfer/accounts.py`, `name_lookup.py`, their tests, the admin route/schema/service and the frontend (an exception to the CLAUDE.md module boundaries); not the ledger, tranche state machine, fees, or migrations on other tables. He posts a plan comment on #78 before coding (Edward answers within 24 hours, otherwise proceed); Edward reviews and approves the PR in writing before merge. The exception ends when the task is merged. | confirmed by Edward, 2026-11-03 |

---

## Changelog

| Date | Author | Change |
|---|---|---|
| 2026-10-16 | Edward / Claude | Created. Seeded from the Decree 94 plan, ADR-008 and Loc's replies of 2026-10-12, 10-13 and 10-16. |
| 2026-10-22 | Edward / Claude | Reframed Q-001, Q-002 and Q-004 around the rule that FundLok holds no money (D-011). Added Q-013 to Q-016 and D-011 to D-013. Updated Q-003 with the working GPay fee. |
| 2026-10-28 | Edward / Claude | HANDOFF-04 Phase A (P-0). Added D-014 to D-030 from Loc.md / Loc2.md and Edward decisions E1 to E6. Superseded Q-001, Q-002, Q-004, Q-006, Q-013, Q-014, Q-015, Q-016 (kept, marked). Added Q-017 to Q-027 for residual questions and the Q-D3 to Q-D16 provisional-rule table. D-023 supersedes S6. |
| 2026-10-29 | Edward / Claude | Added D-031 (first draft), Q-D18 and Q-028. |
| 2026-10-30 | Edward / Claude | D-031 replaced after Loc's input: payment accounts are verified against the bank's holder name for investors and SMEs. Q-D18 and Q-028 updated; added Q-029 (lookup provider) and Q-030 (review instead of a new account). |
| 2026-10-30 | Edward / Claude | Q-028 resolved as D-032: existing accounts are test data, leave them. |
| 2026-11-03 | Edward / Claude | Added D-035: the account-verification task is delegated to Phat end to end; HANDOFF-05 gets ownership, sync rules, a definition of done and starting instructions. |
