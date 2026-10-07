# Spec: Internal email (admin console)

| Field | Value |
|---|---|
| **Status** | IMPLEMENTED |
| **Owner** | Phat |
| **Implementer(s)** | Phat (backend + frontend) |
| **Module** | `app/admin/internal_email/`, `emails/` (React Email templates), `app/utils/email.py` |
| **Version** | 1.1 (per-admin accounts) |
| **Date** | 2026-10-07 |
| **Related ADR** | — |
| **Depends on** | — |

---

## 1. Context & Goal

Admins need to send company email (announcements, reminders, one-off notes)
without leaving the admin console. **Every admin** (ADMIN and SYSTEM_ADMIN)
connects **their own** Gmail / Google Workspace account with their own **App
Password**, and their email goes out from that address. They then compose a
message: recipients, a template, a subject and the content. A system admin also
sets the recipient domains every admin may send to, and can see every admin's
sends.

All of FundLok's email templates, the platform emails included, are written in
**React Email** (`emails/`) and exported to static HTML at build time. Python
fills them in with Jinja, so the API needs no Node at runtime.

**Goal:** each admin can store their own Gmail credentials safely, send a test
email, and compose and send templated email from their own address, with every
send on record.

---

## 2. Out of Scope

- **Platform emails do not move to Gmail.** Verification, password reset,
  invites and notices still leave through the SMTP settings in the environment
  (`SMTP_*`). Only their templates move to React Email.
- Google OAuth / Gmail API and service accounts. App Password only.
- Attachments, CC/BCC, scheduled sends, mailing lists, open tracking.
- HTML or Markdown authored by the admin. The content is plain text.
- More than one connected account per admin, or sending from another admin's
  account.

---

## 3. Data Model

### New table: `email_settings` (one row per admin)

```
email_settings
├── id                      UUID         PK
├── user_id                 UUID         NOT NULL, UNIQUE → users(id) ON DELETE CASCADE
├── gmail_address           TEXT         NOT NULL   -- SMTP username and the From address
├── from_name               TEXT         NOT NULL   -- display name on From
├── app_password_encrypted  TEXT         nullable   -- Fernet, "v1:<token>"; never returned
├── enabled                 BOOLEAN      NOT NULL DEFAULT FALSE
├── last_verified_at        TIMESTAMPTZ  nullable   -- last successful test email
├── created_at              TIMESTAMPTZ  DEFAULT NOW()
└── updated_at              TIMESTAMPTZ  DEFAULT NOW()
```

A row is only ever read or written for the signed-in admin: the service looks
it up by the caller's user id, never by an id from the request.

### Org-wide policy: `system_settings` key `internal_email_policy`

```
{ "allowed_domains": ["fundlok.com", ...] }   -- empty = any domain
```

Set by a SYSTEM_ADMIN. It applies to every admin's sends. A per-admin list
would restrict nothing, since each admin could widen their own.

### New table: `internal_emails` (send log, one row per send attempt)

```
internal_emails
├── id             UUID         PK
├── sent_by        UUID         nullable → users(id) ON DELETE SET NULL
├── from_address   TEXT         NOT NULL
├── recipients     TEXT[]       NOT NULL
├── template       TEXT         NOT NULL   -- general | announcement | action_required | test
├── subject        TEXT         NOT NULL
├── content        JSONB        NOT NULL   -- heading, body, button_label, button_url
├── status         TEXT         NOT NULL, CHECK IN ('PENDING', 'SENT', 'FAILED')
├── error_code     TEXT         nullable
├── created_at     TIMESTAMPTZ  DEFAULT NOW()
└── sent_at        TIMESTAMPTZ  nullable
INDEX (sent_by, created_at)
```

The row is written as `PENDING` and committed **before** contacting Gmail, then
moved to `SENT` or `FAILED`. A crash mid-send leaves a `PENDING` row rather than
no record, and pending rows count toward the rate limit.

### Encryption at rest

`app_password_encrypted` uses Fernet under `EMAIL_SETTINGS_ENCRYPTION_KEY`, the
same scheme as `app/auth/totp_crypto.py`, and a separate key from it and from
`SECRET_KEY`. With no key configured, saving a password fails closed (503).
Generate one with:

