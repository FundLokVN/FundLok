# Direct-transfer disbursement and repayment design

Oct 27, 2026 · @Lok

## Summary

**Updated 4 Nov 2026.** Lok and thầy Huy replaced invest-any-time with a commit-then-accept flow. Investors commit and sign while the listing is open, the SME accepts or rejects the committed total, and only then does every investor pay, on one disbursement day. No money moves before the SME says yes, so refunds, unwanted partial loans and stub interest disappear. Launch on the happy path; dispute and exception handling (who has to prove what) is medium term.

- **Commit first, pay later.** The listing stays open for up to 14 days and closes as soon as commitments fill the target. Each verified investor commits at least 10M and signs the contract alone; no money moves yet. One listing per SME, and no new listing until the last loan is repaid in full.
- **The SME decides.** The SME sees the committed total and accepts or rejects it by the acceptance deadline, 1 day before the disbursement date. If it stays silent, 50% of the target or more counts as accept and less than 50% as reject; the SME can override either way before the deadline. A rejected listing ends and every investor is told.
- **One transfer day, checked both ways.** On acceptance FundLok sends each investor a VietQR. Investors pay on the disbursement day and upload a screenshot of the transfer; the SME uploads a screenshot of the money arriving. When both match, the tranche is Confirmed. Everyone starts on the same day, so there is no stub interest.
- **Repayment is checked one way ("the Binance way").** The SME pays each investor and uploads the transfer screenshot. The investor presses Done or Not received; no answer within 24 hours counts as done. Repayment stays manual for the MVP; bank standing orders or batch uploads cut the SME's 840 daily transfers on a 4-month loan.

Still open, none of it blocking launch:

- Disputes and exceptions: who has to prove what when screenshots do not match or an investor presses Not received. Medium term; staff review uploaded bank statements case by case until then.
- Consequences for an investor who commits and signs but does not pay on the disbursement day (Lok to design).
- A forged screenshot, which only bank data can rule out.
- An SME that does not pay FundLok's invoice, and the SME's legal duty to withhold 5% income tax on investors' interest.

**Current constraints (Lok, 28 Oct).** Two things are not available today, and the whole flow below is built without them:

- **No bank data access.** FundLok has no API into customers' bank accounts and no partner for it. Every confirmation and every dispute runs on documents the customer uploads (transfer receipts and bank statements), scanned and checked by staff. A bank feed is for later, once a licensed partner is in place.
- **No CIC connection.** CIC is planned for the future; its contract is signed only after SBV grants the certificate. Build CIC as a module that logs every reportable event from day one, so it can be switched on later. It must be connected before the first live disbursement under the certificate.

## Who does what (TL;DR)

Each person's next actions, legal and technical. Owners are proposals for Lok to confirm.

**Lok** (contracts, IBL, CIC)

- [ ] Design the consequences for an investor who commits but does not pay on the disbursement day. Align the dispute rules (who proves what) with Edward and Huy; medium term, not blocking launch. Draft the standing offer with IBL from the Appendix checklist, including the SME's acceptance step and the 50% default.
- [ ] Rewrite contract set v0.1 for direct transfers with IBL, adding the framework offer and drawdown (investor agreement and power of attorney due Oct 30, 2026 per the dossier).
- [ ] Send IBL the eight questions, including no power of attorney for the SME and no guarantee.
- [ ] Ask CIC whether a disputed real-time disbursement report can be corrected.
- [ ] Get quotes from a remote-signing provider. A bank-data partner (SePay or Casso) comes later.

**Huy** (finance, risk, disclosures)

- [ ] Write the pre-signing disclosures: FundLok is not a party, no guarantee, money moves directly, the SME's acceptance step and the 50% default, tax withheld (due Oct 28, 2026 per the dossier).
- [ ] Draft the SME's monthly tax-withholding report and check it with the accountant.
- [ ] Update the fee model: drop the 300,000 VND GPay line, add about 44,000 VND of e-signing per loan, and model the investor-fee collection risk.
- [ ] Set the interim review policy for mismatched screenshots, Not received reports and investor no-shows: who decides, and what evidence counts.

**Edward** (architecture, security)

- [ ] Lock the data model: Transfer as the core object, the new tranche states (Committed, QR sent, Paid, Confirmed, Active), the SME acceptance step with its deadline and 50% default, and a CIC event log that is stored now and sent once CIC is connected. Dispute rules come later (medium term).
- [ ] Design screenshot parsing for both sides with duplicate checks, plus a simple exceptions queue for staff; the full dispute flow comes later.
- [ ] Spec manual document scanning: receipt and bank-statement upload, field extraction, a staff review screen for exceptions and disputes, and the append-only evidence log. Bank-data integration waits for a partner.

**Phat** (build)

- [ ] Drop the scheduled co-signing flow from 26 Oct.
- [ ] Build disbursement: listing window, commit and sign with a cap check, the SME's accept or reject screen with the deadline and 50% default, QRs sent on the disbursement day, screenshot upload by both sides, and Confirmed tranches starting together.
- [ ] Build the repayment MVP: a "today's payments" screen with one VietQR per investor and a screenshot upload; Done or Not received for the investor with a 24-hour auto-done; standing-order set-up instructions per bank.

**Minh** (integrations, hosting)

- [ ] Integrate the remote-signing provider for the framework offer, drawdown and investor acceptance.
- [ ] Set up notifications (email plus SMS or Zalo) for commitments, the SME's acceptance deadline, the disbursement day, confirmations and reminders.
- [ ] Store contracts, receipts and evidence in Vietnam on the Viettel IDC setup.

## What is already decided

Every design below must keep money moving only between the investor's and the SME's own accounts. The rules settled in #decree-94 between 22 and 27 October, plus the product parameters in the Decree 94 dossier:

