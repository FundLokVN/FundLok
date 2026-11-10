# Spec: Listing documents signed after approval (SME Service Agreement + Standing Offer)

| Field | Value |
|---|---|
| **Status** | DRAFT (built ahead of review; needs Edward's review of the migration and the activation gate) |
| **Owner** | Phat |
| **Implementer(s)** | Phat (backend `app/loans/`, `app/projects/`, frontend) |
| **Module** | `app/loans/signing.py`, `app/loans/agreement/` (`standing_offer*.py`, `terms.py`), `app/projects/service.py` (`activate_project_if_approved`) |
| **Version** | 1.2 (1.2: the admin panel shows and re-checks both signed documents with NEAC, the guarantor and the check code included; 1.1: the Standing Offer carries the assessment result and repayment estimate; both documents carry empty signature fields, one per signer, and a check code; the upload checks both automatically) |
| **Date** | 2026-11-10 |
| **Related ADR** | ADR-009 |
| **Depends on** | `docs/specs/market/investor-service-agreement.md` (the NEAC check this reuses), `docs/specs/direct_transfer/funding-window.md` (rule 1: the standing offer), the Contract set (documents 2 and 3) |

---

## 1. Context & Goal

The SME Service Agreement (Contract 2) used to be step 4 of the loan application wizard, signed before FundLok had assessed anything, so its Annex said the rate would come later. Both contracts say otherwise. Contract 2: *"Signed: when the SME accepts its listing terms after FundLok's assessment"*. Contract 3, the Standing Offer to Conclude Loan Contracts: *"Signed: by the SME with its enterprise digital signature and by the Personal Guarantor with a personal digital signature, at the same time as the SME Service Agreement, once the assessment is done and the SME agrees to the loan terms; signing publishes the Listing."*

So the wizard no longer asks for any signature. Once FundLok approves a request (the operator's approval and a LOCKED score run, which fixes the rate), the SME's dashboard offers both documents, filled in with the approved terms. The SME signs both and uploads them back, and the Personal Guarantor also signs the Standing Offer. When both are verified, the project goes live, and that is what publishes the listing.

## 2. Out of Scope

- Linking the funding window's `standing_offers` row to the SME's signed PDF. The window still stores the machine-readable terms snapshot, signed with the mock signer (`app/direct_transfer`, Edward's module; funding-window.md rule 1, I-25).
- The revenue share for extensions (*s*), which the Standing Offer says the system fills in per Listing. Nothing computes it yet, so it prints as a dotted blank.
- Remote signing inside FundLok. The SME signs outside the platform (USB token or the CA's remote signing) and uploads the PDF.

## 3. Data Model

No new table. Each signed document is a `loan_application_documents` row, as the Service Agreement already was:

| document_type | Contract | Signatures required |
|---|---|---|
| `sme_service_agreement` | 2, SME Service Agreement | the company's (MST = approved KYB enterprise code, else the project's tax ID) |
| `sme_standing_offer` (new; migration `a37775d5b929` admits it to the check constraint) | 3, Standing Offer | the company's **and** a separate personal signature by the Guarantor (CCCD = the signing user's approved eKYC `person_number`) |

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
| Check code (every page) | over everything it is filled in with except the date prepared | over the SME, the Guarantor, the terms, the assessment, the estimate and the minimum ticket |

The check code (`pdf.check_code`) is the first 12 hex digits of a SHA-256 over those values, printed as `XXXX-XXXX-XXXX`.

## 4. API Contract

All routes are SME-only and limited to the application's owner (403 otherwise, 404 for an unknown application).

| Method | Path | Returns |
|---|---|---|
| GET | `/loans/applications/{id}/service-agreement` | PDF (unchanged route; the Annex now prints the approved rate once the terms are locked) |
| GET | `/loans/applications/{id}/standing-offer` | PDF; 409 `SIGNING_NOT_OPEN` until the terms are locked |
| GET | `/loans/applications/{id}/listing-documents` | `ListingDocumentsOut` |
| POST | `/loans/applications/{id}/listing-documents/{kind}` (multipart `file`; `kind` = `service-agreement` \| `standing-offer`) | `ListingDocumentsOut` after the upload; 404 for any other kind |
| GET | `/loans/applications/{id}/listing-documents/{kind}/signed` | the signed PDF as uploaded; 404 when none |

```python
class SignedListingDocumentOut(BaseModel):
    signed: bool
    signed_at: datetime | None
    file_name: str | None

class ListingDocumentsOut(BaseModel):
    ready: bool               # approved with locked terms: there is something to sign
    published: bool           # project.status == "ACTIVE"
    guarantor_verified: bool  # the signing user has an approved eKYC with an ID number
    service_agreement: SignedListingDocumentOut
    standing_offer: SignedListingDocumentOut
```

`SignedListingDocumentOut` also carries `checks` (null until a verified upload): `checked_at`, `verified_by` ("NEAC"), `terms_match`, and `signers` as `[{role: "SME" | "GUARANTOR", name}]`. These come from the document's newest verdict, and the SME's signing card shows them.

Admin: the project panel shows each signed document's newest verdict (`signature_check`, from the upload or a later re-check), with `POST /admin/documents/{id}/neac-check` re-checking either type (`app/loans/agreement/neac_check.check_signed_document`). A re-check covers the same ground as the upload:
- right document and today's check code: `terms_match` is `null` for a copy that has no code, such as one signed in the old wizard;
- NEAC's verdict and the company's tax code;
- on the Standing Offer, a separate valid signature from the Personal Guarantor (`guarantor_*`), matched against the eKYC ID of the user who uploaded it.

`AdminSignatureCheck` gains `document_type`, `terms_match`, `check_code`, `guarantor_required`, `guarantor_matches`, `guarantor_expected_id`, `guarantor_expected_name`, `guarantor_name`, `guarantor_id`, `guarantor_name_matches` and `guarantor_problems`.

`POST /uploads/init-upload` refuses both signed types (400 `SIGNED_AFTER_APPROVAL`), so neither can arrive without the NEAC check. `POST /loans/applications/{id}/submit` no longer requires a signed agreement.

## 5. State Machine

```
Project:  DRAFT ──(admin APPROVED + score run LOCKED + both documents signed)──▶ ACTIVE
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
7. Investors are never shown either signed document (`verified_document_types` excludes both). The Standing Offer carries the Guarantor's personal details.
8. Projects that went live before this change stay live. Approved projects still in DRAFT now need both signatures, including ones whose Service Agreement was signed in the old wizard: that row counts, and only the Standing Offer is left.

## 7. Error Cases

| HTTP | X-Error-Code | When |
|---|---|---|
| 409 | `SIGNING_NOT_OPEN` | terms not locked yet |
| 409 | `SIGNING_ALREADY_SIGNED` | this document is already on file |
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
- admin (`tests/admin/test_sme_agreement_neac_check.py`):
  - `test_a_standing_offer_signed_by_the_company_and_the_guarantor_is_verified`
  - `test_a_standing_offer_without_the_guarantors_own_signature_is_flagged`
  - `test_a_copy_whose_details_changed_since_signing_no_longer_matches`
- `test_both_approvals_without_the_signed_documents_do_not_activate`
- `test_the_second_signature_activates_an_approved_project`

Frontend: the wizard has four steps (legal, revenue and costs, CIC, review). Approved applications show both documents on the status card (`ListingSigning`), with preview, download, how to sign and upload. The tracker gains a "Sign & publish" step.

## 9. Open Questions

1. **Window length.** The Standing Offer says the window closes at the latest on day 12, with disbursement on day 14 and day 13 for the SME's decision. `DirectTransferParams.window_days` is 14. One of the two needs to change (Edward / Lok).
2. **Revenue share *s*.** Who sets it per Listing, and when? Until something does, the signed Offer carries a blank.
3. **Listing ID.** No listing exists before publication, so the Offer prints the application reference as the Listing ID. Is that acceptable, or should the Listing be created at approval?
4. **Guarantor identity.** The Guarantor is taken to be the signing user (the project owner, eKYC-verified). If the owner and the legal representative differ, should another person be able to sign as Guarantor?
6. **Working days a month.** Contract 3, `DirectTransferParams.working_days_per_month` and this estimate use 21. The grading engine's `grading_params` (`repayment.working_days_per_month`) and `repayment_schedule.calendar.DEFAULT_WORKING_DAYS_PER_MONTH` are still 22. The schedule's own spec (v1.4 D14) says new work should default to 21. Align the engine and calendar defaults (Edward).
7. **FundLok's countersignature on the Service Agreement.** Party A has a signature block but no field, and FundLok does not sign today. Should FundLok countersign before or after publishing?
5. **Window record.** Should the funding window's `standing_offers` row point at the SME's signed PDF instead of the mock-signed snapshot? (Edward's module.)
