> **Filed by Edward, 2026-11-15**, from Loc's document "Appendix M Remediation Decisions" (decided by Loc, 15 Nov 2026). Author and owner: Loc. The text below is unchanged. The engineering record of these decisions is D-036 to D-057 in `docs/outstanding-issues.md`; the work plan is `docs/handoffs/HANDOFF-06-appendix-m-remediation.md`.

Decision log · Appendix M remediation · all decisions final

# Appendix M remediation decisions

Decided by Loc, 15 Nov 2026Answers Edward's Q1–Q9 (14 Nov plan) and the follow-upsLoc owns the handbook and updates it to match

## What changes from the handbook and the 14 Nov plan

- **Loan contracts are signed on disbursement day.** A commitment on Days 0–12 is a signed pledge, and a lender may commit more than once to the same project. On Day 14 each lender signs one loan contract for its total committed amount, then pays.
- **Extension and re-amortisation ship at launch, approved automatically by rule.** When the SME files, the system applies the 11.2 test; there is no staff approval step.
- **Late repayment interest ships at launch:** 150% of the loan rate on overdue principal (never above 30% a year), paid to each investor. FundLok's 2.5% lender fee is unchanged. Default, backstop, recovery and fraud stay after launch.
- **No late interest on FundLok's SME activation fee.** An unpaid fee only blocks the next listing after 7 days.
- **Lenders earn the full graded rate.** The 2.5% is FundLok's yearly lender fee, invoiced separately; remove the code's cap that limits lenders to 2.5% over the whole term.
- **Calendar days for every deadline and window, and public holidays ignored (technical debt).** Daily repayments stay Monday to Friday, 21 a month.
- **One account per business, with limits per business.** Each legal entity is underwritten separately and gets its own 100M (Phat's one-borrower rule, merged 14 Nov).

## Decisions

| # | Topic | Decision | vs handbook | Owner |
| --- | --- | --- | --- | --- |
| Funding round and disbursement |  |  |  |  |
| A1 | Launch flow | Commit on Days 0–12 (no money moves); the SME accepts or declines by 15:00 on Day 13; everyone signs and transfers on Day 14. One shared schedule: same rate, start date, end date and payment day for every lender. | as handbook | Edward, Phat |
| A1b | When contracts are signed | All loan contracts are signed on disbursement day. Extra commitments from the same lender fold into that lender's one contract on Day 14. The Lender Service Agreement is still signed at a lender's first commitment. | changes 10.4, App. M 3.1 | Edward, Phat |
| A2 | Day 13 / 14 on weekends | Both stay on their calendar day. | as handbook | Edward |
| A3 | Below 50% | Silence at 50% or more means accept; below 50% means auto-decline (no loan, no money, no fee, every lender told). The SME may accept manually below 50% before the deadline. | as handbook | Edward, Phat |
| A4 | Partly funded round | Revenue share s stays as set; the SME pays the same interest rate whether or not the round fills. | closes App. G | Edward |
| A5 | Disputes | SME on a disbursement: 24 hours. Lender "Not received": 24 hours. Calendar time. 30-day window for anything counted as received by silence, with a bank statement; staff decide under Finance's interim policy, then the bank's trace. The window is shown next to each auto-confirmation. | as handbook | Edward, Phat |
| A6 | Commitments and no-shows | Minimum 10M per commitment (or exactly the remainder), up to capacity; **several commitments per lender per project allowed**. A lender who has not signed and paid by 15:00 on Day 14 is locked and that loan never takes effect. Unlock: redo eKYC and type the exact acknowledgement sentence (text and time logged). Second no-show in the lender's lifetime = permanent closure. No lock with proof of force majeure or a bank outage (staff decide). | changes 10.1, 10.4 | Edward, Phat |
| A7 | Lender signing method | OTP plus a logged click-to-sign on Day 14. A provider for individual digital signatures comes later. | launch design | Edward, Phat |
| Repayment and money |  |  |  |  |
| B8 | Lender rate | Lenders earn the project's full graded rate (capped at 20%). FundLok's lender fee F = P × 2.5% × M/12 + VAT, invoiced monthly on money actually received. Remove `investor_return_bps`; the offer total and the repayment plan then agree. | as handbook | Edward, Phat |
| B10 | Unpaid days and late interest | A day with no SME proof by the end of its due date is **Late**, shown to staff, the SME and the lender, never "paid", and not counted for lender fees. **Late interest at launch**: 150% of the loan rate on overdue principal, never above 30% a year, plus 10% a year on overdue interest (handbook 11.4), counted in calendar days, paid by the SME to each lender. FundLok's 2.5% fee is unchanged. A Late instalment blocks extension filings. No Default, backstop or recovery flags at launch. Remove `presume_paid_days` for unproven days. | moves 11.4 into launch | Edward, Phat |
| B11 | Rounding | Contract Art. 7.2: interest part = A × I / T rounded down; tax to the nearest đồng; the last instalment takes the remainder. | closes App. G | Edward |
| B12 | SME activation fee | 3% of confirmed principal + VAT, due in 7 calendar days, never netted. Unpaid after 7 days: next listing blocked. **No late interest** (remove it from SME Service Agreement Art. 5.4). Paid to FundLok account **116889688, Techcombank** (BIN 970407): set `FUNDLOK_FEE_*` and show the fee VietQR on the invoice and email. | changes 4.2, 10.9 | Edward, Phat |
| B13 | Unpaid lender fee | After 7 days the lender cannot commit to new loans; nothing else is locked. No late fee. | changes 4.2, 10.9 | Edward, Phat |
| C16 | Extension | **At launch, automatic.** The SME files by the 5th with that month's tax declaration or bank inflow statement and its revenue. If revenue is below what the loan expects (shortfall U = instalments due − s × revenue > 0) and no instalment is Late, the system extends by rule: extra days rounded up to a multiple of 5, capped at the backstop room; new schedule version saved, old one kept; the daily amount never rises; each lender's fee rises by P × 2.5% × extra days / 252; SME, guarantor and lenders notified; no extension fee. | changes 18.1, App. M 9.1 | Edward, Phat |
| C23 | Days and holidays | Every deadline, window and cut-off in calendar days. Daily repayments Monday to Friday (21 a month, 252 a year). Public holidays ignored: a normal repayment day, no deadline moves. Holiday handling logged as technical debt. | changes 10.1; tech debt | Edward |
| Borrowers, offer, documents |  |  |  |  |
| B9 | Borrower limits | Per legal entity. **One account per business** (Phat's rule, merged 14 Nov): each business registers its own account, gets its own 100M and its own "one listing until repaid in full". A business that has repaid in full may borrow again (handbook 10.1, App. M 7.7); today it cannot, because it can only ever have one project. | as handbook | Edward, Phat |
| B14 | Wording | T&S Art. 3.2 "FundLok không thu hồi nợ…"; new Art. 4.4 (lawful claims only; threats, harassment or hired collectors close the account); /why-us "Doanh nghiệp trả trực tiếp cho từng nhà đầu tư mỗi ngày làm việc"; /terms "phục vụ giải ngân và nhận tiền trả nợ trực tiếp"; Art. 7 "30 days". | as handbook | Phat |
| B15 | Offer fields | DRR (on 21 days), revenue share s and guarantor come from the grading engine; Edward fills in what it doesn't produce yet (s, guarantor). | as handbook | Edward, Phat |
| C17 | Documents | Keep the code's five uploads (charter, registration, e-invoices, tax filings, CIC); the engine reads tax filings, e-invoices, CIC, KYB and the form. | changes 6.3 | Loc |
| C18 | Return for correction | Add a "returned to SME" application state. Technical debt. | tech debt | Edward, Phat |
| C22 | Loan duration | 1–6 months. | closes App. G | — |
| C24 | Grading gates | Gate 3 and the CIC gate stay on; SMEs still provide their CIC report. | closes App. G | Edward |
| Accounts and operations |  |  |  |  |
| C19 | Misconduct | A permanent CLOSED status with a reason, set by staff. | as handbook | Edward, Phat |
| C20 | Suspended | Means no new offers or investments only; repaying, confirming and logging in still work. | as handbook | Edward |
| C21 | After launch | T&S change notice and self-service closure; fraud flags (M11); Default, backstop and recovery (rest of M10). | as handbook | all |
| D1 | Baseline | Yes to Edward's proposal: write a baseline overview of what FundLok is, what we're building and where it's going, once testing is locked down. | — | Edward |

Nothing is waiting on a decision. Loc is updating the handbook to match (see the v3.2 change list).