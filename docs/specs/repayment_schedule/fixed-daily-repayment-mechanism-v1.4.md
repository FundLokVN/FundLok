<!--
SOURCE OF TRUTH for v1.4. Drafted by Edward Wong from `docs/FundLok - Extension
model v3.2 (27 Sep 2026).pdf`, which replaces v3/v3.1 of the extension model and
supersedes the extension-pricing mechanism in v1.3 (Loc Vuong, 27 Aug 2026).
v1.3 remains untouched at `fixed-daily-repayment-mechanism-v1.3.md` per its own
note ("Loc owns the .docx, do not edit to change the spec") -- this is a new
version file, not an edit to that one. Per CLAUDE.md: if it is not written
down, it does not exist. Sections unaffected by the extension-model change are
carried forward from v1.3 verbatim; sections that changed are rewritten below
and flagged inline. Everything this document infers beyond what the source PDF
states verbatim is marked OPEN ITEM rather than asserted as decided.
-->

# FundLok — Fixed-Daily Repayment Mechanism
## System Design Specification (v1.4)

| Document | Fixed-Daily Repayment Mechanism — System Design Specification |
|---|---|
| Version | 1.4 |
| Date | 27 September 2026 |
| Author | Edward Wong (edward.wong@fundlok.com) |
| Audience | FundLok engineering / system design |
| Status | Draft for implementation. Extension mechanics are decided per source PDF; several adjacent mechanics the PDF does not specify are listed as open items (§13) rather than invented. |
| Supersedes | v1.3's extension-pricing mechanism only (§4.2–4.4, D6, D7, D21, INV-13/14/15). Everything else in v1.3 (origination, daily collection, backstop, terminal states) carries forward unchanged. |
| Change in v1.4 | IBL counsel resolved OP-12 (v1.3 §13): an approved extension is not "quá hạn" (overdue) under Điều 466.5(b), so the statutory 150% late-rate basis does not survive being framed as an approved new term. The late-repayment uplift (D6/D7) is **retired for extension months**. Extensions now re-price the outstanding balance at the **original contract rate r** over the new total term, plus a separate small flat extension fee that must sit outside the 20% cap. The 150% late rate is retained for genuinely missed instalments **outside** an approved extension (arrears) — see OP-14, mechanics not yet specified. D21 (uplift rebate) is retired: there is no longer an uplift to rebate. Affects: D6, D7, D21 (all retired, replaced below); §4.2–4.4 rewritten; §7.2 rewritten to the exact undersell/extension-length formula; data model changes to `revenue_submission` / `extension` / `fee_ledger`; INV-13/14/15 retired; §12.2/12.3 worked examples replaced with the source PDF's own worked example; new open items OP-14 through OP-18. |
| Change in v1.3 | The extension charge is now a LATE-REPAYMENT UPLIFT: extension months bear 1.5x the contract rate per Điều 466.5(b) BLDS 2015, capped at 30%. It passes through to investors. Early repayment inside the original term rebates it. §4.2-§4.4 rewritten; D6, D7 revised; D21-D22 added. **(Retired in v1.4 — see above.)** |
| Change in v1.2 | FundLok has NO direct-debit / pull capability and no OpenAPI banking access. All collection is SME-initiated PUSH, reconciled against inbound transfers. §6 rewritten; D9 revised; D18-D20 added. |
| Change in v1.1 | The terminal rule is no longer a 24-month write-off. It is ACCELERATION at 1.33x the declared term: the full remaining balance falls due. Affects D13, D15-D17, §4.5, §5, §7.3, §10, §11, §12. |
| Currency | VND, integer đồng. No sub-unit. No decimals anywhere in the system. |

Why this exists. v1.3 specified the fixed-daily mechanic replacing the revenue-sweep model. This version updates only the extension-pricing piece of that mechanic, because the legal basis v1.3 relied on for pricing extension days (D6/D7, D22) did not survive counsel review. Everything else in v1.3 stands.


## 1  Purpose and scope

### 1.1  What this specifies

The complete repayment mechanic for a FundLok SME facility: how the obligation is sized at origination, how it is collected daily, what happens when a collection fails, how and when the schedule may be extended, and how the facility terminates.

### 1.2  In scope

Origination sizing and schedule generation

Daily collection, retry, arrears and recovery escalation

Month-end review and the extension (relief) path

The extension fee and its cap-exemption conditions

Early repayment

Terminal states: settlement and write-off

Data model, emitted events, and system invariants

### 1.3  Out of scope

Each of the following is a separate workstream and must not be inferred from this document.

| Excluded | Where it lives |
|---|---|
| Credit scoring / underwriting | Underwriting spec. This document consumes the output rate r as an input. |
| Investor-side allocation & returns | Investor platform spec. This document only emits repayment events. |
| Disbursement and fund custody | Bank/FBO custodial workstream (VPBank). This document starts at DISBURSED. |
| Revenue-data acquisition & integration | E-invoice data workstream. §7 treats revenue evidence as an input. |
| Tamper / fraud detection on revenue evidence | Deferred by decision, 27 Aug 2026. Separate spec. |
| Contract drafting and legal wording | Legal workstream. §4.3 states the compliance arithmetic only. |
| Sizing the extension fee itself, and its cost-basis documentation | Not yet done — OP-17. This spec states the *conditions* the fee must meet (§2, D24), not its amount. |


