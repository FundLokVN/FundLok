# Spec: Emailing the SME its platform activation-fee invoice

| Field | Value |
|---|---|
| **Status** | IMPLEMENTED |
| **Owner** | Phat |
| **Implementer(s)** | Phat (`app/admin/fee_notice/`) |
| **Module** | `app/admin/fee_notice/`, hook in `app/admin/router.py` |
| **Version** | 1.0 |
| **Date** | 2026-11-12 |
| **Related ADR** | ADR-009 |
| **Depends on** | `docs/specs/direct_transfer/fees.md` (Edward: the invoice itself), `docs/specs/admin/internal-email.md` v2.1 (the reminder sender) |

---

## 1. Context & Goal

FundLok charges the SME one platform activation fee per funding round: 3% of
the money the investors confirmed sending, plus VAT, on a separate invoice paid
into FundLok's own account and never netted from principal (D-023). Edward's
`app/direct_transfer` already issues it as a `fee_invoices` row, kind
`SME_ACTIVATION`, when the round closes (`fees.issue_sme_activation_invoice`,
called from `close.py`). Nothing told the SME.

**Goal:** as soon as the invoice exists, the SME receives it by email (amount,
VAT, total, due date, transfer memo, FundLok's account), exactly once.

## 2. Out of Scope

- The fee itself: formula, VAT, due date, blocks, late fee (`fees.md`, Edward).
  Nothing here changes when or how much is charged.
- The investor service-fee invoices (`INVESTOR_SERVICE`).
- The VAT e-invoice (staff issue it out of band, `ManualInvoiceProvider`).
- An SME screen listing invoices (`GET /fees/invoices` in `fees.md` is not wired yet).
- A VietQR image in the email (the account and memo are written out).

## 3. Data Model

### Table: `fee_invoice_notices` (migration `70c7a22db421`)

```
invoice_id    UUID PK → fee_invoices(id) ON DELETE CASCADE
status        TEXT  SENDING | SENT | FAILED
channel       TEXT  NULL | GMAIL | PLATFORM
from_address  TEXT  NULL
sent_to       TEXT  NOT NULL
attempts      INT   NOT NULL DEFAULT 1
last_error    TEXT  NULL     -- a code (X-Error-Code or exception class), never a message
created_at, updated_at TIMESTAMPTZ NOT NULL; sent_at TIMESTAMPTZ NULL
```

## 4. API Contract

No new endpoint. `POST /admin/funding/rounds/{window_id}/advance` (unchanged
request and response) runs `run_fee_invoice_emails()` as a background task
after it responds.

## 5. State Machine

```
(none) ──claim──► SENDING ──sent──► SENT
                     └──failed──► FAILED ──next run, attempts < 5──► SENDING
SENDING left by a crash mid-send is never retried (the email may have gone).
```

## 6. Business Rules

1. **When.** After every admin Advance. In production that is how a round
   closes (the scheduled worker is not deployed), so the email goes out right
   after the round closes, which is right after the last investor's transfer
   is confirmed and counted. Each run sweeps every invoice not yet emailed, so
   a failed one is retried by the next Advance. `fee_invoice_email_job` is the
   ARQ job to register next to `invoice_job` once a worker is deployed.
2. **Which invoices.** `SME_ACTIVATION`, status `ISSUED`, issued within
   `sme_invoice_due_days` (7). A paid, overdue, voided or older invoice is
   never emailed, so the first run does not send old invoices.
3. **Once.** The `fee_invoice_notices` row is inserted (`ON CONFLICT DO
   NOTHING`) or a `FAILED` row is flipped to `SENDING`, and committed, before
   anything is sent. Only the run that wrote it sends.
4. **Who sends.** The reminder sender chosen under System settings
   (`internal_email.resolve_reminder_sender`): logged in `internal_emails`
   under its owner and counted towards their hourly limit. If none is chosen
   or it cannot send, the platform SMTP relay (`app/utils/email.send_email`).
5. **Content.** Vietnamese, with English below: project, confirmed total, the
   3% fee, VAT, total due, due date (ICT), invoice number, transfer memo
   (`reference_code`), FundLok's account (`FUNDLOK_FEE_BANK_BIN`,
   `FUNDLOK_FEE_ACCOUNT_NUMBER`, `FUNDLOK_FEE_ACCOUNT_NAME`; when unset the
   email asks the SME to reply for it, and a warning is logged). It says the
   fee is separate and never deducted, that the VAT e-invoice follows, and that
   an unpaid fee blocks the next round. Template `action_required`; a
   dashboard button only when `FRONTEND_URL` is https.
6. **Retries.** Up to 5 attempts; then the row stays `FAILED` for a person.

## 7. Error Cases

| Case | Result |
|---|---|
| Reminder sender unset, paused, revoked, rate-limited or Gmail down | platform relay sends instead |
| Platform relay fails / address invalid | row `FAILED`, `last_error` = exception class / `INVALID_ADDRESS`; retried next run |
| The whole run fails | logged; the admin's Advance already succeeded |

## 8. Acceptance Criteria (`tests/admin/test_fee_invoice_email.py`)

- `test_the_sme_is_emailed_its_activation_fee_once`
- `test_the_email_says_where_to_pay`
- `test_without_fundlok_s_account_the_sme_is_told_to_reply`
- `test_the_reminder_sender_goes_first`
- `test_the_platform_email_takes_over_when_gmail_cannot_send`
- `test_a_failed_send_is_retried_by_the_next_run`
- `test_it_gives_up_after_the_last_attempt`
- `test_only_a_new_unpaid_invoice_is_emailed`
- `test_advancing_a_round_emails_the_sme_its_fee`

## 9. Open Questions

- Edward: register `fee_invoice_email_job` on the direct-transfer worker when
  it is deployed (it closes rounds on its own then, without an Advance).
- Are `FUNDLOK_FEE_*` set on the prod-api service? Without them the email has
  no account to pay into.
- Should the SME also get an in-app notification and an invoice screen?
