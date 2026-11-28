# Runbook: SCRUM-649, Edward's production checks (3.3, 3.6, 3.8, 1.16)

Prepared 2026-11-26. This is the how-to for the four rows of `go-live-checklist.md` that are Edward's. Phat's rows (3.1, 3.2, 3.4, 3.5) are not here. Nothing in this runbook has been run: each step needs production, real people and real documents.

Tick each row in `go-live-checklist.md` with the date and initials when it passes, and paste the evidence line from the template at the end into the row's note. **Never put a real ID, tax code beyond the company's public one, account number beyond FundLok's own, or a receipt image into the repo or Jira.** Evidence lines carry ids and verdicts only.

## What you need before starting

| Need | Why | Who |
|---|---|---|
| A production SME account whose company has an `APPROVED` KYB, an approved application, and a USB/CA token with a valid digital certificate in the company's name (MST on the certificate) | 3.3 | A real SME, or a company of ours with a token |
| The Personal Guarantor with an approved eKYC and a verified phone for the Zalo signing code | Day 0 signing for the Standing Offer and Loan Contract template | The guarantor |
| A dedicated round with a controlled SME and three distinct eligible test investors, each with the Investor Service Agreement signed | 3.6 | Edward |
| Approval for the live transfers, loan servicing, and reconciliation plan in 3.6 | 3.6 | Edward |
| A phone with a bank app that scans VietQR | 3.8 | Edward |
| An admin login with a second factor, and `gcloud` access to read the Cloud Run log | all | Edward |
| Cloudflare R2 access for the production bucket (dashboard, or `aws s3 ls --endpoint-url <R2_ENDPOINT_URL>` with the read key) | 1.16 | Edward |

If no willing SME with a token exists yet, 3.3 is the long pole: ask for it first.

---

## 3.3 CA token and the NEAC check

**Pass when:** the admin panel shows NEAC valid and every other check passing for a real signed Standing Offer, and an altered copy fails.

1. Log in as the SME. On the approved application, open **Standing Offer**, download the PDF (the page footer prints the application reference and the document version; the check relies on both).
2. Sign it with the company’s USB/CA token. Use the Day 0 Zalo code for the guarantor after the SME uploads the Standing Offer and the Loan Contract template. A second certificate in the PDF is optional for the current flow.
3. Upload the signed PDF through the signed-copy upload. The upload runs the check on the way in.
4. As admin, open the project panel and the document. Expected, all passing: **right document**, **right numbers** (check code), **NEAC** (document unchanged, signature valid, certificate not revoked), **right signer** (MST equals the approved KYB enterprise code, name compared), **guarantor** (confirm the recorded Zalo signature separately from the PDF’s NEAC verdict).
5. Press "Kiểm tra lại với NEAC" (`POST /admin/documents/{document_id}/neac-check`). Expected: same verdict, a second audit row.
6. **Altered copy:** For the negative check, use a separate approved test application with no Standing Offer on file. Sign its own Standing Offer, keep the valid original, then alter a signed part of the copy while preserving the application reference and terms check code. Upload the altered copy first. Expect `SIGNING_SIGNATURE_INVALID`, then upload the valid original and expect acceptance. A document mismatch or an already-signed refusal does not prove this check.
7. Read the audit rows: `audit_log` entity `LOAN_APPLICATION_DOCUMENT`, action `NEAC_SIGNATURE_CHECKED`, newest first. The verdict is in `after_state`.

Failure notes:
- NEAC unreachable or slow: `NEAC_VERIFY_URL` is unset on `prod-api` (default `https://neac.gov.vn/vi/Home/VerifyFileByNeac`). Retry once; if the site is down, the row stays open and say so in the checklist, do not tick it.
- "Right signer" fails on a real token: compare the certificate's MST with the KYB enterprise code before suspecting the code.

Evidence to record: application id, document id, the verification results, the altered copy’s failing check, date.

## 3.6 Receipt OCR (and 1.16, R2 storage)

**Pass when:** three real receipts give matched (accepted), denied, and sent to staff, and all three files are in the R2 bucket.

Production already has `RECEIPT_OCR_ENABLED=1` and `RECEIPT_AUTO_VERDICT_ENABLED=1` (checklist 1.13, 1.14), and the image has Tesseract with `vie` and `eng`.

Use one dedicated round with three distinct eligible test investors and a verified bank account owned by the test SME. FundLok's fee account is used only for the fee invoice. The system folds all pledges by one investor in a round into one ticket, so three pledges from one investor cannot drive this test.