## 2  Locked design decisions

These are settled per the source PDF and v1.3 where unaffected. IDs D1–D22 are as numbered in v1.3; retired ones are marked so their history is traceable. New decisions continue from D23.

| ID | Decision | Value | Decided |
|---|---|---|---|
| D1 | Repayment mechanic | Fixed daily instalment. NOT a revenue sweep. | 27 Aug 2026 |
| D2 | Daily floor level | 100% of the scheduled daily amount. Hard. No tolerance band, no grace amount. | 27 Aug 2026 |
| D3 | True-up direction | DOWNSIDE ONLY. The floor is also a ceiling — the daily amount never rises above its current value. Revenue above expectation produces NO change. | 27 Aug 2026 |
| D4 | Total repayable | Fixed at origination. Increases ONLY by an approved extension's re-priced interest (D23) and any extension fee ledgered separately (D24). Never otherwise. | 27 Aug 2026, revised 27 Sep 2026 |
| D5 | Duration | Variable. Extends on approved relief. Never shortens automatically (follows from D3). | 27 Aug 2026 |
| ~~D6~~ | ~~Late-repayment uplift, 1.5× contract rate on extension months~~ | **RETIRED.** See D23. | Retired 27 Sep 2026 |
| ~~D7~~ | ~~Uplift passes through to investors~~ | **RETIRED.** See D24. | Retired 27 Sep 2026 |
| D8 | Early repayment | Permitted, no penalty, no discount. SME-initiated only. Same total, received sooner. Interaction with an in-progress extension's fee/extra-interest is not specified by the source PDF — see OP-15. | 27 Aug 2026 |
| D9 | Missed payment path | Warning emitted to local system + SME call task within the grace window → arrears if still unpaid → recollection. A missed instalment **outside an approved extension** may now carry the 150% late rate (Điều 466.5(b)) per D22 revised — mechanics not yet specified, see OP-14. | 27 Aug 2026, annotated 27 Sep 2026 |
| D10 | Relief authority | Automatic rules engine, with escalation to human on defined exceptions (§7.4). | 27 Aug 2026 |
| D11 | Revenue evidence | E-invoice data. Verified revenue = the closed month's e-invoice revenue. Monthly review cadence. | 27 Aug 2026, reaffirmed 27 Sep 2026 |
| D12 | Interest basis | Flat on original principal. The 20% cap is tested on total nominal interest as a flat fee on the borrowed amount — NOT on realised IRR. Extension days are priced on this same basis (D23). | 27 Aug 2026 |
| D13 | Backstop | 1.33x the declared term, computed in SCHEDULED BUSINESS DAYS: ceil(4 x N0 / 3). Reaching it ACCELERATES the entire remaining balance to immediately due — it is a demand, not a write-off. Unaffected by the extension-pricing change. | 27 Aug 2026 |
| D14 | Business-day calendar | **21 business days/month, 252/year** (1 week = 5 business days), per the grading engine's factors file (`grading_params_v1.yaml`), which the source PDF names as source of truth. Configurable per SME; this is the default. Supersedes v1.3's stated default of 22 — v1.3 and the factors file had drifted; the factors file wins. | 27 Aug 2026, corrected 27 Sep 2026 |
| D15 | Maximum declared term | Per `grading_params_v1.yaml`, `allowed_durations_months` is currently `[1, 2, 3, 4, 5, 6]` — i.e. 6 months is the current maximum, not 12. This conflicts with v1.3's stated "offered terms are 6 and 12 months" and with the backstop table in §4.5, which was computed assuming up to 12. See OP-16 — the backstop table needs re-deriving once the term range is confirmed stable. | 27 Aug 2026, flagged stale 27 Sep 2026 |
| D16 | Backstop immutability | Computed once at origination, stored on the facility. No process, extension or human may move it. | 27 Aug 2026 |
| D17 | Write-down ordering | A write-down can only FOLLOW a missed backstop, never precede it. Recovery activity may start earlier; the write-down event may not (INV-9). | 27 Aug 2026 |
| D18 | NO PULL AUTHORITY | FundLok cannot debit an SME account. No direct-debit mandate, no OpenAPI banking. Every collection is an SME-initiated PUSH, detected by reconciling inbound transfers. This is a hard external constraint, not a design choice. | 27 Aug 2026 |
| D19 | Collection channels | Three, assigned at onboarding as a waterfall: (A) rail sweep — SME's customers pay a FundLok-issued QR; (B) standing order set up by the SME at their own bank; (C) manual push against an automated reminder. | 27 Aug 2026 |
| D20 | Payment reference | Every facility carries a unique payment reference so inbound transfers self-identify. Without it, reconciliation at scale is not tractable. | 27 Aug 2026 |
| ~~D21~~ | ~~Uplift rebate on early/partial extension use~~ | **RETIRED.** No uplift exists to rebate under the v1.4 mechanism. | Retired 27 Sep 2026 |
| D22 | Legal characterisation of the extension | **Revised.** v1.3 documented the extension as FORBEARANCE specifically to preserve the "overdue" (quá hạn) basis needed for the 150% late rate. IBL counsel's review (which produced this v1.4 update) determined that basis does not hold for an approved extension. Since extension days are now priced at the plain contract rate r (D23), no forbearance fiction is needed — the extension can be documented as an **agreed new term**. This inference follows directly from the source PDF but is not itself stated verbatim in it; confirm the contract-wording implication with counsel before finalising templates. | Resolved 27 Sep 2026 (inferred from source PDF; confirm with counsel) |
| **D23** | Extension re-pricing rate | Extension days bear the **same flat rate r** as the original principal — no uplift. Extra interest for the extension = `round(P × r × extension_days / 252)`. Statutory basis: keeps the whole facility, extension days included, under Điều 468's 20% cap without relying on Điều 466.5(b). | 27 Sep 2026 |
| **D24** | Extension fee | A small flat VND fee per extension, kept by FundLok, kept **outside** the 20% cap. To stay outside the cap it must, per the source PDF: have a documented cost basis; be governed by a separate FundLok–SME service agreement; be invoiced separately with VAT; never be deducted from principal or folded into the daily instalment; never be shared with investors. Amount not yet sized — OP-17. **Not charged for MVP — see D27.** | 27 Sep 2026 |
| **D25** | Extension length rounding | Extension length = undersell ÷ scheduled monthly amount (a fraction of a month), converted to business days at the facility's `D`, then **rounded up to the next whole week** (1 week = 5 business days, fixed, independent of `D`). Even a small undersell costs a full week — this is stated as a known cost of the design, not a defect. | 27 Sep 2026 |
| **D26** | Extension trigger and cadence | The SME files after the month closes; review is not automatic (matches v1.3 §7.1, reaffirmed). Extensions are repeatable after any month, each independently re-priced and fee'd, until the 1.33× backstop (D13) is reached; if an extension would need more room than is left, the loan extends only to the backstop and the remainder falls due there (same rule as v1.3 EC-08, reaffirmed). | 27 Sep 2026 |
| **D27** | MVP scope — extension fee deferred | The extension fee (D24) is **designed but not implemented for MVP**. The schema/ledger separation exists now (`ext_fee_amount`, `fee_ledger_id`, INV-16) specifically so it can be turned on later without a breaking migration, but for MVP the field is never populated and no fee is charged, sized, or invoiced. Extension **re-pricing** (D23 — same-rate extra interest) is real MVP scope and is implemented. This same design-now/implement-later split applies to FundLok service fees generally, not just this one — treat OP-17/OP-18 as post-MVP work, not an MVP blocker. | 27 Sep 2026 |

