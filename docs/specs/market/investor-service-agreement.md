# Spec: Investor Service Agreement (Contract 4): digitally signed PDF with the first commitment

| Field | Value |
|---|---|
| **Status** | DRAFT (built ahead of review; needs Edward's review of the migration and the model registry line) |
| **Owner** | Phat |
| **Implementer(s)** | Phat (backend `app/market/`, frontend) |
| **Module** | `app/market/investor_agreement/`, `app/market/funding.py`, `app/market/funding_router.py` |
| **Version** | 1.3 (1.1: the master agreement is a digitally signed PDF, as for the SME; 1.2: each commitment's Annex is kept as a filled-in PDF and shown before paying; 1.3: commit first, so the agreement's Annex is filled in with the commitment's code and values, then sign, then pay) |
| **Date** | 2026-11-08 |
| **Related ADR** | ADR-009 |
| **Depends on** | `docs/specs/direct_transfer/funding-window.md`, `docs/specs/direct_transfer/end-to-end-payment-flow.md` ("Contract structure"), `docs/specs/direct_transfer/fees.md` (investor fee formula), the SME Service Agreement step (`app/loans/agreement/`), Contract set v0.3 (5 Nov 2026, draft pending IBL review) |

---

## 1. Context & Goal

Contract set v0.3 makes the investor sign Contract 4, the "Hợp đồng dịch vụ dành cho Nhà đầu tư / Investor Service Agreement", with FundLok. Per the contracts table it is the *"Master at the first commitment; a short confirmation on each later commitment"*, signed *"with a remote digital signature"*; *"Later commitments sign only an Investment Confirmation (Annex)."*

Phat chose to sign it the way an SME signs its service agreement (Contract 2), with every blank filled in, the Annex included. So the first commitment is made first: agreeing to the terms holds the amount and gives the commitment its code, and no money moves. The platform then fills the agreement PDF in with the investor's verified details and that commitment's Annex (code, listing, SME, amount, rate and term, fee, expected disbursement date). The investor downloads it, signs it with their digital signature (remote signing or USB token) and uploads the signed copy. Only then does the platform give the payment details.

**Goal:** no money moves on a first commitment until the investor has uploaded a digitally signed copy of the current Investor Service Agreement, filled in with that commitment's Annex and recorded server-side (version, signing time, commitment, file). Every later commitment keeps its own filled-in Investment Confirmation (the Annex) when the investor agrees, and is never asked for the master agreement again.

## 2. Out of Scope

- Validating the signature. The check is presence-only (`app/loans/agreement/signature.py`, a `/ByteRange` signature dictionary), exactly as for the SME. Certificate chain, signer identity and "is this the right document" are for staff review.
- An admin screen for the signed copies.
- A digital signature on each commitment's Annex. It is filled in as a PDF and confirmed in-app (covered by the commitment's MockSigner-signed acceptance), not signed with the investor's own digital signature (Phat's choice, 9 Nov).
- The Loan Contract (Contract 5) as its own document.
- The ID card address for individuals. GVerify's OCR does not return one, so it prints as a dotted blank (§9).

## 3. Data Model

### New table: `investor_service_agreements` (migration `918db72f448e`, revises `f736e64d867c`)

```
investor_service_agreements
  id               UUID         PK
  investor_id      UUID         NOT NULL  FK users.id ON DELETE RESTRICT
  version          TEXT         NOT NULL  text.VERSION of the PDF signed, e.g. "investor-agreement-v0.3"
  signed_at        TIMESTAMPTZ  NOT NULL  when the signed copy was uploaded and accepted
  tranche_id       UUID         NULL      FK tranches.id ON DELETE RESTRICT: the commitment it was signed with,
                                          whose Annex it carries (null only outside the flow, e.g. fixtures)
  party_b          JSONB        NOT NULL  {holder_type, name, id_number, address, bank_account_number,
                                           bank_account_name, bank_name}, any value nullable
  signed_file_key  TEXT         NOT NULL  evidence storage key (evidence_files, purpose SIGNED_DOCUMENT)
  document_sha256  TEXT         NOT NULL  SHA-256 of the signed PDF as uploaded
  file_name        TEXT         NULL
  file_size_bytes  BIGINT       NOT NULL
  ip_address       TEXT         NULL
  user_agent       TEXT         NULL
  created_at       TIMESTAMPTZ  NOT NULL  DEFAULT now()
  UNIQUE (investor_id, version)
```

Append-only, one per investor per version. A new version is a new row, and the old row is kept.

### New table: `investment_confirmations` (migration `ee32119154bd`, revises `918db72f448e`)

