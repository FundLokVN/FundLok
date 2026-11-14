# Spec: Investor Service Agreement (Contract 4): confirmed with a Zalo one-time code at the first commitment

| Field | Value |
|---|---|
| **Status** | DRAFT (built ahead of review; needs Edward's review of the migrations and the model registry line, and IBL's review of signing by one-time code) |
| **Owner** | Phat |
| **Implementer(s)** | Phat (backend `app/market/`, frontend) |
| **Module** | `app/market/investor_agreement/`, `app/market/funding.py`, `app/market/funding_router.py` |
| **Version** | 2.0 (1.1: the master agreement is a digitally signed PDF, as for the SME; 1.2: each commitment's Annex is kept as a filled-in PDF and shown before paying; 1.3: commit first, so the agreement's Annex is filled in with the commitment's code and values, then sign, then pay; 1.4: the signed copy is verified with NEAC and must be the investor's own; **2.0: the investor confirms with a one-time code sent by Zalo instead of a digital signature, and reads the Loan Contract the SME signed before committing**) |
| **Date** | 2026-11-11 |
| **Related ADR** | ADR-009 |
| **Depends on** | `docs/specs/direct_transfer/funding-window.md`, `docs/specs/direct_transfer/end-to-end-payment-flow.md` ("Contract structure"), `docs/specs/direct_transfer/fees.md` (investor fee formula), `docs/specs/users/phone-verification.md` (the Zalo ZBS sender, `app/phone_verification/zalo.py`), `docs/specs/loans/listing-signing.md` (the SME's signed Loan Contract), Contract set v0.3 (5 Nov 2026, draft pending IBL review) |

---

## 1. Context & Goal

Contract set v0.3 makes the investor sign Contract 4, the "Hợp đồng dịch vụ dành cho Nhà đầu tư / Investor Service Agreement", with FundLok. Per the contracts table it is the *"Master at the first commitment; a short confirmation on each later commitment"*, signed *"with a remote digital signature"*; *"Later commitments sign only an Investment Confirmation (Annex)."*

Versions 1.x had the investor download the agreement, sign it with their own digital signature and upload it, checked with NEAC. Investors are individuals, and individuals rarely hold a digital signature (open question 5 of v1.4), so on 11 Nov 2026 Phat replaced that step: the investor reads the agreement on the platform and confirms it with a one-time code sent by Zalo to the phone number on their account. The code is their signature. FundLok has no organisation investors, so there is one flow.

The first commitment is still made first: agreeing to the terms holds the amount and gives the commitment its code, and no money moves. The platform fills the agreement PDF in with the investor's verified details and that commitment's Annex (code, listing, SME, amount, rate and term, fee, expected disbursement date). The investor reads it, ticks that they agree, asks for the code and enters it. Only then does the platform give the payment details.

Before committing at all, the investor can also read the Loan Contract template (Contract 5) the SME and its Personal Guarantor signed for the listing: it is the contract their commitment concludes.

**Goal:** no money moves on a first commitment until the investor has confirmed the current Investor Service Agreement, filled in with that commitment's Annex, with a one-time code, and the platform has recorded it (version, time, commitment, the PDF as confirmed, the code's record). Every later commitment keeps its own filled-in Investment Confirmation (the Annex) when the investor agrees, and is never asked for the master agreement again. An eligible investor can read the SME-signed Loan Contract before committing.

## 2. Out of Scope