D14's correction is the one to notice first. Every N₀/backstop/daily-instalment figure computed off the old default of 22 days/month will be off by ~4.5% from what the live grading engine assumes. This document's own §12.1 worked example uses D=22 as an explicit input (not the default) and remains valid as an illustration of a facility configured that way, but new implementation work should default to 21/252 unless a specific SME is configured otherwise.

D22's resolution is why D6/D7 could retire: the entire reason v1.3 needed the "forbearance" framing (D22 as originally written) was to preserve access to the 150% overdue-interest statute for extension days. Once that access is gone, there's nothing left for the forbearance fiction to protect — the extension can just be an ordinary agreed amendment, priced at the ordinary contract rate.


## 3  Definitions and notation

Carried forward from v1.3 unless noted. `lr` and `U` (late rate, uplift) are retired — nothing replaces them, since extension days now price at plain `r`.

| Symbol | Name | Definition |
|---|---|---|
| P | Principal | Amount disbursed to the SME. Never changes. |
| r | Flat annual rate | Underwriting output. Annual, flat on original principal. Never changes after origination. Now also the rate applied to extension days (D23). |
| x | Declared duration | Duration in months declared by the SME at application. Used for sizing only. |
| D | Business days per month | Scheduled collection days per month. Default 21 (D14, revised). Per-SME configurable. |
| s | Revenue percentage | Target repayment as a share of monthly revenue — v3.2's "revenue %" and v1.3's "share percentage" are the same figure. Fixed at origination (v3.2 D26: "one line decides everything"). Sizing input and the sole shortfall-test input — no separate materiality threshold. |
| I | Total interest | I = P × r × x/12. Fixed at origination. |
| T | Total repayable | Current total the SME owes across the life of the facility, excluding the extension fee (tracked separately, D24). T₀ at origination. |
| N | Scheduled payment days | Total collection days across the facility. N₀ = D × x. |
| A | Daily instalment | Amount DUE each business day (not debited — see D18). A₀ at origination. Monotonically non-increasing. |
| B | Outstanding balance | B = T − collected_to_date. |
| Δ_days | Extension length | Additional business days granted by an approved extension, rounded up to the next whole week (D25). |
| E | Expected monthly revenue | Derived at origination: E = (T₀ / x) / s. |
| y | Actual duration | Realised duration in months at settlement, acceleration or write-off. |
| N_bs | Backstop day | ceil(4 × N₀ / 3). The scheduled business day on which the full balance falls due. Immutable (D16). |
| Δ_max | Max cumulative extension | N_bs − N₀, in business days. All extensions together cannot exceed it. |
| obligation_m | Revenue-% obligation for month m | s × verified_revenue_m. Computed once the month closes. |
| undersell_m | Undersell for month m | max(0, scheduled_monthly_amount − obligation_m). The basis for the extension length (D25). |
| ext_fee | Extension fee | Flat VND per approved extension (D24). Amount TBD — OP-17. |
| ref | Payment reference | Unique per facility. The sole basis on which inbound transfers are matched (D20). |
| channel | Collection channel | A, B or C per D19. Stored on the facility; feeds expected compliance. |

