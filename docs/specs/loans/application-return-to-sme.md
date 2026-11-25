# Spec: Submit checks the required documents; staff can return an application to the SME

| Field | Value |
|---|---|
| **Status** | DRAFT |
| **Owner** | Edward |
| **Implementer(s)** | Edward (backend) / Phat (screen AM-P18, and his modules `app/loans/`, `app/admin/`, `app/uploads/`, `app/projects/`) |
| **Module** | `app/loans/`, `app/admin/`, `app/uploads/`, `app/projects/`, `app/lending/models.py` |
| **Version** | 1.0 |
| **Date** | 2026-11-25 |
| **Related ADR** | — |
| **Depends on** | `docs/specs/loans/`, admin approval (`POST /admin/applications/{id}/decision`); Jira SCRUM-621 (AM-E16), Appendix M 2.2, decision C18 (technical debt) |

Touches shared and Phat-owned files, which need Phat's agreement before build: `app/lending/models.py` (two columns, one new status), `alembic/` (one migration), and code in `app/loans/`, `app/admin/`, `app/uploads/`, `app/projects/`.

---

## 1. Context & Goal

In the Appendix M test an application was submitted while its CIC report was still `PENDING` (an unreadable PDF). Submit did not look at documents at all. Staff approval then refused it (`DOCUMENTS_MISSING`), and because staff can only answer `APPROVED` or `REJECTED`, and the SME cannot edit a submitted application, the application was stuck: nobody could fix it.

**Goal:** (1) Submit is refused until the five required documents are `UPLOADED`. (2) Staff can send a submitted application back to the SME with a note; the SME fixes it and submits again.

---

## 2. Out of Scope

- Changing which documents are required (`REQUIRED_DOCUMENT_TYPES`) or how a document is judged readable.
- Automatic return (the system never returns an application by itself).
- A limit on how many times an application can be returned.
- The AM-P18 screen (Phat).

---

## 3. Data Model

`loan_applications.status` has a database CHECK `loan_applications_status_check` (`DRAFT`, `SUBMITTED`, `UNDER_REVIEW`, `APPROVED`, `REJECTED`). It must accept the new value.

```
loan_applications (altered)
  status        text NOT NULL   -- CHECK replaced: adds 'RETURNED_TO_SME'
  returned_at   timestamptz NULL -- when staff last returned it; cleared on resubmit
  return_note   text NULL        -- what the SME must fix; cleared on resubmit (the audit log keeps every return)
```

One Alembic revision (hash-based ID, chained off the current head). Downgrade turns `RETURNED_TO_SME` rows into `SUBMITTED` and restores the old CHECK.

The document rule is the existing `missing_documents` function (in `app/admin/service.py`), reused unchanged: a required type counts only when its row is `UPLOADED` (a `PENDING` row is not a document), and `business_registration` is covered by an `APPROVED` KYB.

---

## 4. API Contract

### `POST /loans/applications/{id}/submit` (changed)

Role SME, owner only. Accepted from `DRAFT` and `RETURNED_TO_SME`. Refuses with **409** and header `X-Error-Code: DOCUMENTS_MISSING` when any required document is missing:

```
409  { "detail": "Required documents missing: cic_report, tax_filings" }
```

(the same code and wording staff approval uses today, so the screen handles one error). On success the status is `SUBMITTED`, `submitted_at` is set, `returned_at` and `return_note` are cleared, and the audit entry `SUBMIT` records `resubmitted: true` when it came from `RETURNED_TO_SME`.

### `POST /admin/applications/{id}/return` (new)

Role ADMIN or SYSTEM_ADMIN. For applications in `SUBMITTED` or `UNDER_REVIEW` with `admin_approval = PENDING`.

```
Request   { "note": string }              // required, 1 to 1000 characters: what the SME must fix
Response  200 AdminLoanApplicationRow     // adds status: "RETURNED_TO_SME", returned_at, return_note
```

Effects: `status = RETURNED_TO_SME`, `returned_at = now`, `return_note = note`, `admin_approval` stays `PENDING`. The SME's owners get the same "application updated" email staff decisions send. Audit entry `APPLICATION_RETURNED` (actor, note, before and after status).

### Reads

`GET` of an application (SME and admin views) returns `status`, `returned_at`, `return_note`. The admin list can filter on `status = RETURNED_TO_SME`.

### Editing while returned

Every place that today requires `status == "DRAFT"` to change the application accepts `DRAFT` or `RETURNED_TO_SME`: saving figures (`app/loans/service.py`), the document presign and confirm (`app/uploads/service.py`), the other wizard saves in `app/loans/router.py` that check the same status. The SME can replace a document; all uploaded files and figures stay as they were until changed.