- A digital signature by the investor, and the NEAC check of it. Rows of `investor-agreement-v0.3` keep their NEAC verdict; nothing new is checked with NEAC here. (`neac.py` stays: the SME's listing documents use it.)
- Verifying the phone number as the account's (`users.phone_verified_at`). The code goes to the phone on the account, verified or only typed at registration; confirming does not mark it verified (§9).
- A fallback channel. An investor whose number has no Zalo account cannot confirm (they are told to use a number with Zalo).
- Organisation investors. Party B is still filled from a KYB for an `ENTITY` account, but nothing else is built for them.
- Each investor's own copy of Contract 5 (listing-signing.md open question 8). The investor reads the template the SME signed.
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
  signed_at        TIMESTAMPTZ  NOT NULL  when the investor confirmed with the code
  tranche_id       UUID         NULL      FK tranches.id ON DELETE RESTRICT: the commitment it was signed with,
                                          whose Annex it carries (null only outside the flow, e.g. fixtures)
  party_b          JSONB        NOT NULL  {holder_type, name, id_number, address, bank_account_number,
                                           bank_account_name, bank_name}, any value nullable
  signed_file_key  TEXT         NOT NULL  evidence storage key (evidence_files, purpose SIGNED_DOCUMENT): the PDF
                                          as confirmed, with the confirmation line
  document_sha256  TEXT         NOT NULL  SHA-256 of that PDF
  file_name        TEXT         NULL
  file_size_bytes  BIGINT       NOT NULL
  signature_check  JSONB        NULL      how it was confirmed: {verified_by: "ZALO_OTP", checked_at, phone,
                                          phone_masked, channel, code_id, code_sent_at}. Rows of v0.3 hold NEAC's
                                          verdict instead (verified_by "NEAC", signer, issuer, ...; migration
                                          0538f8bc779a)
  signature_checked_at TIMESTAMPTZ NULL
  ip_address       TEXT         NULL
  user_agent       TEXT         NULL
  created_at       TIMESTAMPTZ  NOT NULL  DEFAULT now()
  UNIQUE (investor_id, version)
```

Append-only, one per investor per version. A new version is a new row, and the old row is kept.

### New table: `investor_agreement_codes` (migration `dd37a6f4decf`)

```
investor_agreement_codes
  id           UUID         PK
  investor_id  UUID         NOT NULL  FK users.id ON DELETE RESTRICT
  tranche_id   UUID         NOT NULL  FK tranches.id ON DELETE RESTRICT: the commitment the code confirms
  version      TEXT         NOT NULL  the agreement version it confirms
  phone        TEXT         NOT NULL  canonical +84xxxxxxxxx: the account's phone when it was sent
  code_hash    TEXT         NOT NULL  HMAC-SHA256(SECRET_KEY, "investor-agreement:<id>:<tranche>:<version>:<code>")
  channel      TEXT         NOT NULL  zalo | log
  expires_at   TIMESTAMPTZ  NOT NULL  created_at + 5 min
  attempts     INTEGER      NOT NULL  DEFAULT 0
  consumed_at  TIMESTAMPTZ  NULL
  created_at   TIMESTAMPTZ  NOT NULL  DEFAULT now()
  INDEX (investor_id, created_at)
```

Rows are kept: they are the send counter and the trail behind each signature. The migration also joins the two chains that grew from `0538f8bc779a`: listing signing (`b8e2d6f4a1c7`) and phone verification (`efd8a69b9406`).

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

Insert-only, one per commitment: the Annex of Contract 4 Art. 7. For a later commitment it is filled in when the investor agrees; for the first one, when the agreement is confirmed with it. Either way, before any payment details exist.

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
| `GET /funding/me/investor-agreement` | `InvestorAgreementOut {version, required, phone, signed_at, signed_version, file_name, tranche_id, signature_check: {verified_by, checked_at, phone, signer_name, signer_id, issuer_name, signed_time, name_matches} \| null, party_b}`. `phone` is the number the code goes to, masked (`0703***489`), or null when the account has no Vietnamese mobile. |
| `GET /funding/me/investor-agreement/document[?tranche_id=…][&download=true]` | The current version as a PDF, filled in with the investor's details and, with `tranche_id` (their own commitment, else 404), that commitment's code and Annex; without it, the Annex is the blank template. `inline` or `attachment`, `no-store`. Generated each time. File name `FundLok-hop-dong-dich-vu-nha-dau-tu-{REF}[-{CODE}]-{VERSION}.pdf`. |
| `POST /funding/me/investor-agreement/code` (`{version, tranche_id}`) | 200 `AgreementCodeOut {phone (masked), channel: zalo \| log, expires_in: 300, resend_in: 60}`. Sends the code. Needs the eligible-investor gate and the investor's own live commitment. |
| `POST /funding/me/investor-agreement` (`{version, tranche_id, code}`; `code` is 6 digits) | 201 with `InvestorAgreementOut`. Same gate. Keeps the confirmed PDF and that commitment's Annex, and opens its payment details. Audited as `INVESTOR_AGREEMENT_SIGNED` (`signed_by`, masked `phone`, `code_id`). |
| `GET /funding/me/investor-agreement/signed[?download=true]` | The PDF kept when the current version was confirmed, byte for byte. 404 when none. |
| `GET /funding/windows/{id}/loan-contract[?download=true]` | The Loan Contract template the SME and its Guarantor signed for the listing, as uploaded. Eligible investors only (403 otherwise); 404 for an unknown window, the SME's own, or a listing with no signed copy. Each view is audited (`LOAN_APPLICATION` / `LOAN_CONTRACT_VIEWED_BY_INVESTOR`). |
| `GET /funding/windows/{id}/investment-confirmation?amount_vnd=N` | `InvestmentConfirmationOut`: the Annex for a commitment, as it will be signed (shown on the terms step). |
| `GET /funding/tranches/{id}/investment-confirmation` | `SignedInvestmentConfirmationOut {id, tranche_id, reference_code, listing_id, listing_ref, sme_name, amount_vnd, annual_rate_pct, term_months, service_fee_vnd, service_fee_bps_per_year, expected_disbursement_date, confirmed_at, agreement_version}`, for the investor's own ticket only (404 otherwise, or for tickets from before 1.2). |
| `GET /funding/tranches/{id}/investment-confirmation/document[?download=true]` | The filled-in Annex PDF kept for that ticket, exactly as stored. |
| `POST /funding/windows/{id}/reservations` | Unchanged. A first commitment is made without the agreement (code, held amount, no money moved). |
| `GET /funding/tranches/{id}/payment` | 409 `INVESTOR_AGREEMENT_REQUIRED` until the investor has confirmed the current version. Receipts are never refused for it. |

## 5. State Machine

None new. The agreement is either required (no row for the current version) or confirmed. A code is live (latest, unused, under 5 minutes old, under 5 wrong guesses) or dead. A first commitment goes RESERVED → SIGNED as before (the acceptance), then waits for the agreement before its payment details are given.

## 6. Business Rules

1. **Text and version.** The text lives in `app/market/investor_agreement/text.py`, from Contract set v0.3 except how Party B signs: v0.3 says "with a remote digital signature"; from `investor-agreement-v0.3.1` the signing line, the Annex's "part of" line and Party B's signature block say Party B confirms with a one-time code sent by Zalo to their phone number. That wording is FundLok's own and awaits IBL's review (§9). `VERSION` is printed on every page. Bump it whenever a word changes, and every investor confirms the new text with their next commitment; so investors who signed v0.3 confirm v0.3.1 once. The frontend has no copy of the text; it sends back the version the status gave it.
2. **PDF.** fpdf2 with embedded Be Vietnam Pro. Party B comes from the approved payout account (holder type and bank details) and the verified profile: eKYC full name and ID number, plus the OCR address if any. The fallback name is the account's full name. Unknown values print as dotted blanks. Generated for a commitment, the Annex shows "Mã cam kết / Commitment: {code}" and all six rows filled in. Without one, it is the blank template. The copy kept as the record adds one line under the signature blocks: Party B confirmed at {time, Vietnam time} with a one-time code sent by Zalo to {masked phone}.
3. **Commit first.** `reserve_and_accept` makes a first commitment without the agreement: the amount is held (2-hour hold), the commitment gets its reference code, and the acceptance (investing terms plus the Annex values) is signed in-app. No money moves.
4. **Who can ask for a code or confirm.** An eligible investor (KYC and approved bank account), for their own commitment (404 otherwise), whose hold has not lapsed (409 `HOLD_EXPIRED`), with `version` equal to `VERSION` (409 `INVESTOR_AGREEMENT_OUTDATED`), and no confirmation of this version on file (409 `INVESTOR_AGREEMENT_ALREADY_SIGNED`).
5. **Sending the code.** It goes by Zalo ZBS (the OTP template, `PHONE_OTP_CHANNEL=zalo`) to the phone number on the investor's account, which must be a Vietnamese mobile (409 `PHONE_REQUIRED` otherwise). The investor cannot name another number here. Six digits from `secrets`, valid 5 minutes. One code a minute and five in a rolling 24 hours per investor, counted under an advisory lock (429 `CODE_COOLDOWN` / `CODE_DAILY_LIMIT`, with `Retry-After`). A send Zalo refuses is rolled back: it neither counts nor starts a cooldown. A number Zalo cannot reach (the codes in `phone_verification.service.NO_ZALO_ERRORS`) is a 400 `PHONE_NO_ZALO`; any other refusal a 502 `CODE_NOT_SENT`; Zalo not configured a 503 `CODE_UNAVAILABLE`. With `PHONE_OTP_CHANNEL=log` and `DEBUG=True` the code is written to the server log instead (503 without `DEBUG`).
6. **What makes the code a signature on this document.** The code's hash is bound to its row, the commitment and the agreement version, so a code sent for one commitment or version cannot confirm another (400 `CODE_EXPIRED`). Only the investor's latest code can be redeemed; it is single use; five wrong guesses kill it (400 `CODE_INCORRECT`, then `CODE_TOO_MANY_ATTEMPTS`; wrong guesses are committed even though the request fails).
7. **Confirming.** On the right code the server renders the agreement once more (rule 2, with the confirmation line), stores that PDF as evidence, and writes the row: version, time, commitment, Party B, the PDF's key, hash and size, the IP address and browser, and `signature_check` (`verified_by: "ZALO_OTP"`, the phone, the channel, the code's id and send time). It then keeps that commitment's Annex (rule 9).
8. **Payment gate.** `GET /funding/tranches/{id}/payment` is refused (409 `INVESTOR_AGREEMENT_REQUIRED`) while the investor has no confirmation of `VERSION`. Receipt upload is not gated, so money already sent still reaches staff.
9. **Annex per commitment.** Before the payment details exist (for a later commitment when the investor agrees; for the first, when the agreement is confirmed with it), the server fills in the Annex (`pdf.render_annex`: commitment reference, the agreement version and confirmation date, both parties, the six rows, Article 7, the confirmation time in Vietnam time), stores the PDF as evidence and writes an `investment_confirmations` row. The commitment's signed acceptance names the row and the PDF's hash. A replayed reservation reuses the row. The transfer step shows it above the QR, with View and Download.
10. **One confirmation per version.** There is no replacement.
11. **The SME-signed Loan Contract.** `GET /funding/windows/{id}/loan-contract` serves the `sme_loan_contract` document of the application behind the window's contract, exactly as the SME uploaded it. It names the Personal Guarantor (name, ID number, address), so it is served only to an eligible investor, never listed among a project's public documents, never to the SME through this route, and every view is audited. This amends listing-signing.md rule 7 for the Loan Contract only; the Service Agreement and the Standing Offer stay hidden from investors.

## 7. Error Cases

| Case | Status | `X-Error-Code` |
|---|---|---|
| Payment details while the current version is not confirmed | 409 | `INVESTOR_AGREEMENT_REQUIRED` |
| Code or confirmation for a commitment that is not the investor's | 404 | — |
| Code or confirmation after the commitment's hold lapsed | 409 | `HOLD_EXPIRED` |
| Code or confirmation when this version is already confirmed | 409 | `INVESTOR_AGREEMENT_ALREADY_SIGNED` |
| Code or confirmation naming another version | 409 | `INVESTOR_AGREEMENT_OUTDATED` |
| Code or confirmation without approved KYC or bank account | 403 | — |
| Code, and the account has no Vietnamese mobile number | 409 | `PHONE_REQUIRED` |
| Code within a minute of the last | 429 | `CODE_COOLDOWN` |
| Sixth code in 24 hours | 429 | `CODE_DAILY_LIMIT` |
| The number has no Zalo account that can receive it | 400 | `PHONE_NO_ZALO` |
| Zalo refused or unreachable | 502 | `CODE_NOT_SENT` |
| Zalo not configured, or `log` without `DEBUG` | 503 | `CODE_UNAVAILABLE` |
| Wrong code | 400 | `CODE_INCORRECT` |
| Fifth wrong code | 400 | `CODE_TOO_MANY_ATTEMPTS` |
| No live code for this commitment and version | 400 | `CODE_EXPIRED` |
| Code not six digits | 422 | — |
| Confirmed copy requested with none on file | 404 | — |
| Loan Contract for a listing with no signed copy, an unknown window, or the SME's own | 404 | — |
| Loan Contract requested by an investor who is not eligible | 403 | — |

## 8. Acceptance Criteria

Tests: `tests/market/test_investor_service_agreement.py`.

- [x] `test_a_first_commitment_gets_its_code_but_no_payment_details_until_signed`
- [x] `test_the_agreement_pdf_is_filled_in_with_the_investor_and_the_commitments_annex`
- [x] `test_confirming_with_the_code_keeps_the_agreement_and_the_first_annex_and_opens_payment`
- [x] `test_the_code_goes_by_zalo_to_the_accounts_phone`
- [x] `test_an_account_without_a_vietnamese_mobile_is_asked_for_one`
- [x] `test_a_wrong_code_is_counted_and_the_fifth_kills_the_code`
- [x] `test_only_a_live_code_confirms`
- [x] `test_a_code_confirms_only_the_commitment_it_was_sent_for`
- [x] `test_codes_are_limited_to_one_a_minute_and_five_a_day`
- [x] `test_a_code_zalo_does_not_deliver_is_not_counted`
- [x] `test_the_log_channel_stands_in_for_zalo_only_in_debug`
- [x] `test_an_old_version_is_not_confirmed`
- [x] `test_the_agreement_is_confirmed_with_the_investors_own_live_commitment`
- [x] `test_the_agreement_is_confirmed_once_per_version`
- [x] `test_an_investor_without_kyc_or_bank_account_cannot_confirm`
- [x] `test_a_later_commitment_keeps_its_annex_at_once_and_pays_straight_away`
- [x] `test_replaying_a_commitment_keeps_one_investment_confirmation`
- [x] `test_a_new_version_is_signed_with_the_next_commitment`
- [x] `test_the_status_asks_for_the_agreement_until_signed_then_shows_what_was_signed`
- [x] `test_an_organisation_investor_is_filled_from_its_business_verification`
- [x] `test_the_investment_confirmation_preview_is_filled_in_from_the_window`
- [x] `test_only_the_investor_who_committed_sees_its_documents`
- [x] `test_an_eligible_investor_reads_the_loan_contract_the_sme_signed`
- [x] `test_a_listing_without_a_signed_loan_contract_has_none_to_show`

Frontend (`app/(app)/dashboard/invest/`): a first-time investor gets 1 Số tiền → 2 Điều khoản (the SME-signed Loan Contract to view or download, a note on what comes next, the Annex) → 3 Hợp đồng dịch vụ (the commitment code and hold deadline, the PDF preview with this commitment's Annex filled in, download, a tick box "I have read and agree", "Send the confirmation code by Zalo" to the masked number, the six-digit code, resend after 60 seconds) → 4 Chuyển khoản. An account with no Vietnamese mobile is sent to its profile to add one. Later commitments get 1 Số tiền → 2 Điều khoản (the Loan Contract, a reminder of the confirmed agreement with "confirmed with a code sent by Zalo to …", "Xem hợp đồng đã xác nhận") → 3 Chuyển khoản. The transfer step always starts with the "Xác nhận đầu tư (Phụ lục) đã ký" card (View, Download PDF) above the QR. `make_eligible_investor` in conftest includes a confirmed agreement (`signed_agreement=False` to opt out).

## 9. Open Questions

| # | Question | Owner |
|---|---|---|
| 1 | GVerify's eKYC OCR returns no address, so an individual's Party B address is blank. Ask the investor for it, or get it from GVerify? | Phat / Edward |
| 2 | **Legal weight of the code.** Does confirming with a one-time code sent to the investor's phone meet what Contract 4 and the law on e-transactions need for this agreement, and is the v0.3.1 wording acceptable? Until IBL answers, the wording is FundLok's own. | IBL / Lok |
| 2a | **Whose phone.** The code goes to the phone on the account, which may only have been typed at registration (accounts from before phone verification). Require `phone_verified_at` first, or let a successful confirmation mark the number verified? | Phat / Edward |
| 2b | **The Zalo template.** The same OTP template serves sign-up and this confirmation, so the message does not say what the code confirms. A second template naming the agreement would be clearer; it needs Zalo's approval. | Phat |
| 2c | **The Guarantor's personal data.** The SME-signed Loan Contract shows the Guarantor's name, ID number and address to every eligible investor who opens the listing's terms. Is that covered by the Guarantor's consent, or should investors get a copy with those details masked? | IBL |
| 3 | Staff review of signed copies: an admin view, and what happens to a copy found to be wrong after a commitment. | Phat |
| 4 | "Expected Disbursement Date" in the Annex is the hold's last ICT day (pay within the 2-hour hold). It changes with commit-then-accept. | Edward |
| 5 | No fallback when the investor's number has no Zalo account: SMS, or an email code? | Phat |