```
investment_confirmations
  id                          UUID         PK
  tranche_id                  UUID         NOT NULL UNIQUE  FK tranches.id ON DELETE RESTRICT
  investor_id                 UUID         NOT NULL  FK users.id ON DELETE RESTRICT, indexed
  agreement_id                UUID         NOT NULL  FK investor_service_agreements.id ON DELETE RESTRICT
  listing_id                  UUID         NOT NULL  FK listings.id ON DELETE RESTRICT
  sme_name                    TEXT         NOT NULL
  amount_vnd                  BIGINT       NOT NULL
  annual_rate_pct             NUMERIC(6,3) NOT NULL
  term_months                 INTEGER      NOT NULL
  service_fee_vnd             BIGINT       NOT NULL  P × 2.5% × M / 12, before VAT
  service_fee_bps_per_year    INTEGER      NOT NULL
  expected_disbursement_date  DATE         NOT NULL
  file_key                    TEXT         NOT NULL  the filled-in PDF in evidence storage (SIGNED_DOCUMENT)
  document_sha256             TEXT         NOT NULL
  confirmed_at                TIMESTAMPTZ  NOT NULL
  created_at                  TIMESTAMPTZ  NOT NULL  DEFAULT now()
```

Insert-only, one per commitment: the Annex of Contract 4 Art. 7. For a later commitment it is filled in when the investor agrees; for the first one, when the agreement signed with it is uploaded. Either way, before any payment details exist.

### Signed acceptance (existing document, new fields)

Each commitment's `FUNDLOK_INVESTOR_ACCEPTANCE` JSON gains:

```
investor_service_agreement: {id, version, signed_at, document_sha256}
                            # first commitment: {version, signed_with_this_commitment_next: true}
investment_confirmation:    {id, listing_id, sme_name, committed_amount_vnd, annual_rate_pct, term_months,
                             expected_service_fee_vnd, service_fee_bps_per_year, expected_disbursement_date,
                             document_sha256}   # the investment_confirmations row and its PDF's hash
                            # first commitment: the values only (its record comes with the signed agreement)
```

## 4. API Contract (all INVESTOR only; 403 for any other role)

| Method and path | Purpose |
|---|---|
| `GET /funding/me/investor-agreement` | `InvestorAgreementOut {version, required, signed_at, signed_version, file_name, tranche_id, party_b}` |
| `GET /funding/me/investor-agreement/document[?tranche_id=…][&download=true]` | The current version as a PDF, filled in with the investor's details and, with `tranche_id` (their own commitment, else 404), that commitment's code and Annex; without it, the Annex is the blank template. `inline` or `attachment`, `no-store`. Generated each time, never stored. File name `FundLok-hop-dong-dich-vu-nha-dau-tu-{REF}[-{CODE}]-{VERSION}.pdf`. |
| `POST /funding/me/investor-agreement` (multipart `file`, `version`, `tranche_id`) | 201 with `InvestorAgreementOut`. Needs the eligible-investor gate (KYC and approved bank account) and the investor's own live commitment. Keeps that commitment's Annex and opens its payment details. Audited as `INVESTOR_AGREEMENT_SIGNED`. |
| `GET /funding/me/investor-agreement/signed[?download=true]` | The signed copy of the current version, byte for byte. 404 when none. |
| `GET /funding/windows/{id}/investment-confirmation?amount_vnd=N` | `InvestmentConfirmationOut`: the Annex for a commitment, as it will be signed (shown on the terms step). |
| `GET /funding/tranches/{id}/investment-confirmation` | `SignedInvestmentConfirmationOut {id, tranche_id, reference_code, listing_id, listing_ref, sme_name, amount_vnd, annual_rate_pct, term_months, service_fee_vnd, service_fee_bps_per_year, expected_disbursement_date, confirmed_at, agreement_version}`, for the investor's own ticket only (404 otherwise, or for tickets from before 1.2). |
| `GET /funding/tranches/{id}/investment-confirmation/document[?download=true]` | The filled-in Annex PDF kept for that ticket, exactly as stored. |
| `POST /funding/windows/{id}/reservations` | Unchanged. A first commitment is made without the agreement (code, held amount, no money moved). |
| `GET /funding/tranches/{id}/payment` | 409 `INVESTOR_AGREEMENT_REQUIRED` until the investor has a signed copy of the current version. Receipts are never refused for it. |

## 5. State Machine

None new. The agreement is either required (no row for the current version) or signed. A first commitment goes RESERVED → SIGNED as before (the acceptance), then waits for the agreement before its payment details are given.

## 6. Business Rules