D3 is still the counter-intuitive one. The revenue percentage remains a sizing input and the month-end shortfall test, but it is not a live payment mechanic — there is no code path that increases the daily amount. Assert this (INV-2) rather than trusting it.


## 4  Calculation rules

Arithmetic discipline. All money is integer VND. Division uses floor. Every rounding remainder is absorbed by the FINAL instalment, never distributed. Do not use floats for money anywhere in this system.

### 4.1  Origination

Unchanged from v1.3. Inputs: P, r, x, D, s. Executed once, on transition DISBURSED → ACTIVE.

I   = round( P × r × x / 12 )

T₀  = P + I

N₀  = D × x

A₀  = floor( T₀ / N₀ )

rem = T₀ − A₀ × N₀            // absorbed by instalment N₀

Sizing constraint, evaluated at approval. If it fails, the facility is not approved at this size:

monthly_obligation = T₀ / x

E                  = monthly_obligation / s

require:  s × declared_monthly_revenue  ≥  monthly_obligation

### 4.2  Extension — undersell, re-pricing and re-spread

**Rewritten in v1.4.** Executed only on an approved extension (§7), after a review month closes. Reproduces the source PDF's flow exactly (its own numbered steps 5–10):

1. Month closes. `obligation_m = s × verified_revenue_m`.
2. `undersell_m = max(0, scheduled_monthly_amount − obligation_m)`. If zero, no extension — the SME simply paid as scheduled (matches v1.3 §7.2, unchanged).
3. If `undersell_m > 0`, the SME may file for an extension:

raw_days   = ( undersell_m / scheduled_monthly_amount ) × D

Δ_days     = ceil( raw_days / 5 ) × 5            // D25 — rounded up to whole weeks

extra_int  = round( P × r × Δ_days / 252 )       // D23 — same rate, day-denominated

T_new      = T_current + extra_int               // re-price

B          = T_new − collected_to_date

days_left  = ( N_scheduled − days_paid ) + Δ_days

A_new      = floor( B / days_left )              // re-spread; remainder to final day

ext_fee_amount = 0 for MVP (D27) — post-MVP: <per D24, amount TBD — OP-17>   // ledgered separately, never folded into T or A_new

The extension fee is invoiced and collected separately from the schedule — it never touches `T`, `B`, or `A_new` (D24). Only the re-priced interest (`extra_int`) enters the schedule.

### 4.3  Statutory ceilings

**Rewritten in v1.4.** One ceiling now, not two — there is no separate late-rate ceiling for extension days since they price at plain `r`.

require:  r ≤ 0.20                                // Điều 468 — contract rate, applies uniformly including extension days

duration_for_cap = max( M, y )                     // declared or actual, whichever longer, M now includes extension months

implied_nominal  = ( T / P − 1 ) × 12 / duration_for_cap

require:  implied_nominal ≤ 0.20                   // INV-5, unchanged in spirit

Separately, for the extension fee to stay **outside** this cap at all (i.e. not count toward `implied_nominal`), it must satisfy every condition in D24: flat VND, documented cost basis, its own FundLok–SME service agreement, invoiced with VAT, never deducted from principal or the daily instalment, never shared with investors. Failing any one of these means the fee should be treated as disguised interest and folded into the cap test instead — this spec does not resolve what happens if that occurs; flag to counsel if a fee structure risks it.

The missed-instalment late rate (Điều 466.5(b), 150% of `r` capped at 30%, per D9/D22) is retained **outside** an approved extension — i.e. for genuine arrears — but its calculation mechanics are not specified here; see OP-14.

### 4.4  Early repayment

