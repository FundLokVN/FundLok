# Spec: Internal email (admin console)

| Field | Value |
|---|---|
| **Status** | IMPLEMENTED |
| **Owner** | Phat |
| **Implementer(s)** | Phat (backend + frontend) |
| **Module** | `app/admin/internal_email/`, `emails/` (React Email templates), `app/utils/email.py` |
| **Version** | 2.0 (Google OAuth 2.0 + Gmail API replace SMTP + App Password) |
| **Date** | 2026-11-07 (v1.2: 2026-10-07) |
| **Related ADR** | — |
| **Depends on** | — |

---

## 1. Context & Goal

Admins need to send company email (announcements, reminders, one-off notes)
without leaving the admin console. **Every admin** (ADMIN and SYSTEM_ADMIN)
connects **their own** Gmail / Google Workspace account with **Google OAuth
2.0** ("Connect with Google", scope `gmail.send`), and their email goes out from
that address through the **Gmail API**. They then compose a message: recipients
(typed, picked from the system's users, or "add all FundLok emails" in one
click), a template, a subject and the content. Each recipient gets their own
copy. A system admin also sets the recipient domains every admin may send to,
and can see every admin's sends.

v2.0 replaced the v1 App Password + Gmail SMTP connection: an App Password is a
full mailbox credential (it can also read mail over IMAP), it needs 2-Step
Verification and manual copying, and Google is retiring it for Workspace. OAuth
grants only "send", is revocable from the Google account, and the address comes
from Google rather than being typed.

All of FundLok's email templates, the platform emails included, are written in
**React Email** (`emails/`) and exported to static HTML at build time. Python
fills them in with Jinja, so the API needs no Node at runtime.

**Goal:** each admin can connect their own Google account safely, send a test
email, and compose and send templated email from their own address, with every
send on record.

---

## 2. Out of Scope

- **Platform emails are unchanged.** Verification, password reset, invites and
  notices still leave through the SMTP settings in the environment (`SMTP_*`,
  `app/utils/email.py`: mailpit locally, the configured relay in production).
  This spec covers only Admin > Email.
- App Passwords / Gmail SMTP for Admin > Email (removed in v2.0), service
  accounts and domain-wide delegation.
- Attachments, CC/BCC, scheduled sends, mailing lists, open tracking.
- HTML or Markdown authored by the admin beyond the sanitised subset (§6.7).
- More than one connected account per admin, or sending from another admin's
  account.

---

## 3. Data Model

### Table: `email_settings` (one row per admin)

```
email_settings
├── id                       UUID         PK
├── user_id                  UUID         NOT NULL, UNIQUE → users(id) ON DELETE CASCADE
├── gmail_address            TEXT         nullable   -- From address, from Google's verified ID token
├── from_name                TEXT         NOT NULL   -- display name on From
├── refresh_token_encrypted  TEXT         nullable   -- Google OAuth refresh token, Fernet "v1:<token>"; never returned; null = not connected
├── oauth_scopes             TEXT         nullable   -- space-separated scopes Google granted
├── token_updated_at         TIMESTAMPTZ  nullable   -- when the stored token was granted
├── enabled                  BOOLEAN      NOT NULL DEFAULT FALSE
├── last_verified_at         TIMESTAMPTZ  nullable   -- last successful test email with the current token
├── signature_name/title/phone TEXT       nullable   -- appended when "Include my email signature" is ticked
├── created_at               TIMESTAMPTZ  DEFAULT NOW()
└── updated_at               TIMESTAMPTZ  DEFAULT NOW()
```

Migration `c4e8a1f7b2d9` (v2.0) adds the three OAuth columns, makes
`gmail_address` nullable, clears `last_verified_at` and **drops
`app_password_encrypted`**. Sender name, signature and `enabled` are kept, and
so is the old address, so the console shows "Reconnect" for it. Every admin
connects once with Google after the upgrade.

A row is only ever read or written for the signed-in admin: the service looks
it up by the caller's user id (on the OAuth callback, the id in the signed
state that admin's browser started), never by an id from a request body.

### Org-wide policy: `system_settings` key `internal_email_policy`

```
{ "allowed_domains": ["fundlok.com", ...] }   -- empty = any domain
```

