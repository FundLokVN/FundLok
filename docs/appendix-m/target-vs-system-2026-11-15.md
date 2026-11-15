> **Filed by Edward, 2026-11-15**, from Loc's document "FundLok Target vs System" (gap register, 15 Nov 2026). Author and owner: Loc. The text below is unchanged. **This is a snapshot** of backend `main @ 81a5f2e` and will go stale as gaps close; the Phase 1 fixes (T1 to T5) have since merged. The live record is `docs/outstanding-issues.md` and `docs/handoffs/HANDOFF-06-appendix-m-remediation.md`.

Gap register · 15 Nov 2026

# FundLok target vs what the system does today

**Target** = handbook v3.1 (14 Nov) plus Loc's decisions of 15 Nov **System** = backend `main @ 81a5f2e`, frontend `production @ dd0b40d`; full test 14 Nov, script rerun on the new code 15 Nov

Each row says what we agreed, what the code does now, and who closes the gap. "Phase" follows Edward's remediation plan (1 = independent fixes, 2 = money-flow rebuild, 3 = everything else). The last two sections list the documents that no longer match: handbook passages changed by decision, and the project memory file `CLAUDE.md`.

gap: not built or wrongpartialmatchesdocument to update

## System vs target

| Area | Target (handbook + decisions) | System today |  | Owner · phase |
| --- | --- | --- | --- | --- |
| Funding round |  |  |  |  |
| Commitments | A signed pledge on Days 0–12, no money moves. Minimum 10M per commitment (or exactly the remainder), up to capacity; a lender may commit more than once. Loan contracts are signed on Day 14: one per lender for its total. | A commitment is a 2-hour payment hold; the lender must pay at once. Minimum, capacity and last-ticket rules work. A second commitment is refused only while the first is live. | gap | Edward, Phat · 2 |
| SME decision | Accept or "Không chấp nhận" by 15:00 on Day 13 (calendar day). Silence at 50%+ means accept; below 50% means decline. Manual accept below 50% allowed. Reminders 3 days and 1 day before (in-app, SMS, Zalo), each logged. | No decision step, no deadline, no buttons, no acceptance reminders, no SMS sender. Window = open time + 14 days. | gap | Edward, Phat · 2 |
| Below 50% | Auto-decline: no loan, no money, no fee; every lender told. | 40M of 100M became two active loans plus a 1,320,000 SME fee (the 50% rule sits on a disabled branch). | gap | Edward · 2 |
| Disbursement | Day 14: every lender gets a VietQR (SME account, exact amount, unique reference), pays before 15:00, uploads proof. One pooled schedule, a separate loan contract per lender. | VietQR content is correct but issued at commit. Each confirmed ticket becomes its own loan on its own start date (`repay_from_approval=True`). | gap | Edward · 2 |
| SME receipt check | The SME uploads its receipt within 24 hours (calendar), or silence counts as received. Tranches confirm after that. | The SME cannot upload a receipt, only press "Not received". Window is 2 working days. A staff decision on a review confirms with no SME step. | gap | Edward, Phat · 2 |
| Lender no-show | Locked at 15:00 on Day 14. Unlock = re-eKYC plus the exact sentence. Second miss = permanent closure. Shortfall reopens for 7 calendar days. A top-up ends on the original end date. | The ticket simply expires; no lock, no unlock flow, no closure. Freed capacity stays open until the window ends. No top-up formula. | gap | Edward, Phat · 3 |
| Proof checks | Duplicate or edited screenshots and mismatches go to the exceptions queue. | Mismatches go to review (works). Duplicates are refused but never queued. Edited images are not detected. OCR exists but is off locally. | partial | Edward · 3 |
| Repayment |  |  |  |  |
| Schedule maths | I = P·r·M/12, T = P + I, A = T/N0 rounded down, N0 = 21 × M. Interest part = A·I/T rounded down, tax 5% rounded to the nearest đồng, last instalment takes the remainder. | A, T, N0 and last instalment match 10.8 exactly. Interest is taken as a remainder and tax floored with a last-day catch-up, so some lenders are 1 đồng/day off (731,250 vs 731,249). | partial | Edward · 1 |
| Lender rate | Lenders earn the full graded rate (capped at 20%); FundLok's 2.5% a year lender fee is invoiced separately. | Lenders are capped at 2.5% flat over the term (`investor_return_bps=250`). The SME's offer card shows a different total from its repayment plan. | gap | Edward, Phat · 1 |
| Days and holidays | Repayments Monday to Friday; public holidays ignored (a normal day); all deadlines in calendar days. Holiday handling = technical debt. | Repayments skip public holidays via a holiday table (2027 provisional, 2028 missing). Several windows use working days. | gap | Edward · 1 |
| Silence and proof | A lender's silence after the SME's proof means received (24 hours). A day with no SME proof by the end of its due date is Late: shown to staff, SME and lender, not counted for fees, blocks extension filings, and accrues late interest (150% of the loan rate on overdue principal, max 30% a year, plus 10% a year on overdue interest, calendar days) paid to each lender. | The (undeployed) job marks every past-due day presumed paid, even with no SME proof. Report window is 2 working days. No 30-day window. No staff route for "Not received". | gap | Edward, Phat · 1–2 |
| Paying | One QR per lender per day; payments applied to the oldest day first. | Today's payments works. The SME can pay a newer day and leave an older one unpaid. | partial | Edward · 1 |
| Extension | **At launch, automatic by rule** (no staff approval), handbook 11.2: file by the 5th with a tax declaration or bank inflow statement; if the shortfall test passes and nothing is Late, extend by days rounded up to 5 within the backstop; new schedule version; daily never rises; no fee; everyone notified. | Not built. Only schedule version 1 exists. The contract and SME dashboard already promise it. | gap | Edward, Phat · 2–3 |
| Late interest | At launch (see above). | Not built: no Late state, no late interest. | gap | Edward, Phat · 2 |
| Default, recovery, fraud | After launch (rest of M10, M11). | Not built (arrears states defined, never triggered). | matches | — |
| Fees |  |  |  |  |
| SME activation fee | 3% of confirmed principal + VAT, due in 7 calendar days, never netted. Fee QR to Techcombank 116889688. Next listing blocked only after the due date. No late interest. | Amount, VAT and due date are correct; never netted; late interest 0 (matches). No QR, no account details in the email. Blocks from the moment of issue. SME Service Agreement Art. 5.4 still promises late interest. | partial | Edward, Phat · 1 |
| Lender service fee | F = P × 2.5% × M/12 + VAT, invoiced monthly on money actually received, due in 7 days. Unpaid: no new commitments only (no lock, no late fee). | Formula correct, but it runs only in an undeployed job, counts presumed-paid days, the invoice isn't visible to the lender, and investing is blocked from issue. | gap | Edward, Phat · 1 |
| Tax withholding report | The SME gets monthly withholding figures (sum of daily tax per individual lender). | Calculation exists in code; no route, no screen. | gap | Edward, Phat · 3 |
| Eligibility, offer and documents |  |  |  |  |
| Borrower limits | One account per business; 100M outstanding per business; no new listing until repaid in full; **a business that has repaid in full can borrow again**. | Since `d2ea9ee`: one project per business, one project in progress and one live listing per account, projects only for the verified business. Retested 15 Nov: the 150M case is now refused. Still missing: a repaid business can never apply again (one project per business, ever). | partial | Phat, Edward · 1 |
| Offer and Standing Offer | Shows size, graded rate, term, daily, DRR (21 days), revenue share s and guarantor, from the grading engine. | No DRR, s or guarantor on the offer. The Standing Offer prints s as "……%". Engine DRR uses 22 days. | gap | Edward, Phat · 3 |
| Signing | SME and guarantor sign; project goes live (Day 0). Lenders sign their loan contract on Day 14 with OTP plus a logged click. | Three separate CA-signed PDF uploads checked with NEAC. Manual KYB leaves the legal representative blank, which blocks signing. Lender signing is a stand-in. | partial | Edward, Phat · 2–3 |
| Application | Five documents (charter, registration, e-invoices, tax filings, CIC); submit needs them all; staff can return an application for correction; loans of 1–6 months; grading gates on. | Five documents enforced at approval but not at submit; staff can only approve or reject (stuck application). 1–6 months works. Gates are off. | partial | Edward, Phat · 3 |
| Accounts, wording, operations |  |  |  |  |
| Account states | Suspended = no new offers or investments only. A permanent CLOSED status with a reason for misconduct. | SUSPENDED blocks everything, repayments included. No CLOSED status. | gap | Edward, Phat · 3 |
| Wording | T&S Art. 3.2 "FundLok không thu hồi nợ…"; new Art. 4.4; Art. 7 = 30 days; no collection wording anywhere. | No debt-collection disclosure; "\[30\]" placeholder; /why-us "Thu nợ vào mỗi ngày làm việc"; /terms "giải ngân/thu nợ". | gap | Phat · 1 |
| Scheduled work | Round advancing, settlement, overdue marking and fee invoices run on a schedule in production. | Production deploys only the API; those jobs exist only as an undeployed worker. Staff must press Advance. | gap | Edward · 1 |
| Consent and KYC | Three boxes plus OTP; record of who, when, what, purpose; eKYC/eKYB through TPV Datatrust. | Works (email OTP; purpose implied by flag names). eKYC/eKYB via GVerify, which is TPV Datatrust. | matches | — |