**Rewritten in v1.4.** D8 (no penalty, no discount) still governs the base case. The source PDF does not address what happens if the SME clears the facility while an approved extension is in progress — specifically, whether the extension fee (D24) and the re-priced extra interest (D23) already added to `T` get any kind of adjustment, the way v1.3's uplift did (D21, now retired). **Do not invent an answer here — this is OP-15.** Until resolved, the safe default matching D8's general "no discount" principle is: `payoff = B` (current outstanding, no rebate of any kind), since v1.4 no longer has an uplift-specific carve-out requiring one. Confirm this against the source before implementing.

### 4.5  Backstop and acceleration

Unchanged from v1.3 — the backstop formula does not depend on how extension days are priced.

N_bs   = min( ceil( 4 × N₀ / 3 ),  24 × D )     // integer: (4×N₀ + 2) // 3

Δ_max  = N_bs − N₀                              // total extension budget, in days

On reaching business day N_bs with a balance outstanding, the entire remaining amount — unrecovered principal, all incurred interest, and any unpaid extension fees — falls due in a single demand:

acceleration_amount = T − collected_to_date

| Declared x | N₀ (days, D=21) | N_bs (days) | = months | Max extension |
|---|---|---|---|---|
| 1 month | 21 | 28 | 1.33 | 7 days |
| 2 months | 42 | 56 | 2.67 | 14 days |
| 3 months | 63 | 84 | 4.00 | 21 days |
| 6 months | 126 | 168 | 8.00 | 42 days |

**Recomputed for D14's corrected default (D=21) and D15's current term range (1–6 months, per `grading_params_v1.yaml`).** v1.3's table assumed D=22 and terms up to 12 months; both inputs changed (D14, D15), so the table is regenerated here rather than reused. This is exactly the kind of drift OP-16 flags — re-verify this table if the term range or default `D` changes again.

Acceleration is a demand, not a write-off. Daily collection CONTINUES during the cure period. Only if the cure period expires unpaid does the facility move to IN_RECOVERY. Ordering constraint (D17/INV-9) is unchanged.


## 5  Facility lifecycle

Unchanged from v1.3 — the state machine, its transition table, and the meaning of each state (DRAFT → APPROVED → DISBURSED → ACTIVE → ARREARS_WARNING/ARREARS → UNDER_REVIEW → ACCELERATED → IN_RECOVERY → SETTLED/WRITTEN_OFF) do not depend on how extension days are priced. See v1.3 §5 for the full table. One addition: ARREARS may, per D9/D22, now carry the 150% late rate on the overdue amount — this does not change the state machine's shape, only what accrues while a facility sits in that state. Mechanics TBD, OP-14.


## 6  Daily collection (push model)

Unchanged from v1.3 in its entirety (§6, D18–D20, EC-01 through EC-11, EC-17, EC-18). The push-collection model, channel waterfall, and missed-payment escalation do not depend on extension pricing. See v1.3 §6 for the full text.


## 7  Month-end review and extension

### 7.1  Trigger and cadence

Unchanged from v1.3: a review window opens at each month-end. The SME may submit revenue evidence within it. No submission means no change.

### 7.2  Shortfall test

**Rewritten to the source PDF's exact terms (§4.2 above formalises the same thing symbolically):**

obligation_m = s × verified_revenue_m

undersell_m  = max( 0, scheduled_monthly_amount − obligation_m )

If `undersell_m = 0` the review closes with no change. Per D3 there is no upside branch: revenue above expectation must not alter A, T, or duration. This is unchanged from v1.3 — only the extension-length and re-pricing steps that follow a nonzero undersell have changed (§4.2).

### 7.3  Eligibility guards

Carried forward from v1.3 with formula updates:

Evidence must validate against the e-invoice data source (unchanged).

`elapsed_days + Δ_days` must not exceed `N_bs` (D13). If it would, refuse the extension outright and let it extend only to the backstop (D26) — matches v1.3 EC-08.

`A_new ≤ A_current` (INV-2). Under D23's same-rate pricing this holds for any lawful rate exactly as it did under the old uplift model (§4.2's `extra_int` is interest-only, the days it adds displace principal) — a violation means the rate is mis-specified; reject, do not clamp silently.

### 7.4  Authority and escalation (D10)

Unchanged from v1.3. The rules engine auto-approves by default; escalates to human review on: a second or subsequent extension on the same facility; shortfall exceeding a configurable materiality threshold; resulting duration within a configurable margin of N_bs; facility currently in ARREARS or IN_RECOVERY. Note per D26/v3.2, the origination-time `s` is already the single line deciding eligibility and extension size — the materiality threshold here is an *escalation* trigger (routes to a human), not a second eligibility gate, so it does not conflict with v3.2's "no separate 30% threshold" statement.


## 8  Data model

Facility, schedule_version, scheduled_payment, inbound_transfer, and state_transition are unchanged from v1.3 — see v1.3 §8 for the full field lists. Only `revenue_submission` / `extension` / `fee_ledger` change, since those carried the uplift-specific fields.

#### revenue_submission / extension / fee_ledger

**Rewritten in v1.4.**