| Rule | What it means for the design | Source |
| --- | --- | --- |
| FundLok never holds money at any point in the flow | No collection account, no "fake escrow", no virtual account in FundLok's name. Holding funds risks a charge of illegal banking activity. | Lok, 22 Oct; Lok, 27 Oct |
| GPay is out | GPay has no pay-in/pay-out between two accounts that are not FundLok's. | Lok, 23 Oct |
| FBO bank account is out | Thầy Huy: "not worth the risk". Direct transfer between the two parties only. | Lok, 23 Oct |
| Decree 94, Art. 11.1.b | Disbursement and payment of principal, interest and fees go through the customers' own bank accounts or the customers' own e-wallets. | Dossier writing guide (verbatim text) |
| Full principal goes to the SME | The 3% activation fee is never deducted. The SME pays it separately against a VAT invoice. | Lok, 27 Oct; IBL letter, 28 Sep |
| Never charge money first | No subscription or prepaid fee to unlock investing or listing. | Lok, 23 Oct |
| Fees are collected afterwards | SME is invoiced after disbursement. Investor fee runs on a credit model: settle before reinvesting (like Grab's no-show fee). | Lok, 23 Oct |
| Repayments are manual for the MVP | Automation comes later; get loans and investors going first. | Lok, 27 Oct |
| Commit first, pay on one day | Investors commit and sign while the listing is open; nobody pays until the SME accepts. Then every investor pays on the disbursement day by QR. | Lok and thầy Huy, 4 Nov |
| The SME accepts or rejects | Deadline 1 day before the disbursement date. Silent: 50% of the target or more = accept, below 50% = reject. The SME can override either way before the deadline. A loan too small to be worth it is never forced on the SME. | Lok and thầy Huy, 4 Nov |
| Disbursement is checked both ways | The investor uploads the transfer screenshot and the SME uploads the receipt screenshot; both match = disbursement done. | Lok and thầy Huy, 4 Nov |
| Repayment is checked one way | The SME uploads the transfer screenshot; the investor presses Done or Not received, no screenshot needed; no answer within 24 hours = done. | Lok and thầy Huy, 4 Nov |
| Launch on the happy path | Dispute and exception handling (who has to prove what) is aligned later, then built in. | Lok, 4 Nov |
| 14-day window, one listing per SME | Commitments close when they fill the target or after 14 days; capped at the asked amount. No new listing until the previous loan is repaid in full. | Lok, 28 Oct |
| CIC follows the money | Report each disbursement and repayment when the money moves; correct by resubmission if wrong. | Lok, 28 Oct |
| Daily repayment is the product promise | Instalments fall on every working day. Weekly settlement is not offered. | Lok, 28 Oct |
| Contracts form by standing offer | The SME signs one public offer per listing; each investor's signed commitment is a separate loan contract that takes effect only if the SME accepts and the money arrives. See the Appendix. | Lok, 28 Oct and 4 Nov |
| Commission on Confirmed tranches only | BD and introducer commission is paid once a tranche is Confirmed, and does not change after that. | Lok, 28 Oct |
| No bank data, no CIC yet | All evidence runs on uploaded screenshots and statements checked by staff. CIC is planned; its events are logged now and sent once connected. | Lok, 28 Oct |

Product parameters from the dossier: at most 100M outstanding per borrower per platform; minimum ticket 10M with no cap on investors (the model assumes 10); one loan contract per investor; flat interest of about 16% a year; a 1–6 month term repaid by a fixed daily instalment on working days (21 a month). CIC needs a real-time report for each disbursement plus daily and monthly reports.

## The two approaches

**History (27 Oct).** This comparison chose Approach 2 over scheduled co-signing. On 4 Nov Approach 2 was itself replaced by the commit-then-accept flow in [Recommended disbursement design](#m2q6z0qjaek.22645): investors still sign alone, but nobody pays until the SME accepts, and everyone pays on one day.

Both approaches move money directly from the investor's bank to the SME's bank and rely on a receipt as evidence. They differ in one thing: whether investor and SME must be present at the same time.

**Approach 1: scheduled co-signing** ([Phat's flow, 26 Oct](https://fundlok.slack.com/archives/C0C8K179WF2/p1793008838700469))

1. Investor clicks Invest, picks an amount or number of tickets, and proposes time slots.
2. FundLok sends a meeting invitation to the investor and the SME owner.
3. The SME picks a slot; both get the calendar invite and a video link.
4. In the session, both review the terms and sign.
5. Once both have signed, FundLok shows the investor the SME's bank account.
6. The investor transfers and uploads the receipt.
7. An admin (or a bank API) verifies it, and the SME is asked to confirm receipt.

**Approach 2: invest any time, reconcile later** ([Lok, 27 Oct](https://fundlok.slack.com/archives/C0C8K179WF2/p1793034498908709))

1. Investor commits any amount at any time while the listing is open.
2. Investor signs electronically and sees the SME's bank account.
3. Investor transfers directly and uploads the receipt.
4. FundLok counts the tranche as transferred and emails the SME.
5. The SME can dispute that the money arrived.
6. Whatever is raised becomes the loan. The SME repays on that amount from about 2 weeks after the listing opens.

## Approach 1: scheduled co-signing

Scheduling buys same-moment confirmation and trust, but at a fixed human cost per investor that no software removes. Its real benefit, instant two-sided confirmation, can be had without a meeting (see the recommended design).

**Pros**

- **Instant confirmation.** The SME checks its balance during the session, so receipt is confirmed on the spot and disputes are rare.
- **Trust.** The investor meets the owner, which helps with the founder-funded pilot and the first outside investors.
- **A natural moment** for disclosures, a video identity check and questions.
- **Works today.** It needs a calendar and an e-signature tool, nothing else.

**Cons**

| Con | Fixable by automation | How, or why not |
| --- | --- | --- |
| No-show by investor or SME | Partly | Reminders, one-tap rebooking and a no-show fee on the investor's credit ledger reduce it. People still miss sessions; "we can never trust users with their time". |
| Days pass between "Invest" and money moving | Partly | Self-serve booking shortens the wait. Money still waits for the slower of two calendars, and interest cools meanwhile. |
| The SME sits through one session per investor | No | At the 10M minimum, a 100M loan means up to 10 sessions. Group sessions help, but the owner's time is the cost and it grows with every investor. |
| FundLok staff host or witness sessions | No | Human time per tranche caps volume at the team's calendar. |
| Partial funding still happens when investors drop out | No | Scheduling does not remove it; it only delays finding out. |
| Verification is still a receipt checked by an admin | Partly | Receipt matching helps, as in Approach 2. Only a bank data feed makes it bank-proof. |
| Investor signs, then does not pay | Yes | The tranche lapses automatically if no receipt arrives within a set time. |
| Each investor starts on a different day, so schedules stagger | Yes | A common repayment start date fixes it, but then the meeting adds nothing. |

The meeting carries no legal weight: Vietnamese law accepts an e-contract signed by each party at a different time (see the signing section).

## Approach 2: invest any time, reconcile later

Approach 2 wins: 10 of its 12 cons are fully or partly solved by software. The two that are not, partial funding and refunds after cancellation, are product and contract questions rather than build questions.

**Pros**

- **Seamless for investors**, Lok's priority: reserve, sign and pay in minutes, any time. NAPAS interbank transfers run around the clock.
- **No no-shows.** Nobody waits on anyone.
- **Scales.** The SME's work per investor is a one-tap confirmation, or nothing if it stays silent.
- **Fits partial funding**, like GoFundMe's keep-what-you-raise model.
- **Almost free.** No payment-intermediary fee; GPay would have charged 3,300 VND per transfer.
- **Clean legal story.** Money only ever moves between customers' own accounts (Art. 11.1.b).

**Cons**

| Con | Fixable by automation | How, or why not |
| --- | --- | --- |
| Forged or edited receipts | Partly | Match amount, reference, time and names against the QR FundLok issued; the SME's confirm-or-dispute catches the rest. Only bank data would be proof, and FundLok has no bank access today, so a tranche rests on documents: "attested", then confirmed by the SME or by its silence. |
| SME never confirms | Yes | Reminders, then auto-confirm when the dispute window lapses. |
| SME falsely claims the money never arrived | Partly | Both sides upload bank statements and staff review them. If that does not settle it, the investor asks their bank to trace the transfer (tra soát) and the bank's answer decides. Judging a dispute stays manual. |
| SME ends up with less than it needs | No | The nature of partial funding. Show live progress, let the SME close early or extend once, and use the minimum ticket. |
| Tranches arrive on different days | Yes | One common repayment start date. The system computes stub interest for money that arrived early. |
| Two investors pay at once and overshoot the target or the 100M cap | Yes | Reserve a ticket before showing the QR; reservations expire. Check the borrower's total before reserving: FundLok's own records and the SME's declaration now, CIC once connected. |
| Investor reserves, then does not pay | Yes | The reservation lapses. No money moved, so no harm; repeat offenders get a cooldown. |
| Wrong amount, wrong account or missing reference | Partly | A dynamic VietQR pre-fills account, amount and reference. A typed transfer that misses still needs the SME to refund the difference. |
| Payment from someone else's account (AML) | Partly | The payer name on the receipt must match the investor's KYC name, or the tranche is blocked. Returning the money needs the SME. |
| Loan cancelled after money moved (KYB fails, fraud, SME withdraws) | No | FundLok cannot pull money back; only the SME can refund each investor. Needs a contract clause, a deadline and a tracked refund. |
| CIC report timing while a tranche awaits confirmation | Yes | A fixed rule, coded once: report at attestation and correct if disputed, or report at confirmation. |
| The fee depends on the confirmed total | Yes | Auto-issue the SME's e-invoice when the window closes. |

## Gaps the chat hasn't covered yet

The biggest gap is repayment, not disbursement: in a direct-transfer model the SME pays every investor separately, every working day. The other gaps are rules that need writing down before Phat builds.

Under the 4 Nov flow the gaps about partial funding, refunds and stub interest close, because no money moves before the SME accepts and everyone pays on the same day. The repayment gaps still apply.

| Gap | Why it matters | Proposed handling |
| --- | --- | --- |
| The SME pays each investor separately, daily | 10 investors × 84 working days (4 months) = 840 manual transfers per loan. That is the likeliest reason an SME misses a day. | Standing orders or batch upload (see Repayment). |
| The SME must withhold 5% income tax on interest | Paying directly makes the SME the payer of the investor's income. Loan interest stays taxed at 5% under the 2025 PIT Law, in force from 1 July 2026. On a 10M ticket over 4 months that is 26,667 VND, about 317 VND a day. | FundLok computes each day's principal, interest and tax per investor and gives the SME a monthly withholding report. IBL item 6 is still open. |
| When does interest start for each tranche? | Money arrives on different days during the window. | Common start date plus stub interest (see Recommended design). |
| The 100M cap with tranches arriving in parallel | The cap is per borrower per platform, and 400M across all sandbox platforms via CIC. Until CIC is connected, rely on FundLok's records and the SME's signed declaration. | Check the cap when a ticket is reserved, before the QR is shown. |
| CIC timing | Planned, not connected yet. Once live, CIC wants a real-time report per disbursement plus daily balances. "Attested" is not yet "confirmed", and a repayment counts as paid unless reported. | Follow the money: report each movement when it happens and correct by resubmission if it turns out wrong (Lok, 28 Oct). |
| Payment from someone else's account | An investor may pay from a relative's account, which breaks KYC and AML. | Payer name must match the KYC name, or the tranche is blocked. |
| Refunds and cancellations | FundLok cannot reverse a transfer. | Contract clause: the SME refunds within a fixed number of working days, tracked like any other transfer. |
| Weekends and holidays | NAPAS runs every day, but instalments fall only on working days. A daily bank order might also run at weekends. | Pick an order frequency that skips non-working days, or settle weekly. |
| The SME's own bank fees | Some banks charge business accounts per transfer. [VIB](https://www.vib.com.vn/vn/bieu-phi-va-dieu-kien-tai-khoan-kh-to-chuc), for example, charges 10,000 VND per fast transfer to another bank: 840 daily payments would cost the SME 8.4M VND on a 100M loan. | Tell SMEs at onboarding which banks transfer free, and show the cost before they sign. Where fees apply, weekly settlement cuts them about fivefold. |
| Security of a bank feed | [SePay](https://developer.sepay.vn/en/sepay-webhooks) links an account by internet-banking login or OAuth, depending on the bank. | Not in scope now. When a partner is in place: OAuth or official-API banks only; never handle an SME's banking password. |
| Collecting the investor fee | Under the credit model, an investor who never reinvests never pays. That is about 22% of revenue per loan (833,333 of 3,833,333 VND). | Monthly invoice with a pay link; block new reservations while unpaid. |
| Evidence for disputes and the D94 audit | Every confirmation rests on a receipt and a timestamp. | Keep the receipt file, its hash, the parsed fields and the SME's response in an append-only log. |

## Signing: what the law allows

Nothing in Vietnamese law requires investor and SME to sign together or at the same moment. The most seamless route is a standing offer: the SME signs once per listing and each investor accepts alone.

- **Civil Code 2015, Art. 400–401.** A contract is concluded when the offeror receives the acceptance; a written contract, when the last party signs. It takes effect at conclusion unless the parties agree otherwise, so a contract can take effect on receipt of funds. ([FastCA summary](https://fastca.vn/thoi-diem-giao-ket-hop-dong-dien-tu))
- **Law on Electronic Transactions 2023, Art. 34–35** (in force 1 July 2024). Offer and acceptance may be made by data message, and contracts formed through automated systems are valid. ([FastCA summary](https://fastca.vn/thoi-diem-giao-ket-hop-dong-dien-tu))
- **Decree 23/2025/NĐ-CP** (in force 10 April 2025). Remote signing (ký số từ xa) is a recognised public digital signature model, so an investor can sign on a phone without a USB token. ([Art. 26](https://hethongphapluat.com/nghi-dinh-23-2025-nd-cp-quy-dinh-ve-chu-ky-dien-tu-va-dich-vu-tin-cay/dieu-26))
- **IBL memo, 16 Sep.** E-signatures are enforceable for our contracts.

| Option | How it works | Signatures for a 10-investor loan | What the SME does per investor | Caveat |
| --- | --- | --- | --- | --- |
| Standing offer (chosen) | The SME signs one standard offer per listing: rate, term, schedule formula, maximum amount, valid for the funding window. Each investor signs a one-page acceptance. The contract is concluded when the acceptance reaches the SME through the platform, and takes effect when funds are confirmed. | 11 | Nothing | Chosen by Lok, 28 Oct. IBL confirms the wording for several separate contracts (Art. 391.1). Legal basis and checklist in the Appendix. |
| Daily batch countersign | Investors sign any time; the SME countersigns all pending contracts once a day. The QR appears only after the countersign. | 10 investor, plus one SME batch per day | One tap a day | Money waits up to a day; the contract is concluded at the SME's signature. |
| Signing session (Approach 1) | Both sign during a scheduled meeting. | 20 | Attends a meeting | No legal advantage over the two above. |

Which signature: investors use a remote digital signature from a licensed provider for the strongest evidence. A platform signature with eKYC and OTP is cheaper but weaker in a dispute. SMEs use their enterprise digital signature, which most companies already use for e-tax and e-invoices. At the dossier's budget of about 4,000 VND per signing request, the standing offer costs about 44,000 VND per loan.

## Contract structure

FundLok signs two service agreements and nothing else. Every loan is a separate contract between one investor and the SME, formed online without anyone signing on anyone else's behalf. This is the legal frame the build must follow; IBL still has to confirm points 1, 3 and 4 below.

&#91;embedded content: contract map · 3 parties, 3 relationships\]

The accent line is the only loan relationship. FundLok's two lines are service contracts paid by fees; no money for the loan passes along them.

**The documents**

| Document | Who signs | When | What it fixes | Must never contain |
| --- | --- | --- | --- | --- |
| Platform service agreement | SME and FundLok | SME onboarding | Listing service; the 3% activation fee on a separate VAT invoice; a narrow mandate to publish listings, issue QR codes, compute schedules, send notices and collect documents | Authority to borrow, sign or receive money for the SME; netting the fee from principal |
| Framework offer | The SME, to any verified investor | SME onboarding; valid 12 months (proposed) | All loan terms: rate formula within 20% a year, 1–6 month term, fixed daily instalment, extension formula and 1.33× backstop, holiday pause, late interest, tax withholding, the acceptance step and 50% default, disputes. Ceiling of 100M outstanding | Any obligation or guarantee from FundLok |
| Drawdown request | The SME | Each listing | Amount within the remaining 100M, window dates and the rate for this listing, signed with the SME's digital signature | Terms outside the framework offer |
| Loan contract | One investor and the SME | When the investor commits; takes effect once the SME accepts and funds are confirmed | Amount, disbursement date, start date, the system-generated schedule, both registered bank accounts | FundLok as a party |
| Investor service agreement and limited power of attorney | Investor and FundLok | Investor onboarding | The 2.5% a year fee, billed monthly; a listed servicing mandate (reminders, notices, records); risk disclosures | Discretion to invest or sign for the investor; holding money; any guarantee |
| Data consents | SME and investor, separately | Onboarding | Personal-data consents, CIC lookup, and later the bank feed | Bundling consent into the loan |

**Six legal rules everyone works to**

1. **FundLok is never a lender, the SME's agent for borrowing, or a guarantor.** It signs only the two service agreements.
2. **One investor, one contract.** Each investor is lender of record under its own loan contract. IBL's memo says this is what keeps a multi-investor loan outside securities rules.
3. **No power of attorney to borrow or sign for the SME.** FundLok already acts for investors; acting for the SME as well risks the Civil Code Art. 141.3 bar on representing both sides. IBL cleared only a limited, listed servicing mandate.
4. **No guarantee of repayment or of funding.** A guarantee makes FundLok a party to the loan, and the Law on Credit Institutions 2024 treats a bank guarantee as credit extended by licensed institutions. Topping up a shortfall would make FundLok a lender.
5. **The contract forms online, not in a meeting.** It is concluded when the investor's acceptance reaches the SME through the platform (Civil Code Art. 400.1) and takes effect only once the SME has accepted and the funds are confirmed (Art. 401).
6. **The SME's legal representative signs.** KYB checks the SME's charter for any borrowing limit or required owner approval.

**What changes in contract set v0.1** (the 14 Oct draft assumed GPay)

- Remove the SME's irrevocable authorisation to GPay and every virtual-account clause.
- Add direct payment to each investor's registered account, two-way screenshot evidence on disbursement, and Done or Not received with a 24-hour auto-confirm on repayment.
- Add the SME's acceptance step with its deadline and 50% default, the single disbursement day, the investor's duty to pay on that day, and the common start date.
- Add the SME's duty to withhold 5% income tax on interest, using FundLok's monthly figures.
- Bill the investor fee monthly instead of deducting it; FundLok never touches the interest.
- Add plain-Vietnamese pre-signing disclosures: FundLok is not a party, and nothing is guaranteed.

## Recommended disbursement design

Build the commit-then-accept flow (Lok and thầy Huy, 4 Nov). Investors commit and sign alone while the listing is open, the SME accepts or rejects the total, and then every investor pays on one disbursement day. FundLok only sends the QRs and matches the screenshots.

&#91;embedded content: disbursement flow · commit, SME accepts, one transfer day\]

The accent line is the only point where money moves, straight from the investor's bank to the SME's; every other arrow is information.

**Rules**

1. **Listing.** The SME signs the standing offer and the listing opens for up to 14 days, with a target of the amount the SME asks for, at most 100M. One listing per SME at a time, and no new listing until the previous loan is repaid in full.
2. **Commit and sign.** A verified investor commits at least 10M and never more than the capacity left, and signs the contract with a remote digital signature on the spot. FundLok checks the remaining capacity and the borrower's total (its own records and the SME's declaration now; CIC once connected). No money moves at this stage.
3. **Commitments close.** As soon as commitments fill the target, or at the end of day 14, whichever comes first. The SME then sees the committed total.
4. **SME acceptance.** The SME accepts or rejects by the acceptance deadline, 1 day before the disbursement date. If it does not answer, 50% of the target or more counts as accept and less than 50% as reject. The SME can still reject above 50%, or accept a smaller loan below 50%, before the deadline. The rule is printed in the standing offer, and the SME gets reminders showing the committed total before the deadline.
5. **Rejected.** The listing ends, every investor is told and the commitments lapse. No money has moved, so there is nothing to refund and no fee.
6. **Disbursement day.** On acceptance FundLok sends each investor a dynamic VietQR with the SME's account, the exact committed amount and a unique reference. Everyone pays on the same day. The payer account must be in the investor's own name. Money sent above a committed amount is not part of the loan and not FundLok's liability.
7. **Two-way check.** The investor uploads a screenshot of the transfer; the SME uploads a screenshot of the money arriving. FundLok matches amount, time, reference and names; when both match, the tranche is Confirmed. Anything else goes to an exceptions queue for staff.
8. **Investor no-show.** An investor who committed and signed but does not pay on the day breaks a signed contract. The consequences, and whether the loan goes ahead on what did arrive, are for Lok to design.
9. **Start and fee.** Confirmed tranches start repaying together on the next working day, so there is no stub interest. FundLok issues the SME's e-invoice for 3% of the confirmed total.
10. **Disputes.** Launch on the happy path. Who has to prove what in a mismatch is medium term: until then staff review uploaded bank statements case by case, and a bank trace (tra soát) settles a conflict.

## Repayment

Keep repayment manual for the MVP, but make it one action a day instead of ten. Start with a daily payment screen and move each SME to bank standing orders as soon as its bank allows. A read-only bank feed would remove reporting altogether, but it needs a partner, so it is not in scope now.

Worked example: a 100M loan at 16% flat over 4 months (84 working days) costs the SME 1,253,968 VND a day. An investor with 10M receives 125,397 VND a day before tax: 119,048 principal and 6,349 interest. The SME withholds 317 VND of tax, so the investor nets 125,080.

| Option | SME actions per loan (10 investors) | Automation | When | Main catch |
| --- | --- | --- | --- | --- |
| 1. A VietQR per investor, paid by hand | 840 transfers | FundLok only generates the QRs | Now | Missed or partial days are likely; business accounts may pay per transfer. |
| 2. Daily batch upload | 84 uploads | FundLok generates the file in the bank's batch template | Now, where the bank offers batch transfers (e.g. [VCB DigiBiz](https://vnba.org.vn/vi/vcb-digibiz-cap-nhat-them-nhieu-tinh-nang-moi-danh-cho-doanh-nghiep-sme-7589.htm)) | Still daily; one template per bank; batch fees vary. |
| 3. Standing order per investor | 10 set-ups, then none | The bank executes; FundLok sends set-up instructions | Now, where the bank offers daily orders ([Techcombank](https://techcombank.com/khach-hang-doanh-nghiep/quan-tri-hang-ngay/tien-gui-va-dich-vu-quan-ly-tai-khoan/dich-vu-chuyen-tien-dinh-ky): daily or monthly, free to register) | Fixed amount: every extension changes the daily and each order must be edited. It must also skip weekends and holidays. |
| 4. Weekly settlement | 170 transfers or 17 uploads | The ledger still accrues daily | Lok's call | Investors stop seeing money daily, and the SME holds a week of repayments. |
| 5. Read-only bank feed on the SME's account | Confirms payments; does not make them | Auto-matches every repayment and disbursement by reference code | Later, with a partner | Not available today: needs a licensed partner and SME consent. |
| 6. Payment initiation by a partner | None | A bank or e-wallet pushes or pulls on the SME's mandate | Later | [Circular 64/2024](https://thuvienphapluat.vn/hoi-dap-phap-luat/se-trien-khai-open-api-trong-nganh-ngan-hang-tu-ngay-132025-138043253.html) opens payment-initiation APIs only to banks and licensed payment intermediaries, so FundLok needs a partner. Decree 94 also allows customers' own e-wallets. |

Order of rollout: option 1 on a single "today's payments" screen, with option 3 offered at onboarding wherever the SME's bank supports it, and option 2 for banks without standing orders. Options 5 and 6 wait for a partner. Option 4 is off: daily repayment is the product promise.

Verification is one-way, "the Binance way" (Lok and thầy Huy, 4 Nov): the SME pays each investor and uploads the transfer screenshot; the investor presses Done or Not received, with no screenshot needed. No answer within 24 hours counts as done. Not received goes to staff review on uploaded bank statements; the rules on who proves what come later.

## Collecting FundLok's fees

Both fees can be collected without touching customer money, because each is paid separately into FundLok's own account as FundLok's revenue. Automation can bill and chase; it cannot make anyone pay.

| Fee | On a 100M loan with 10 investors | Billed | Paid | If unpaid |
| --- | --- | --- | --- | --- |
| SME activation fee, 3% of confirmed principal | 3,000,000 VND plus VAT | E-invoice issued automatically once the tranches are Confirmed | SME pays by VietQR, separately from any repayment | Next listing blocked and a late fee under the service agreement. It can never be netted from principal or repayments. |
| Investor service fee, 2.5% a year, fixed at signing | 833,333 VND per loan, 83,333 per 10M ticket | Accrues monthly, pro rata to what the investor has actually received | Monthly invoice with a pay link (credit model) | New commitments blocked until settled. Investors who never come back never pay: about 22% of revenue per loan. |

The payment-intermediary line disappears: GPay would have charged 3,300 VND per transfer, and the dossier budgets 300,000 VND per loan for it. There is no new running cost today; a bank feed would add a subscription later.

## Build spec for Phat and Edward

The core object is Edward's confirmed party-to-party transfer: from, to, amount, reference, rail, evidence and a confirmation level. Everything else hangs off a tranche, one per investor contract, whose states are below.

&#91;embedded content: tranche states · 8 states, 8 transitions\]

Only Confirmed tranches join the loan, and only after the SME has accepted. Today every confirmation rests on uploaded screenshots checked by staff; there is no bank data.

**Objects**

| Object | Key fields |
| --- | --- |
| Listing | SME, target (the asked amount, at most 100M), committed total, rate, term, window end, acceptance deadline, disbursement date, SME decision (accepted, rejected, auto-accepted, auto-rejected), status. One listing per SME; no new listing until the previous loan is repaid in full. |
| Standing offer | Listing, signed file and hash, signature certificate, valid until |
| Tranche | Investor, committed amount, state, reference code, committed / QR sent / paid / confirmed times |
| Transfer | From account, to account, amount, reference, rail (QR, standing order, batch upload), evidence (screenshot or statement), confirmation level, linked tranche or repayment day |
| Repayment day | Tranche, date, principal, interest, tax withheld, net amount, status (due, paid with screenshot, done by investor, auto-done after 24 hours, not received) |
| Dispute | Subject, raised by, each side's evidence, outcome, decided by (rules medium term) |
| Fee ledger | SME invoices; investor fees accrued, invoiced and paid |

**Confirmation levels**, lowest to highest: Reported (one side's screenshot uploaded) → Confirmed (on disbursement, both screenshots match; on repayment, the investor pressed Done or 24 hours passed). A higher level, Verified (bank-data match), is reserved for when a partner is in place.

**Timers**

| Timer | Value | When it fires |
| --- | --- | --- |
| Commitment window | Up to 14 days; closes early when commitments fill the target | SME sees the total; acceptance clock starts |
| SME acceptance deadline | 1 day before the disbursement date | Silent: 50% of the target or more auto-accepts, below 50% auto-rejects |
| Disbursement day | The day after the acceptance deadline (placeholder) | QRs sent; investors pay and upload; unpaid commitments flagged as no-shows |
| Repayment confirmation | 24 hours after the SME uploads its screenshot | Day auto-marked done |
| SME fee invoice | Due in 7 days (proposed) | New listings blocked, late fee |
| Investor fee invoice | Monthly | New commitments blocked until paid |

**Screenshot checks**

- Read amount, date and time, payer name, payer account, payee account, memo and the bank's transaction ID. Reject a screenshot missing any of them.
- Amount must be exact, the memo must carry the reference, the payee must be the SME's registered account, and the payer name must match the investor's KYC name.
- On disbursement, the SME's receipt screenshot must show the same amount, reference and transaction ID as the investor's.
- Reject a bank transaction ID already used, so one screenshot cannot count twice.
- Reference code: "FL" plus 8 upper-case letters and digits, no diacritics. Check the shortest memo length among the target banks before fixing the format.

**CIC events (planned: log now, send once CIC is connected)**

- Confirmed (both screenshots match): real-time disbursement report.
- Void: correction.
- Daily: outstanding balance per tranche, counting days marked done.
- Not received, once staff decide: correction by resubmission.

## Decisions and points to align

Status after Lok's calls on 28 Oct and the 4 Nov flow agreed with thầy Huy. Medium term items wait until after launch. **Decided** items are ready to build. **Placeholder** items can be built but will change. **To align** items need a conversation before Phat builds that part. Four of these answer [Edward's questions of 24 Oct](https://fundlok.slack.com/archives/C0C8K179WF2/p1792777256292479?thread_ts=1792680760.287329&cid=C0C8K179WF2).

| Item | Status | Rule |
| --- | --- | --- |
| Disbursement flow | Decided | Commit, then the SME accepts, then one transfer day (Lok and thầy Huy, 4 Nov). Replaces Approach 2; scheduled co-signing stays dropped. |
| Contract formation | Decided | Standing offer: the SME signs one public offer per listing, and each investor's signed commitment forms a separate loan contract. The offer states the SME's acceptance step and the 50% default. Legal basis and drafting checklist in the Appendix; IBL confirms the wording. |
| Commitment window | Decided | Up to 14 days; closes as soon as commitments fill the target. Capped at the asked amount (at most 100M). One listing per SME; no new listing until the previous loan is repaid in full. |
| SME acceptance | Decided | Accept or reject by 1 day before the disbursement date. Silent: 50% of the target or more = accept, below 50% = reject. The SME can override either way before the deadline. |
| Disbursement date | Placeholder | The day after the acceptance deadline. |
| Disbursement check | Decided | Two-way: the investor's transfer screenshot plus the SME's receipt screenshot. |
| Investor no-show | To design: Lok | An investor who commits and signs but does not pay on the day. Consequences, and whether the loan goes ahead on what arrived, to be designed. |
| Repayment check (Edward Q1) | Decided | One-way: the SME uploads the transfer screenshot; the investor presses Done or Not received; no answer within 24 hours = done. |
| Interest on early money | Decided | None needed: every tranche moves on the same day and starts repaying together on the next working day. |
| Disputes and exceptions (Edward Q3) | Medium term | Launch on the happy path. Who has to prove what is aligned later (Lok, Edward, Huy), then built in. Until then staff review uploaded bank statements case by case; a bank trace settles a conflict. |
| CIC reporting | Decided | Follow the money: report each disbursement and repayment when the money moves; correct by resubmission if it turns out wrong. Planned: no CIC connection yet, so log the events now and send them once connected. |
| Unpaid fees (Edward Q4) | Decided | Both sides run on credit. The SME is invoiced after disbursement (its money arrives first, so it should pay soonest); unpaid means no new listing and a late fee. The investor fee accrues monthly; unpaid means no new commitments. |
| Minimum ticket | Decided | 10M minimum ticket, so up to 10 investors per 100M loan (Lok, 30 Oct). |
| Repayment frequency | Decided | Daily on working days, as the product promise. |
| Overfunding | Decided | The loan is capped at the asked amount, at most 100M, and investors cannot commit beyond it. Money sent above a committed amount is not a loan and not FundLok's liability. |
| BD and introducer commission | Decided | Paid only on Confirmed tranches. Final once paid. |
| Late disputes and CIC corrections | Medium term | Decide together with the dispute rules. |
| Evidence | Decided | Manual document scanning: screenshots and bank statements uploaded by customers, checked by staff. No bank API or bank-data partner for now. |

Edward's Q2, what happens if the loan does not fill, is answered by the SME's acceptance step: no money moves until the SME accepts, and silence follows the 50% rule.

**For IBL**

- [ ] Does the SME's standing offer, accepted by each investor's signed commitment, form a separate valid loan contract with each investor that takes effect only if the SME accepts and the money arrives? Should the acceptance step be drafted as a condition for the contract to take effect (Civil Code Art. 120) or as an agreed right to terminate (Art. 428)? Does the wording in the Appendix stop Art. 391.1 from ending the offer at the first acceptance?
- [ ] Can the SME's silence count as acceptance at 50% of the target or more, given the rule is agreed in the signed offer (Art. 393.2)?
- [ ] What remedies can the contract give against an investor who signs a commitment but does not pay on the disbursement day?
- [ ] Is a two-way screenshot check on disbursement, and a one-way check with a 24-hour auto-confirm on repayment, traceable enough for Art. 11.1.b and the dossier?
- [ ] How should the SME withhold and declare 5% income tax on daily interest (open item 6)?
- [ ] For later, when a bank-data partner is in place: what consent wording covers a read-only feed of the SME's bank transactions, and does it raise any "holding" concern?
- [ ] Confirm FundLok should hold no power of attorney to borrow or sign for the SME, and what the SME service mandate may cover.
- [ ] Confirm FundLok may not guarantee repayment or funding, and whether a licensed guarantor or credit insurer could.

**For CIC**

- [ ] Can a disbursement reported in real time be corrected if it is later disputed, and within what period?

**For banks and vendors**

- [ ] SME banks: daily standing orders that skip weekends and holidays; batch-upload templates and fees for business accounts.
- [ ] Later: SePay or Casso for business-account coverage including outgoing transfers, OAuth or login, price, licence and data-processing terms.
- [ ] MoMo or ZaloPay: transfers between customers' own wallets, recurring payments, holds, and fees.
- [ ] Remote-signing providers: price per signature, batch signing, and API.

## Appendix: the standing offer (open contract)

Use this when drafting the contract set with IBL. The SME's listing is a public offer to sign loan contracts (đề nghị giao kết hợp đồng tới công chúng). Each investor's signed commitment forms a separate loan contract between that investor and the SME, which takes effect only once the SME has accepted the committed total and the money has arrived. FundLok runs the platform and issues the QR, but is never a party, lender or guarantor.

### Legal basis

| Article | Text | What it means for the offer |
| --- | --- | --- |
| Civil Code 2015, Art. 386.1 | "Đề nghị giao kết hợp đồng là việc thể hiện rõ ý định giao kết hợp đồng và chịu sự ràng buộc về đề nghị này của bên đề nghị đối với bên đã được xác định hoặc tới công chúng (sau đây gọi chung là bên được đề nghị)." | An offer may be made to the public, so one listing can be addressed to every verified investor. |
| Civil Code 2015, Art. 386.2 | "Trường hợp đề nghị giao kết hợp đồng có nêu rõ thời hạn trả lời, nếu bên đề nghị lại giao kết hợp đồng với người thứ ba trong thời hạn chờ bên được đề nghị trả lời thì phải bồi thường thiệt hại cho bên được đề nghị mà không được giao kết hợp đồng nếu có thiệt hại phát sinh." | An investor turned away because the listing filled could claim damages, unless the offer says capacity is first come, first served and the offer ends when full. |
| Civil Code 2015, Art. 388.1 | "a) Do bên đề nghị ấn định; b) Nếu bên đề nghị không ấn định thì đề nghị giao kết hợp đồng có hiệu lực kể từ khi bên được đề nghị nhận được đề nghị đó, trừ trường hợp luật liên quan có quy định khác." | The offer states its own start: the moment the listing is published. |
| Civil Code 2015, Art. 390 | "Bên đề nghị giao kết hợp đồng có thể hủy bỏ đề nghị nếu đã nêu rõ quyền này trong đề nghị và bên được đề nghị nhận được thông báo về việc hủy bỏ đề nghị trước khi người này gửi thông báo chấp nhận đề nghị giao kết hợp đồng." | The SME can withdraw a listing only if the offer reserves that right. |
| Civil Code 2015, Art. 391 | "Đề nghị giao kết hợp đồng chấm dứt trong trường hợp sau đây: 1. Bên được đề nghị chấp nhận giao kết hợp đồng; … 3. Hết thời hạn trả lời chấp nhận; … 5. Khi thông báo về việc hủy bỏ đề nghị có hiệu lực; …" | Read literally, the first acceptance ends the offer. The offer must say it is an offer to sign a separate contract with each accepting investor, up to the target. IBL to confirm. |
| Civil Code 2015, Art. 394.1 | "Khi bên đề nghị có ấn định thời hạn trả lời thì việc trả lời chấp nhận chỉ có hiệu lực khi được thực hiện trong thời hạn đó; nếu bên đề nghị giao kết hợp đồng nhận được trả lời khi đã hết thời hạn trả lời thì chấp nhận này được coi là đề nghị mới của bên chậm trả lời." | An acceptance after the window closes forms no contract, so money sent late is not a loan. |
| Civil Code 2015, Art. 400.1 and 400.4 | "1. Hợp đồng được giao kết vào thời điểm bên đề nghị nhận được chấp nhận giao kết." "4. Thời điểm giao kết hợp đồng bằng văn bản là thời điểm bên sau cùng ký vào văn bản hay bằng hình thức chấp nhận khác được thể hiện trên văn bản." | The contract is concluded when the investor's signed acceptance reaches the SME through the platform. The investor signs last. |
| Civil Code 2015, Art. 401.1 | "Hợp đồng được giao kết hợp pháp có hiệu lực từ thời điểm giao kết, trừ trường hợp có thỏa thuận khác hoặc luật liên quan có quy định khác." | The offer can make each contract take effect when the investor's funds are confirmed. |
| Civil Code 2015, Art. 406 | "1. Điều kiện giao dịch chung là những điều khoản ổn định do một bên công bố để áp dụng chung cho bên được đề nghị giao kết hợp đồng; nếu bên được đề nghị chấp nhận giao kết hợp đồng thì coi như chấp nhận các điều khoản này. 2. Điều kiện giao dịch chung chỉ có hiệu lực với bên xác lập giao dịch trong trường hợp điều kiện giao dịch này đã được công khai để bên xác lập giao dịch biết hoặc phải biết về điều kiện đó. … 3. … Trường hợp điều kiện giao dịch chung có quy định về miễn trách nhiệm của bên đưa ra điều kiện giao dịch chung, tăng trách nhiệm hoặc loại bỏ quyền lợi chính đáng của bên kia thì quy định này không có hiệu lực, trừ trường hợp có thỏa thuận khác." | Standard terms bind the investor only if shown before acceptance; one-sided exemption clauses are void. |
| Law on Electronic Transactions 2023, Art. 34.1 | "Hợp đồng điện tử được giao kết hoặc thực hiện từ sự tương tác giữa một hệ thống thông tin tự động với người hoặc giữa các hệ thống thông tin tự động với nhau không bị phủ nhận giá trị pháp lý chỉ vì không có sự kiểm tra hay can thiệp của con người vào từng hành động cụ thể do các hệ thống thông tin tự động thực hiện hay vào hợp đồng." | A contract formed through the platform's automated flow is valid. |
| Law on Electronic Transactions 2023, Art. 35.2 | "Đề nghị giao kết và chấp nhận giao kết hợp đồng điện tử được thực hiện thông qua thông điệp dữ liệu, trừ trường hợp các bên có thỏa thuận khác." | Offer and acceptance can both be made as data messages on the platform. |

### What the offer must say

1. **Parties.** The SME offers, to verified FundLok investors, to sign a separate loan contract with each investor who accepts. FundLok is not a party.
2. **Separate contracts.** Each acceptance forms its own contract. One investor's acceptance does not end the offer for the others until the target is reached (Art. 391.1).
3. **Amount.** The target is the amount the SME asks for, at most 100M. Minimum ticket 10M. Capacity is first come, first served, and no investor can commit beyond the capacity left (Art. 386.2).
4. **Window.** Valid from publication for up to 14 days (Art. 388.1); ends early as soon as commitments fill the target (Art. 391). Acceptances after that form no contract (Art. 394.1).
5. **SME acceptance.** The SME accepts or rejects the committed total by the acceptance deadline, 1 day before the disbursement date. If it does not answer, the loan proceeds at 50% of the target or more and is cancelled below 50%; silence has this effect only because the offer says so in advance (Art. 393.2). On rejection no contract takes effect and no fee is due.
6. **Effect and start.** Each contract takes effect when the SME has accepted and the investor's funds are confirmed (Art. 401.1). All investors pay on the disbursement day; daily instalments start on the next working day, so there is no stub interest.
7. **Loan terms.** Flat rate within the 20% cap, term, the daily instalment formula on working days, late interest and acceleration as cleared by IBL, and the SME's 5% income tax withholding.
8. **Payment.** Only from the investor's own account to the SME's registered account, by the platform QR with its reference, on the disbursement day. Money above the committed amount, or sent without a commitment, is not a loan and not FundLok's liability; the SME returns it.
9. **Investor's duty to pay.** An investor who signed a commitment must pay on the disbursement day once the SME accepts. Remedies for a no-show to be set with IBL once Lok has designed them.
10. **Withdrawal right.** The SME may withdraw the offer before acceptance in stated cases, such as failed KYB or suspected fraud (Art. 390). After commitments close, the acceptance step in item 5 applies.
11. **Disputes and evidence.** Two-way screenshots on disbursement; one-way on repayment, with Done or Not received and a 24-hour auto-confirm. Who has to prove what in a dispute is still to align (medium term).
12. **Disclosure.** Show the full terms before an investor accepts, with no one-sided exemption clauses (Art. 406), plus the Decree 94 pre-signing disclosures: FundLok is not a party, gives no guarantee, and money moves directly.
13. **Signing.** The SME signs the offer once per listing with its enterprise digital signature. Each investor signs the acceptance with a remote digital signature (Decree 23/2025). The contract is concluded when the acceptance reaches the SME through the platform (Art. 400.1 and 400.4; Law on Electronic Transactions 2023, Art. 34–35).

### Still for IBL

- Confirm the "separate contract with each accepting investor" wording overcomes Art. 391.1.
- Confirm how to draft the SME's acceptance step and the 50% silence rule (Art. 120 or Art. 428; Art. 393.2).
- Check the loan contract template and pre-signing disclosure requirements in Decree 94, Annex III, §3.2 and §8.2.
- Check whether the investor terms fall under Decision 07/2024/QĐ-TTg, the list of standard-form contracts that must be registered.

## Sources

**#decree-94 (Slack)**

- [Lok, 22 Oct: final decision on holding money and GPay](https://fundlok.slack.com/archives/C0C8K179WF2/p1792652153998349)
- [Lok, 22 Oct: the three payment approaches](https://fundlok.slack.com/archives/C0C8K179WF2/p1792680595457149)
- [Edward, 23 Oct: option 3, direct transfer plus detection](https://fundlok.slack.com/archives/C0C8K179WF2/p1792704214789829?thread_ts=1792680760.287329&cid=C0C8K179WF2)
- [Lok, 23 Oct: thầy Huy rules out the FBO account; fee approach](https://fundlok.slack.com/archives/C0C8K179WF2/p1792736510308449?thread_ts=1792680760.287329&cid=C0C8K179WF2)
- [Edward, 24 Oct: four questions for Lok](https://fundlok.slack.com/archives/C0C8K179WF2/p1792777256292479?thread_ts=1792680760.287329&cid=C0C8K179WF2)
- [Phat, 26 Oct: scheduled co-signing flow](https://fundlok.slack.com/archives/C0C8K179WF2/p1793008838700469)
- [Lok, 27 Oct: partial funding and invest any time](https://fundlok.slack.com/archives/C0C8K179WF2/p1793034498908709)
- [Lok, 27 Oct: receipt-based disbursement](https://fundlok.slack.com/archives/C0C8K179WF2/p1793071984128769?thread_ts=1793034498.908709&cid=C0C8K179WF2)

Lok, 4 Nov 2026 (this chat): commit-then-accept flow, SME acceptance deadline and 50% default, two-way and one-way checks, 24-hour auto-done, agreed with thầy Huy.

**Internal documents (FundLok general folder)**

- Dossier/FundLok\_Decree94\_Dossier\_Writing\_Guide.md: product parameters, Art. 11.1.b text, fee and cost lines
- FundLok\_IBL\_Memo\_Clarification\_28Sep2026.docx: Route B, fee structure and open questions to IBL

**Web pages**

- [FastCA: when an electronic contract is concluded](https://fastca.vn/thoi-diem-giao-ket-hop-dong-dien-tu) (Civil Code Art. 399–401; Law on Electronic Transactions 2023, Art. 15–18 and 34–35)
- [Luật Dương Gia: Civil Code Art. 141 on representing both sides](https://luatduonggia.vn/phap-luat/nguoi-dai-dien-giao-dich-voi-chinh-minh-co-duoc-khong/)
- [Decree 23/2025/NĐ-CP, Art. 26 (hethongphapluat.com)](https://hethongphapluat.com/nghi-dinh-23-2025-nd-cp-quy-dinh-ve-chu-ky-dien-tu-va-dich-vu-tin-cay/dieu-26)
- [Thư Viện Pháp Luật: income tax on loan interest from 1 July 2026](https://thuvienphapluat.vn/ma-so-thue/phap-luat-thue/tu-ngay-172026-tien-lai-cho-vay-cua-ca-nhan-co-con-phai-chiu-thue-tncn-khong-220552.html)
- [Thư Viện Pháp Luật: Open API under Circular 64/2024](https://thuvienphapluat.vn/hoi-dap-phap-luat/se-trien-khai-open-api-trong-nganh-ngan-hang-tu-ngay-132025-138043253.html)
- [Techcombank: periodic transfer service for businesses](https://techcombank.com/khach-hang-doanh-nghiep/quan-tri-hang-ngay/tien-gui-va-dich-vu-quan-ly-tai-khoan/dich-vu-chuyen-tien-dinh-ky)
- [VNBA: VCB DigiBiz batch transfers](https://vnba.org.vn/vi/vcb-digibiz-cap-nhat-them-nhieu-tinh-nang-moi-danh-cho-doanh-nghiep-sme-7589.htm)
- [VIB: fee schedule for business accounts](https://www.vib.com.vn/vn/bieu-phi-va-dieu-kien-tai-khoan-kh-to-chuc)
- [SePay: webhooks overview](https://developer.sepay.vn/en/sepay-webhooks)
- [Civil Code 2015, Art. 386, 388, 390, 391, 394, 400, 401, 406 (hethongphapluat.com)](https://hethongphapluat.com/bo-luat-dan-su-2015/dieu-386)
- [Law on Electronic Transactions 2023, Art. 34–35 (hethongphapluat.com)](https://hethongphapluat.com/luat-giao-dich-dien-tu-2023/dieu-34)
- [Decision 07/2024/QĐ-TTg: standard-form contracts that must be registered (caselaw.vn)](https://caselaw.vn/van-ban-phap-luat/509624-quyet-dinh-so-07-2024-qd-ttg-ngay-20-06-2024-cua-thu-tuong-chinh-phu-ve-danh-muc-san-pham-hang-hoa-dich-vu-phai-dang-ky-hop-dong-theo-mau-dieu-kien-giao-dich-chung)