## Handbook passages changed by decision

| Handbook | Says today | Change to |  |
| --- | --- | --- | --- |
| 10.1, 10.4, 7.2 step 4, glossary "Commitment", Appendix M 3.2 | Each lender commits once per project. | A lender may commit more than once to a project it is already in. (How an extra commitment is signed: still open.) | update |
| 18.1, 9.2 | Extension is in "Next build"; "not built". | Extension and re-amortisation are in launch scope. | update |
| 4.2, 10.9, Appendix B | Unpaid lender fee: new commitments blocked and account locked. | New commitments blocked only. | update |
| 10.1, Appendix M 7.5 | Repayments Monday to Friday excluding public holidays; a holiday has no instalment. | Holidays ignored for now (technical debt); all deadlines in calendar days. | update |
| 6.3 | Six documents. | Five uploads: the VAT declarations and year-end statement travel in the tax-filings zip. | update |
| Appendix G | Open: rounding, s when partly funded, loan duration, grading gates. | Closed: Art. 7.2 rounding; s stays as set; 1–6 months; gates on. Still open: extension approver and speed. | update |
| 4.2, 10.9, App. B | Unpaid SME fee: late interest under the Civil Code. | No late interest on the SME fee; next listing blocked after 7 days. (SME Service Agreement Art. 5.4 changes too.) | update |
| 10.4, 10.3 timeline, App. M 3.1 | The lender signs the loan contract when committing. | A commitment is a signed pledge; loan contracts are signed on Day 14, one per lender for its total. | update |
| 11.3, 11.4, 18.1 | Late flags and late interest are in "Next build". | Late (end of due date) and late interest ship at launch (150% of the loan rate, max 30%, plus 10% on overdue interest, calendar days). Default, backstop, recovery stay later. | update |
| 11.2, App. M 9.1 | Filing enters the staff queue; staff approve. | Extension is applied automatically by rule when the SME files; no staff approval. | update |

