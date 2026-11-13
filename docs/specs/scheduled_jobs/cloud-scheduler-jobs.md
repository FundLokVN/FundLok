# Spec: Scheduled jobs through Google Cloud Scheduler (and the daily repayment reminder, by email and Zalo)

| Field | Value |
|---|---|
| **Status** | IMPLEMENTED |
| **Owner** | Phat |
| **Implementer(s)** | Phat (`app/scheduled_jobs/`, `app/admin/system_email.py`, `app/phone_verification/zalo.py`) |
| **Module** | `app/scheduled_jobs/` |
| **Version** | 1.1 (Zalo reminder) |
| **Date** | 2026-11-13 |
| **Related ADR** | ADR-009 |
| **Depends on** | `docs/specs/admin/internal-email.md` v2.1 (reminder sender), `docs/specs/admin/fee-invoice-email.md`, Edward's `app/direct_transfer/repayment.todays_payments` |

---

## 1. Context & Goal

Production deploys only the API (`prod-api` on Cloud Run); the ARQ worker has
no deployed service (`docs/deployment/ci-cd-pipeline.md`), so no scheduled job
runs there. Rather than deploy and pay for an always-on worker, Google Cloud
Scheduler calls a protected endpoint on `prod-api` at a set time: Cloud
Scheduler's free tier covers a few jobs, and Cloud Run bills only while the
call runs.

**Goal:** a safe way for Cloud Scheduler to run jobs on prod-api, and the first
two jobs: the daily repayment reminder to each SME, and a daily retry of SME
fee invoice emails.

## 2. Out of Scope

- Edward's worker jobs (`app/direct_transfer/worker.py`: round advancing,
  repayment-day presume/settle, `invoice_job`, CIC). They can be exposed the
  same way, but they change money state and are his to decide.
- Reminders to investors; lateness, arrears or late fees (out of MVP scope).
- A Zalo message for the fee-invoice job, or for earlier unpaid days (the
  template says "due today"; staff see late days in red on Admin >
  Transactions > Repayments, `docs/specs/admin/overdue-repayments-admin.md`).
- A VietQR image in the email.

## 3. Data Model

### Table: `repayment_reminders` (migration `47604f91d6f7`)

```
sme_user_id    UUID  → users(id) ON DELETE CASCADE   ┐ PK
reminder_date  DATE  (the ICT working day)            ┘
status         TEXT  SENDING | SENT | FAILED
channel        TEXT  NULL | GMAIL | PLATFORM
from_address   TEXT  NULL;  sent_to TEXT NOT NULL
due_count INT, due_vnd BIGINT, earlier_count INT      -- what the email said
attempts INT DEFAULT 1, last_error TEXT NULL           -- a code, never a message
created_at, updated_at TIMESTAMPTZ; sent_at TIMESTAMPTZ NULL
```

### Table: `repayment_zalo_reminders` (migration `5b8e2c1d9f40`)

```
tranche_id     UUID  → tranches(id) ON DELETE CASCADE ┐ PK
reminder_date  DATE  (the ICT working day)             ┘
sme_user_id    UUID  → users(id) ON DELETE CASCADE
status         TEXT  SENDING | SENT | FAILED
sent_to        TEXT  the 84xxxxxxxxx number
amount_vnd BIGINT, days_left INT                       -- what the message said
attempts INT DEFAULT 1, last_error TEXT NULL           -- ZALO_<code> or a short name
created_at, updated_at TIMESTAMPTZ; sent_at TIMESTAMPTZ NULL
```

### Configuration (environment on prod-api)

| Variable | Value |
|---|---|
| `SCHEDULER_SERVICE_ACCOUNT` | `cloud-scheduler@fundlok.iam.gserviceaccount.com` |
| `SCHEDULER_AUDIENCE` | the prod-api URL the jobs use as OIDC audience |
| `ZALO_ZBS_REMINDER_TEMPLATE_ID` | `666161` (the approved `fundlok_reminder` template). Unset: no Zalo reminder |