| Field | Type | Notes |
|---|---|---|
| revenue_submission_id | uuid | One per review window per facility. |
| review_period | date | Month being evidenced. |
| evidenced_revenue | int64 | As validated, not as claimed. |
| validation_status | enum | PENDING \| VALIDATED \| REJECTED |
| obligation_amount | int64 | `obligation_m = s × evidenced_revenue`. New in v1.4 — replaces implicit computation. |
| undersell_amount | int64 | `undersell_m`. New in v1.4, replaces the old shortfall boolean-only test with a stored figure — it's now the direct input to extension length. |
| extension_id | uuid | Null if no extension resulted. |
| delta_days | int | Δ_days in business days, already rounded to whole weeks (D25). The contractual unit. |
| extra_interest_amount | int64 | `extra_int` from §4.2 — replaces `uplift_amount`. Priced at plain `r`, not a late rate. |
| ~~late_rate_applied~~ | ~~decimal~~ | **Retired.** No late rate applies to extension days under D23. |
| ~~uplift_rebated~~ | ~~int64~~ | **Retired.** No uplift exists to rebate (D21 retired). See OP-15 for what, if anything, replaces this. |
| delta_days_used | int | Extension days actually consumed before settlement. Retained from v1.3 in case OP-15 resolves to some form of proration; unused until then. |
| ext_fee_amount | int64 | `ext_fee` (D24). Amount TBD pending OP-17 — field exists, value not yet populated by policy. |
| ext_fee_invoice_ref | text | Reference to the separate FundLok–SME service-agreement invoice required by D24. Never a reference into the loan's own ledger. |
| decision_path | enum | AUTO_APPROVED \| HUMAN_APPROVED \| DECLINED \| REFUSED_BOUND |
| fee_ledger_id | uuid | Separate ledger entry for `ext_fee_amount` — must not share a ledger row with `extra_interest_amount`, since one goes to investors (interest) and the other is FundLok's own revenue (D24), never shared with investors. |


## 9  Emitted events

Unchanged from v1.3 except the extension-related payloads:

| Event | Emitted when |
|---|---|
| extension.approved / extension.declined | Review concluded. Carries `undersell_amount`, `delta_days`, `extra_interest_amount`, `ext_fee_amount`, `decision_path`. Replaces v1.3's `late_rate_applied`/`uplift_amount` payload. |
| ~~uplift.rebated~~ | **Retired.** No replacement event defined yet pending OP-15. |

All other events (`facility.disbursed`, `facility.schedule_generated`, `reminder.emitted`, `transfer.received`, `transfer.matched`, `transfer.unmatched`, `payment.satisfied`, `payment.missed`, `facility.arrears_warning_raised`, `facility.arrears_opened`, `facility.recovery_triggered`, `review.opened`, `revenue.submitted`/`revenue.validated`, `facility.accelerated`, `facility.settled`, `facility.written_off`) are unchanged from v1.3 §9.


## 10  System invariants

Unchanged from v1.3 except the uplift-specific ones, which are retired outright (there is nothing to replace them with, since there's no longer an uplift):

| ID | Invariant | Enforcement point |
|---|---|---|
| INV-1 | collected_to_date + outstanding = total_repayable, exactly, at all times | After every payment posting and every extension |
| INV-2 | current_daily_amount never increases (D3) | Schedule regeneration — reject, do not clamp |
| INV-3 | total_repayable increases only via §4.2's `extra_int` (never via the extension fee, D24) | Any write to total_repayable |
| INV-4 | No facility remains in ACTIVE or ARREARS past day N_bs with B > 0 — it must be ACCELERATED | Daily backstop evaluation |
| INV-9 | WRITTEN_OFF is reachable only when day_index > N_bs (D17) | Guard on the transition into WRITTEN_OFF |
| INV-10 | backstop_day_index never changes after origination (D16) | DB-level immutability on the column |
| INV-11 | Sum of all extensions in days ≤ Δ_max = N_bs − N₀ | Every extension approval |
| ~~INV-13~~ | ~~late_rate_applied ≤ 30%~~ | **Retired** — no late rate on extension days under D23. |
| ~~INV-14~~ | ~~uplift_amount stored separately from base interest~~ | **Retired** — replaced in spirit by INV-16 below (fee/interest separation still matters, just for a different reason). |
| ~~INV-15~~ | ~~uplift_rebated = uplift_amount on in-term settlement~~ | **Retired** — no uplift to rebate (D21). |
| INV-5 | implied_nominal ≤ 20% (D12, §4.3) | Origination and every extension |
| INV-6 | At most one scheduled_payment per facility per date | DB unique constraint on (facility_id, scheduled_date) |
| INV-12 | An inbound_transfer is applied to at most one facility, exactly once | Unique constraint plus a settled match_status transition |
| INV-7 | Sum of scheduled amounts in the active version = outstanding balance | Schedule generation |
| INV-8 | No money field is ever a float | Type system / schema review |
| **INV-16** | `ext_fee_amount` is never added to `total_repayable`, `outstanding`, or any `scheduled_payment.amount_due` — it is ledgered only via `fee_ledger_id` (D24) | Schema / write path — a fee write that touches the loan balance is a defect |
| **INV-17** | The rate applied to any extension day equals the facility's own `r`, never a multiple of it (D23) | Extension re-pricing |


## 11  Edge cases

Unchanged from v1.3 (EC-01 through EC-19) except the uplift-specific ones:

| ID | Case | Required behaviour |
|---|---|---|
| EC-01 through EC-19 (excl. EC-20–EC-23) | — | See v1.3 §11 — Tết/holidays, rounding remainder, partial payment, arrears ordering, overpayment, extension-while-in-arrears escalation, multiple sequential extensions capped at Δ_max, refusal at the backstop, mid-month early repayment, standing-order lapse, missing/wrong reference, lump-sum application, duplicate statement import, and rail-sweep-excess policy (OP-10, still undecided) are all unaffected by the pricing-mechanism change and carry forward verbatim. |
| ~~EC-20~~ | ~~Second or later extension — compound uplift on previous uplift~~ | **Retired as written** (no uplift). Replacement: each extension's `extra_int` is computed independently off `P` (never off a previous extension's `extra_int`) — principal-based, not compounding. |
| ~~EC-21~~ | ~~Contract rate at 20% ceiling — late rate caps at 30%~~ | **Retired.** Extension days price at plain `r` (D23); there is no late-rate multiplier to cap separately from the base 20% test (§4.3). |
| ~~EC-22~~ | ~~SME clears part-way through extension — pro-rata uplift rebate~~ | **Retired pending OP-15.** No uplift to rebate; whether any pro-rata treatment applies to `extra_int` or `ext_fee_amount` is unresolved. |
| ~~EC-23~~ | ~~Extension approved, then SME clears inside original term — full uplift rebate~~ | **Retired pending OP-15**, same reason. |