The generated contracts follow the handbook and need the same edits: the Standing Offer (Art. 2 day-12 close, one commitment), the T&S (Art. 3.2, 3.4, 3.5, new 4.4, 7) and the loan contract (Art. 10 extension, Art. 7.2 rounding stays).

## Project memory (CLAUDE.md) out of date

`CLAUDE.md` is the AI context file every session reads, owned by Edward. These lines no longer match the code or the decisions. My Claude memory holds nothing that conflicts.

| CLAUDE.md says | Reality |  |
| --- | --- | --- |
| "ML grading engine assigns interest rate tier" | A deterministic 0–100 grade (no tiers, no letter grades) and the 8.3 rate formula with a versioned parameter set. | update |
| Lenders receive "converted shares after repayment"; module `app/share_conversion/` | No share conversion anywhere in the handbook; the module does not exist. | update |
| Module `app/compliance/` (Decree 94 caps) | Does not exist. The borrower cap lives in `app/direct_transfer/` (`cross_platform_cap_vnd` 400M, provisional) and must become 100M per legal entity. | update |
| `app/verification/` = KYC/KYB (Didit) | Live KYC/KYB runs on `app/gverify/` (TPV Datatrust); Didit is legacy but still mounted. `app/gverify`, `app/consent`, `app/phone_verification`, `app/scheduled_jobs` and `app/rates` are missing from the ownership map. | update |
| Tranche state machine (RESERVED → SIGNED → ATTESTED → CONFIRMED → ACTIVE) and ADR-009 as the accepted model | Correct for today's code, but superseded once ADR-011 (the Day-13/Day-14 flow) lands. The ADR index needs ADR-011. | update |
| MVP scope: extension in (no fee), lateness out | Matches the 15 Nov decisions. | matches |
| Fees: 3% SME activation, 2.5% a year investor fee | Matches the decisions; the code's lender-return cap contradicted it and is being removed. | matches |
| Testing: "never hit a real DB" | The suite deliberately runs on a throwaway Postgres (the repo's own test skill says so); reword as "never a shared DB". | update |
| Banking: Brankas client and omnibus account | Mocked indefinitely; superseded by ADR-009 direct transfer. The description can be trimmed. | update |

## Edward's 14 Nov plan vs the decisions

| Plan says | Change |
| --- | --- |
| Extension is not in any phase | Add M9 extension (automatic by rule) to launch scope. |
| Unpaid days: no arrears states, no late interest | Late state and late interest ship at launch; only Default, backstop and recovery stay out. |
| Q8: take the plan total as the source of truth | No: remove `investor_return_bps`; lenders earn the graded rate and both totals then agree. |
| Phase 1: add the 2028 holiday calendar (M7.5) | Drop it: holidays are ignored for now (technical debt). |
| Phase 2: one pooled loan at Day 14 | Yes, with the contract signing on Day 14 too, and several commitments per lender folded into one contract. |
| B3 cross-project cap | Largely done by Phat's `d2ea9ee` (needs Edward's review). Remaining: let a repaid business borrow again. |
| Phase 3: "M2.2 file replacement bug" | No bug: replacement worked in every run. The M2.2 defect is that an application submits with a document missing and staff can't return it. |
| Phase 3: "M1.3 CIC pending" | The CIC refusal was my synthetic test file. The M1.3 defect is that a manual KYB approval can't record the legal representative and address, which blocks signing. |
| B12 SME fee | Add: no late interest; change SME Service Agreement Art. 5.4. |

Nothing is waiting on Loc. Next full Appendix M run: after Phase 2 (the scripts are already updated for the one-borrower rule).