Set by a SYSTEM_ADMIN. It applies to every admin's sends. A per-admin list
would restrict nothing, since each admin could widen their own.

### Table: `internal_emails` (send log, one row per send attempt)

```
internal_emails
├── id             UUID         PK
├── sent_by        UUID         nullable → users(id) ON DELETE SET NULL
├── from_address   TEXT         NOT NULL
├── recipients     TEXT[]       NOT NULL
├── template       TEXT         NOT NULL   -- general | announcement | action_required | test
├── subject        TEXT         NOT NULL
├── content        JSONB        NOT NULL   -- heading, body, button_label, button_url, include_signature
├── status         TEXT         NOT NULL, CHECK IN ('PENDING', 'SENT', 'FAILED')
├── error_code     TEXT         nullable
├── undelivered    TEXT[]       NOT NULL DEFAULT '{}'  -- recipients whose copy did not go out (migration 11d74e4c16bf)
├── created_at     TIMESTAMPTZ  DEFAULT NOW()
└── sent_at        TIMESTAMPTZ  nullable
INDEX (sent_by, created_at)
```

The row is written as `PENDING` and committed **before** contacting Google, then
moved to `SENT` or `FAILED`. A crash mid-send leaves a `PENDING` row rather than
no record, and pending rows count toward the rate limit.

### Encryption at rest

`refresh_token_encrypted` uses Fernet under `EMAIL_SETTINGS_ENCRYPTION_KEY`
(`crypto.encrypt_secret` / `decrypt_secret`), the same scheme as
`app/auth/totp_crypto.py`, and a separate key from it and from `SECRET_KEY`.
With no key configured, connecting fails closed (the grant is revoked at Google
and nothing is stored). Rotating the key makes stored tokens unreadable, which
the service treats as "reconnect with Google". Generate one with:

```
python -c "from app.admin.internal_email.crypto import generate_encryption_key as g; print(g())"
```

### Configuration

| Variable | Meaning |
|---|---|
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | A "Web application" OAuth client; the Gmail API enabled on its project. The id falls back to the sign-in client's `GOOGLE_CLIENT_ID`, so reusing that client needs only its secret. No id or no secret = Connect answers 503 `GMAIL_OAUTH_NOT_CONFIGURED`. |
| `GOOGLE_OAUTH_REDIRECT_URI` | Defaults to `{FRONTEND_URL}/api/admin/email/oauth/callback` (Google → frontend `/api` proxy → this API). Must be registered on the client exactly. |
| `GMAIL_CONNECT_ALLOWED_DOMAINS` | Comma-separated domains a connected account must be on (e.g. `fundlok.com`). Empty = any. |

---

## 4. API Contract

Every endpoint is **admins only** (ADMIN and SYSTEM_ADMIN; 403 for everyone
else, 401 if not signed in), except the OAuth callback (below). Only
`PUT /admin/email/policy` is SYSTEM_ADMIN only. `/settings` always means the
signed-in admin's own. Errors are `{"detail": "<English>"}`. Failures the UI
translates also carry an `X-Error-Code` header.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/admin/email/settings` | — | `EmailSettingsOut` |
| PUT | `/admin/email/settings` | `EmailSettingsUpdate` | `EmailSettingsOut` |
| GET | `/admin/email/oauth/authorize` | — | `EmailOAuthStart` + binding cookie |
| GET | `/admin/email/oauth/callback?code=&state=&error=` | — (Google's redirect) | 303 → `{FRONTEND_URL}/admin/email?connected=true` or `?error=<code>` |
| POST | `/admin/email/disconnect` | — | `EmailSettingsOut` |
| POST | `/admin/email/settings/test` | — | `EmailTestResult` |
| POST | `/admin/email/preview` | `EmailContent` | `EmailPreview` |
| POST | `/admin/email/send` | `SendEmailRequest` | `InternalEmailOut` |
| GET | `/admin/email/messages?limit=20` | — | `list[InternalEmailOut]` (own; SYSTEM_ADMIN: everyone's) |
| GET | `/admin/email/recipients?q=&fundlok_only=&limit=200` | — | `list[RecipientOut]` |
| GET | `/admin/email/policy` | — | `EmailPolicyOut` |
| PUT | `/admin/email/policy` | `EmailPolicyUpdate` (SYSTEM_ADMIN) | `EmailPolicyOut` |

v2.0 removed `DELETE /admin/email/settings` (use `POST /disconnect`) and the
`gmail_address` / `app_password` fields of `EmailSettingsUpdate`.

```
EmailSettingsOut      { configured: bool,            -- a Google token is stored
                        enabled: bool, gmail_address: str | null, from_name: str | null,
                        needs_reconnect: bool,       -- an address but no token: Google revoked
                                                     -- access, or migrated from App Password
                        connected_at: datetime | null,
                        last_verified_at: datetime | null, updated_at: datetime | null,
                        signature_name/title/phone: str | null }
