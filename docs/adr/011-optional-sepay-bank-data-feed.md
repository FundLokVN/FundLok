# ADR-011: Optional SePay Bank-Data Feed (Amends ADR-009)

| Field | Value |
|---|---|
| **Status** | Proposed (Phat, 2026-10-30). Needs Edward's review before the branch merges. |
| **Date** | 2026-10-30 |
| **Deciders** | Edward Wong (CTO), Phat Huynh |
| **Amends** | ADR-009, "Bank-data feed (SePay/Casso): needs a licensed partner and SME consent; out of scope until one exists." |
| **Related** | `docs/specs/oauth/sepay-connect.md` |

## Context

ADR-009 makes every transfer confirmation rest on uploaded receipts reviewed by staff. It reserves the `Verified` confirmation level for a future bank-data partner and lists SePay/Casso as out of scope until a partner and SME consent exist.

SePay offers an OAuth2 API. With it, an account owner grants a third-party app read access to their own bank accounts and transactions, plus the right to manage webhooks on their account. FundLok never touches money through it. The SME (or investor) links their bank to SePay themselves, and FundLok only reads what they allow.

## Decision

1. **Optional, user-consented connection.** Any SME or investor can connect their own SePay account to FundLok from their profile page. The consent is SePay's OAuth approval screen plus a statement in our UI of what we read. Users can disconnect at any time.
2. **Read-only use of money data.** The scopes requested are `bank-account:read`, `transaction:read` and `webhook:read|write|delete`. No SePay API moves money, so the connection cannot construct a payment instruction.
3. **Never a dependency.** Receipt upload and staff review stay the path every transfer can complete on. A SePay outage, expired token or disconnect only means confirmations fall back to that path. This respects "never build a production dependency on an endpoint we do not control".
4. **Phase 1 (this branch):** connect, disconnect, connection status and listing the linked bank accounts. Nothing reads transactions or confirms transfers yet.
5. **Phase 2 (separate spec, Edward's area):** create a webhook on the SME's account, receive incoming transfers and mark a matching tranche's transfer `Verified`. That changes `app/direct_transfer/` and needs its own review.

## Consequences

- Positive: payments can be confirmed in minutes without staff work for users who opt in. Businesses that already use SePay need no extra setup.
- Negative: FundLok stores OAuth tokens for a third party's API. They are encrypted at rest with their own key (`SEPAY_TOKEN_ENCRYPTION_KEY`).
- Negative: SePay must approve FundLok's OAuth app before it works anywhere but a sandbox.
- Open: legal (IBL) should confirm that reading an SME's transaction feed with consent is compatible with Decree 94 and does not make FundLok a payment intermediary. FundLok receives data, not funds, so we expect it is.
