# Spec: Phone verification at sign-up (Zalo ZBS OTP)

| Field | Value |
|---|---|
| **Status** | REVIEW |
| **Owner** | Phat |
| **Implementer(s)** | Both (backend in `app/phone_verification/`, frontend `/verify-phone`) |
| **Module** | `app/phone_verification/` |
| **Version** | 1.0 |
| **Date** | 2026-11-04 |
| **Related ADR** | — |
| **Depends on** | Registration (`app/auth/`), Google/Microsoft sign-in (`app/oauth/`), role selection (`PATCH /users/me/role`) |

Touches shared files, which need Edward's agreement: `app/users/models.py` (new column), `app/models/__init__.py` (new model), `app/core/config.py` (settings), `alembic/` (migrations `6849c7e7af26`, `efd8a69b9406`).

---

## 1. Context & Goal

Accounts are created with an email (verified by link, or by Google/Microsoft) and nothing else proven about the person. A verified mobile number gives operations a working contact channel and raises the cost of throwaway accounts. Both sign-up paths already meet at one point, "email verified, no role yet", so the check sits there.

**Goal:** a new account proves a Vietnamese mobile number with a 6-digit one-time code, sent as a Zalo ZBS OTP template message, before it may choose a role.

Parked: the company has no Zalo Official Account yet, so the shipping version is Firebase phone sign-in (`feature/users/phone-verification`). This branch adds the same `users.phone_verified_at` column, so it must be rebased onto that one before it can merge.

---

## 2. Out of Scope

- Phone as a sign-in factor or 2FA method.
- Re-verifying when a number is changed later from the profile page (the change clears the verified flag; it does not prompt for a code yet).
- Gating accounts that already have a role (every account created before this ships, and admins).
- International numbers.

---

## 3. Data Model

```text
users.phone_verified_at   TIMESTAMPTZ NULL   -- when users.phone was proven; NULL = unproven. Cleared when the number changes.

phone_verification_codes
  id           UUID PK
  user_id      UUID FK users.id ON DELETE CASCADE
  phone        TEXT NOT NULL          -- canonical +84xxxxxxxxx
  code_hash    TEXT NOT NULL          -- HMAC-SHA256(SECRET_KEY, "<id>:<code>"); never the code
  channel      TEXT NOT NULL          -- zalo | log
  expires_at   TIMESTAMPTZ NOT NULL   -- created_at + 5 min
  attempts     INTEGER NOT NULL DEFAULT 0
  consumed_at  TIMESTAMPTZ NULL
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
  INDEX (user_id, created_at), INDEX (phone, created_at)
```

Rows are kept after use: they are the send counter for the limits in §6.

```text
zalo_oa_tokens                         -- one row per Zalo app; shared by every instance
  id                 UUID PK
  app_id             TEXT UNIQUE NOT NULL
  access_token       TEXT NOT NULL      -- "v1:<fernet>", ZALO_TOKEN_ENCRYPTION_KEY
  refresh_token      TEXT NOT NULL      -- "v1:<fernet>"; replaced on every refresh
  access_expires_at  TIMESTAMPTZ NOT NULL
  seeded_from        TEXT NOT NULL      -- sha256 of the ZALO_OA_REFRESH_TOKEN that began the chain
  updated_at         TIMESTAMPTZ NOT NULL
```

---

## 4. API Contract

All three require a session (`get_current_user`).

### `POST /users/me/phone/send-code`

```jsonc
// request
{ "phone": "0912 345 678", "turnstile_token": "…" }   // any of 0…, 84…, +84…; spaces, dots, dashes allowed
// 200
{ "phone": "+84912345678", "channel": "zalo", "expires_in": 300, "resend_in": 60 }   // channel: zalo | log
```

### `POST /users/me/phone/verify`

```jsonc
// request
{ "code": "123456" }
// 200: the GET /users/me body, with phone = "+84912345678", phone_verified = true
```

### `GET /users/me` (added fields)

