# Spec: Change an existing account's role (promote to admin, demote)

| Field | Value |
|---|---|
| **Status** | REVIEW |
| **Owner** | Phat |
| **Implementer(s)** | Phat (backend + frontend) |
| **Module** | `app/admin/` |
| **Version** | 1.0 |
| **Date** | 2026-11-23 |
| **Related ADR** | none |
| **Depends on** | `POST /admin/users/admins` (invite) and `PATCH /admin/users/{id}/status` in `app/admin/router.py` |

---

## 1. Context & Goal

Staff could only grant admin access by inviting a brand-new email (`POST /admin/users/admins`, SYSTEM_ADMIN only).
That route deliberately refuses an email that already has an account, so a teammate who signed up as an investor,
or never picked a role, could not be made an admin without a second account. Nothing could take admin access away
again either, short of suspending the account.

**Goal:** staff can change an existing account's role from the admin users table. ADMIN and SYSTEM_ADMIN can
promote an account to ADMIN. Only a SYSTEM_ADMIN can grant SYSTEM_ADMIN or change an admin-level account's role
(demotion included). Every change is audited and signs the account out everywhere.

---

## 2. Out of Scope

- Inviting new admins. `POST /admin/users/admins` is unchanged and stays SYSTEM_ADMIN only.
- Moving an account between INVESTOR and SME, or from no role to INVESTOR/SME. A member picks that role
  themselves at `/select-role`.
- Moving an account back to "no role". A demoted admin is given INVESTOR or SME explicitly.
- Emailing or notifying the account about the change.
- Accounts that hold both investor and SME activity. A user has one role.

---

## 3. Data Model

No schema change. `users.role` is free text (`SME`, `INVESTOR`, `ADMIN`, `SYSTEM_ADMIN`, or null) and stays so.
The change writes:

- `users.role`: the new role.
- `refresh_tokens.revoked_at` = now on every live token of the account (every session ends).
- One `audit_logs` row (below).

### Audit row

```
audit_logs
├── entity_type   'USER'
├── entity_id     target user id
├── action        'USER_ROLE_CHANGED'
├── actor_id      the admin who made the change
├── before_state  {"role": <old role or null>}
├── after_state   {"role": <new role>,
│                  "direction": "PROMOTION" | "DEMOTION",
│                  "note": <text or null>,
│                  "sessions_revoked": <int, sessions ended>,
│                  "is_external_domain": <bool, email not @fundlok.com>}
└── ip_address    request.client.host
```

`direction` is `PROMOTION` when the new role ranks above the old one, otherwise `DEMOTION`. Ranking:
no role / INVESTOR / SME < ADMIN < SYSTEM_ADMIN.

---

## 4. API Contract

### `PATCH /admin/users/{user_id}/role`

Auth: Bearer session with a second factor, role ADMIN or SYSTEM_ADMIN
(`require_roles(Role.ADMIN, Role.SYSTEM_ADMIN)`). Not a GET, so it is not in the read-audit list.

Request:

```json
{ "role": "ADMIN", "note": "Joins the operations team" }
```

| Field | Type | Rules |
|---|---|---|
| `role` | `"ADMIN" \| "SYSTEM_ADMIN" \| "INVESTOR" \| "SME"` | required |
| `note` | `string \| null` | optional, at most 500 characters |

Unknown fields → 422 (`extra="forbid"`).

Response `200`: `AdminUserDetail`, the same body as the status route:

```json
{ "id": "…", "email": "…", "full_name": "…", "role": "ADMIN", "status": "ACTIVE",
  "email_verified": true, "created_at": "…" }
```

---

## 5. State Machine

```
(none) | INVESTOR | SME ──promote──▶ ADMIN            actor: ADMIN or SYSTEM_ADMIN
(none) | INVESTOR | SME ──promote──▶ SYSTEM_ADMIN     actor: SYSTEM_ADMIN
ADMIN                   ──promote──▶ SYSTEM_ADMIN     actor: SYSTEM_ADMIN
SYSTEM_ADMIN            ──demote───▶ ADMIN            actor: SYSTEM_ADMIN
ADMIN | SYSTEM_ADMIN    ──demote───▶ INVESTOR | SME   actor: SYSTEM_ADMIN
```

Every other move is refused (Error Cases).

---

## 6. Business Rules

1. **Who may call.** Only ADMIN and SYSTEM_ADMIN reach the route. INVESTOR, SME and roleless accounts get 403
   from the route guard, before any lookup.
2. **No self-change.** Nobody changes their own role. Promoting yourself is escalation; demoting yourself can
   lock the last operator out.
3. **Admin-level accounts are SYSTEM_ADMIN's.** Only a SYSTEM_ADMIN may change the role of an ADMIN or
   SYSTEM_ADMIN account. This is the same line `PATCH /admin/users/{id}/status` draws.
4. **Granting SYSTEM_ADMIN is SYSTEM_ADMIN's.** An ADMIN can grant ADMIN, never a role above their own.
5. **Only admin promotions and demotions.** A non-admin account can only move to ADMIN or SYSTEM_ADMIN. An admin
   account can move to any other role. INVESTOR ↔ SME and none → INVESTOR/SME are refused.