Before any production transfer, agree the test accounts, amounts, review and settlement owners, and cleanup with Finance and the production owner. The plan must cover all three transfers, any unpaid amounts or refunds, and any real contract, repayment schedule, fee invoice, or report that formation creates. If that plan is unavailable, run the verdict scenarios in staging and leave the production row open.

Each investor pledges once. The SME signs the Day 13 contracts and accepts the round. Each investor signs their Day 14 copy before using that ticket's QR. Confirm the schedule and staff availability before sending money, since automatic advance and close can form a loan from a confirmed ticket.

| # | Receipt | How to make it | Expected |
|---|---|---|---|
| a | Correct | Use investor A’s 10,000,000 VND ticket, transfer exactly 10,000,000 to the payee account in the QR with the QR's memo code, screenshot the bank's success page, upload | `ACCEPTED`, ticket confirmed |
| b | Wrong amount | Use investor B’s 10,000,000 VND ticket. Transfer the approved mismatch amount with B’s memo and upload the receipt | `DENIED` (`failure_codes` includes the amount mismatch), investor asked to upload the right one |
| c | Blurry | Use investor C’s ticket. Transfer the correct amount with C’s memo and upload an out-of-focus photo of the success page | `NEEDS_REVIEW`: waiting for staff in the admin funding review (Transactions tab) |

Read the result, for each receipt (confirm the exact screen names on the day; the admin funding screens are Phat's): investor `GET` on the ticket shows `transfer.system_check` (`verdict`, `confidence`, `failure_codes`, `review_reasons`, `receiver`), and staff see it in the admin funding review and the Transactions tab's activity log (`TRANSFER_SYSTEM_ACCEPTED`, `_DENIED`, `_NEEDS_REVIEW`).

After recording the verdicts, staff verify actual bank receipt and handle each ticket under the agreed plan. Review a receipt as received only when the bank evidence supports it. A ticket may already be ACTIVE after staff approval or automatic advance. Service any formed loan through its normal repayment flow, or use an early settlement only if Finance confirms that the process is available for that loan. For money that did not join a loan, use the approved reconciliation or tracked refund process and retain the audit trail. Finish the row only after reconciling all three transfers and recording the disposition of each ticket.

**1.16 proof (R2):** after the uploads, list the bucket. Expected: three objects under `evidence/RECEIPT/<first-two-hex-of-hash>/<file-id>`. Also open one receipt from the admin panel (this mints a download URL): it must display. If an object is missing or the URL fails, 1.16 is not done even though 3.6 passed. Contracts and signed documents (3.3) are the second proof: the signed PDF from step 3 of 3.3 must also download.

Evidence to record: three ticket ids, the three verdicts, the object keys (not the images), date.

## 3.8 Fee QR

**Pass when:** a bank app scan of a real SME fee invoice shows Techcombank 116889688, the right amount and the right reference.

1. Use a production SME fee invoice (the 3% activation fee). If none exists, coordinate with Finance to use an invoice from the approved test round after normal loan formation. The invoice response carries `vietqr_payload` (null when `FUNDLOK_FEE_BANK_BIN` and `FUNDLOK_FEE_ACCOUNT_NUMBER` are unset; they are set to 970407 and 116889688, checklist 1.2 and 1.3).
2. Open the invoice on the SME's screen (and the fee email's bank block) and scan the QR with a bank app. **Do not confirm the transfer.**
3. Check on the bank screen:
   - bank: Techcombank (BIN 970407)
   - account: 116889688, holder as in `FUNDLOK_FEE_ACCOUNT_NAME`
   - amount: fee plus VAT plus any late fee, as the invoice total
   - memo: the invoice's `reference_code`
4. Cross-check against the database/API: `fee_vnd + vat_vnd + late_fee_vnd` of that invoice equals the scanned amount.

If the amount differs, it is a bug in `invoice_vietqr` (`app/direct_transfer/fees.py`): do not tick; open an issue with the invoice id.

Evidence to record: invoice id, scanned bank name, scanned account (last 4 digits only), scanned amount, memo, date.

## Order of work (suggested)

1. Today: ask for the SME with a token and the guarantor (3.3 lead time).
2. Agree the production test and reconciliation plan, then set up the dedicated round and three investors for 3.6 and the R2 proof (1.16).
3. 3.8 after a fee invoice exists (it follows a formed loan; coordinate with the round used in 3.6 or an existing one).
4. 3.3 when the token holder is available.

## Evidence line template

```
3.x  YYYY-MM-DD  initials  <ids only>  <verdict(s)>  <pass | fail | blocked: reason>
```