## 12  Worked examples

### 12.1  Origination

Unchanged from v1.3 — reproduced here since it remains a valid worked example (it specifies D=22 as an explicit input, illustrating a non-default configuration under the corrected D14):

Inputs: P = 300,000,000 · r = 12% · M = 4 months · D = 22 · s = 10%.

| Item | Value | Note |
|---|---|---|
| Total interest | 12,000,000 | P × 0.12 × 4/12 |
| Total repayable T₀ | 312,000,000 | Fixed at origination |
| Scheduled payment days N₀ | 88 | 22 × 4 |
| Daily instalment A₀ | 3,545,454 | floor(312,000,000 / 88) |
| Monthly obligation | 78,000,000 | T₀ / 4 |
| Min expected revenue E | 780,000,000 | 78,000,000 / 0.10 |

### 12.2  Extension — the source PDF's own worked example

**Replaces v1.3 §12.2/12.3 entirely** (those were uplift-model examples and no longer apply). Reproduced faithfully from `docs/FundLok - Extension model v3.2 (27 Sep 2026).pdf`:

Inputs: P = 100,000,000 · r = 15%/yr · declared term = 2 months (42 business days, D=21).

| Item | Value | Note |
|---|---|---|
| Total due | 102,500,000 | P + P×r×2/12 |
| Daily due | 2,440,476 | floor(102,500,000 / 42) |
| Scheduled per month | 51,250,000 | T₀ / 2 |
| Backstop day | 56 | ceil(4×42/3) — 14 days of extension room |

Month 1: SME pays the full 51,250,000. At month-end, verified revenue × revenue % = only 41,000,000 → the SME files.

| Item | Value | Note |
|---|---|---|
| Undersell | 10,250,000 | 51,250,000 − 41,000,000 |
| Raw extension | 4.2 days | 0.2 month × 21 days/month |
| Δ_days (rounded to week) | 5 days | ceil(4.2/5) × 5 = 1 week |
| Extra interest | 297,619 | round(100,000,000 × 0.15 × 5/252) |
| New total | 102,797,619 | 102,500,000 + 297,619 |
| Still owed | 51,547,619 | 102,797,619 − 51,250,000 |
| Days left | 26 | (42 − 21) + 5 |
| New daily from month 2 | 1,982,601 | ≈ 51,547,619 / 26, remainder to final day |
| Backstop room left | 9 days | 14 − 5 |

What one bad month buys, by loan length (same 20%-of-scheduled undersell, 5 extension days): 2 months → daily −18.8%; 6 months → daily −4.2%; 12 months → daily −1.8% (12-month figure illustrative only — not currently an offered term, D15). Same compensation in every case; the extra days are spread over whatever is left, so long loans see a smaller monthly drop. That's the design working, not a gap.

### 12.3  Backstop — worst case

**Not reproduced with invented figures.** v1.3's §12.4 computed a "MAX TOTAL EVER OWED" using the old uplift formula; that number no longer applies and this document does not invent a replacement, because the extension fee (D24) is not yet sized (OP-17) — without it, the maximum total owed at the backstop cannot be stated exactly (extra interest alone is computable per D23, but the fee's contribution is unknown). Recompute this worked example once OP-17 is resolved.