6. **Promotion preconditions** (old role is none, INVESTOR or SME):
   1. The account is not SUSPENDED. Restore it first.
   2. Its email is verified. An unverified `name@fundlok.com` account may be someone squatting a staff address.
   3. It has no money in progress. A user has one role, so promotion would cut them off from screens they still
      need. In progress means any of:
      - a project still in progress that they own (`app/projects/borrower.unfinished_project`: not closed or
        archived, its request not ended);
      - as a lender, in a funding round that is still live (`WINDOW_LIVE_STATES`): a live tranche
        (`TRANCHE_LIVE_STATES`, including repaying ones), a PLEDGED commitment, or a PLEDGED/SIGNED top-up.
      Service agreements with FundLok (Contract 2, Contract 4) alone do not block. They are agreements with
      FundLok, not money owed between members.
7. **Never the last SYSTEM_ADMIN.** Demoting a SYSTEM_ADMIN needs at least one other non-suspended SYSTEM_ADMIN
   left. Rule 2 already guarantees this for one request at a time. The check exists for two SYSTEM_ADMINs
   demoting each other at once. Every role change locks the target and all SYSTEM_ADMIN rows in one statement,
   in id order, so concurrent changes queue instead of deadlocking.
8. **Every change ends every session** of the account, in the same transaction as the role write. The account
   signs in again under its new role. An admin role needs a second factor (`app/utils/jwt.SECOND_FACTOR_ROLES`),
   so a promoted account sets one up at that sign-in. An old (possibly stolen) investor session cannot carry on
   as an admin session.
9. **Every change is audited** (Data Model) in the same transaction, with the caller's IP.

---

## 7. Error Cases

Every refusal carries `X-Error-Code`. Checks run in this order.

| # | Condition | Status | `X-Error-Code` |
|---|---|---|---|
| 1 | Not signed in | 401 | none |
| 2 | Caller is not ADMIN / SYSTEM_ADMIN | 403 | none (route guard) |
| 3 | Body invalid (unknown role, extra field, note > 500) | 422 | none |
| 4 | No such user | 404 | `USER_NOT_FOUND` |
| 5 | Target is the caller | 409 | `ROLE_CHANGE_SELF` |
| 6 | Target is ADMIN/SYSTEM_ADMIN and caller is not SYSTEM_ADMIN | 403 | `ROLE_CHANGE_FORBIDDEN` |
| 7 | New role is SYSTEM_ADMIN and caller is not SYSTEM_ADMIN | 403 | `ROLE_CHANGE_FORBIDDEN` |
| 8 | New role equals current role | 409 | `ROLE_UNCHANGED` |
| 9 | Target is not admin-level and new role is INVESTOR/SME | 409 | `ROLE_CHANGE_NOT_SUPPORTED` |
| 10 | Promotion of a SUSPENDED account | 409 | `ROLE_CHANGE_SUSPENDED` |
| 11 | Promotion of an account whose email is unverified | 409 | `ROLE_CHANGE_EMAIL_UNVERIFIED` |
| 12 | Promotion of an account with a project in progress | 409 | `ROLE_CHANGE_OPEN_PROJECT` |
| 13 | Promotion of an account with money in a live funding round | 409 | `ROLE_CHANGE_LIVE_INVESTMENT` |
| 14 | Demoting the last non-suspended SYSTEM_ADMIN | 409 | `LAST_SYSTEM_ADMIN` |

A refused change writes nothing: no role change, no session revoked, no audit row.

---

## 8. Acceptance Criteria

Test file: `tests/admin/test_user_role_change.py`.

- `test_admin_promotes_a_roleless_account_to_admin`
- `test_admin_promotes_an_investor_with_no_live_money`
- `test_system_admin_promotes_a_member_to_system_admin`
- `test_system_admin_promotes_an_admin_to_system_admin`
- `test_admin_cannot_grant_system_admin`
- `test_admin_cannot_change_another_admins_role`
- `test_system_admin_demotes_an_admin_to_investor`
- `test_system_admin_demotes_a_system_admin_to_admin`
- `test_investor_and_sme_cannot_change_roles` (403)
- `test_anonymous_caller_is_refused` (401)
- `test_nobody_changes_their_own_role`
- `test_unknown_user_is_404`
- `test_same_role_is_refused`
- `test_member_to_member_moves_are_refused`
- `test_unknown_role_and_extra_fields_are_422`
- `test_suspended_account_cannot_be_promoted`
- `test_unverified_email_cannot_be_promoted`
- `test_sme_with_a_project_in_progress_cannot_be_promoted`
- `test_investor_in_a_live_round_cannot_be_promoted`
- `test_last_system_admin_cannot_be_demoted`
- `test_promotion_writes_one_audit_row_with_actor_roles_and_ip`
- `test_refused_change_writes_nothing`
- `test_role_change_signs_the_account_out_everywhere`
- `test_promoted_account_needs_a_second_factor_after_signing_in_again`

---

## 9. Open Questions

- Q1. Should the account be emailed when its role changes? Not built. The audit row is the record today.
- Q2. Edward to confirm the "money in progress" definition (rule 6.3) covers every live direct-transfer state.
