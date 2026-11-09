# Spec: Investor Service Agreement (Contract 4): digitally signed PDF before the first commitment

| Field | Value |
|---|---|
| **Status** | DRAFT (built ahead of review; needs Edward's review of the migration and the model registry line) |
| **Owner** | Phat |
| **Implementer(s)** | Phat (backend `app/market/`, frontend) |
| **Module** | `app/market/investor_agreement/`, `app/market/funding.py`, `app/market/funding_router.py` |
| **Version** | 1.2 (1.1: the master agreement is a digitally signed PDF, as for the SME; 1.2: each commitment's Annex is kept as a filled-in PDF and shown before paying) |
| **Date** | 2026-11-08 |
| **Related ADR** | ADR-009 |
| **Depends on** | `docs/specs/direct_transfer/funding-window.md`, `docs/specs/direct_transfer/end-to-end-payment-flow.md` ("Contract structure"), `docs/specs/direct_transfer/fees.md` (investor fee formula), the SME Service Agreement step (`app/loans/agreement/`), Contract set v0.3 (5 Nov 2026, draft pending IBL review) |

---

## 1. Context & Goal

Contract set v0.3 makes the investor sign Contract 4, the "Hợp đồng dịch vụ dành cho Nhà đầu tư / Investor Service Agreement", with FundLok. Per the contracts table it is the *"Master at the first commitment; a short confirmation on each later commitment"*, signed *"with a remote digital signature"*; *"Later commitments sign only an Investment Confirmation (Annex)."*

Phat chose to sign it the way an SME signs its service agreement (Contract 2). The platform fills the PDF in with the investor's verified details. The investor downloads it, signs it with their digital signature (remote signing or USB token) and uploads the signed copy. Reserving refuses an investor without one.

**Goal:** no first commitment goes through until the investor has uploaded a digitally signed copy of the current Investor Service Agreement, recorded server-side (version, signing time, the file). Every commitment signs its own Investment Confirmation (the Annex) in-platform, and is never asked for the master agreement again.

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
  party_b          JSONB        NOT NULL  {holder_type, name, id_number, address, bank_account_number,
                                           bank_account_name, bank_name}, any value nullable
  signed_file_key  TEXT         NOT NULL  evidence storage key (evidence_files, purpose SIGNED_DOCUMENT)
  document_sha256  TEXT         NOT NULL  SHA-256 of the signed PDF as uploaded
  file_name        TEXT         NULL
  file_size_bytes  BIGINT       NOT NULL
  ip_address       TEXT         NULL
  user_agent       TEXT         NULL
  created_at       TIMESTAMPTZ  NOT NULL  DEFAULT now()
  INDEX (investor_id, version, signed_at)
```

Append-only. The investor's latest row for a version is the one that counts, and earlier rows are kept.

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

Insert-only, one per commitment: the Annex of Contract 4 Art. 7, filled in when the investor agrees, before any payment details exist.

### Signed acceptance (existing document, new fields)

Each commitment's `FUNDLOK_INVESTOR_ACCEPTANCE` JSON gains:

```
investor_service_agreement: {id, version, signed_at, document_sha256}
investment_confirmation:    {id, listing_id, sme_name, committed_amount_vnd, annual_rate_pct, term_months,
                             expected_service_fee_vnd, service_fee_bps_per_year, expected_disbursement_date,
                             document_sha256}   # the investment_confirmations row and its PDF's hash
```

## 4. API Contract (all INVESTOR only; 403 for any other role)

| Method and path | Purpose |
|---|---|
| `GET /funding/me/investor-agreement` | `InvestorAgreementOut {version, required, signed_at, signed_version, file_name, replaceable, party_b}` |
| `GET /funding/me/investor-agreement/document[?download=true]` | The current version as a PDF, filled in with the investor's details. `inline` or `attachment`, `no-store`. Generated each time, never stored. File name `FundLok-hop-dong-dich-vu-nha-dau-tu-{REF}-{VERSION}.pdf`. |
| `POST /funding/me/investor-agreement` (multipart `file`, `version`) | 201 with `InvestorAgreementOut`. Needs the eligible-investor gate (KYC and approved bank account). Audited as `INVESTOR_AGREEMENT_SIGNED`. |
| `GET /funding/me/investor-agreement/signed[?download=true]` | The signed copy of the current version, byte for byte. 404 when none. |
| `GET /funding/windows/{id}/investment-confirmation?amount_vnd=N` | `InvestmentConfirmationOut`: the Annex for a commitment, as it will be signed (shown on the terms step). |
| `GET /funding/tranches/{id}/investment-confirmation` | `SignedInvestmentConfirmationOut {id, tranche_id, reference_code, listing_id, listing_ref, sme_name, amount_vnd, annual_rate_pct, term_months, service_fee_vnd, service_fee_bps_per_year, expected_disbursement_date, confirmed_at, agreement_version}`, for the investor's own ticket only (404 otherwise, or for tickets from before 1.2). |
| `GET /funding/tranches/{id}/investment-confirmation/document[?download=true]` | The filled-in Annex PDF kept for that ticket, exactly as stored. |
| `POST /funding/windows/{id}/reservations` | Unchanged body. 409 `INVESTOR_AGREEMENT_REQUIRED` without a signed copy of the current version. |

## 5. State Machine

None new. The agreement is either required (no row for the current version) or signed. A signed copy is `replaceable` until a commitment is made after it.

## 6. Business Rules

1. **Text and version.** The text lives in `app/market/investor_agreement/text.py`, verbatim from Contract set v0.3. `VERSION` is printed on every page. Bump it whenever a word changes, and every investor signs the new text before their next commitment. The frontend has no copy of the text; it sends back the version the status gave it.
2. **PDF.** The same renderer as the SME agreement (fpdf2, embedded Be Vietnam Pro). Party B comes from the approved payout account (holder type and bank details) and the verified profile. `INDIVIDUAL`: eKYC full name and ID number, plus the OCR address if any. `ENTITY`: KYB business name, enterprise code and address. The fallback name is the account's full name. Unknown values print as dotted blanks. The Annex prints as a blank template, with a note that it is completed on the platform at each commitment.
3. **Upload checks**, in order: `version` equals `VERSION`; the file is a PDF; it carries a digital signature; the current copy has not been committed under; size at most 10 MB. It is stored as evidence and a row is written with Party B as the PDF fills it in.
4. **Reservation gate.** `reserve_and_accept` refuses an investor with no row for `VERSION` (409 `INVESTOR_AGREEMENT_REQUIRED`) before anything is reserved. Each acceptance names the agreement row and its hash, and carries the commitment's Annex.
5. **Annex per commitment.** When the investor agrees, before the payment details exist, the server fills in the Annex (`pdf.render_annex`: commitment reference, the agreement version and signing date, both parties, the six rows, Article 7, the confirmation time in Vietnam time), stores the PDF as evidence and writes an `investment_confirmations` row. The commitment's signed acceptance names the row and the PDF's hash. A replayed reservation reuses the row. The transfer step shows it above the QR, with View and Download.
6. **Replacement.** A new upload of the current version replaces the copy that counts, but only while no tranche has been reserved after it. After that: 409 `INVESTOR_AGREEMENT_IN_USE`.

## 7. Error Cases

| Case | Status | `X-Error-Code` |
|---|---|---|
| Reserving with no signed copy of the current version | 409 | `INVESTOR_AGREEMENT_REQUIRED` |
| Upload names another version | 409 | `INVESTOR_AGREEMENT_OUTDATED` |
| Upload is not a PDF | 415 | `UNSUPPORTED_TYPE` |
| PDF with no digital signature (e.g. FundLok's own download sent straight back) | 422 | `INVESTOR_AGREEMENT_UNSIGNED` |
| Upload over 10 MB | 413 | `FILE_TOO_LARGE` |
| Replacing a copy already committed under | 409 | `INVESTOR_AGREEMENT_IN_USE` |
| Upload without approved KYC or bank account | 403 | — |
| Signed copy requested with none on file | 404 | — |

## 8. Acceptance Criteria

Tests: `tests/market/test_investor_service_agreement.py`.

- [x] `test_the_agreement_pdf_is_filled_from_the_investors_verified_details`
- [x] `test_an_organisation_investor_is_filled_from_its_business_verification`
- [x] `test_a_first_commitment_without_a_signed_agreement_is_refused`
- [x] `test_the_signed_agreement_is_recorded_with_its_version_and_signing_time`
- [x] `test_the_unsigned_agreement_sent_straight_back_is_refused`
- [x] `test_only_a_pdf_of_the_current_version_is_taken`
- [x] `test_an_investor_without_kyc_or_bank_account_cannot_upload`
- [x] `test_the_first_commitment_goes_through_once_the_signed_agreement_is_in`
- [x] `test_a_later_commitment_signs_only_its_investment_confirmation`
- [x] `test_each_commitment_keeps_its_filled_in_investment_confirmation_before_payment`
- [x] `test_replaying_a_commitment_keeps_one_investment_confirmation`
- [x] `test_only_the_investor_who_committed_sees_its_investment_confirmation`
- [x] `test_a_wrong_file_can_be_replaced_until_a_commitment_is_made_under_it`
- [x] `test_a_new_version_asks_every_investor_to_sign_again`
- [x] `test_the_status_asks_for_the_agreement_until_signed_then_shows_what_was_signed`
- [x] `test_the_investment_confirmation_is_filled_in_from_the_window`
- [x] `test_only_an_investor_has_an_investor_agreement`

Frontend (`app/(app)/dashboard/invest/`): a first-time investor gets 1 Số tiền → 2 Hợp đồng dịch vụ (PDF preview, download, how to sign, upload of the signed copy) → 3 Điều khoản → 4 Chuyển khoản. On the transfer step, above the QR, a "Xác nhận đầu tư (Phụ lục) đã ký" card shows the filled-in Annex with View and Download PDF. Afterwards every commitment shows 1 Số tiền → 2 Điều khoản → 3 Chuyển khoản, with a reminder ("Xem hợp đồng đã ký" opens the signed copy; "Thay bằng tệp khác" while replaceable) and the filled Annex above the terms. `make_eligible_investor` in conftest now includes a signed agreement (`signed_agreement=False` to opt out).

## 9. Open Questions

| # | Question | Owner |
|---|---|---|
| 1 | GVerify's eKYC OCR returns no address, so an individual's Party B address is blank. Ask the investor for it, or get it from GVerify? | Phat / Edward |
| 2 | Validate signatures (pyHanko: certificate chain, signer name against the eKYC name) instead of the presence check? | Edward |
| 3 | Staff review of signed copies: an admin view, and what happens to a copy found to be wrong after a commitment. | Phat |
| 4 | "Expected Disbursement Date" in the Annex is the hold's last ICT day (pay within the 2-hour hold). It changes with commit-then-accept. | Edward |
| 5 | Do individual investors realistically hold a digital signature? If not, the platform's remote-signing vendor (funding-window.md rule 1) is needed before launch. | Lok / Edward |
