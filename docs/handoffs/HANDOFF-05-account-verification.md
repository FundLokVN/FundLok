# HANDOFF-05 — Verifying bank accounts against the bank's holder name

| | |
|---|---|
| **Status** | AGREED (Edward and Loc, Slack, 2026-10-30). Provisional until pilot experience: PROVISIONAL(Q-D18) |
| **From** | Edward |
| **To** | **Phat: owns this task end to end, backend and UI** (D-035) |
| **Decision** | D-031 in `docs/outstanding-issues.md`. Spec: `docs/specs/direct_transfer/transfer-and-evidence.md`, section "Who verifies an account" |
| **Code** | PR #78 holds the backend rule as a starting point. Phat takes it over: push to the same branch (`feature/direct-transfer/sme-account-name-match`), change anything that needs changing |

## Ownership and how we stay in sync (read this first)

**Phat owns this task completely: the backend rule, the admin and user screens, the notifications, the tests.** For this task only, Phat may edit `app/direct_transfer/accounts.py`, `app/direct_transfer/name_lookup.py`, their tests, the admin route/schema/service, and the frontend. That is an agreed exception to the module boundaries in CLAUDE.md (D-035). It does **not** cover the ledger, the tranche state machine, fees, or migrations on any other table: ask Edward first. Edward reviews and approves; he does not code this task.