## 13  Open items and external dependencies

Carried forward from v1.3 where still open, with new items from this update. None blocks starting on §4–§6 (unchanged from v1.3).

| ID | Item | Status / owner |
|---|---|---|
| OP-01 | Statement feed and reconciliation mechanics | Still blocked on VPBank — unchanged from v1.3. |
| OP-02 | Revenue-evidence integration | Still open — unchanged from v1.3. Now more load-bearing than before: D11/D26 make "closed month's e-invoice revenue" the sole input to the extension mechanism, not just a sizing input. |
| OP-05 | Business-day calendar source | Still open — unchanged from v1.3. Needs an annual Vietnamese public-holiday calendar. |
| OP-06 | Tamper/fraud controls on revenue evidence | Still deferred — unchanged from v1.3. |
| OP-07 | Provisioning and aging policy | Still open — unchanged from v1.3, though the loss-recognition tail's bound changes with D15's term range (see OP-16). |
| ~~OP-09~~ | ~~Re-run the volume model~~ | Still open, and now doubly so — both the backstop range (D15/OP-16) and the extension-fee revenue (OP-17) feed it. |
| ~~OP-12~~ | ~~Legal characterisation of the extension~~ | **RESOLVED 27 Sep 2026** — IBL counsel determined the 150% overdue basis does not survive an approved-extension characterization. This is the change that produced this entire document (D22, D23). |
| OP-13 | Điều 466.5(a) — 10%/yr on overdue interest | Still open, unchanged from v1.3 — separate, small, unclaimed statutory entitlement, unrelated to this update. |
| OP-03 | grace_window, recovery_threshold, cure_period length | Still open — unchanged from v1.3. |
| OP-04 | Exception thresholds for §7.4 | Still open — unchanged from v1.3. |
| **OP-14** | Missed-instalment 150% late-rate mechanics (D9/D22) | **New.** The source PDF states the 150% rate "can still be charged on missed instalments outside an extension" but does not specify how it accrues, on what base, whether it compounds, or how it's collected. v1.3's own ARREARS handling (§6.3) had no interest-penalty mechanic at all — this is new ground, not a v1.3 gap being closed. Needs its own design before ARREARS can charge anything. |
| **OP-15** | Early repayment during an in-progress extension | **New.** Neither v1.3's rebate (D21, retired) nor the source PDF addresses whether `extra_interest_amount` or `ext_fee_amount` get any adjustment on early payoff mid-extension. §4.4 states a conservative default (no rebate of either) pending resolution — do not implement without confirming this against the source. |
| **OP-16** | Declared-term range vs. the backstop table | **New.** v1.3's backstop table (§4.5) assumed terms up to 12 months with D=22; the live config (`grading_params_v1.yaml`) currently allows `[1, 2, 3, 4, 5, 6]` months with D=21 (D14/D15). §4.5's table above is recomputed for the current config, and the 1–6 month range is now confirmed (Loc, 2026-11-13, handbook v3.1 Money flow tab, symbol M). Re-verify this table if the range changes again. |
| **OP-17** | Size the extension fee, and its cost basis | **New**, directly from the source PDF's own to-do list. **Deferred post-MVP (D27)** — does not block MVP implementation of §4.2's re-pricing (extra interest only, no fee). Blocks, once picked back up: `ext_fee_amount` population, the backstop worst-case worked example (§12.3), and the fee's own IBL cap-exemption paperwork (D24). |
| **OP-18** | Add an Extension tab to the fee model | **New**, directly from the source PDF's own to-do list. **Deferred post-MVP (D27)**, same as OP-17. The fee model's IR band (currently "IR 60 → 16.40%" per the source PDF, vs. a stated "17% average" implying IR 50) needs to reconcile with whatever `ext_fee_amount` ends up being, since the fee is FundLok's own revenue (D24) and feeds the same model. |


## 14  Non-functional requirements

Unchanged from v1.3 in its entirety — idempotency on the inbound side (D18, INV-12), integer VND everywhere (INV-8), Asia/Ho_Chi_Minh timestamps, append-only ledgers and state_transition, schedule regeneration as new versioned rows, and loud/halting invariant assertions. See v1.3 §14 for the full text.

End of specification. §2 (locked decisions, especially D22–D27) and §12.2 (the source PDF's own worked example) are the two sections to read first for what actually changed from v1.3. §13's new open items (OP-14 through OP-18) are what block calling this fully implementable — except OP-17/OP-18, which D27 explicitly defers past MVP rather than treating as a blocker. The others (OP-14, OP-15, OP-16) still apply to MVP scope and should not be silently resolved by an implementer; they need the same kind of explicit decision D22–D27 got. Questions on the extension-pricing change to Edward Wong; questions on everything carried forward from v1.3 remain per that document's own routing (mechanism to Loc Vuong; excluded workstreams in §1.3 to their respective owners).