Either scheduler variable unset: every job answers 503. The Zalo reminder also
needs the OTP's `ZALO_APP_ID`, `ZALO_APP_SECRET`, `ZALO_OA_REFRESH_TOKEN`,
`ZALO_TOKEN_ENCRYPTION_KEY` (already set on prod-api).

## 4. API Contract

Not in the OpenAPI schema; the web app never calls them.

| Method | Path | Returns |
|---|---|---|
| POST | `/internal/jobs/repayment-reminders` | `JobResult` |
| POST | `/internal/jobs/fee-invoice-emails` | `JobResult` |

```
Authorization: Bearer <Google-signed OIDC ID token>
JobResult { job: str, sent: int, failed: int, skipped: str | null,   -- skipped: NOT_A_WORKING_DAY
            zalo: { sent: int, failed: int, skipped: str | null } | null }
-- zalo: repayment-reminders only; skipped NOT_CONFIGURED | NOT_A_WORKING_DAY
```
401 no/invalid token (an app session is not one), 403 a token for another
account or unverified email, 503 not configured or maintenance mode.

## 5. State Machine

`repayment_reminders` (per SME per day) and `repayment_zalo_reminders` (per
ticket per day), as `fee_invoice_notices`:
`(none) → SENDING → SENT | FAILED`; `FAILED → SENDING` on a later run that day
while `attempts < 3`; `SENDING` left by a crash is never retried.

## 6. Business Rules

1. **Who may run a job.** `require_scheduler` verifies the token with
   google-auth (Google's signature, issuer, expiry, audience =
   `SCHEDULER_AUDIENCE`) and that `email` is `SCHEDULER_SERVICE_ACCOUNT` with
   `email_verified`. prod-api is public, so Cloud Run does not check it; this
   does.
2. **Safe to call twice.** Cloud Scheduler retries on a non-2xx or timeout;
   each job records what it sent and skips it next time.