```
python -c "from app.admin.internal_email.crypto import generate_encryption_key as g; print(g())"
```

---

## 4. API Contract

Every endpoint is **admins only** (ADMIN and SYSTEM_ADMIN; 403 for everyone
else, 401 if not signed in). Only `PUT /admin/email/policy` is SYSTEM_ADMIN only.
`/settings` always means the signed-in admin's own. Errors are
`{"detail": "<English>"}`. Failures the UI translates also carry an
`X-Error-Code` header.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/admin/email/settings` | — | `EmailSettingsOut` |
| PUT | `/admin/email/settings` | `EmailSettingsUpdate` | `EmailSettingsOut` |
| DELETE | `/admin/email/settings` | — | `EmailSettingsOut` (unconfigured) |
| POST | `/admin/email/settings/test` | — | `EmailTestResult` |
| POST | `/admin/email/preview` | `EmailContent` | `EmailPreview` |
| POST | `/admin/email/send` | `SendEmailRequest` | `InternalEmailOut` |
| GET | `/admin/email/messages?limit=20` | — | `list[InternalEmailOut]` (own; SYSTEM_ADMIN: everyone's) |
| GET | `/admin/email/policy` | — | `EmailPolicyOut` |
| PUT | `/admin/email/policy` | `EmailPolicyUpdate` (SYSTEM_ADMIN) | `EmailPolicyOut` |

```
EmailSettingsOut      { configured: bool, enabled: bool, gmail_address: str | null,
                        from_name: str | null, has_app_password: bool,
                        last_verified_at: datetime | null, updated_at: datetime | null }
EmailSettingsUpdate   { gmail_address: email, from_name: str (1–80),
                        app_password: str | null   -- null/blank keeps the stored one
                        enabled: bool }
EmailPolicyOut        { allowed_domains: str[], updated_at: datetime | null, updated_by: UUID | null }
EmailPolicyUpdate     { allowed_domains: str[] (≤ 20) }
EmailTestResult       { sent_to: str, last_verified_at: datetime }
EmailContent          { template: "general" | "announcement" | "action_required",
                        subject: str (1–200), heading: str | null (≤ 200),
                        body: str (1–10 000), button_label: str | null (≤ 60),
                        button_url: https URL | null (≤ 2 000) }
SendEmailRequest      EmailContent + { to: email[] (1–20) }
EmailPreview          { subject: str, html: str }
InternalEmailOut      { id, sent_by: UUID | null, from_address, recipients: str[], template,
                        subject, status, error_code: str | null, created_at, sent_at }
```

The test email always goes to **the signed-in admin's own address**, and never
to an address supplied in the request. A successful test marks the settings
verified only if they still hold the address and password that were tested:
a save during the Gmail call is not marked verified by mistake.

---

## 5. State Machine

```
PENDING ──(Gmail accepted)──▶ SENT
   └─────(any failure)──────▶ FAILED
```

---

## 6. Business Rules

1. **Fixed transport.** Gmail SMTP at `smtp.gmail.com:465`, implicit TLS with
   certificate verification. Host and port are not configurable, so the
   settings cannot point the server at an internal host.
2. **App password format.** Spaces are stripped (Google displays it in four
   groups). It must then be 16 letters or digits.
3. **Changing the address needs a new password.** An App Password belongs to
   one Google account.
4. **Enabling needs a password**, either stored or provided in the same request.
5. **The secret never leaves the server.** It is not returned, logged or
   written to the audit log. Responses say only `has_app_password`.
6. **Audit.** `EMAIL_SETTINGS_UPDATED` and `EMAIL_SETTINGS_CLEARED` (entity =
   the admin) record the address, name, enabled flag, and whether the password
   changed. `EMAIL_POLICY_UPDATED` records the allowed domains before and after.
   `INTERNAL_EMAIL_SENT` records the send-log id, the recipient count and the
   status.
7. **Content is plain text.** It is HTML-escaped and newlines become `<br/>`.
   The button URL must be `https://`. CR/LF are rejected in the subject,
   heading, from name and button label.
8. **Recipients.** 1–20 per send, de-duplicated, sent as one message (every
   recipient sees the list). When the policy's `allowed_domains` is non-empty,
   each recipient's domain (in punycode) must be in it, compared exactly.