How we stay in sync (written down so nobody has to remember it):
1. **Plan first.** Before coding, post a short comment on PR #78: what you will change and in which files; which decisions you rely on (D-031, D-032, this file); anything here you disagree with or find unclear; and how backend and UI will go live together (see "Definition of done"). That comment is the read-back that proves we understood each other. Edward answers within 24 hours; **if there is no answer after 24 hours, go ahead.**
2. **Decisions already made are not re-opened in code.** If you think one is wrong, add a `Q-` entry to `docs/outstanding-issues.md` (options, a default, an owner) and ask in Slack. Do not edit a decided item or quietly build something different (the lesson of #76).
3. **Docs change in the same PR as the behaviour.** If the PR changes what the system does, it also updates the spec section "Who verifies an account" (`transfer-and-evidence.md`), this file's checklist, and the register. A PR that changes behaviour without touching docs is not done.
4. **Before merging:** run `./scripts/docker_test.sh` (or the suite via your venv, see `docs/testing.md`) and `scripts/premerge_check.sh <PR>` (it must say GO). This touches AML (who may receive and send money), so **Edward approves the PR in writing before you merge it**.
5. **After merging:** tick the checklist below, set the register entry to its final state, and say so in the Slack channel.
6. **Stuck or unsure:** ask in Slack in this form: the question, two or three options, your recommendation, and what you will do if nobody answers by a stated time. We are 12 hours apart, so every message should carry enough context to act on without a reply.

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

**T1. Take over the backend (PR #78).** It already contains a first version of the rule; keep, change or replace it as needed so that it matches the agreement. What it does today:
- `POST /admin/payment-accounts/{id}/decision` body gains `verified_holder_name` (string) and `accept_difference` (bool, default false), next to `decision` and `note`.
- `note` is now required for **approval too** (422 `REASON_REQUIRED`).
- Approval requires `verified_holder_name` (422 `HOLDER_NAME_REQUIRED`).
- Errors: 409 `HOLDER_NAME_MISMATCH` (investor: bank name is not the KYC name; SME: differs and `accept_difference` is false), 409 `KYC_NAME_MISSING` (investor with no KYC name), 409 `ALREADY_DECIDED`.
- Queue rows (`AdminPaymentAccountOut`) gain `expected_name` (the KYC name, or the SME's verified KYB company name; null if none) and `name_match` (does the typed name equal it; null if no expected name).

**T2. Staff review screen.** For each pending account show: owner and role, bank, account number, the name the user typed, **the expected KYC/KYB name** and the hint. Then a short instruction: "Look up this account at the bank and type the holder name exactly as it shows." Fields: *Holder name at the bank* (required to approve), *Reason / what you checked* (required for both), and for SME accounts a checkbox *Accept the difference* (enabled only when the names differ). For an investor account whose bank name differs, hide Approve and offer Reject only. Map the error codes above to plain messages.

**T3. User screens (investor and SME).** Labels: **Waiting verification**, **Verified**, **Rejected** (show the reason). Investors: disable Invest until they have a verified account, with a link to add one; SMEs: the same for opening a funding window. When `name_match` is false (in the save response and in the list) show a warning: "The name you entered doesn't match your verified name. We will check it with the bank. If the bank's name differs from your verified name, the account will be rejected." It never blocks saving. Tell the user to save an account **in their own name** (investors: exactly as on their ID).

**T4. Notify the owner of the decision** (in-app, using the existing notification service): "Your bank account ending 9012 is verified" / "was rejected: <reason>". Staff need not email unless you add it.

**T5. Existing data: CLOSED (D-032, Edward 2026-10-30).** The accounts approved on save earlier are test data, not real accounts, so nothing to migrate and nothing for you to do. One reminder for later: before any real user data exists, clear the test data or re-verify any account with `approved_by` NULL.

**T6. Later (not now): rejected accounts.** Loc wants review/re-submission instead of opening a new account. Edward will spec it first (Q-030). It also needs the database rule that stops a rejected account number being saved again.

## Tasks for Edward
- Review Phat's plan comment (within 24 hours) and approve the PR before merge.
- Q-029 (which bank-name lookup provider) and Q-030 (review instead of a new account), with Loc: these are decisions and designs, not part of this task's code.
- The staff guide (one page, with Loc).
- No coding on this task. If something in `app/direct_transfer/` needs a change beyond the exception above, Edward does it on Phat's request.

## Definition of done
- [ ] Plan comment posted on #78 and answered (or 24 hours passed)
- [ ] Admin approve sends `note` and `verified_holder_name` (and `accept_difference` for an SME); errors mapped to plain messages (`HOLDER_NAME_REQUIRED`, `HOLDER_NAME_MISMATCH`, `KYC_NAME_MISSING`, `REASON_REQUIRED`, `ALREADY_DECIDED`)
- [ ] An investor account cannot be approved when the bank's name differs from the KYC name; an SME difference needs the explicit acceptance and a reason
- [ ] User screens show Waiting verification / Verified / Rejected (with the reason) and the name-mismatch warning; Invest and Open-window are blocked until verified
- [ ] The owner is notified of the decision
- [ ] Tests for the backend rule and the UI; `premerge_check.sh` says GO; the suite passes
- [ ] **Backend and UI go live together.** The backend now requires `verified_holder_name` and a note on approval, so the old admin screen would get errors: merge both on the same day, or add a temporary setting that keeps the old behaviour until the UI is deployed (Phat's choice; record which)
- [ ] Docs updated (spec section, this checklist, register) in the same PR
- [ ] Edward approved the PR in writing
- [ ] After merge: Slack message posted and the register set to its final state

## Starting instructions for a Claude session working on this task
Paste or follow this at the start of the session:
1. Read `CLAUDE.md`, this file, `docs/specs/direct_transfer/transfer-and-evidence.md` (section "Who verifies an account"), and the register entries D-031, D-032, D-035, Q-D18, Q-029, Q-030 in `docs/outstanding-issues.md`.
2. Do not change a decided rule in code. If you think one is wrong, say so and write a `Q-` entry; do not build around it.
3. Post the plan as a comment on PR #78 before changing code, then wait up to 24 hours for Edward's reply.
4. Stay inside the files this task may touch (section "Ownership"). Ask before touching anything else.
5. Run the tests with `./scripts/docker_test.sh`; run `scripts/premerge_check.sh <PR>` before merging; update the docs in the same PR.
6. Search the repo's docs with the `search_fl_docs` tool if it is available; otherwise read the files above.

## Staff guide in one paragraph
Open the account, read the expected name. Look up the account at the bank and compare. Investor: bank name must equal the KYC name, otherwise reject with the reason. SME: if it differs only by an abbreviation or trade name and you have confirmed it (bank letter or statement), approve with *Accept the difference* and say why; if it is a person's account, reject. Reject only when it is final; if the user can fix it, leave it waiting and contact them. Always write the reason.