```jsonc
{ "phone_verified": false, "phone_verification_required": true }
```

`phone_verification_required` = `PHONE_VERIFICATION_REQUIRED and role is null and phone_verified_at is null`. The frontend routes on it alone.

### `PATCH /users/me/role`

Refused with 403 while `phone_verification_required` is true.

---

## 5. State Machine

```text
(registered) --email link / provider-verified--> EMAIL_VERIFIED
EMAIL_VERIFIED --send-code--> CODE_SENT --verify ok--> PHONE_VERIFIED --PATCH role--> ROLE_CHOSEN
CODE_SENT --expired | 5 wrong--> EMAIL_VERIFIED (send again)
```

Frontend routes: `/verify-email` → `/verify-phone` → `/select-role`, enforced in `proxy.ts` in that order.

---

## 6. Business Rules

1. Gate applies only to accounts with no role. Accounts with a role are never gated.
2. Vietnamese mobiles only: `(+84|84|0)[35789]` + 8 digits. Stored canonical as `+84…`; sent to Zalo as `84…`.
3. Sending needs a verified email (`403` otherwise) and a Turnstile token when captcha is required.
4. Limits, counted from `phone_verification_codes` under advisory locks: one send per 60 s per account; 5 per rolling 24 h per account; 5 per rolling 24 h per number across all accounts. Refusals are `429` with `Retry-After`.
5. A send that Zalo refuses is rolled back: it neither counts nor starts a cooldown.
6. A code is 6 digits from `secrets`, valid 5 minutes, dies after 5 wrong attempts, and is single use. Only the account's latest code can be redeemed.
7. A number proven by another account cannot be sent to (`400 Phone number already registered`).
8. A proven claim beats a typed one: verifying releases the number from any other account that only typed it at registration (`PHONE_RELEASED` audit row on that account).
9. Success writes `PHONE_VERIFIED` to the audit log.
10. Changing `phone` via `PATCH /users/me` clears `phone_verified_at` unless it is the same number in another spelling.
11. Channels (`PHONE_OTP_CHANNEL`): `zalo` (ZBS OTP template, the code in parameter `ZALO_ZBS_OTP_PARAM`), `log` (server log, only with `DEBUG=True`).
12. No fallback channel. A number Zalo cannot reach at all (number Zalo rejects -108, no Zalo account -118, refuses these messages -139/-141, not eligible -140; also the older ZNS codes -114/-119) gets `400` with a reason the user can act on (use another number); every other Zalo refusal is a `502`, missing configuration a `503`.
13. Zalo OA token: the access token lives ~25 h; each refresh returns a new refresh token and retires the old one. The pair is stored encrypted in `zalo_oa_tokens`; a refresh runs under an advisory lock (re-checking after acquiring it, so instances never double-refresh) and commits in its own transaction, so it is kept even when the send that needed it rolls back. A send answered with -124 (token invalid) refreshes once and retries once. `ZALO_OA_REFRESH_TOKEN` seeds the first refresh; a different value there later (after re-authorising the OA) starts a new chain. Each ZBS send carries the code row's id as `tracking_id`.

Zalo request shapes and error codes from Zalo's ZBS Template Message docs (developers.zalo.me/docs/zbs-template-message; ZBS is the renamed ZNS, same endpoint). `ZALO_ZBS_DEVELOPMENT_MODE=true` adds `mode: "development"` to the body, which delivers only to admins of the app or the OA:
`POST https://business.openapi.zalo.me/message/template`, header `access_token`, body `{phone: "84…", template_id, template_data: {otp}, tracking_id}`, success is `error == 0` (refusals also answer HTTP 200). Token refresh: `POST https://oauth.zaloapp.com/v4/oa/access_token`, form `refresh_token, app_id, grant_type=refresh_token`, header `secret_key: <app secret>`.


---

## 7. Error Cases