---

## 5. State Machine

```
DRAFT ──submit (documents complete)──▶ SUBMITTED ──score run──▶ UNDER_REVIEW
SUBMITTED | UNDER_REVIEW ──staff: return──▶ RETURNED_TO_SME ──submit (documents complete)──▶ SUBMITTED
SUBMITTED | UNDER_REVIEW ──staff decision──▶ REJECTED   (admin_approval APPROVED: status unchanged, as today)
```

`RETURNED_TO_SME` is not terminal and does not change `admin_approval` (still `PENDING`).

---

## 6. Business Rules

1. Submit and resubmit both run the document check; the check is done inside the same transaction as the status change.
2. Staff cannot approve or reject a `RETURNED_TO_SME` application (409 `APPLICATION_RETURNED`); the SME must resubmit first.
3. An application with a **locked** score run cannot be returned (409 `SCORE_LOCKED`): from that point the score is what the decision is built on. Returning from `UNDER_REVIEW` without a locked run is allowed; resubmitting makes it `SUBMITTED`, and a new score run is a new row (old runs stay, append-only).
4. The underwriting engine does not run on a `RETURNED_TO_SME` application (its existing status check already refuses anything not `SUBMITTED` or `UNDER_REVIEW`).
5. The **repeat-application** path (`app/projects/service.py`, which sets `SUBMITTED` directly after copying documents) runs the same check. If documents are missing it leaves the application `DRAFT`, so the SME can upload the rest and submit.
6. The note is shown to the SME exactly as staff wrote it. It is not machine-parsed.
7. Returning does not delete documents, figures or score runs.

---

## 7. Error Cases

| Case | Status | `detail` / header |
|---|---|---|
| Submit with a required document missing | 409 | `Required documents missing: <types>`, `X-Error-Code: DOCUMENTS_MISSING` |
| Submit from any status other than `DRAFT` or `RETURNED_TO_SME` | 400 | `Application is not in DRAFT status` (as today) |
| Return without a note, or a note over 1000 characters | 422 | validation error |
| Return an application not `SUBMITTED` or `UNDER_REVIEW` | 409 | `Only a submitted application can be returned` |
| Return when `admin_approval` is not `PENDING` | 409 | `Application has already been <approved/rejected>` (as the decision endpoint) |
| Return with a locked score run | 409 | `SCORE_LOCKED`: `This application has a locked score and cannot be returned` |
| Approve or reject a returned application | 409 | `APPLICATION_RETURNED`: `This application was returned to the SME` |
| Edit an application that is not `DRAFT` or `RETURNED_TO_SME` | 400 | `Application is not in DRAFT status` (as today) |
| Non-staff calls return | 403 | as for every admin route |

---

## 8. Acceptance Criteria

Test files: `tests/loans/` (submit), `tests/admin/test_admin_approval.py` (return and decision), `tests/uploads/` (editing), migration test.

- `test_submit_refused_when_required_document_missing`
- `test_submit_refused_when_cic_report_still_pending`
- `test_submit_lists_every_missing_document_type`
- `test_submit_counts_verified_registration_as_business_registration`
- `test_submit_succeeds_with_all_documents_uploaded`
- `test_staff_can_return_submitted_application_with_note`
- `test_return_requires_note`
- `test_return_from_under_review_allowed_without_locked_score_run`
- `test_return_refused_with_locked_score_run`
- `test_return_refused_when_already_decided`
- `test_return_writes_audit_entry_and_emails_owners`
- `test_returned_application_cannot_be_approved_or_rejected`
- `test_returned_application_not_scored_by_engine`
- `test_sme_can_edit_figures_and_replace_document_while_returned`
- `test_resubmit_after_return_clears_note_and_goes_to_submitted`
- `test_resubmit_still_refused_when_document_missing`
- `test_repeat_application_stays_draft_when_documents_missing`
- `test_migration_allows_returned_status_and_downgrades_to_submitted`

---

## 9. Open Questions

1. **Return from `UNDER_REVIEW`.** Proposal above: allowed unless the score is locked. Alternative: only from `SUBMITTED`. Needs Loc.
2. **Re-grading after a return.** If the SME changes figures after a score run, the old run stays as history and staff run a new one. Confirm no automatic re-run is wanted.
3. **Notification wording.** Reuse "application updated", or a specific "your application needs changes" email with the note? Proposal: specific email; wording from Loc or Phat.
4. **Existing stuck application.** Any application already submitted with a missing document (the Appendix M case) can be returned once this ships; no data fix needed.