1. **Text and version.** The text lives in `app/market/investor_agreement/text.py`, verbatim from Contract set v0.3. `VERSION` is printed on every page. Bump it whenever a word changes, and every investor signs the new text before their next commitment. The frontend has no copy of the text; it sends back the version the status gave it.
2. **PDF.** The same renderer as the SME agreement (fpdf2, embedded Be Vietnam Pro). Party B comes from the approved payout account (holder type and bank details) and the verified profile. `INDIVIDUAL`: eKYC full name and ID number, plus the OCR address if any. `ENTITY`: KYB business name, enterprise code and address. The fallback name is the account's full name. Unknown values print as dotted blanks. Generated for a commitment, the Annex shows "Mã cam kết / Commitment: {code}" and all six rows filled in, with a note that later commitments are confirmed on the platform. Without one, it is the blank template.
3. **Commit first.** `reserve_and_accept` makes a first commitment without the agreement: the amount is held (2-hour hold), the commitment gets its reference code, and the acceptance (investing terms plus the Annex values) is signed in-app. No money moves.
4. **Upload checks**, in order: size at most 10 MB; the commitment is the investor's own (404); `version` equals `VERSION`; the file is a PDF; it carries a digital signature; no signed copy of this version is on file (409 `INVESTOR_AGREEMENT_ALREADY_SIGNED`); the commitment's hold has not lapsed (409 `HOLD_EXPIRED`). It is stored as evidence, a row is written with the commitment and Party B, and that commitment's Annex is kept (rule 6).
5. **Payment gate.** `GET /funding/tranches/{id}/payment` is refused (409 `INVESTOR_AGREEMENT_REQUIRED`) while the investor has no signed copy of `VERSION`. Receipt upload is not gated, so money already sent still reaches staff.
6. **Annex per commitment.** Before the payment details exist (for a later commitment when the investor agrees; for the first, when the agreement signed with it is uploaded), the server fills in the Annex (`pdf.render_annex`: commitment reference, the agreement version and signing date, both parties, the six rows, Article 7, the confirmation time in Vietnam time), stores the PDF as evidence and writes an `investment_confirmations` row. The commitment's signed acceptance names the row and the PDF's hash. A replayed reservation reuses the row. The transfer step shows it above the QR, with View and Download.
7. **One signature per version.** There is no replacement: a wrong file is for staff review.

## 7. Error Cases

| Case | Status | `X-Error-Code` |
|---|---|---|
| Payment details while no signed copy of the current version is on file | 409 | `INVESTOR_AGREEMENT_REQUIRED` |
| Upload for a commitment that is not the investor's | 404 | — |
| Upload after the commitment's hold lapsed | 409 | `HOLD_EXPIRED` |
| Upload when this version is already signed | 409 | `INVESTOR_AGREEMENT_ALREADY_SIGNED` |
| Upload names another version | 409 | `INVESTOR_AGREEMENT_OUTDATED` |
| Upload is not a PDF | 415 | `UNSUPPORTED_TYPE` |
| PDF with no digital signature (e.g. FundLok's own download sent straight back) | 422 | `INVESTOR_AGREEMENT_UNSIGNED` |
| Upload over 10 MB | 413 | `FILE_TOO_LARGE` |
| Upload without approved KYC or bank account | 403 | — |
| Signed copy requested with none on file | 404 | — |

## 8. Acceptance Criteria

Tests: `tests/market/test_investor_service_agreement.py`.

- [x] `test_a_first_commitment_gets_its_code_but_no_payment_details_until_signed`
- [x] `test_the_agreement_pdf_is_filled_in_with_the_investor_and_the_commitments_annex`
- [x] `test_uploading_the_signed_agreement_keeps_the_first_annex_and_opens_payment`
- [x] `test_the_unsigned_agreement_sent_straight_back_is_refused`
- [x] `test_only_a_pdf_of_the_current_version_is_taken`
- [x] `test_the_agreement_is_signed_with_the_investors_own_live_commitment`
- [x] `test_the_agreement_is_signed_once_per_version`
- [x] `test_an_investor_without_kyc_or_bank_account_cannot_upload`
- [x] `test_a_later_commitment_keeps_its_annex_at_once_and_pays_straight_away`
- [x] `test_replaying_a_commitment_keeps_one_investment_confirmation`
- [x] `test_a_new_version_is_signed_with_the_next_commitment`
- [x] `test_the_status_asks_for_the_agreement_until_signed_then_shows_what_was_signed`
- [x] `test_an_organisation_investor_is_filled_from_its_business_verification`
- [x] `test_the_investment_confirmation_preview_is_filled_in_from_the_window`
- [x] `test_only_the_investor_who_committed_sees_its_documents`

Frontend (`app/(app)/dashboard/invest/`): a first-time investor gets 1 Số tiền → 2 Điều khoản (with a note on what comes next) → 3 Hợp đồng dịch vụ (the commitment code and hold deadline, the PDF preview with this commitment's Annex filled in, download, how to sign, and the upload of the signed copy) → 4 Chuyển khoản. Later commitments get 1 Số tiền → 2 Điều khoản (reminder of the signed agreement, "Xem hợp đồng đã ký") → 3 Chuyển khoản. The transfer step always starts with the "Xác nhận đầu tư (Phụ lục) đã ký" card (View, Download PDF) above the QR. `make_eligible_investor` in conftest includes a signed agreement (`signed_agreement=False` to opt out).

## 9. Open Questions

| # | Question | Owner |
|---|---|---|
| 1 | GVerify's eKYC OCR returns no address, so an individual's Party B address is blank. Ask the investor for it, or get it from GVerify? | Phat / Edward |
| 2 | Validate signatures (pyHanko: certificate chain, signer name against the eKYC name) instead of the presence check? | Edward |
| 3 | Staff review of signed copies: an admin view, and what happens to a copy found to be wrong after a commitment. | Phat |
| 4 | "Expected Disbursement Date" in the Annex is the hold's last ICT day (pay within the 2-hour hold). It changes with commit-then-accept. | Edward |
| 5 | Do individual investors realistically hold a digital signature? If not, the platform's remote-signing vendor (funding-window.md rule 1) is needed before launch. | Lok / Edward |