| Case | Status | `detail` |
|---|---|---|
| Not a Vietnamese mobile / code not 6 digits | 422 | validation |
| Email not verified | 403 | Verify your email address first |
| Number already verified on this account | 409 | This phone number is already verified |
| Number proven by another account | 400 | Phone number already registered |
| Cooldown | 429 | Please wait before requesting another code |
| Daily limit (account or number) | 429 | Too many codes requested today. Try again later. |
| Number has no Zalo account that can receive (rule 12) | 400 | This number has no Zalo account that can receive the code |
| Zalo refused / unreachable | 502 | Could not send the verification code. Please try again. |
| Zalo not configured, or `log` without DEBUG | 503 | Phone verification is not available right now |
| Wrong code | 400 | Incorrect code |
| 5th wrong code | 400 | Too many incorrect attempts. Request a new code. |
| No live code (expired, used, exhausted, none) | 400 | This code has expired. Request a new one. |
| Role chosen before verifying | 403 | Verify your phone number before choosing a role |

---

## 8. Acceptance Criteria

Tests: `tests/phone_verification/test_phone_verification.py` and `test_zalo.py`; frontend `proxy.test.ts` ("the phone gate") and `lib/phone-verification.test.ts`.

- `test_send_code_sends_a_code_by_zalo`
- `test_send_code_requires_sign_in`, `test_send_code_requires_verified_email`
- `test_send_code_rejects_non_vietnamese_mobiles`, `test_send_code_accepts_every_vietnamese_spelling`
- `test_resend_waits_out_the_cooldown`, `test_daily_limit_per_account`, `test_daily_limit_per_number_across_accounts`
- `test_number_proven_by_another_account_cannot_be_sent_to`
- `test_zalo_failure_returns_502_and_is_not_counted`, `test_a_number_without_zalo_is_told_so`, `test_missing_zalo_configuration_returns_503`, `test_log_channel_only_with_debug`
- `test_correct_code_verifies_the_phone`, `test_wrong_code_is_refused_and_counted`, `test_code_dies_after_five_wrong_attempts`, `test_expired_code_is_refused`, `test_only_the_latest_code_counts`, `test_code_cannot_be_used_twice`, `test_malformed_code_is_rejected`
- `test_verifying_releases_the_number_from_an_unverified_holder`
- `test_role_selection_waits_for_phone_verification`, `test_google_sign_up_is_gated_the_same_way`, `test_users_with_a_role_are_never_gated`, `test_gate_is_off_when_not_required`
- `test_changing_the_profile_number_clears_verification`
- Zalo: `test_zalo_sends_the_code_through_the_otp_template`, `test_template_parameter_name_is_configurable`
- Zalo token: `test_tokens_are_stored_encrypted`, `test_a_live_token_is_reused`, `test_an_expiring_token_is_refreshed_with_the_rotated_refresh_token`, `test_a_rejected_token_is_refreshed_once_and_retried`, `test_a_new_seed_in_the_environment_starts_a_new_chain`, `test_the_token_survives_a_failed_send`
- Zalo refusals: `test_a_number_without_zalo_is_refused_with_a_reason`, `test_other_zalo_refusals_are_a_502`, `test_missing_zalo_configuration_is_a_503`, `test_a_refused_refresh_is_a_502`, `test_no_refresh_token_anywhere_is_a_503`
- Zalo HTTP: `test_post_sends_headers_and_body_as_given`, `test_post_raises_on_http_failure`

---

## 9. Open Questions

1. **Zalo OA:** needs a verified (paid) Official Account with ZBS enabled and an approved OTP-type template. Not available yet.
2. **Foreign investors:** codes go to Vietnamese mobiles only. Do we need international numbers?
3. **Profile edits:** should changing the number on the profile page require a new code instead of just clearing the flag?
4. **Zalo development mode:** Zalo is said to accept a development-mode send (free, delivered only to the OA's admins), but its request field could not be confirmed from a source, so it is not implemented. Test with a real OTP template instead, or add it once confirmed.
