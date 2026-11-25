# Spec: Account status CLOSED, and SUSPENDED narrowed to "no new offers or investments"

| Field | Value |
|---|---|
| **Status** | DRAFT |
| **Owner** | Edward |
| **Implementer(s)** | Edward (backend) / Phat (screen AM-P20) |
| **Module** | `app/users/` (model), `app/admin/` (endpoint), `app/utils/jwt.py`, `app/auth/`, `app/oauth/` |
| **Version** | 1.0 |
| **Date** | 2026-11-25 |
| **Related ADR** | — |
| **Depends on** | D-043 (T&S Art. 4.4), D-047 (suspended accounts), `docs/specs/admin/` user status endpoint; Jira SCRUM-620 (AM-E15), Appendix M 12.2 and 11.1 |

Touches shared files, which need Phat's agreement before build: `app/users/models.py` (two new columns), `app/auth/service.py`, `app/admin/` (his module), `alembic/` (one migration).

---

## 1. Context & Goal

Staff need a permanent way to shut an account (a lender using threats or hired collectors, T&S Art. 4.4; later a second lender no-show, SCRUM-612), with a recorded reason. Separately, D-047 decided that SUSPENDED is a mild measure: no new offers or investments, nothing else. D-047 is only half built: the repayment endpoints already accept a suspended account (`allow_suspended`, PR #100), but a suspended user cannot log in or refresh, and every other endpoint still returns 403. So a suspended SME or lender can repay only if their session was already open.

**Goal:** add a permanent `CLOSED` status with a mandatory reason, audited; make `SUSPENDED` refuse only new offers and new investments.

---

## 2. Out of Scope

- The lender no-show lock and its unlock flow (SCRUM-612). It will call the same closure function this spec adds, with reason `SECOND_NO_SHOW`.
- Reopening a CLOSED account. It is permanent.
- Blocking a closed person from registering again under a different email.
- Handling money already owed to or by a closed account (staff settle it outside the platform for now; see Open Questions).
- The AM-P20 screen itself (Phat).

---

## 3. Data Model

`users.status` is text with a database CHECK `users_status_check` (`ACTIVE`, `PENDING_KYC`, `SUSPENDED`, `REJECTED`). The model comment says "no CHECK constraint", which is wrong; the constraint still exists and would reject `CLOSED`.

```
users (altered)
  status               text NOT NULL   -- CHECK replaced: adds 'CLOSED'
  status_reason_code   text NULL       -- set with CLOSED (required) or SUSPENDED (optional); cleared when status returns to ACTIVE or PENDING_KYC
  status_changed_at    timestamptz NULL -- set on every status change through the admin endpoint
```

`status_reason_code` values (CHECK, so a typo cannot be stored):

| Code | Meaning |
|---|---|
| `THREATS_OR_COLLECTORS` | Threats, harassment or hired collectors against an SME (T&S 4.4) |
| `SECOND_NO_SHOW` | Second lender no-show (T&S 4.3, SCRUM-612) |
| `FRAUD` | Fraud or false documents |
| `POLICY_BREACH` | Any other breach of the terms |
| `OTHER` | Anything else; `note` is then required |

Python constants: `USER_STATUS_CLOSED = "CLOSED"`, added to `USER_STATUSES`. Migration: one Alembic revision (hash-based ID, chained off the current head) that adds the two columns and swaps the CHECK. Downgrade turns `CLOSED` rows into `SUSPENDED`, then restores the old CHECK.

The free-text `note` is not a column; it goes to the audit entry as today.

---

## 4. API Contract

### `PATCH /admin/users/{user_id}/status` (changed)

Role: ADMIN or SYSTEM_ADMIN, as today.

```
Request
{
  "status": "ACTIVE" | "PENDING_KYC" | "SUSPENDED" | "CLOSED",
  "reason_code": "THREATS_OR_COLLECTORS" | "SECOND_NO_SHOW" | "FRAUD" | "POLICY_BREACH" | "OTHER" | null,
  "note": string | null           // required when reason_code = OTHER
}

Response 200: AdminUserDetail  (adds status_reason_code: string | null, status_changed_at: datetime | null)
```

- `reason_code` is **required** when `status = CLOSED`, optional for `SUSPENDED`, ignored (and cleared) for `ACTIVE` and `PENDING_KYC`.
- A CLOSED account can no longer be moved to any status through this endpoint.

### Effect of each status

| Status | Log in, refresh, read | Repay, confirm receipts | New project, application, listing, commitment, investment |
|---|---|---|---|
| ACTIVE, PENDING_KYC | yes | yes | yes (existing eligibility rules still apply) |
| SUSPENDED | **yes (changed)** | yes | **no** |
| CLOSED | **no** | **no** | **no** |

Endpoints refused for a SUSPENDED account (new offers and investments): `POST /projects`, `POST /projects/repeat`, `POST /sme/businesses`, `POST /loans/applications` and `POST /loans/applications/{id}/submit`, `POST /listings`, `POST /listings/{id}/orders`, `POST /funding/windows/{id}/reservations`, `POST /windows/{id}/commitments`. Everything else a suspended account does today with a session keeps working, including withdrawing an existing commitment.

Implementation shape: `get_current_user` stops rejecting SUSPENDED and rejects CLOSED; a new dependency `require_not_suspended` (added to the endpoints above) returns 403. The `allow_suspended` plumbing becomes redundant and is removed once the tests pass.

---

## 5. State Machine

```
ACTIVE ⇄ PENDING_KYC
ACTIVE | PENDING_KYC ⇄ SUSPENDED
ACTIVE | PENDING_KYC | SUSPENDED → CLOSED        (terminal)
```

---

## 6. Business Rules

1. CLOSED needs a `reason_code`; the reason is stored on the user and in the audit entry (`USER_STATUS_CHANGED`, before and after state, actor, IP).
2. CLOSED is permanent through the API.
3. A CLOSED account's open sessions end on the next request (refresh tokens revoked at closure), and it cannot log in, refresh or use Google sign-in.
4. The existing guards stay: no self-change; only a SYSTEM_ADMIN may act on an admin-level account.
5. Login keeps answering "Incorrect email or password" to a wrong password before it reveals CLOSED (no account enumeration).
6. Closing an account does not delete or alter its ledger, transfers, contracts or audit trail.

---

## 7. Error Cases

| Case | Status | `detail` |
|---|---|---|
| CLOSED without `reason_code` | 422 | validation error |
| `reason_code = OTHER` without `note` | 422 | validation error |
| Any change to a CLOSED account | 409 | `This account is closed and cannot be changed` |
| Suspended account on a new-offer or investment endpoint | 403 | `This account is suspended: new offers and investments are not available` |
| Closed account, any authenticated request | 403 | `This account has been closed` |
| Closed account, login, refresh or Google sign-in | 403 | `This account has been closed` (after the password check) |
| Self-change, or non-SYSTEM_ADMIN acting on an admin | 409 / 403 | unchanged |

---

## 8. Acceptance Criteria

Test file: `tests/admin/test_user_status.py` (extended), `tests/market/test_suspended_repayment.py` (rewritten for the new rule).

- `test_close_account_requires_reason_code`
- `test_close_account_with_other_requires_note`
- `test_close_account_records_reason_and_audit_entry`
- `test_closed_account_cannot_be_changed`
- `test_closed_account_session_rejected_on_next_request`
- `test_closed_account_cannot_login_refresh_or_oauth`
- `test_closed_login_wrong_password_does_not_reveal_status`
- `test_close_keeps_self_and_admin_guards`
- `test_suspended_account_can_login_refresh_and_read_profile`
- `test_suspended_sme_refused_new_project_application_listing`
- `test_suspended_investor_refused_new_reservation_and_commitment`
- `test_suspended_account_can_still_repay_and_confirm_receipts`
- `test_suspended_investor_can_withdraw_existing_commitment`
- `test_reactivating_clears_reason_code`
- `test_migration_allows_closed_status_and_downgrades_to_suspended`

---

## 9. Open Questions

1. **Reason codes.** The five codes above are my proposal from T&S 4.3 and 4.4. Loc to confirm or change (Phat's AM-P20 dropdown follows this list).
2. **A suspended lender with an existing pledge.** Should signing the contract and paying for a pledge made before the suspension also be refused (it is new money out)? Proposal: yes, block `POST /tranches/{id}/contract` for a suspended lender; the pledge then lapses at the normal cut-off. Needs Loc.
3. **Money owed by or to a closed account.** A closed SME still owes its lenders, and a closed lender is still owed. Proposal: staff handle it offline and the platform keeps the schedule untouched. Needs Loc and Legal.
4. **Mistaken closure.** With no API reopen, a wrong closure needs a database fix by Edward with an audit entry. Acceptable, or should SYSTEM_ADMIN get a reopen action?