EmailSettingsUpdate   { from_name: str (1–80), enabled: bool,
                        signature_name: str | null (≤ 80), signature_title: str | null (≤ 120),
                        signature_phone: str | null (≤ 40) }      -- extra fields → 422
EmailOAuthStart       { authorization_url: str }
EmailPolicyOut        { allowed_domains: str[], updated_at: datetime | null, updated_by: UUID | null }
EmailPolicyUpdate     { allowed_domains: str[] (≤ 20) }
EmailTestResult       { sent_to: str, last_verified_at: datetime }
EmailContent          { template: "general" | "announcement" | "action_required",
                        subject: str (1–200), heading: str | null (≤ 200),
                        body: str (1–10 000), button_label: str | null (≤ 60),
                        button_url: https URL | null (≤ 2 000), include_signature: bool }
SendEmailRequest      EmailContent + { to: email[] (1–50) }
RecipientOut          { id, full_name: str | null, email, role: str | null, is_fundlok: bool }
EmailPreview          { subject: str, html: str }
InternalEmailOut      { id, sent_by: UUID | null, from_address, recipients: str[], template,
                        subject, status, error_code: str | null, undelivered: str[],
                        created_at, sent_at }
```

### The OAuth flow

1. The console calls `GET /oauth/authorize` and sends the browser to
   `authorization_url`: Google's `o/oauth2/v2/auth` with `response_type=code`,
   `scope=openid email profile https://www.googleapis.com/auth/gmail.send`,
   `access_type=offline`, `prompt=consent` (a refresh token on every connect,
   reconnects included), `include_granted_scopes=true`, a PKCE S256
   `code_challenge`, and `state`.
2. `state` is a JWT signed with `SECRET_KEY`: the admin's id, `purpose:
   gmail_oauth_state` (so it is never accepted as a session), the PKCE
   challenge, 10-minute expiry. The PKCE **verifier** is set in an httpOnly,
   SameSite=Lax cookie `gmail_oauth_binding` with **no Path attribute**, so the
   browser scopes it to `/api/admin/email/oauth` and returns it only to the
   callback.
3. Google redirects to `GET /oauth/callback`. It is not a session endpoint: the
   admin is the one in `state`, and the browser must hold the cookie whose hash
   is the challenge in `state` (a state lifted from a URL is useless elsewhere;
   an expired access token during consent does not fail the connect). The admin
   must still be ADMIN/SYSTEM_ADMIN and not suspended.
4. The code is exchanged at `oauth2.googleapis.com/token` with the verifier and
   client secret. The ID token (received directly over TLS) gives the address;
   issuer, audience and expiry are checked. Only the refresh token is stored,
   encrypted. The callback always ends in a 303 to Admin > Email and clears the
   binding cookie.

The test email always goes to **the signed-in admin's own address**, and never
to an address supplied in the request. A successful test marks the settings
verified only if they still hold the token that was tested: a reconnect during
the Google call is not marked verified by mistake.

---

## 5. State Machine

Send log:

```
PENDING ──(Gmail accepted)──▶ SENT
   └─────(any failure)──────▶ FAILED
```

Connection (`email_settings`):

```
NONE ──connect──▶ CONNECTED ──disconnect──▶ NONE (address cleared, sending off)
                     │  ▲
   Google refuses    │  │ connect
   the token         ▼  │
                 NEEDS_RECONNECT (token cleared, address + switch kept)
