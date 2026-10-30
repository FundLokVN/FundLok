# HANDOFF-05 — Verifying bank accounts against the bank's holder name

| | |
|---|---|
| **Status** | AGREED (Edward and Loc, Slack, 2026-10-30). Provisional until pilot experience: PROVISIONAL(Q-D18) |
| **From** | Edward (backend, `app/direct_transfer/`) |
| **To** | Phat (admin and user screens, routes, notifications) |
| **Decision** | D-031 in `docs/outstanding-issues.md`. Spec: `docs/specs/direct_transfer/transfer-and-evidence.md`, section "Who verifies an account" |
| **Code** | PR #78 (backend rule, your admin route and schema: small additions, needs your OK) |

## Here is our agreement

1. **One standard, for investors and SMEs:** FundLok name = KYC (investor) or KYB (SME) name = **bank account holder name**. Reason: AML. Money disbursed to an SME can be large, refunds and disputes go back to the account, and they must lead to the verified person or company.
2. **Every saved account starts "waiting verification" (PENDING).** Nobody can invest, reserve, list or open a funding window with it until it is verified. You already built this (PENDING on save; only APPROVED counts).
3. **The name the user types is only a hint.** It is self-declared, so it never approves anything. We still show whether it matches, so staff see it at a glance and the user gets an early warning.
4. **Staff verify it against the bank.** They look up the holder name at the bank (today: type the bank and account number into a bank app's transfer screen; it shows the holder without moving money), compare it with the KYC/KYB name, then approve or reject. They record the holder name exactly as the bank shows it.
5. **Investor accounts:** approved only if the bank's name equals the KYC name. If not, reject it (the investor must fix their KYC or use their own account). No exceptions.
6. **SME accounts:** if the bank's name differs (an abbreviation like "CTY" for "CÔNG TY", a trade name), staff may approve it only by explicitly accepting the difference and writing why (for example "bank letter seen"). A director's personal account is rejected.
7. **A written reason is required for both approval and rejection.** It is logged permanently with the staff member, the time, the holder name they saw and the name expected.
8. **A decision is final.** A rejected user saves a new account for now. Loc wants a proper review/re-submit flow later (Q-030).
9. **Automation later.** A bank-name lookup (it returns the holder name only, never balances or transactions) can replace the staff lookup. The hook exists and is switched off; the provider is Loc's decision (Q-029).

## What you have already built (thank you, nothing to redo)
PENDING on save · `GET /admin/payment-accounts` queue · `POST /admin/payment-accounts/{id}/decision` with an audit row · nobody reviews their own account · only APPROVED lists an SME's project or lets an investor order · an approved SME account publishes its projects.

## Tasks for Phat

**T1. Review the backend changes to your admin files (PR #78).** Small additions, all to match the agreement:
- `POST /admin/payment-accounts/{id}/decision` body gains `verified_holder_name` (string) and `accept_difference` (bool, default false), next to `decision` and `note`.
- `note` is now required for **approval too** (422 `REASON_REQUIRED`).
- Approval requires `verified_holder_name` (422 `HOLDER_NAME_REQUIRED`).
- Errors: 409 `HOLDER_NAME_MISMATCH` (investor: bank name is not the KYC name; SME: differs and `accept_difference` is false), 409 `KYC_NAME_MISSING` (investor with no KYC name), 409 `ALREADY_DECIDED`.
- Queue rows (`AdminPaymentAccountOut`) gain `expected_name` (the KYC name, or the SME's verified KYB company name; null if none) and `name_match` (does the typed name equal it; null if no expected name).

**T2. Staff review screen.** For each pending account show: owner and role, bank, account number, the name the user typed, **the expected KYC/KYB name** and the hint. Then a short instruction: "Look up this account at the bank and type the holder name exactly as it shows." Fields: *Holder name at the bank* (required to approve), *Reason / what you checked* (required for both), and for SME accounts a checkbox *Accept the difference* (enabled only when the names differ). For an investor account whose bank name differs, hide Approve and offer Reject only. Map the error codes above to plain messages.

**T3. User screens (investor and SME).** Labels: **Waiting verification**, **Verified**, **Rejected** (show the reason). Investors: disable Invest until they have a verified account, with a link to add one; SMEs: the same for opening a funding window. When `name_match` is false (in the save response and in the list) show a warning: "The name you entered doesn't match your verified name. We will check it with the bank. If the bank's name differs from your verified name, the account will be rejected." It never blocks saving. Tell the user to save an account **in their own name** (investors: exactly as on their ID).

**T4. Notify the owner of the decision** (in-app, using the existing notification service): "Your bank account ending 9012 is verified" / "was rejected: <reason>". Staff need not email unless you add it.

**T5. Existing data (decide with Edward, Q-028).** Accounts approved on save earlier have `approved_by` NULL and were never checked, for investors as well as SMEs. If any are real pilot accounts, a one-time migration should set them back to PENDING so staff verify them (this affects investors who have already ordered). If all are test data, nothing to do. Please tell us which it is.

**T6. Later (not now): rejected accounts.** Loc wants review/re-submission instead of opening a new account. Edward will spec it first (Q-030). It also needs the database rule that stops a rejected account number being saved again.

## Tasks for Edward
- Backend rule, name lookup hook, tests, spec, register: done in PR #78.
- Q-029: evaluate a bank-name lookup provider with Loc (vendor, cost, terms). Nothing is connected.
- Q-030: design the review/re-submit flow, then hand it to you.
- Staff how-to (one page: how to look up the holder name, what to reject, wording for reasons), written with Loc.

## Staff guide in one paragraph
Open the account, read the expected name. Look up the account at the bank and compare. Investor: bank name must equal the KYC name, otherwise reject with the reason. SME: if it differs only by an abbreviation or trade name and you have confirmed it (bank letter or statement), approve with *Accept the difference* and say why; if it is a person's account, reject. Reject only when it is final; if the user can fix it, leave it waiting and contact them. Always write the reason.
