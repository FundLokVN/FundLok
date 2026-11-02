# Spec: Connect a SePay Account (OAuth2)

| Field | Value |
|---|---|
| **Status** | DRAFT |
| **Owner** | Phat |
| **Implementer(s)** | Phat (backend `app/oauth/sepay/` + frontend) |
| **Module** | `app/oauth/sepay/` |
| **Version** | 1.0 |
| **Date** | 2026-10-30 |
| **Related ADR** | ADR-011 (proposed, amends ADR-009) |
| **Depends on** | `docs/specs/direct_transfer/*` (for phase 2 only) |

---

## 1. Context & Goal

Investors pay SMEs directly by VietQR, and today staff confirm each payment from an uploaded receipt. SePay can report incoming transfers on an account in real time, but only for an account whose owner has granted access. This spec covers the first step: letting an SME or investor connect their own SePay account to FundLok from the profile page, through SePay's OAuth2 flow.

**Goal:** a signed-in SME or investor can connect, view and disconnect their SePay account, and FundLok can list the bank accounts linked to it.

SePay docs: https://developer.sepay.vn/en/sepay-oauth2/tong-quan, https://docs.sepay.vn/oauth2

---

## 2. Out of Scope

- Creating a webhook on the user's SePay account, receiving transfers, and matching them to tranches (phase 2, separate spec touching `app/direct_transfer/`).
- Reading transactions (`GET /api/v1/transactions`). The scope is requested now so users do not have to reconnect for phase 2.
- Using a SePay bank account as a FundLok payment account. `payment_accounts` is unchanged.
- SePay Bank Hub (partner product).
- Admin views of connections.

---

## 3. Data Model

New table `sepay_connections`, one row per user at most.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID FK `users.id` ON DELETE CASCADE, UNIQUE | |
| `access_token` | TEXT NOT NULL | Fernet-encrypted, `v1:` prefix, key `SEPAY_TOKEN_ENCRYPTION_KEY` |
| `refresh_token` | TEXT NOT NULL | Encrypted the same way |
| `access_expires_at` | TIMESTAMPTZ NOT NULL | now + `expires_in` (SePay: 1 hour) |
| `refresh_expires_at` | TIMESTAMPTZ NOT NULL | now + 30 days (SePay: 1 month) |
| `scopes` | TEXT NOT NULL | Space-separated, as granted |
| `status` | TEXT NOT NULL default `ACTIVE` | `ACTIVE` \| `NEEDS_RECONNECT` (CHECK) |
| `connected_at` | TIMESTAMPTZ NOT NULL | |
| `last_refreshed_at` | TIMESTAMPTZ NULL | |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

Disconnecting deletes the row. The audit log keeps the history.

### Configuration

| Setting | Default | Notes |
|---|---|---|
| `SEPAY_CLIENT_ID` | unset | From SePay after approval |
| `SEPAY_CLIENT_SECRET` | unset | Server-side only |
| `SEPAY_TOKEN_ENCRYPTION_KEY` | unset | Fernet key, its own key like `TOTP_ENCRYPTION_KEY` |
| `SEPAY_BASE_URL` | `https://my.sepay.vn` | |
| `SEPAY_REDIRECT_URI` | `{FRONTEND_URL}/dashboard/settings/profile/sepay-callback` | Must match the app registered at SePay exactly |
| `SEPAY_SCOPES` | `bank-account:read transaction:read webhook:read webhook:write webhook:delete` | |

The feature counts as **configured** only when the client ID, client secret and encryption key are all set. Otherwise connect returns 503 and the UI hides the button.

---

## 4. API Contract

All routes need a signed-in user with role `SME` or `INVESTOR`. Errors are `{"detail": "..."}`.

### `GET /integrations/sepay`
```json
{ "configured": true, "connected": true, "status": "ACTIVE",
  "connected_at": "2026-10-30T09:00:00Z", "scopes": ["bank-account:read", "..."] }
```
When there is no connection: `connected: false` and `status`, `connected_at` null, `scopes` empty.

### `POST /integrations/sepay/connect`
Response `200`: `{ "authorize_url": "https://my.sepay.vn/oauth/authorize?response_type=code&client_id=...&redirect_uri=...&scope=...&state=..." }`