```

---

## 6. Business Rules

1. **Fixed hosts.** Google's authorize, token, revoke and Gmail send URLs are
   constants (`google.py`), not settings, so nothing on the settings screen can
   point the server at another host. All calls use `httpx` with a 20 s timeout.
2. **Scope and identity check.** Google's consent screen lets the admin untick
   "Send email on your behalf". A grant without `gmail.send`, without a verified
   email, outside `GMAIL_CONNECT_ALLOWED_DOMAINS`, whose email does not match
   the signed-in admin's account email, or without a refresh token is refused
   and revoked at Google; nothing is stored.
3. **Reconnecting.** The same Google account: the new token replaces the old,
   and the old one is **not** revoked (Google revokes the whole grant, which
   would kill the new token too). A different account: the old account's
   token is revoked after the new one is saved. Either way `last_verified_at`
   is cleared; sender name, signature and the switch are kept.
4. **Enabling needs a connection.** Turning sending on (off → on) without a
   stored token is a 400. Leaving it on is allowed, so a revoked admin can still
   edit their signature; sending itself still needs the token.
5. **The secret never leaves the server.** The refresh token is not returned,
   logged or written to the audit log (only `connected: bool`). Access tokens
   live only in memory for one send. Logs carry Google's status and error code,
   never a token or Google's response body.
6. **Audit.** `EMAIL_SETTINGS_UPDATED`, `EMAIL_GMAIL_CONNECTED`,
   `EMAIL_GMAIL_DISCONNECTED` and `EMAIL_GMAIL_ACCESS_REVOKED` (entity = the
   admin) record the address, name, enabled flag, signature and whether a token
   is held. `EMAIL_POLICY_UPDATED` records the allowed domains before and after.
   `INTERNAL_EMAIL_SENT` records the send-log id, the recipient count and the
   status.
7. **Content.** The body allows a sanitised subset (bold, italic, underline,
   links to http(s)/mailto, lists, line breaks); everything else is escaped.
   The button URL must be `https://`. CR/LF are rejected in the subject,
   heading, from name, signature and button label.
8. **Recipients.** 1–50 per send, de-duplicated. **One message per recipient**,
   each addressed to that person alone: recipients can be customers picked
   from the directory, and none may see another's address. Each send refreshes
   the access token once, then posts each message (RFC 2822, base64url) to
   `users.messages.send`. A 400 for one message marks that recipient
   undelivered; a 5xx/429/rate-limit/network failure stops the loop and leaves
   the rest undelivered; a 25 s budget does the same. The send is `SENT` if
   anyone got it (`SOME_RECIPIENTS_REFUSED` when some did not), `FAILED` if
   nobody did. When the policy's `allowed_domains` is non-empty, each
   recipient's domain (in punycode) must be in it, compared exactly.
