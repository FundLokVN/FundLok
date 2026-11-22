# Spec: Listing documents signed after approval (SME Service Agreement, Standing Offer, Loan Contract template)

| Field | Value |
|---|---|
| **Status** | DRAFT (built ahead of review; needs Edward's review of the migration and the activation gate) |
| **Owner** | Phat |
| **Implementer(s)** | Phat (backend `app/loans/`, `app/projects/`, frontend) |
| **Module** | `app/loans/signing.py`, `app/loans/agreement/` (`standing_offer*.py`, `terms.py`), `app/projects/service.py` (`activate_project_if_approved`) |
| **Version** | 1.5 (1.5, 2026-11-20: the revenue share *s* is filled in on the Standing Offer and the Loan Contract from the LOCKED score run's daily repayment rate (D-044; Loc, 16 Nov, D-060), covered by both check codes; versions v0.5, with v0.4 copies on file still accepted as an earlier version; the investor listing carries DRR, *s* and the Personal Guarantor's name (SCRUM-637); 1.4: the three documents are laid out as Vietnamese contracts (Quốc hiệu and Tiêu ngữ, the date, the title in capitals with its number, signer names under the boxes) in DejaVu Serif; every detail FundLok has is filled in, the company address falls back to the project's, the SME enters the Guarantor's address on the signing step, and no document can be signed while a party's details are blank; the Loan Contract cites the signed Offer by number and date, so it is signed after the Offer; it states what each Investor's copy fills in as a rule with a worked example at the minimum ticket; versions v0.4; 1.3: the Loan Contract template (Contract 5) is a third document, signed by the SME and the Personal Guarantor, and publishing needs all three; a fallback font covers the symbols Be Vietnam Pro lacks; 1.2: the admin panel shows and re-checks both signed documents with NEAC, the guarantor and the check code included; 1.1: the Standing Offer carries the assessment result and repayment estimate; both documents carry empty signature fields, one per signer, and a check code; the upload checks both automatically) |
| **Date** | 2026-11-10 |
| **Related ADR** | ADR-009 |
| **Depends on** | `docs/specs/market/investor-service-agreement.md` (the NEAC check this reuses), `docs/specs/direct_transfer/funding-window.md` (rule 1: the standing offer), the Contract set (documents 2 and 3) |

---

## 1. Context & Goal

The SME Service Agreement (Contract 2) used to be step 4 of the loan application wizard, signed before FundLok had assessed anything, so its Annex said the rate would come later. Both contracts say otherwise. Contract 2: *"Signed: when the SME accepts its listing terms after FundLok's assessment"*. Contract 3, the Standing Offer to Conclude Loan Contracts: *"Signed: by the SME with its enterprise digital signature and by the Personal Guarantor with a personal digital signature, at the same time as the SME Service Agreement, once the assessment is done and the SME agrees to the loan terms; signing publishes the Listing."*

So the wizard no longer asks for any signature. Once FundLok approves a request (the operator's approval and a LOCKED score run, which fixes the rate), the SME's dashboard offers both documents, filled in with the approved terms. The SME signs both and uploads them back, and the Personal Guarantor also signs the Standing Offer. When both are verified, the project goes live, and that is what publishes the listing.

## 2. Out of Scope

- Linking the funding window's `standing_offers` row to the SME's signed PDF. The window still stores the machine-readable terms snapshot, signed with the mock signer (`app/direct_transfer`, Edward's module; funding-window.md rule 1, I-25).
- Computing *s* separately from the daily repayment rate. Since v1.5 the Standing Offer and the Loan Contract print *s* as the LOCKED score run's `daily_repayment_rate` (the figure the automatic extension already uses, `direct_transfer/extension.py`; D-044, confirmed by Loc on 16 Nov as D-060). A run scored before the rate was stored (#99) has none, so its documents keep the dotted blank, which does not stop signing.
- Remote signing inside FundLok. The SME signs outside the platform (USB token or the CA's remote signing) and uploads the PDF.

## 3. Data Model

No new table. One new column, `loan_applications.guarantor_address` (TEXT, nullable; migration `b8e2d6f4a1c7`): the Personal Guarantor's address as the SME entered it. The eKYC gives the Guarantor's name and ID number but no address (GVerify's OCR returns none), so without it the Offer and the Loan Contract carry a blank. The eKYC's own address wins when it has one.

Each signed document is a `loan_application_documents` row, as the Service Agreement already was:

| document_type | Contract | Signatures required |
|---|---|---|
| `sme_service_agreement` | 2, SME Service Agreement | the company's (MST = approved KYB enterprise code, else the project's tax ID) |
| `sme_standing_offer` (new; migration `a37775d5b929` admits it to the check constraint) | 3, Standing Offer | the company's **and** a separate personal signature by the Guarantor (CCCD = the signing user's approved eKYC `person_number`) |
| `sme_loan_contract` (new; migration `3f4ff91a3825`) | 5, Loan Contract template | the same two as the Standing Offer. Each Investor's own contract is generated from this template when they commit, and the Investor signs that one then (a later step) |

The object is stored at `application_documents/{user_id}/{application_id}/{document_type}.pdf`, status `UPLOADED`. NEAC's verdict is an audit event (`LOAN_APPLICATION_DOCUMENT` / `NEAC_SIGNATURE_CHECKED`), in the same shape `app/loans/agreement/neac_check.py` writes, with `guarantor` added for the Standing Offer.

Listing terms (`app/loans/agreement/terms.py`) are the values the funding window later opens on: `requested_amount`, the LOCKED score run's `interest_rate_pct` (to 0.001), and `duration_months`. Alongside them are the run's business score and lock date. The repayment estimate (`repayment_estimate`) is the repayment schedule's own (`repayment_schedule.origination.compute_origination`):
- interest = P × r × M / 12, rounded to the đồng, and T0 = P + interest;
- N0 = 21 × M, with 21 being the funding parameters' working days a month and Contract 3's own N0;
- A0 = floor(T0 / N0), with the remainder paid on the last day;
- N_bs = min(ceil(4·N0/3), 24 × 21).

The SME's approval card uses the same estimate, so it shows the figures the SME then signs.

### What each document carries

| | Service Agreement | Standing Offer |
|---|---|---|
| Approved terms | Annex: amount, term, rate | Listing details: target, rate, term, N0, minimum ticket |
| Assessment and estimate | — | business score, assessment date, principal, interest, total T0, daily A0 × N0 with the last day, backstop day |
| Empty signature fields | `SME_Signature` (Party B). Party A, FundLok's countersignature, is not collected here | `SME_Signature`, `Guarantor_Signature` |
| Contract 5 | filled at listing level: Borrower, Guarantor, the Standing Offer's number (`{ref}/ĐNGK-FL`) and the day its signed copy was accepted (ICT), rate, term, N0 and the backstop N_bs. What each Investor's copy fills in is stated as the rule it follows: the contract no. (`{ref}/HĐCV-FL`, numbered per Investor at commitment), the Lender (the Investor accepting the Offer), P (from the minimum ticket, all contracts together at most the approved amount), I = P × r × M / 12, T = P + I and A = floor(T / N0), each with its value at the minimum ticket, the Disbursement Date (listing date + 14 days, as in the Offer) and the Investor's signing time (at commitment). *s* as the Offer prints it | `SME_Signature`, `Guarantor_Signature` | over the SME, the Guarantor, the rate and term, *s*, N0 and N_bs, the approved amount, the minimum ticket and the Offer's date |
| Check code (every page) | over everything it is filled in with except the date prepared | over the SME, the Guarantor, the terms, *s*, the assessment, the estimate and the minimum ticket |

The check code (`pdf.check_code`) is the first 12 hex digits of a SHA-256 over those values, printed as `XXXX-XXXX-XXXX`.

A copy of an earlier version (`standing-offer-v0.4`, `loan-contract-v0.4`, both without *s*) is still the right document on a NEAC re-check, with `terms_match: null` (`neac_check.EARLIER_VERSIONS`). A new upload must be the current version (422 `SIGNING_WRONG_DOCUMENT`), so an SME holding a v0.4 copy downloads it again.

### What the investor listing shows (`GET /projects/public`, v1.5, SCRUM-637)

`MarketplaceLoanApplicationOut` adds three fields for the offer screen, each null until it exists:

| Field | Value |
|---|---|
| `daily_repayment_rate` | DRR: the LOCKED run's `daily_repayment_rate`, a fraction (0.085 = 8.5%) |
| `revenue_share` | *s* as the Standing Offer prints it. Today the same figure as DRR; a field of its own so the screen never assumes that |
| `guarantor_name` | the Personal Guarantor the signed Standing Offer names (the uploader of the signed copy, as their approved eKYC names them; `neac_check.signing_user`, `standing_offer.guarantor_for`). The name only: the ID number and address stay on the document |

### Where each party detail comes from

| Detail (code in `missing_details`) | Source |
|---|---|
| `COMPANY_NAME`, `ENTERPRISE_CODE` | the approved KYB, else the project's legal name and tax ID |
| `COMPANY_ADDRESS` | the approved KYB's company address, else the project's address (street, city, state, postal code, country) |
| `LEGAL_REPRESENTATIVE` | the approved KYB's first representative, "name (position)" |
| `BANK_ACCOUNT` | the SME's approved payment account |
| `GUARANTOR_NAME`, `GUARANTOR_ID` | the signing user's approved eKYC |
| `GUARANTOR_ADDRESS` | the eKYC's ID-card address when there is one, else `loan_applications.guarantor_address` |

The Service Agreement needs the company's details; the Offer and the Loan Contract need the company's and the Guarantor's.

### Layout

Each of Contracts 2, 3 and 5 opens the way Vietnamese contracts do: "CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM", "Độc lập - Tự do - Hạnh phúc" with a short rule under it, the same two lines in English, the date ("Ngày dd tháng mm năm yyyy"), the title in capitals in Vietnamese and English, and its number (`{ref}/HĐDV-FL`, `{ref}/ĐNGK-FL`, `{ref}/HĐCV-FL`). The document number in the Contract set, the application reference, the version and the check code follow in small print. The signature blocks close it side by side with the signer's name under each box (Party A: FundLok's General Director). The face is DejaVu Serif, subset to Latin, Vietnamese and the formula symbols (`fonts/DejaVuSerif-*-VN.ttf`). Contract 4 (the investor's agreement) keeps Be Vietnam Pro and is unchanged.

## 4. API Contract

All routes are SME-only and limited to the application's owner (403 otherwise, 404 for an unknown application).

| Method | Path | Returns |
|---|---|---|
| GET | `/loans/applications/{id}/service-agreement` | PDF (unchanged route; the Annex now prints the approved rate once the terms are locked) |
| GET | `/loans/applications/{id}/standing-offer` | PDF; 409 `SIGNING_NOT_OPEN` until the terms are locked |
| GET | `/loans/applications/{id}/loan-contract` | PDF of the Loan Contract template; 409 `SIGNING_NOT_OPEN` until the terms are locked, 409 `SIGNING_OFFER_FIRST` until the Standing Offer is on file |
| PUT | `/loans/applications/{id}/guarantor-address` (`{"address": str}`, 8–300 characters, whitespace collapsed) | `ListingDocumentsOut`; 409 `SIGNING_ALREADY_SIGNED` once the Offer or the Loan Contract is on file. Audited (`LOAN_APPLICATION` / `GUARANTOR_ADDRESS_SET`) |
| GET | `/loans/applications/{id}/listing-documents` | `ListingDocumentsOut` |
| POST | `/loans/applications/{id}/listing-documents/{kind}` (multipart `file`; `kind` = `service-agreement` \| `standing-offer` \| `loan-contract`) | `ListingDocumentsOut` after the upload; 404 for any other kind |
| GET | `/loans/applications/{id}/listing-documents/{kind}/signed` | the signed PDF as uploaded; 404 when none |

```python
class SignedListingDocumentOut(BaseModel):
    signed: bool
    signed_at: datetime | None
    file_name: str | None
    missing_details: list[str]  # what it still prints as a blank (codes above); empty once signed
    waiting_for: str | None     # "standing-offer" for the Loan Contract until the Offer is on file

class ListingDocumentsOut(BaseModel):
    ready: bool               # approved with locked terms: there is something to sign
    published: bool           # project.status == "ACTIVE"
    guarantor_verified: bool  # the signing user has an approved eKYC with an ID number
    guarantor_address: str | None                       # as the Offer and the Loan Contract print it
    guarantor_address_source: Literal["EKYC", "SME"] | None
    service_agreement: SignedListingDocumentOut
    standing_offer: SignedListingDocumentOut
    loan_contract: SignedListingDocumentOut
```

`SignedListingDocumentOut` also carries `checks` (null until a verified upload): `checked_at`, `verified_by` ("NEAC"), `terms_match`, and `signers` as `[{role: "SME" | "GUARANTOR", name}]`. These come from the document's newest verdict, and the SME's signing card shows them.

Admin: the project panel shows each signed document's newest verdict (`signature_check`, from the upload or a later re-check), with `POST /admin/documents/{id}/neac-check` re-checking either type (`app/loans/agreement/neac_check.check_signed_document`). A re-check covers the same ground as the upload:
- right document and today's check code: `terms_match` is `null` for a copy that has no code, such as one signed in the old wizard, and for a copy of an earlier version (`sme-agreement-v0.3`, `standing-offer-v0.3.1`, signed before v0.4), which is still the right document;
- NEAC's verdict and the company's tax code;
- on the Standing Offer, a separate valid signature from the Personal Guarantor (`guarantor_*`), matched against the eKYC ID of the user who uploaded it.

`AdminSignatureCheck` gains `document_type`, `terms_match`, `check_code`, `guarantor_required`, `guarantor_matches`, `guarantor_expected_id`, `guarantor_expected_name`, `guarantor_name`, `guarantor_id`, `guarantor_name_matches` and `guarantor_problems`.

`POST /uploads/init-upload` refuses both signed types (400 `SIGNED_AFTER_APPROVAL`), so neither can arrive without the NEAC check. `POST /loans/applications/{id}/submit` no longer requires a signed agreement.

## 5. State Machine

```
Project:  DRAFT ──(admin APPROVED + score run LOCKED + all three documents signed)──▶ ACTIVE
```

`activate_project_if_approved` checks all three. It runs wherever one of them lands: the admin decision, the score-run lock, and the SME's second signed upload. Whichever lands last flips the project, and then `publish_if_ready` opens the funding window if the SME's bank account is approved.

## 6. Business Rules

1. Nothing can be signed until the terms are locked (`ready`). An operator approval without a LOCKED score run is not ready, because the rate is not known.
2. Each upload must be this application's document: the PDF text must contain the application reference and that document's `VERSION`, both printed on every page.
2a. It must also carry the check code the document would carry if generated now, for the uploading user. A copy downloaded before the terms, the assessment or a party's details changed (for example the bank account investors pay into), or one edited before signing, is refused.
2b. The Standing Offer must have at least as many signed signature fields as signers, i.e. two. This is checked locally, before NEAC.
3. NEAC must accept the signature: document intact, signature valid, certificate not revoked. NEAC unreachable is a 503, and nothing is accepted unchecked.
4. The Standing Offer needs two distinct valid signatures: the company's, and the Guarantor's personal one. A single certificate carrying both IDs counts as one signature.
5. A signed document cannot be replaced (409).
6. Uploads are serialised per application (`SELECT … FOR UPDATE` on the application, after the NEAC call), so two uploads at once still publish.
7. Investors are never shown either signed document (`verified_document_types` excludes both). The Standing Offer carries the Guarantor's personal details. Amended 11 Nov 2026 for the Loan Contract only: an eligible investor can read the signed Loan Contract before committing, through `GET /funding/windows/{id}/loan-contract` (investor-service-agreement.md rule 11). It is still not among `verified_document_types`.
8. Projects that went live before this change stay live. Approved projects still in DRAFT now need both signatures, including ones whose Service Agreement was signed in the old wizard: that row counts, and only the Standing Offer is left.
9. No document is accepted while it prints a party's details as a blank (422 `SIGNING_DETAILS_MISSING`, before NEAC is asked). The SME completes them (KYB, project, bank account, eKYC, or the Guarantor's address on the signing step) and downloads the document again: its check code changes with the new details, so a copy signed with the blank is outdated.
10. The Loan Contract is signed after the Standing Offer (409 `SIGNING_OFFER_FIRST` for its download and upload before then), because it cites the Offer's number and the day its signed copy was accepted.

## 7. Error Cases

| HTTP | X-Error-Code | When |
|---|---|---|
| 409 | `SIGNING_NOT_OPEN` | terms not locked yet |
| 409 | `SIGNING_ALREADY_SIGNED` | this document is already on file; or, for the Guarantor's address, the Offer or the Loan Contract is |
| 409 | `SIGNING_OFFER_FIRST` | the Loan Contract, before the Standing Offer is on file |
| 422 | `SIGNING_DETAILS_MISSING` | the document still prints a party's details as a blank (the codes in `detail`) |
| 413 | `SIGNING_FILE_TOO_LARGE` | over 25 MB |
| 415 | `SIGNING_NOT_PDF` | not a PDF |
| 422 | `SIGNING_UNSIGNED` | no signature dictionary (`/ByteRange`) |
| 422 | `SIGNING_WRONG_DOCUMENT` | reference or version missing from the text |
| 422 | `SIGNING_DOCUMENT_OUTDATED` | right document, but not the current check code |
| 422 | `SIGNING_GUARANTOR_UNVERIFIED` | Standing Offer, and the signing user has no approved eKYC ID number |
| 422 | `SIGNING_SIGNATURE_INVALID` | NEAC rejects the signature (its reasons are in `detail`) |
| 422 | `SIGNING_SIGNER_MISMATCH` | no valid signature is the company's |
| 422 | `SIGNING_GUARANTOR_MISSING` | Standing Offer with fewer than two signed signature fields, or without a separate valid Guarantor signature at NEAC |
| 503 | `NEAC_UNAVAILABLE` | NEAC could not be reached |

## 8. Acceptance Criteria

Tests in `tests/loans/test_listing_signing.py`, and the gate in `tests/projects/test_project_activation.py`:

- `test_submitting_no_longer_needs_a_signed_agreement`
- `test_nothing_can_be_signed_before_fundlok_approves`
- `test_an_operator_approval_without_locked_terms_is_not_ready`
- `test_both_documents_carry_the_approved_terms`
- `test_signing_both_publishes_the_project`
- `test_the_standing_offer_needs_the_guarantors_own_signature`
- `test_the_standing_offer_needs_a_verified_guarantor`
- `test_a_signed_agreement_that_does_not_check_out_is_refused`
- `test_another_applications_document_is_the_wrong_document`
- `test_a_signed_document_cannot_be_replaced`
- `test_only_the_owning_sme_signs`
- `test_the_wizard_cannot_upload_a_signed_document_unchecked`
- `test_two_uploads_at_once_still_publish`
- `test_a_copy_signed_before_the_details_changed_is_outdated`
- `test_the_loan_contract_template_carries_the_parties_and_the_approved_terms`
- `test_signing_all_three_publishes_the_project`
- `test_every_character_of_the_three_contracts_has_a_glyph`
- `test_the_loan_contracts_extension_formula_keeps_its_delta`
- `test_a_loan_contract_template_is_checked_like_the_standing_offer` (admin)
- `test_the_loan_contract_waits_for_the_signed_offer`
- `test_missing_details_must_be_completed_before_signing`
- `test_the_companys_address_comes_from_the_project_without_one_on_the_kyb`
- `test_a_copy_of_an_earlier_version_is_still_the_right_document` (admin)
- admin (`tests/admin/test_sme_agreement_neac_check.py`):
  - `test_a_standing_offer_signed_by_the_company_and_the_guarantor_is_verified`
  - `test_a_standing_offer_without_the_guarantors_own_signature_is_flagged`
  - `test_a_copy_whose_details_changed_since_signing_no_longer_matches`
- `test_both_approvals_without_the_signed_documents_do_not_activate`
- `test_the_second_signature_activates_an_approved_project`

Frontend: the wizard has four steps (legal, revenue and costs, CIC, review). Approved applications show both documents on the status card (`ListingSigning`), with preview, download, how to sign and upload. The tracker gains a "Sign & publish" step.

## 9. Open Questions

1. **Window length.** The Standing Offer says the window closes at the latest on day 12, with disbursement on day 14 and day 13 for the SME's decision. `DirectTransferParams.window_days` is 14. One of the two needs to change (Edward / Lok).
2. **Revenue share *s*.** Resolved in v1.5: *s* is the LOCKED run's daily repayment rate (D-044, D-060), printed on both documents. Still open: should signing wait for *s* on a run scored before the rate was stored, which prints a blank?
3. **Listing ID.** No listing exists before publication, so the Offer prints the application reference as the Listing ID. Is that acceptable, or should the Listing be created at approval?
4. **Guarantor identity.** The Guarantor is taken to be the signing user (the project owner, eKYC-verified). If the owner and the legal representative differ, should another person be able to sign as Guarantor?
6. **Working days a month.** Contract 3, `DirectTransferParams.working_days_per_month` and this estimate use 21. The grading engine's `grading_params` (`repayment.working_days_per_month`) and `repayment_schedule.calendar.DEFAULT_WORKING_DAYS_PER_MONTH` are still 22. The schedule's own spec (v1.4 D14) says new work should default to 21. Align the engine and calendar defaults (Edward).
8. **The Investor's copy of Contract 5.** Should each Investor's contract (generated from the signed template at commitment, with their details and P filled in) replace or sit beside the mock-signed acceptance the tranche stores today? Who generates the per-Investor contract number? (Next step.)
7. **FundLok's countersignature on the Service Agreement.** Party A has a signature block but no field, and FundLok does not sign today. Should FundLok countersign before or after publishing?
5. **Window record.** Should the funding window's `standing_offers` row point at the SME's signed PDF instead of the mock-signed snapshot? (Edward's module.)