3. **Repayment reminder: when.** Daily at 15:00 ICT; on a day that is not a
   working day (the app's VN calendar, `default_calendar().is_business_day`)
   nothing is sent.
4. **Repayment reminder: who and what.** Every non-suspended SME with a round
   in OPEN/FULL/CLOSING/REPAYING and at least one repayment day still `DUE`
   (not marked paid) for today or earlier. Read from `todays_payments`, the
   same list the dashboard's "Today's repayments" shows. The email gives
   today's count and total to transfer (net of tax withheld) and, separately,
   earlier days not yet marked paid. It says the SME pays each investor
   directly and links to the dashboard (https only). Never "late"/"overdue".
5. **Fee invoice emails.** Daily at 15:05 ICT, `send_pending_fee_invoice_emails`
   (`fee-invoice-email.md`): retries a failed send and catches any invoice
   issued without an Advance.
6. **Who sends.** `app/admin/system_email.deliver`: the reminder sender's
   Gmail account (System settings) through the Gmail API, else the platform
   SMTP relay. Gmail sends count towards that admin's 200 recipients an hour;
   past it, the relay takes over.
7. **Zalo repayment reminder** (`app/scheduled_jobs/zalo_reminders.py`). Same
   call as the email, after it. The `fundlok_reminder` ZBS template names one
   investment code, so the SME gets **one message per investor ticket** with a
   day due **today** still `DUE` (no receipt uploaded; uploading makes it
   `PRESUMED_PAID`, so an SME that paid before 15:00 gets nothing). Earlier
   unpaid days alone send nothing. Parameters, all strings: `company_name`
   (project legal name, at most 200 characters), `reference_code` (the
   ticket's memo), `due_date` (today, dd/mm/yyyy), `amount_due` (today's net
   for that investor, digits only), `days_left` (the ticket's repayment days
   after today). Sent only to an SME that is not suspended and has proven its
   phone (`phone_verified_at`): an unproven number may be someone else's.
   Through `zalo.send_template`, the OTP's client and token chain;
   `tracking_id` = `rr` + yymmdd + 24 hex of the ticket id (32 characters).
   Each message costs (400 đ by phone at approval), so it is claimed before it
   is sent, like the email.

## 7. Error Cases

| Case | Result |
|---|---|
| Not configured | 503, nothing runs |
| Maintenance mode on | 503 (maintenance gate); Cloud Scheduler retries |
| Gmail cannot send | platform relay sends |
| Relay fails | row `FAILED`, retried by a later call that day (up to 3) |
| Zalo refuses or is unreachable | Zalo row `FAILED` (`ZALO_<code>`), retried by a later call that day (up to 3); the email is unaffected |
| A `ZALO_*` setting missing | one row `FAILED` (`ZALO_NOT_CONFIGURED`), the rest of the run skipped |
| `ZALO_ZBS_REMINDER_TEMPLATE_ID` unset | no Zalo message; `zalo.skipped = NOT_CONFIGURED` |

## 8. Acceptance Criteria (`tests/scheduled_jobs/test_cloud_scheduler_jobs.py`)

- `test_jobs_do_nothing_until_configured`
- `test_only_the_scheduler_s_own_token_runs_a_job`
- `test_an_admin_session_is_not_a_scheduler`
- `test_each_sme_is_reminded_once_a_working_day`
- `test_nothing_is_sent_on_a_day_off`
- `test_days_marked_paid_are_not_reminded`
- `test_only_earlier_days_still_get_a_reminder`
- `test_a_failed_reminder_is_retried_by_the_next_run`

Zalo (`tests/scheduled_jobs/test_zalo_repayment_reminders.py`):
- `test_the_template_gets_every_parameter_as_text`
- `test_each_ticket_due_today_and_not_paid_gets_one_message`
- `test_a_ticket_the_sme_uploaded_a_receipt_for_gets_nothing`
- `test_only_earlier_unpaid_days_send_nothing`
- `test_an_unproven_phone_gets_nothing`
- `test_nothing_without_the_template_or_on_a_day_off`
- `test_a_message_zalo_refused_is_retried_by_the_next_run`
- `test_a_missing_zalo_setting_stops_the_run_after_one_try`
- `test_the_scheduler_call_reports_the_zalo_messages_too`

## 9. Setting up Cloud Scheduler (once, after the code is deployed)

```bash
PROJECT=fundlok; REGION=asia-southeast1
SA=cloud-scheduler@fundlok.iam.gserviceaccount.com

gcloud services enable cloudscheduler.googleapis.com --project $PROJECT
# No roles needed: prod-api checks the token itself.
gcloud iam service-accounts create cloud-scheduler --display-name "Cloud Scheduler jobs" --project $PROJECT
URL=$(gcloud run services describe prod-api --region $REGION --project $PROJECT --format='value(status.url)')
gcloud run services update prod-api --region $REGION --project $PROJECT \
  --update-env-vars "SCHEDULER_SERVICE_ACCOUNT=$SA,SCHEDULER_AUDIENCE=$URL"

gcloud scheduler jobs create http repayment-reminders --location $REGION --project $PROJECT \
  --schedule "0 15 * * *" --time-zone "Asia/Ho_Chi_Minh" --http-method POST \
  --uri "$URL/internal/jobs/repayment-reminders" \
  --oidc-service-account-email $SA --oidc-token-audience "$URL" \
  --attempt-deadline 300s --max-retry-attempts 3 --min-backoff 5m
gcloud scheduler jobs create http fee-invoice-emails --location $REGION --project $PROJECT \
  --schedule "5 15 * * *" --time-zone "Asia/Ho_Chi_Minh" --http-method POST \
  --uri "$URL/internal/jobs/fee-invoice-emails" \
  --oidc-service-account-email $SA --oidc-token-audience "$URL" \
  --attempt-deadline 300s --max-retry-attempts 3 --min-backoff 5m

# Run one now to check: gcloud scheduler jobs run repayment-reminders --location $REGION --project $PROJECT
```

## 10. Open Questions

- Edward: expose the worker's jobs (round advancing, repayment-day presume and
  settle) the same way? Until then rounds close only on an admin's Advance and
  repayment days are never settled in production.
- Should investors get a reminder too (e.g. to report a missing payment)?
