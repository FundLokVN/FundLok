# Spec: Sign-up acceptance of the Terms & Services and data consent

| Field | Value |
|---|---|
| **Status** | REVIEW |
| **Owner** | Phat |
| **Implementer(s)** | Phat (backend + frontend) |
| **Module** | `app/consent/` (new); one call each in `app/gverify/router.py` and `app/users/router.py` |
| **Version** | 1.0 |
| **Date** | 2026-11-08 |
| **Related ADR** | — |
| **Depends on** | Contract set v0.3, document 1 "Terms & Services and data consent" (Art. 2.3, 3, 6, 10); flow check rows 1–2 |

---

## 1. Context & Goal

Contract 1 is accepted on sign-up by every SME and investor, "with three separate tick boxes and an OTP" (Art. 2.3), after reading the Article 3 disclosures on the same screen. Identity data may go to TPV Datatrust (GVerify eKYC) only after the third box (Art. 10.3). The platform stores which version was accepted and when, as evidence (Art. 6). Today sign-up shows only a passive "by signing up you agree" line and stores nothing.

**Goal:** no SME or investor uses the platform, and no identity data reaches TPV Datatrust, until they have accepted the current Terms version with all three boxes and a one-time code, recorded append-only.

---

## 2. Out of Scope

- The final legal text. v0.3 is a draft pending IBL review; the frontend shows it from one versioned file, and changing `CURRENT_VERSION` makes everyone accept again.
- Withdrawing consent (Art. 10.4) from the UI. The table records `WITHDRAWN` rows; the screen comes later.
- SMS / Zalo OTP. The code goes by email until phone verification ships (§9).
- Consent for Didit (another eKYC processor). Contract 1 names only TPV Datatrust (§9).
- Admins (ADMIN, SYSTEM_ADMIN): FundLok staff, not platform users. They are never asked.

---

## 3. Data Model

### `user_consents` (append-only: rows are inserted, never updated or deleted by the app)

One row per acceptance, the three tick boxes together in one JSONB column.

```
user_consents
├── id           UUID         PK
├── user_id      UUID         NOT NULL → users(id) ON DELETE CASCADE
├── version      TEXT         NOT NULL   -- e.g. "tos-v0.3"
├── consents     JSONB        NOT NULL   -- {"TERMS": true, "DATA_PROCESSING": true, "DATA_SHARING_TPV": true}
├── verified_by  TEXT         NOT NULL   -- "EMAIL_OTP"
├── ip_address   TEXT         nullable
├── user_agent   TEXT         nullable
└── created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
INDEX (user_id, version, created_at)
CHECK ck_user_consents_consents_shape: an object with exactly the keys TERMS,
  DATA_PROCESSING, DATA_SHARING_TPV, each a JSON boolean
```

A user holds the current consent when their **latest** row for `CURRENT_VERSION` has all three boxes `true` (the backend checks each is literally `true`). A later withdrawal (Art. 10.4) is a new row with that box `false`, which ends the consent.

### `consent_codes` (one-time codes)

```
consent_codes
├── id           UUID         PK
├── user_id      UUID         NOT NULL → users(id) ON DELETE CASCADE
├── code_hash    TEXT         NOT NULL   -- HMAC-SHA256(SECRET_KEY, user_id:code); never the code
├── attempts     INTEGER      NOT NULL DEFAULT 0
├── expires_at   TIMESTAMPTZ  NOT NULL   -- created_at + 10 minutes
├── consumed_at  TIMESTAMPTZ  nullable
└── created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
INDEX (user_id, created_at)
```

---

## 4. API Contract

All endpoints need a signed-in user. Errors are `{"detail": "<English>"}` with an `X-Error-Code` header the console translates.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/consent/status` | — | `ConsentStatusOut` |
| POST | `/consent/code` | — | `ConsentCodeOut` (emails a 6-digit code to the account's address) |
| POST | `/consent/accept` | `ConsentAcceptRequest` | `ConsentStatusOut` |

```
ConsentStatusOut     { version: str, required: bool, accepted_at: datetime | null }
ConsentCodeOut       { sent_to: str (masked, e.g. "ph***@fundlok.com"), expires_in_seconds: int,
                       resend_after_seconds: int }
ConsentAcceptRequest { version: str, terms: bool, data_processing: bool, data_sharing_tpv: bool,
                       code: str (6 digits) }