9. **Rate limit.** 200 **recipients** per admin per rolling hour (the sum of
   each logged send's recipients; a test email counts 1; a failed send 1). The
   count and the PENDING insert run under a per-admin, transaction-scoped
   advisory lock (`pg_advisory_xact_lock`), so concurrent requests cannot all
   read the same count and pass. The lock is released before Google is
   contacted.
10. **Gating.** Sending needs the admin's own account `configured && enabled`.
    Otherwise 409.
11. **Revocation by Google.** `invalid_grant` on refresh, a 401 from Gmail, or
    an insufficient-scope 403 means the connection is dead: the send is logged
    `FAILED` with `GMAIL_RECONNECT_REQUIRED`, the token is cleared (only if the
    row still holds that same token) and the response is 409. An outage
    (network, 5xx) is `GMAIL_UNAVAILABLE` and keeps the token.
12. **Disconnect.** Clears the token, scopes, address and verification, turns
    sending off, keeps sender name and signature, then revokes the token at
    Google best effort (after the commit; a Google failure does not fail it).
13. **Isolation.** An admin can only read, change, test, send with or
    disconnect their own account. Deleting the user deletes their settings and
    stored token.
14. **Oversight.** `GET /messages` returns an admin's own sends; for a
    SYSTEM_ADMIN it returns every admin's.
15. **Recipient directory.** `GET /recipients` lists active users with a
    verified email (an unverified address may be a typo or someone else's),
    searched by name or email (LIKE wildcards escaped). `fundlok_only` keeps
    addresses on `FUNDLOK_EMAIL_DOMAINS` (`fundlok.com`, `fundlok.vn`). Admins
    already see every user in the console, so this exposes nothing new.

---

## 7. Error Cases

| Case | Status | X-Error-Code |
|---|---|---|
| Not signed in / not an admin (or not SYSTEM_ADMIN for `PUT /policy`) | 401 / 403 | — |
| Validation (format, lengths, non-https URL, CR/LF, unknown field) | 422 | — |
| OAuth client not configured (`/oauth/authorize`) | 503 | `GMAIL_OAUTH_NOT_CONFIGURED` |
| Turn sending on without a connected account | 400 | `EMAIL_NOT_CONFIGURED` |
| Send/test/enable when connected Google email differs from signed-in account | 409 | `GMAIL_EMAIL_MISMATCH` |
| Send/test while not connected | 409 | `EMAIL_NOT_CONFIGURED` |
| Send while paused | 409 | `EMAIL_DISABLED` |
| Encryption key not configured (send/test) | 503 | `EMAIL_ENCRYPTION_UNAVAILABLE` |
| Stored token cannot be decrypted (key rotated) | 409 | `GMAIL_RECONNECT_REQUIRED` |
| Google refused the token (revoked, expired, scope removed) | 409, row `FAILED` | `GMAIL_RECONNECT_REQUIRED` |
| Recipient outside allowed domains | 400 | `RECIPIENT_DOMAIN_NOT_ALLOWED` |
| Over the hourly limit | 429 | `EMAIL_RATE_LIMITED` |
| Gmail refused every recipient | 502 | `GMAIL_RECIPIENT_REFUSED` |
| Gmail API not enabled on the OAuth client's Cloud project (`SERVICE_DISABLED` / `accessNotConfigured`) | 502, row `FAILED` | `GMAIL_API_DISABLED` |
| Google unreachable / timed out / 5xx / rate limited | 502 | `GMAIL_UNAVAILABLE` |
| Header value the email package would split (U+2028, U+0085, an RFC 2047 encoded word in an address) | 422 | — |
| Gmail accepted some copies but refused others | 200, row `SENT` | `error_code: SOME_RECIPIENTS_REFUSED` (in the body, not the header) |

OAuth callback outcomes (`?error=` on the redirect back; never Google's text):
`cancelled` (admin pressed Cancel), `invalid_state` (bad/expired state, missing
or mismatched binding cookie, no code), `not_admin`, `scope_missing`,
`email_unverified`, `email_mismatch` (connected Google email does not match the
signed-in admin's account email), `domain_not_allowed`, `no_refresh_token`,
`not_configured`, `encryption_unavailable`, `failed` (exchange refused, Google
unreachable, any other Google error).

`GMAIL_AUTH_FAILED` is no longer produced; the console keeps its copy for send
log rows written before v2.0.

---

## 8. Acceptance Criteria

Access and isolation
- `test_only_admins_can_use_internal_email`
- `test_every_admin_can_connect_and_send_but_only_a_system_admin_sets_policy`
- `test_each_admin_has_their_own_account`

Connecting with Google
- `test_authorize_sends_the_admin_to_google_consent`
- `test_the_redirect_uri_can_be_set_explicitly`
- `test_authorize_is_refused_until_oauth_is_configured`
- `test_the_sign_in_client_is_reused_when_no_gmail_client_is_set`
- `test_connecting_stores_the_refresh_token_encrypted_and_never_returns_it`
- `test_the_callback_needs_the_browser_that_started_it`
- `test_a_state_is_not_a_session`
- `test_cancelling_on_google_returns_to_the_console`
- `test_a_connect_without_the_send_permission_is_refused_and_revoked`
- `test_an_unverified_google_address_is_refused`
- `test_the_connect_domain_allowlist_applies`
- `test_a_grant_without_a_refresh_token_is_refused`
- `test_a_failed_code_exchange_is_reported`
- `test_connecting_fails_closed_without_an_encryption_key`
- `test_the_callback_refuses_someone_no_longer_an_admin`
- `test_reconnecting_keeps_the_profile_and_resets_verification`
- `test_connecting_is_audited_without_the_token`

Settings
- `test_settings_start_unconfigured`
- `test_the_profile_can_be_saved_before_connecting`
- `test_enabling_requires_a_connected_account`
- `test_the_settings_no_longer_take_an_address_or_password`
- `test_the_domain_policy_is_normalised_checked_and_audited`
- `test_settings_changes_are_audited_without_the_secret`
- `test_disconnecting_revokes_the_token_and_keeps_the_profile`
- `test_disconnecting_works_when_google_cannot_be_reached`

Test email and revocation
- `test_the_test_email_goes_to_the_admin_and_marks_verified`
- `test_a_revoked_grant_asks_to_reconnect`
- `test_a_gmail_401_asks_to_reconnect`
- `test_a_reconnect_during_a_failed_send_is_not_undone`
- `test_a_disabled_gmail_api_is_reported_as_a_setup_problem`
- `test_an_unreadable_token_asks_to_reconnect`
- `test_an_unreachable_google_is_reported`
- `test_the_test_email_only_verifies_the_token_it_tested`

Sending
- `test_sending_requires_configured_and_enabled`
- `test_a_send_reaches_gmail_with_the_rendered_template`
- `test_the_signature_is_appended_when_ticked`
- `test_content_is_escaped_not_rendered_as_html`
- `test_the_button_url_must_be_https`
- `test_header_injection_is_rejected`
- `test_recipients_outside_allowed_domains_are_refused`
- `test_too_many_recipients_are_rejected`
- `test_the_hourly_limit_applies`
- `test_the_hourly_limit_is_per_admin`
- `test_the_hourly_limit_holds_under_concurrent_sends`
- `test_the_hourly_limit_counts_recipients`
- `test_no_recipient_sees_the_others`
- `test_refused_recipients_are_recorded_and_the_rest_still_go`
- `test_every_recipient_refused_is_a_failure`
- `test_gmail_failing_part_way_keeps_what_was_sent`
- `test_a_send_past_its_time_budget_returns_the_rest_undelivered`
- `test_failed_and_undelivered_copies_do_not_use_up_the_hour`

Directory, preview, log
- `test_the_recipient_directory_lists_active_verified_users`
- `test_only_admins_can_list_recipients`
- `test_preview_renders_without_sending`
- `test_recent_messages_are_listed`
- `test_rich_text_formatting_in_email`, `test_markdown_formatting_in_email_preview`, `test_html_formatting_in_email_preview`
- `test_every_exported_template_renders` (templates)
- `test_platform_emails_keep_their_wording` (templates)

---

## 9. Open Questions

- **Google consent screen and verification.** `gmail.send` is a *sensitive*
  scope. On an OAuth client whose consent screen is **Internal** (Google
  Workspace, `fundlok.com` accounts only) no Google verification is needed.
  On an **External** client in "Testing", refresh tokens expire after 7 days;
  "In production" but unverified shows an "unverified app" warning and caps at
  100 users. Decision needed: a dedicated Internal client (then set
  `GMAIL_CONNECT_ALLOWED_DOMAINS=fundlok.com`) or verification of the shared
  sign-in client.
- Two admins connecting the same Google account share one grant: disconnecting
  either revokes it for both (the other sees "Reconnect").
- Retention of `internal_emails.content`. It is kept indefinitely as the record
  of what each admin sent.

---

## Templates (React Email)

- Source: `emails/src/templates/*.tsx`. Shared pieces live in `_components/`.
  The exporter skips folders that start with `_`.
- `@react-email/ui` (the preview server) ships its own Next.js. `package.json`
  pins it with `overrides.next` (16.3.6, patched for GHSA-vcvr-r3jv-pc5j). The
  pin is exact: bump it when `npm audit` in `emails/` reports a new Next
  advisory. The preview server is development-only, and `.dockerignore` keeps
  `emails/` out of the image.
- Preview while editing: `cd emails && npm install && npm run dev`, then open
  http://localhost:3030.
- **After editing any template: `cd emails && npm run export`, and commit
  `app/email_templates/`.** The export rewrites that folder from scratch.
- Placeholders: a prop's default is its Jinja placeholder (`{{ link }}`), so the
  export emits placeholders. `PreviewProps` holds realistic values for the dev
  preview. Optional blocks use `<If>`, which emits `{% if %}` only in the export.
- Python renders with `app/utils/email_templates.py`: Jinja with autoescape,
  `StrictUndefined` (a missing variable is an error, not a blank), and
  `html.escape` semantics, so values are escaped exactly as before.
