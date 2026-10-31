# Spec: Bank feed via SePay webhook (VERIFIED confirmation)

| Field | Value |
|---|---|
| **Status** | DRAFT. **Sandbox spike only** (D-033). Nothing real connects until Loc approves (Q-031) |
| **Owner** | Edward |
| **Implementer(s)** | Phat (webhook endpoint, event table, SePay setup) / Edward (the steps in `app/direct_transfer/`) |
| **Module** | receiver: new integration module (placement open, see §9); steps: `app/direct_transfer/` |
| **Version** | 0.1 |
| **Date** | 2026-10-31 |
| **Related** | D-018 (no bank feed; this would amend it), D-031, D-033, D-034; `transfer-and-evidence.md`; Loc's option 5 ("read-only bank feed on the SME's account") |
| **Sources** | [SePay webhooks](https://developer.sepay.vn/en/sepay-webhooks), [payload](https://developer.sepay.vn/en/sepay-webhooks/tich-hop-webhook), [OAuth 2.0](https://developer.sepay.vn/en/sepay-oauth2/tong-quan). Read 2026-10-31; re-check before building |

---

## 1. Context & Goal

Today every confirmation rests on an uploaded receipt checked by staff (D-018). The forged-receipt risk and the SME's confirm-or-dispute step exist because FundLok has no bank data. SePay watches a bank account that has been connected to it and POSTs each transaction to our server. If an **SME connects its account**, every investor transfer into it arrives as a bank-sourced `in` event, which can be matched to a tranche by reference code, exact amount and the SME's registered account. That is the reserved confirmation level **VERIFIED** (`transfers.confirmation_level`, already in the schema).

**Goal:** define the webhook receiver, the event record, the matching rules, and the idempotency contract between the receiver (Phat) and the steps (Edward), so a duplicate or retried event can never confirm a tranche or post to the ledger twice. Day 1 is synchronous for the money steps; the switch to ARQ is a documented later step (D-034).

**Two different "accounts" are involved; keep them apart.**
- The **watched bank account**: the SME's bank account, the one an investor pays into. SePay watches it for incoming transfers.
- The **SePay account**: the login on SePay's service in which that bank account is connected. It is not a bank account.

Who owns the SePay account is a design choice with legal and security consequences (§6, "SePay set-up"). Until it is answered, this spec does not assume either.

What SePay does **not** do: it does not check anything. It reports transactions on the connected account; **the matching is our code.** It also does not tell us who paid (§6).

## 2. Out of Scope

- **Repayments** (`out` events on the SME's account): phase 2. The webhook has no beneficiary, so confirming that an outgoing transfer reached the right investor needs separate design.
- Investor-side bank links (every investor connecting their own bank). Not planned: too much friction.
- Virtual accounts (SePay's `subAccount`): forbidden, FundLok never has an account in its own name (D-020).
- Choosing the vendor, price, consent wording and IBL's "holding" answer (Q-031, Loc).
- Any production connection.

## 3. Data Model

```
bank_feed_events                       -- one row per provider event; never deleted
├── id                 UUID PK
├── provider           TEXT NOT NULL   -- 'SEPAY'
├── provider_event_id  TEXT NOT NULL   -- SePay's `id`, as text
├── received_at        TIMESTAMPTZ NOT NULL
├── occurred_at        TIMESTAMPTZ NOT NULL   -- `transactionDate` (Vietnam time)
├── transfer_type      TEXT NOT NULL CHECK IN ('IN','OUT')
├── amount_vnd         BIGINT NOT NULL CHECK (amount_vnd > 0)
├── account_number     TEXT NOT NULL   -- the connected account (the SME's, for an IN event)
├── bank_gateway       TEXT NOT NULL   -- `gateway`
├── content            TEXT NOT NULL   -- the memo, verbatim
├── description        TEXT NULL       -- the bank's description; may name the sender; often empty
├── bank_reference     TEXT NULL       -- `referenceCode`
├── raw                JSONB NOT NULL  -- the payload as received, MINUS `accumulated` (see §6)
├── status             TEXT NOT NULL   -- RECEIVED | PROCESSED | UNMATCHED | NEEDS_REVIEW
├── processed_at       TIMESTAMPTZ NULL
├── transfer_id        UUID NULL → transfers(id)
├── failure_code       TEXT NULL
├── attempts           INT NOT NULL DEFAULT 1   -- deliveries seen for this event
UNIQUE (provider, provider_event_id)
```
`raw` and the identifying columns are never updated; only `status`, `processed_at`, `transfer_id`, `failure_code` and `attempts` change. Every status change is also an append-only `evidence_log` row.

## 4. API Contract

`POST /webhooks/sepay` (called by SePay, not by users). Authenticated (§6). Body: SePay's payload, 12 fields (`id`, `gateway`, `transactionDate`, `accountNumber`, `subAccount`, `code`, `content`, `transferType`, `description`, `transferAmount`, `accumulated`, `referenceCode`).

- **200** `{"success": true}` once the event is stored (and, day 1, processed): also for an event already PROCESSED (idempotent).
- **non-2xx** on any failure to store or process: SePay then retries (up to 7 times over about 33 minutes) and that is our free retry on day 1.
- **401** if authentication fails (and nothing is stored).
- Time budget: SePay waits about 8 seconds (5 s to connect). Stay well under 3 seconds.

## 5. State Machine

```
(arrives) → RECEIVED → PROCESSED          matched; transfer VERIFIED
                     → UNMATCHED          no tranche/reference found: staff queue
                     → NEEDS_REVIEW       found but wrong: amount differs, hold expired, second payment, wrong payee account
```
A retry of a `RECEIVED` event **resumes** it. A retry of a finished event only increments `attempts` and returns 200.

## 6. Business Rules

**Idempotency, the contract between Phat and Edward (D-034).** Two layers, two owners; neither excuses the other.
1. **The door (Phat):** store the event under `UNIQUE(provider, provider_event_id)`; on a duplicate, do not start new work; resume it if `RECEIVED`, acknowledge it if finished; mark `PROCESSED` only after every step returned; reply 200.
2. **Every step (Edward):** each function takes the key `sepay:<id>` and is safe to call any number of times with it: the second call changes nothing and returns the same result. No step may assume the door deduplicated.

**Day-1 processing (D-034, PROVISIONAL(Q-D19)).**
- **Money steps are synchronous, in one database transaction:** store the event, match it, set the transfer to VERIFIED, update the tranche, post the ledger entry. If anything fails the transaction rolls back and the endpoint returns non-2xx, so SePay retries from a clean state.
- **Side effects go to ARQ after the commit:** notifications, email, SMS/Zalo. Never inside the 8-second window.
- **Reconciliation job (ARQ, scheduled):** compare our events with SePay's transactions API for the connected accounts and fill any gap (SePay's retry window is about 33 minutes; our downtime can be longer). Unknown differences go to the staff queue.
- **Switch to asynchronous (documented, later):** the door replies 200 right after storing the event and an ARQ job runs the money steps; only the door changes, because the steps are already repeatable. Triggers to watch, PROVISIONAL numbers: p95 handling time above 3 seconds over a day, or SePay retrying more than 1% of events because we exceeded its time limit, or a step that must call a slow external service. If the switch is made, add our own retry and a failed-jobs list: SePay will no longer retry for us.

**Matching an `IN` event to a tranche.** All must hold, else UNMATCHED or NEEDS_REVIEW:
- A reference `FL[A-Z0-9]{8}` is found in `content` after normalizing it (upper-case, spaces and punctuation removed: banks mangle memos). SePay's `code` field is not used.
- `amount_vnd` equals the tranche amount exactly.
- `account_number` equals the SME's registered payee account on the window, and the bank is the same bank.
- The tranche is SIGNED or ATTESTED (the QR is shown after signing). A late payment (hold expired) or a second payment for a tranche already VERIFIED is NEEDS_REVIEW (overpayment, staff decide).

**What VERIFIED changes (PROVISIONAL(Q-D19)).** The transfer's `confirmation_level` becomes VERIFIED. A tranche reaches CONFIRMED when **both** the bank has verified the money (feed) **and** the receipt's payer name matched the investor's KYC name (ATTESTED). The SME's confirm-or-dispute step is no longer needed for that tranche. If no feed event arrives, the existing flow applies unchanged (SME confirms, or the dispute window lapses): the feed is an upgrade, not a dependency.

**AML: who paid (Q-032).** The payload has **no payer name or account field**. `description` ("full description from the bank") sometimes carries the sender's name (SePay's sample shows `"NGUYEN VAN A chuyen tien"`) but is empty for banks that do not support it, and the docs do not say where it comes from. So the feed proves the **money arrived**, not **who sent it**. Default: **keep the receipt** for the payer-name check against the KYC name (Loc's standard: KYC name = bank name). Where `description` is present, cross-check it against the receipt's payer name and send a difference to staff. The spike must measure which banks fill `description`.

**SePay set-up: whose SePay account holds the connection (open, Q-031; default B).**
- **A. FundLok's single SePay account.** FundLok connects each SME's bank account under it. FundLok then effectively holds every SME's bank connection, which is heavier legally and in security (Loc's rule: never handle an SME's banking password), and FundLok pays SePay.
- **B. Each SME has its own SePay account.** The SME connects its own bank account and authorizes FundLok through SePay's OAuth with **read** permission (`bank-account:read`, `transaction:read`). The SME can revoke it; FundLok never touches bank credentials. More onboarding for the SME. Who pays SePay is unknown. Under B, the webhook is registered on the SME's SePay account (the OAuth `webhook:write` scope) or by the SME, pointing at our endpoint, so the receiver must tell SMEs apart: by the connected `accountNumber`, which must equal the SME's registered payee account.
- **Default and recommendation: B**, pending Loc and IBL (consent wording, "holding" question). The receiver in §4 works for either; only the onboarding and the authentication secret per SePay account differ.

**How the bank is connected to SePay (open).** Only banks that SePay connects through the bank's own OAuth or official API are acceptable. A bank that needs the SME's internet-banking login to be handed to SePay is excluded (Loc's rule). SePay's docs name some official-API banks (ACB, VietinBank, SHB in the changelog) and I could not find a complete list: Phat to establish it.

**Data minimization.** `accumulated` is the SME's account balance after each transaction. Strip it before storing and logging. Say so in the consent wording (Q-031).

**Security.** Authenticate every call: SePay supports HMAC-SHA256, an API key and OAuth 2.0 for webhook calls; we prefer HMAC-SHA256; compare in constant time over HTTPS; reject and store nothing otherwise. Rate-limit the route. The endpoint URL is not a secret. Never put an SME's banking password anywhere: only banks connected through OAuth or an official API (Loc's design document, "Security of a bank feed"). The OAuth webhook scopes (`webhook:write`, `webhook:delete`) can change webhooks: request only the read scopes we need.

## 7. Error Cases

| Case | Result |
|---|---|
| Bad or missing signature/API key | 401, nothing stored |
| Same event again, already PROCESSED | 200, `attempts` incremented, no work |
| Same event again, RECEIVED (an earlier attempt crashed) | resumed; steps repeat safely |
| Reference not found | event kept, UNMATCHED, 200 |
| Amount differs / wrong payee account / second payment / hold expired | NEEDS_REVIEW, 200 |
| `out` event | stored, status PROCESSED with no action (phase 2) |
| Database failure while storing | non-2xx; SePay retries |
| Our deploy down for longer than about 33 minutes | reconciliation job fills the gap |

## 8. Acceptance Criteria

`tests/bank_feed/` (the receiver) and `tests/direct_transfer/test_bank_feed_steps.py` (the steps). Each name is a test.

- [ ] `test_valid_in_event_verifies_the_tranche_and_posts_once`
- [ ] `test_same_event_twice_changes_nothing_the_second_time`
- [ ] `test_retry_of_a_received_event_resumes_it`
- [ ] `test_each_step_called_twice_with_the_same_key_changes_nothing`
- [ ] `test_failure_mid_transaction_rolls_back_and_returns_non_2xx`
- [ ] `test_event_is_matched_after_the_bank_mangles_the_memo`
- [ ] `test_wrong_amount_goes_to_needs_review`
- [ ] `test_wrong_payee_account_goes_to_needs_review`
- [ ] `test_second_payment_for_a_verified_tranche_goes_to_needs_review`
- [ ] `test_unknown_reference_is_stored_unmatched`
- [ ] `test_out_event_is_stored_and_ignored`
- [ ] `test_tranche_confirms_only_when_feed_and_receipt_both_pass`
- [ ] `test_without_a_feed_event_the_existing_flow_still_confirms`
- [ ] `test_accumulated_is_never_stored_or_logged`
- [ ] `test_bad_signature_is_rejected_and_stores_nothing`
- [ ] `test_reconciliation_fills_a_missed_event_once`

## 9. Open Questions

| # | Question | Owner |
|---|---|---|
| 1 | **Q-031:** approve the bank feed for SME accounts (amends D-018): vendor, price, consent wording, IBL's "holding" answer, OAuth/official-API banks only | Loc |
| 2 | **Q-032:** keep the receipt for the payer-name check (default yes) or accept a weaker payer check | Edward, Loc |
| 3 | Which **SePay set-up** is assumed: A (FundLok's one SePay account) or B (each SME's own SePay account, FundLok authorized by OAuth)? Default B. And his flow diagram | Phat |
| 4 | How the SME's **bank** is connected to SePay: the bank's OAuth or official API, or the SME's internet-banking login (excluded)? Which banks work the acceptable way? | Phat |
| 5 | Which banks fill `description` with the sender's name (spike measurement) | Phat, Edward |
| 6 | Where the receiver lives (a new `app/bank_feed/`, or under `app/market/`) | Edward, Phat |
| 7 | Repayments: confirming `out` events (phase 2) | Edward |

## 10. Handoff: the spike (D-033), who does what

**Phat (webhook receiver, sandbox only):**
1. Reply to Edward with the flow diagram and answers to §9 items 3 and 4: which SePay set-up you assume (A: FundLok's one SePay account, or B: each SME's own SePay account with FundLok authorized by OAuth), and how an SME's bank is connected to SePay (the bank's OAuth or official API, or the SME's banking login) and for which banks.
2. Create the event table (§3) with `UNIQUE(provider, provider_event_id)` and an Alembic migration (chain it off the current head; run `scripts/premerge_check.sh <PR>` before merging).
3. Build `POST /webhooks/sepay` (§4): authenticate (HMAC-SHA256 or API key), strip `accumulated`, store, call the steps with the key `sepay:<id>`, mark `PROCESSED` only after they return, reply 200 `{"success": true}`; non-2xx on any failure. Day-1 design: money steps in one transaction, side effects to ARQ after commit (D-034).
4. Use SePay's **test mode** ("simulate bank transactions"): no real bank account, no production URL.
5. Log, for the spike's measurement: how often `description` carries a sender name, per bank (Q-032), and the handling time per event.

**Edward (the steps, `app/direct_transfer/`):** the matching function, VERIFIED on the transfer, the tranche rule (§6), the ledger posting, each safe to call repeatedly with a key; the reconciliation job; the `test_bank_feed_steps.py` tests (§8). The steps are written before the receiver calls them, so the contract in §6 is testable from both sides.

**Not yet (blocked on Loc, Q-031):** any real SME account, any production webhook, any promise to SMEs.