9. **Rate limit.** 20 sends per admin per rolling hour. Test emails count. The
   count and the PENDING insert run under a per-admin, transaction-scoped
   advisory lock (`pg_advisory_xact_lock`), so concurrent requests cannot all
   read the same count and pass. The lock is released before Gmail is
   contacted.
10. **Gating.** Sending needs the admin's own account `configured && enabled`.
    Otherwise 409.
11. **Isolation.** An admin can only read, change, test, send with or
    disconnect their own account. The From address is their connected Gmail
    address; the console starts it from their sign-in email. Deleting the user
    deletes their settings and stored password.
12. **Oversight.** `GET /messages` returns an admin's own sends; for a
    SYSTEM_ADMIN it returns every admin's.

---

## 7. Error Cases

| Case | Status | X-Error-Code |
|---|---|---|
| Not signed in / not an admin (or not SYSTEM_ADMIN for `PUT /policy`) | 401 / 403 | — |
| Validation (format, lengths, non-https URL, CR/LF) | 422 | — |
| Address changed without a new password | 400 | `APP_PASSWORD_REQUIRED` |
| Enable without any password | 400 | `APP_PASSWORD_REQUIRED` |
| Encryption key not configured | 503 | `EMAIL_ENCRYPTION_UNAVAILABLE` |
| Send/test while not configured | 409 | `EMAIL_NOT_CONFIGURED` |
| Send while paused | 409 | `EMAIL_DISABLED` |
| Recipient outside allowed domains | 400 | `RECIPIENT_DOMAIN_NOT_ALLOWED` |
| Over the hourly limit | 429 | `EMAIL_RATE_LIMITED` |
| Google rejected the address/password | 502 | `GMAIL_AUTH_FAILED` |
| Google refused a recipient | 502 | `GMAIL_RECIPIENT_REFUSED` |
| Gmail unreachable / timed out / other SMTP error | 502 | `GMAIL_UNAVAILABLE` |
| Header value the email package would split (U+2028, U+0085, an RFC 2047 encoded word in an address) | 422 | — |
| Gmail accepted the message but refused some recipients | 200, row `SENT` | `error_code: SOME_RECIPIENTS_REFUSED` (in the body, not the header) |
| Stored password cannot be decrypted (key rotated) | 409 | `APP_PASSWORD_UNREADABLE` |

---

## 8. Acceptance Criteria

- `test_only_admins_can_use_internal_email`
- `test_every_admin_can_connect_and_send_but_only_a_system_admin_sets_policy`
- `test_each_admin_has_their_own_account`
- `test_the_domain_policy_is_normalised_checked_and_audited`
- `test_settings_start_unconfigured`
- `test_the_app_password_is_stored_encrypted_and_never_returned`
- `test_a_blank_password_keeps_the_stored_one`
- `test_changing_the_address_requires_a_new_password`
- `test_enabling_requires_a_password`
- `test_a_malformed_app_password_is_rejected`
- `test_saving_fails_closed_without_an_encryption_key`
- `test_settings_changes_are_audited_without_the_secret`
- `test_clearing_removes_the_password_and_disables`
- `test_the_test_email_goes_to_the_admin_and_marks_verified`
- `test_a_rejected_password_is_reported_and_logged`
- `test_sending_requires_configured_and_enabled`
- `test_a_send_reaches_gmail_with_the_rendered_template`
- `test_content_is_escaped_not_rendered_as_html`
- `test_the_button_url_must_be_https`
- `test_header_injection_is_rejected`
- `test_recipients_outside_allowed_domains_are_refused`
- `test_the_hourly_limit_applies`
- `test_the_hourly_limit_is_per_admin`
- `test_the_hourly_limit_holds_under_concurrent_sends`
- `test_the_test_email_only_verifies_the_credentials_it_tested`
- `test_preview_renders_without_sending`
- `test_recent_messages_are_listed`
- `test_every_exported_template_renders` (templates)
- `test_platform_emails_keep_their_wording` (templates)

---

## 9. Open Questions

- Should the Gmail address be locked to the admin's sign-in email? Today it
  starts from it and can be changed (e.g. to a shared team mailbox the admin
  holds an App Password for). Gmail itself only sends as the account the App
  Password belongs to.
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
