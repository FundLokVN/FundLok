# Spec: The Personal Guarantor signs by Zalo one-time code

| Field | Value |
|---|---|
| **Status** | DRAFT (built with the spec; needs Edward's review of the migration) |
| **Owner** | Phat |
| **Implementer(s)** | Phat (backend `app/loans/`, frontend) |
| **Module** | `app/loans/guarantor_otp.py`, `app/loans/guarantor_models.py`, `app/loans/signing.py`, `app/loans/router.py` |
| **Version** | 1.0 |
| **Date** | 2026-11-18 |
| **Source** | Money flow FINAL v1.0 (Loc, locked 17 Nov 2026): section 1 Day 0 and Day 13; ops rule "Guarantor ready before Day 0" |
| **Depends on** | `docs/specs/loans/listing-signing.md` v1.4 (the SME's signed uploads, unchanged), the Zalo OTP template (`zalo.send_otp`, `ZALO_ZBS_OTP_TEMPLATE_ID`) |

---

## 1. Context & Goal

Listing signing v1.4 asks the Personal Guarantor for a **personal digital signature** in the Standing Offer and the Loan Contract template PDFs. Money flow FINAL v1.0 says otherwise:

- Day 0: *"The guarantor (the owner, or the largest capital holder) signs the standing offer by OTP sent by Zalo; the SME signs with its digital signature. The listing goes live."*
- Day 13: *"the guarantor signs each [loan contract] by OTP sent by Zalo (its signature is not a condition of acceptance)."*
- Ops rule: *"Day-0 signing opens only when the personal guarantor has its own FundLok account (three tick boxes and a Zalo OTP) and eKYC APPROVED through TPV Datatrust. If the guarantor is the signed-up owner, this is already met."*

**Goal:** the guarantor signs with a one-time code sent by Zalo through the OTP template FundLok already uses for phone verification and the investor's agreement. One code signs every document of a step; what it signed is kept by hash.

## 2. Out of Scope

- A guarantor who is not the signed-up SME user (the largest capital holder with an account of their own). Today the guarantor is the signing user, as their approved eKYC names them (`standing_offer.guarantor_for`), so their account's phone gets the code. A separate guarantor account is a later step.
- The contract wording. Contracts 3 and 5 (v0.4) still describe a personal digital signature in their signing clause and keep their `Guarantor_Signature` box; the text is updated with the FINAL v1.0 contract set, which prevails (open question 1).
- Day 13 signing of each investor's loan contract uses the same mechanism (`WINDOW_LOAN_CONTRACTS`) and is in the Day-13 PR.

## 3. Data Model

Migration `c5e8a1f3d920` (off `d14a7c2e5b31`), new tables only.

```
guarantor_signing_codes
├── id            UUID PK
├── user_id       UUID NOT NULL → users(id)
├── purpose       TEXT NOT NULL CHECK IN ('LISTING_DOCUMENTS','WINDOW_LOAN_CONTRACTS')
├── subject_id    UUID NOT NULL     -- the loan application (Day 0) or the funding window (Day 13)
├── phone         TEXT NOT NULL     -- canonical +84…
├── code_hash     TEXT NOT NULL     -- HMAC(SECRET_KEY, row id, purpose, subject, code)
├── channel       TEXT NOT NULL     -- zalo | log (DEBUG only)
├── expires_at    TIMESTAMPTZ NOT NULL
├── attempts      INT NOT NULL default 0
├── consumed_at   TIMESTAMPTZ
└── created_at    TIMESTAMPTZ NOT NULL default now()
Index (user_id, created_at)

guarantor_signatures
├── id                UUID PK
├── purpose, subject_id                 UNIQUE together: one signature per step
├── signer_user_id    UUID NOT NULL → users(id)
├── signer_name       TEXT
├── signer_id_number  TEXT NOT NULL     -- the approved eKYC's ID number
├── phone             TEXT NOT NULL
├── code_id           UUID NOT NULL → guarantor_signing_codes(id)
├── documents         JSONB NOT NULL    -- [{document_type, sha256, check_code}] exactly what was signed
├── signed_at         TIMESTAMPTZ NOT NULL
├── ip_address, user_agent  TEXT
└── created_at        TIMESTAMPTZ default now()
```
Each signature also writes an audit row `GUARANTOR_SIGNED_BY_ZALO_OTP`.

## 4. API Contract

Both routes: SME, the application's owner (`owned_application`).

`POST /loans/applications/{application_id}/guarantor-signature/code` → `200 { "phone": "0903***489", "channel": "zalo", "expires_in": 300, "resend_in": 60 }`

`POST /loans/applications/{application_id}/guarantor-signature` `{ "code": "123456" }` → `200 ListingDocumentsOut`

`ListingDocumentsOut` gains:
```json
{
  "guarantor_missing": ["EKYC", "PHONE"],      // empty when the guarantor is ready
  "guarantor_signed_by": "ZALO_OTP | DIGITAL_SIGNATURE | null",
  "guarantor_signed_at": "iso | null",
  "guarantor_phone": "0903***489 | null"        // where the code goes
}
```

| Status | Code | When |
|---|---|---|
| 409 | `SIGNING_NOT_OPEN` | not approved with locked terms |
| 409 | `SIGNING_GUARANTOR_NOT_READY` | no approved eKYC with an ID number, or no verified phone (both routes, and every upload) |
| 409 | `GUARANTOR_SIGNS_AFTER_UPLOAD` | the SME's signed Standing Offer and Loan Contract are not both on file |
| 409 | `GUARANTOR_ALREADY_SIGNED` | signed already (by code or in the PDF) |
| 409 | `PHONE_REQUIRED` | no verified Vietnamese mobile |
| 429 | `CODE_COOLDOWN`, `CODE_DAILY_LIMIT` | one a minute, five a day (`Retry-After`) |
| 400 | `PHONE_NO_ZALO` | the number has no Zalo account that can receive it |
| 502 / 503 | `CODE_NOT_SENT` / `CODE_UNAVAILABLE` | Zalo refused or is not configured |
| 400 | `CODE_EXPIRED`, `CODE_INCORRECT`, `CODE_TOO_MANY_ATTEMPTS` | no live code for this step, a wrong guess (counted), the fifth wrong guess |

## 5. State Machine

```
SME uploads SA, SO, LC (enterprise signature only)  ──►  guarantor code sent  ──►  code redeemed  ──►  project ACTIVE
                                                     (or SO and LC already carry the guarantor's own signature ──► ACTIVE)
```

## 6. Business Rules

1. **Guarantor ready.** No upload is accepted, and no code is sent, until the guarantor (the signing user) has an approved eKYC with an ID number and a verified phone.
2. **SME signs in the PDF; the guarantor by code.** The Standing Offer and the Loan Contract template no longer need a second signed field or a NEAC-checked personal signature of the guarantor. A copy that carries one still records it (`guarantor_signs_by = DIGITAL_SIGNATURE`) and counts as the guarantor's signature: copies signed before 18 Nov keep working.
3. **One code, both documents.** The code signs the SME-signed Standing Offer and Loan Contract on file; their SHA-256 and check codes are kept with the signature.
4. **Publishing.** The project goes live when the three documents are on file **and** the guarantor has signed (`signing.all_signed`).
5. **Code rules** as the investor's agreement: five minutes, one a minute, five a day, five attempts, single use, bound to the step and the subject, a new code retires the last.

## 7. Error Cases

See section 4. A wrong guess is committed before the 400 is returned, so attempts count.

## 8. Acceptance Criteria

`tests/loans/test_listing_signing.py`:
- [x] `test_the_guarantor_signs_by_zalo_code_and_that_publishes_the_project`
- [x] `test_the_guarantor_signs_only_after_the_sme_signed_copies_are_on_file`
- [x] `test_a_code_for_another_listing_does_not_sign_this_one`
- [x] `test_a_copy_with_the_guarantors_own_digital_signature_still_counts`
- [x] `test_signing_waits_for_a_ready_guarantor`

## 9. Open Questions

| # | Question | Owner | Default used |
|---|---|---|---|
| 1 | The FINAL v1.0 wording of Contract 3 and 5's signing clause for a guarantor who signs by Zalo code | Loc | Contract text unchanged (v0.4); the code record is the guarantor's signature |
| 2 | A guarantor who is not the signed-up owner (largest capital holder): their own account and phone | Loc, Phat | Not supported yet: the guarantor is the signing user |

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-11-18 | Phat / Claude | First version, from Money flow FINAL v1.0 |
