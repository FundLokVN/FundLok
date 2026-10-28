# HANDOFF-04: proposed `CLAUDE.md` changes (for Edward to apply)

`CLAUDE.md` is modified and uncommitted in the working tree, so it was **not edited**. Apply the hunks below by hand (they are written against `CLAUDE.md` v1.2 as currently on disk). Reason for each: ADR-009 (direct-transfer custody) supersedes ADR-008 and the omnibus assumptions (`docs/outstanding-issues.md` D-014 to D-030).

```diff
@@ What FundLok Is
-- **Revenue model:** FL charges a fee percentage deducted from each disbursement/distribution, processed through the omnibus account.
-- **Omnibus/custodial account:** FL holds a custodial account at a licensed Vietnamese bank. All fund movements (lender → FL, FL → borrower, borrower → FL, FL → lender) flow through this account. The Open API middleware is partner-TBD (a Brankas-style aggregator or the custodial bank direct). Per ADR-003, the ledger is built omnibus-first but escrow-compatible in case only a per-project escrow account can be signed.
-- **Ledger:** FL maintains an internal append-only ledger that mirrors every bank movement. The ledger is the legal record. It is immutable at both the application layer and the database layer (PostgreSQL trigger).
+- **Revenue model:** FL charges two fees, each invoiced separately and paid into FL's own account after the fact (ADR-009, D-023): an SME activation fee of 3% of confirmed principal (VAT invoice, due 7 days, never netted from principal) and an investor service fee of 2.5% a year (accrued monthly, invoiced).
+- **No custody (ADR-009):** FL never holds money. Money moves directly between the investor's and the SME's own bank accounts (Decree 94 Art. 11.1.b). FL issues a dynamic VietQR, collects uploaded receipts, records evidence, computes schedules and reports. There is no FL cash account, no bank API and no CIC connection yet; confirmations rest on uploaded documents reviewed by staff. ADR-002/003's omnibus account and ADR-008's GPay rail are superseded for all new paths.
+- **Ledger:** FL maintains an internal append-only ledger. On the direct-transfer path it is a confirmed-transfer model: `transfers` is the evidence record and claim legs post against a platform-level `EXTERNAL` account; no `OMNIBUS_CASH` legs (D-026, `docs/specs/direct_transfer/ledger-amendment.md`). It is immutable at both the application layer and the database layer (PostgreSQL trigger).
@@ Tech Stack
-| Banking | Open API — custodial partner TBD (Brankas aggregator or direct bank); must meet Circular 64 (OAuth2, TLS 1.2+, ISO 20022) |
+| Payments | Direct bank-to-bank transfer by VietQR (NAPAS 247 / EMVCo). No payment-initiation or bank-data partner today; `app/banking/` stays mocked. A partner may be added later behind the transfer `rail` and `confirmation_level` fields. |
@@ Module Ownership / Edward (CTO)
+- `app/direct_transfer/` — funding window, tranches, transfers and evidence, disputes, repayment collection, fees, CIC event log (ADR-009). Phat's routers call its service functions.
-- `app/payments/` — disbursement/repayment posting (currently a stopgap shim over `app/ledger/`, not yet its own spec'd flow)
+- `app/payments/` — legacy omnibus disbursement/repayment shim over `app/ledger/` (deprecated behind a flag once `app/direct_transfer/` is wired)
@@ Architecture Quick Reference / Omnibus Money Flow
-### Omnibus Money Flow
-```
-Lender          → [Brankas inbound]  → FL Omnibus Account  → ledger: FUNDING
-Borrower        → [Brankas inbound]  → FL Omnibus Account  → ledger: REPAYMENT
-FL Omnibus Acct → [Brankas outbound] → Borrower            → ledger: DISBURSEMENT
-FL Omnibus Acct → [Brankas outbound] → Lender              → ledger: DISTRIBUTION
-FL Omnibus Acct → [internal]         → FL Revenue          → ledger: FEE
-```
+### Direct-Transfer Money Flow (ADR-009)
+```
+Investor → [own bank, VietQR]  → SME's own account   evidence: transfer (receipt)   ledger on confirmation
+SME      → [own bank, VietQR]  → Investor's account  evidence: repayment day        ledger on settlement
+SME      → [own bank]          → FL's own account    FL fee invoices (separate, never netted)
+FL holds no money; receipts, statements and staff review are the evidence.
+```
@@ Core Lending State Machine
-Order:            PENDING_PAYMENT → FILLED | CANCELLED | EXPIRED
+Order:            legacy omnibus path only (deprecated behind a flag)
+Tranche:          RESERVED → SIGNED → ATTESTED → CONFIRMED → ACTIVE
+                  RESERVED|SIGNED → EXPIRED; SIGNED|ATTESTED → UNDER_REVIEW → CONFIRMED | VOID; CONFIRMED → VOID (before ACTIVE)
@@ ADR Index
-| ADR-002 | Omnibus/custodial account via Brankas | Accepted |
-| ADR-003 | Omnibus-first with escrow-compatible ledger seam | Accepted |
+| ADR-002 | Omnibus/custodial account via Brankas | Superseded by ADR-009 for new paths |
+| ADR-003 | Omnibus-first with escrow-compatible ledger seam | Accepted (seam kept; no new path posts cash to it) |
-| ADR-008 | Payment rails and custody model — FundLok holds no money | Proposed |
+| ADR-008 | Payment rails and custody model (GPay) | Superseded by ADR-009 |
+| ADR-009 | Direct-transfer custody model, funding window, evidence-based confirmation | Accepted |
@@ Current Phase / Phase 1
-- [ ] Banking Open API integration (Circular 64) + `bank_transactions` + reconciliation
+- [ ] ~~Banking Open API integration~~ — dropped for MVP (ADR-009): no bank API; evidence-based direct transfer
+- [x] Direct-transfer disbursement and repayment (HANDOFF-04, `docs/specs/direct_transfer/`, `app/direct_transfer/`)
```

Also worth updating when convenient: the memory notes `project_gpay_virtual_account` and `project_mb_bank_custodial` are stale (GPay and MB Bank are out; see D-009, D-020).