```

`GET /users/me` gains `consent_required: bool` (always false for admins), which the frontend proxy reads to send the user to `/consent`.

**Admin > Users** (admins only, existing endpoints):

- `GET /admin/overview?mode=users`: each `UserRow` gains `consent_status` (`ACCEPTED` | `REQUIRED` | `NOT_APPLICABLE` for admins) and `consent_accepted_at`. One query per page (`DISTINCT ON user_id`), judged exactly as the gate.
- `GET /admin/users/{id}`: gains `consent: { status, current_version, accepted_at, records: [{ id, version, consents, verified_by, ip_address, user_agent, created_at }] }`, records newest first, older versions included.

---

## 5. State Machine

```
NO CONSENT ──(3 boxes + valid code)──▶ CONSENTED (current version)
CONSENTED ──(CURRENT_VERSION changes)──▶ NO CONSENT for the new version
```

---

## 6. Business Rules

1. **Where it happens.** Right after sign-up and email verification, before choosing a role: the proxy sends an SME/investor with `consent_required` to `/consent`, which comes before `/select-role`. Google sign-ups land there too, and existing users on their next visit.
2. **Same screen.** `/consent` shows the Article 3 disclosures in full, the whole Terms in a scrollable panel, and the three tick-box statements of Contract 1, each its own unticked box, Vietnamese with English beneath.
3. **All three boxes are required** to continue, and the request must name `CURRENT_VERSION` (an old page cannot accept a newer text). Missing box → 422; stale version → 409 `CONSENT_VERSION_CHANGED`.
4. **One-time code.** 6 digits from `secrets`, valid 10 minutes, single use, at most 5 wrong tries per code, a new code at most every 60 seconds and 10 a day per account (429 `CONSENT_CODE_RATE_LIMITED`). Sending a new code retires the previous ones. Only an HMAC of the code is stored.
5. **Recording.** One row per acceptance holding the three boxes in `consents`, with version, IP and user agent, plus an audit row `CONSENT_ACCEPTED`. The backend checks all three are `true` before inserting (as well as the request schema), and the database refuses a malformed `consents`. Rows are never updated or deleted by the app.
6. **eKYC gate.** `POST /gverify/kyc/verify`, `POST /gverify/kyc/handoff/verify` and `POST /gverify/kyb/verify` refuse with 403 `CONSENT_REQUIRED` until the current consent is held. Investing and applying need KYC/KYB, so the backend gates every value action transitively.
7. **Role choice.** `PATCH /users/me/role` refuses with 403 `CONSENT_REQUIRED` too, so a new account cannot become an SME or investor without consenting.
8. **Admins are exempt** everywhere above.

---

## 7. Error Cases

| Case | Status | X-Error-Code |
|---|---|---|
| Not signed in | 401 | — |
| A box unticked, code not 6 digits | 422 | — |
| Version is not the current one | 409 | `CONSENT_VERSION_CHANGED` |
| No code sent / code expired / used / retired | 400 | `CONSENT_CODE_EXPIRED` |
| Wrong code | 400 | `CONSENT_CODE_INVALID` |
| Too many wrong tries on this code | 400 | `CONSENT_CODE_EXPIRED` |
| Code requested too soon / too often today | 429 | `CONSENT_CODE_RATE_LIMITED` |
| eKYC or role choice without consent | 403 | `CONSENT_REQUIRED` |

---

## 8. Acceptance Criteria

- `test_a_new_user_must_consent`
- `test_admins_are_never_asked`
- `test_the_code_is_emailed_to_the_account_and_stored_hashed`
- `test_codes_are_rate_limited`
- `test_accepting_records_one_row_with_all_three_boxes`
- `test_consent_counts_only_when_all_three_are_true`
- `test_the_database_refuses_a_malformed_consents_row`
- `test_every_box_is_required`
- `test_a_stale_version_is_refused`
- `test_a_wrong_code_is_refused_and_counts_an_attempt`
- `test_a_code_is_single_use_and_expires`
- `test_a_new_code_retires_the_previous_one`
- `test_a_new_version_asks_again`
- `test_ekyc_needs_consent`
- `test_choosing_a_role_needs_consent`
- `test_users_me_reports_consent_required`
- `test_the_user_list_shows_who_accepted_the_terms` (tests/admin/test_admin_user_consent.py)
- `test_the_user_panel_shows_the_consent_records`
- `test_a_user_who_has_not_accepted_shows_as_required`
- `test_an_admins_panel_says_not_applicable`

---

## 9. Open Questions

- **OTP channel.** Contract 1 says "an OTP" without a channel. Email now; switch to SMS/Zalo when phone verification ships? (IBL)
- **All three boxes required?** Box 3 (TPV sharing) is needed for eKYC, so the service cannot work without it. Is requiring it at sign-up acceptable under Law 91/2025/QH15? (IBL)
- **Head office.** Contract 1 names the head office in Vĩnh Long (enterprise code 1501167629); the company charter says 123 Trương Định, Ho Chi Minh City. One must be updated before the text is final.
- **Didit.** If Didit is still used for any user, its sharing needs its own consent line.
- **Retention.** `ON DELETE CASCADE` matches every other per-user table; if consent evidence must outlive a deleted account, change it before accounts can be deleted.