`state` is a JWT signed with `SECRET_KEY`: `{"typ": "sepay_oauth_state", "sub": <user id>, "nonce": <random>, "exp": now + 10 min}`. Returns 503 when not configured.

### `POST /integrations/sepay/callback`
Body: `{ "code": "...", "state": "..." }`. The frontend callback page calls this with the query parameters SePay redirected with.
1. Verify `state`: signature, `typ`, not expired, and `sub` equals the signed-in user.
2. Exchange the code: `POST {SEPAY_BASE_URL}/oauth/token`, form-encoded `grant_type=authorization_code, code, redirect_uri, client_id, client_secret`.
3. Upsert the user's row with encrypted tokens and `status = ACTIVE`.
4. Audit `SEPAY_CONNECTION / CONNECT`.

Response `200`: the same body as `GET /integrations/sepay`.

### `GET /integrations/sepay/bank-accounts`
Lists the user's SePay bank accounts (`GET /api/v1/bank-accounts`, every page):
```json
[{ "id": 123, "label": null, "account_number": "123123123123", "account_holder_name": "HUYNH TAN PHAT",
   "bank_short_name": "VietinBank", "bank_full_name": "...", "bank_bin": "970415", "bank_logo_url": "...", "active": true }]
```
`accumulated` (the balance) is deliberately **not** returned or stored.

### `DELETE /integrations/sepay`
Deletes the connection and audits `SEPAY_CONNECTION / DISCONNECT`. Returns `204`, and also `204` when there was no connection.

---

## 5. State Machine

```
(none) --callback ok--> ACTIVE --refresh fails (invalid_grant)--> NEEDS_RECONNECT
ACTIVE | NEEDS_RECONNECT --callback ok--> ACTIVE
any --DELETE--> (none)
```

---

## 6. Business Rules

1. Tokens are only ever used server-side. They are never returned to the browser or written to logs.
2. An access token expiring within 60 seconds is refreshed before use. A SePay 401 triggers one refresh and one retry.
3. Each refresh returns a new refresh token, and **both** new tokens are saved, because the old refresh token stops working.
4. An `invalid_grant` on refresh sets `NEEDS_RECONNECT`. The UI then asks the user to reconnect.
5. The balance (`accumulated`) is never stored or returned.
6. Connecting again replaces the existing tokens. There is one connection per user.
7. Nothing in FundLok depends on a connection existing (ADR-011 §3).

---

## 7. Error Cases

| Case | Status | Detail |
|---|---|---|
| Feature not configured | 503 | `SePay connection is not available.` |
| Role not SME/INVESTOR | 403 | (existing RBAC) |
| `state` invalid, expired or for another user | 400 | `This SePay link has expired. Please try connecting again.` |
| SePay rejects the code (`invalid_grant`) | 400 | `SePay did not accept the sign-in. Please try connecting again.` |
| SePay unreachable or 5xx | 502 | `SePay is not responding. Please try again later.` |
| Bank accounts requested with no connection | 404 | `No SePay account is connected.` |
| Bank accounts requested while `NEEDS_RECONNECT` | 409 | `Your SePay connection has expired. Please reconnect.` |

---

## 8. Acceptance Criteria

- `test_status_when_not_configured_reports_unconfigured`
- `test_connect_returns_authorize_url_with_signed_state`
- `test_connect_refused_when_not_configured`
- `test_callback_stores_encrypted_tokens_and_audits`
- `test_callback_rejects_state_for_another_user`
- `test_callback_rejects_expired_state`
- `test_callback_maps_sepay_invalid_grant_to_400`
- `test_bank_accounts_hides_balance`
- `test_bank_accounts_refreshes_expired_token_and_saves_both_tokens`
- `test_refresh_invalid_grant_marks_needs_reconnect`
- `test_disconnect_deletes_connection_and_audits`
- `test_investor_and_sme_allowed_admin_refused`

---

## 9. Open Questions

1. SePay must approve FundLok's OAuth app (client ID and secret). Who contacts them, and what is the timeline?
2. Does SePay have a token revocation endpoint? The docs only say deleting the app revokes tokens. Until we know, disconnect only deletes our copy, and the UI tells users they can also remove FundLok in SePay.
3. Where does a business set the payment-code format (`FL` + 8 characters) so SePay fills the `code` field? Needed for phase 2.
4. Legal (IBL) review of ADR-011.
