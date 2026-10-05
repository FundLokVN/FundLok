# Spec: Investor tab of the public rate page

| Field | Value |
|---|---|
| **Status** | REVIEW (implemented ahead of review at the product owner's request; Edward to review) |
| **Owner** | Phat |
| **Implementer(s)** | Phat (backend + frontend) |
| **Module** | `app/rates/` (backend), `app/rate/` (frontend) |
| **Version** | 1.0 |
| **Date** | 2026-09-27 |
| **Related ADR** | ADR-004 (Decree 94 / Circular 64 posture) |
| **Depends on** | `docs/specs/underwriting/grading-engine-core.md` (§R10 pricing, §10 disclosure) |

Source design: `FundLok_LaiSuat_Page_Prototype.html` rev 6 (Lok, 15 Sep 2026), investor panel.

---

## 1. Context & Goal

`/rate` answered only the borrower's question. The prototype also has an investor tab: a prospective investor picks an amount, a commitment, a risk tier and a reinvestment pace, and sees an illustrative target yield with the full walk-down (loan rate, minus expected loss, minus FundLok's fee, equals what they keep per loan, then annualised).

Pressing **Calculate** is the lead-capture moment: the visitor's name, email and optional phone are stored together with their choices, in exchange for seeing the number. Most people who calculate never sign up, and sales wants those names. **Sign up for more information** is a later, optional step.

**The one structural change from the prototype.** The prototype computed the yield in the browser from the bank reference rate (BULR) and the tier-to-internal-rating mapping. Lok's standing rule is that neither is ever shown externally. All pricing therefore runs on the server, and the browser receives only the published walk-down.

## 2. Out of Scope

- A self-serve investor account or KYC. Follow-up stays one to one (Điều 3.3 posture).
- Emailing the investor. Nothing is sent; sales contacts them.
- Deduplicating leads by email. Each calculation is its own enquiry row.
- Rate limiting (captcha only). Recommended at the edge (Cloudflare / Cloud Armor), not in the app.
- Recalibrating the cadence multipliers to other board rates (see §6, rule 4).

## 3. Data Model

Table `investor_rate_inquiries` (migration `f3c7edc544fc`, chained off `abdfb76f0409`).

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | Also the capability for the estimate / sign-up calls. Never exposes PII. |
| `reference` | VARCHAR(16) UNIQUE | `FL-` + 6 chars from an alphabet without 0/O/1/I/L. Read out over the phone. |
| `full_name` | VARCHAR(120) | Whitespace-collapsed; control characters rejected. |
| `email` | VARCHAR(254) | Lower-cased. Indexed. |
| `phone` | VARCHAR(20) NULL | 8–15 digits; `0-9 + ( ) space . -` only. |
| `locale` | VARCHAR(5) NULL | `vi` or `en`. |
| `amount_vnd` | BIGINT | 1,000,000 – 1,000,000,000,000. |
| `commitment_months` | INT | 6 or 12 (CHECK). |
| `risk_tier` | VARCHAR(20) | `conservative` / `balanced` / `growth` (CHECK). |
| `reinvestment_cadence` | VARCHAR(20) | `none` / `quarterly` / `monthly` / `weekly` / `daily` (CHECK). |
| `bank_rate_pct` | NUMERIC(5,2) | Board rate the estimate used. **Admin only.** |
| `loan_rate_pct`, `net_per_loan_pct`, `net_apy_pct` | NUMERIC(5,2) | What the server computed and showed. |
| `estimated_return_vnd` | BIGINT | |
| `acknowledged_illustrative_at` | TIMESTAMPTZ | The "illustration, not an offer" gate. |
| `consented_contact_at` | TIMESTAMPTZ | Decree 13/2023/ND-CP consent to store and contact. |
| `signed_up_at` | TIMESTAMPTZ NULL | |
| `signup_amount_vnd`, `signup_commitment_months`, `signup_risk_tier`, `signup_reinvestment_cadence`, `signup_net_apy_pct` | NULL | Choices on screen at sign-up. Differ from calculate time because the estimate stays live. |
| `session_id` | VARCHAR(128) NULL | |
| `ip_address` | INET NULL | Resolved by `TrustedProxyMiddleware`. |
| `user_agent` | VARCHAR(512) NULL | |
| `created_at` | TIMESTAMPTZ | Indexed DESC. |

## 4. API Contract

All public routes are unauthenticated and return `{"detail": ...}` on error.

### `GET /api/v1/rates/investor/tiers`
The hint under the tier buttons, shown before any details are given.
```json
{"tiers": [{"risk_tier": "conservative", "loan_rate_pct": 12.8}, {"risk_tier": "balanced", "loan_rate_pct": 14.0}, {"risk_tier": "growth", "loan_rate_pct": 15.2}]}
```

### `POST /api/v1/rates/investor/leads` → 201
Captcha-checked (`turnstile_token`). Stores the lead.
```json
{"full_name": "Nguyễn Văn A", "email": "a@example.com", "phone": "0901 234 567", "locale": "vi",
 "amount_vnd": 1000000000, "commitment_months": 12, "risk_tier": "balanced", "reinvestment_cadence": "monthly",
 "acknowledged_illustrative": true, "consent_contact": true, "session_id": "sess_…", "turnstile_token": "…"}
```
Response:
```json
{"lead_id": "uuid", "reference": "FL-7K2QXD",
 "estimate": {"loan_rate_pct": 14.0, "expected_loss_pct": 1.0, "fee_pct": 2.5, "net_per_loan_pct": 10.5,
              "net_apy_pct": 17.27, "estimated_return_vnd": 172704000, "avg_loan_months": 4, "capital_turns": 3.0}}
```

### `POST /api/v1/rates/investor/leads/{lead_id}/estimate` → 200
Body: the four choices. Returns an `estimate`. Writes nothing. 404 for an unknown lead, so the yield is never available without the lead capture.

### `POST /api/v1/rates/investor/leads/{lead_id}/signup` → 200
Body: the four choices. Returns `{"reference", "signed_up_at"}`. Idempotent: the first sign-up stands.

### `GET /admin/rates/investor-leads` (ADMIN / SYSTEM_ADMIN)
Query: `page`, `page_size` (≤100), `search` (name / email / reference, ≤120, LIKE wildcards literal), `risk_tier`, `signed_up` (bool). Returns `{total, page, page_size, items[]}` with every column above.

### `GET /admin/rates/investor-leads/{lead_id}` (ADMIN / SYSTEM_ADMIN)

Unlike the SME inquiry routes, these admin routes have **no `/api/v1` alias**: they hold personal data.

## 5. State Machine

```
(none) --calculate--> CALCULATED --sign up--> SIGNED_UP
```
`SIGNED_UP` is terminal; a second sign-up returns the first.

## 6. Business Rules

1. **Loan rate per tier** = the grading engine's `price()` (`app/underwriting/grading/pricing.py`) at the tier's internal rating, using the board rate in force from `bank_rate_config`, the same table admin score runs read. Falls back to `pricing.bank_rate_default_pct` (with an error log) if no row is in force.
2. **Tier ratings** (never returned): conservative 90, balanced 75, growth 60. Balanced is the locked scenario; the other two are the prototype's evenly spaced extrapolation.
3. **Expected loss**: 0.5 / 1.0 / 2.0 pp. **Fee**: 2.5 pp (public since prototype rev 5).
4. **Annualisation**: reinvesting uses the 12-month simulation factors (duration 6 → 1.5344, 12 → 1.6448; cadence quarterly 0.6173, monthly 1.0, weekly 1.2402, daily 1.3145). No reinvestment = net per loan × 4 / commitment months. These were fitted at a 12% board rate; at other board rates the walk-down stays exact but the annualised figure is an extrapolation.
5. **Estimated return** uses the unrounded yield, like the prototype.
6. **Never returned publicly**: tier ratings, bank rate, any grade or score. Only admins see `bank_rate_pct`.
7. **Never trusted from the client**: the displayed yield. The prototype's `shown_net_apy` read DOM text; sign-up now recomputes.
8. **Contact fields lock** in the UI once the lead exists.

## 7. Error Cases

| Condition | Status | Notes |
|---|---|---|
| Missing / false consent or gate acknowledgement | 422 | No row stored. |
| Invalid name, email, phone, amount, tier, cadence, commitment, locale | 422 | No row stored. |
| Captcha missing / failed (secret configured) | 400 | Captcha provider unreachable or non-JSON: 500 / 503, fails closed. |
| Unknown `lead_id` on estimate or sign-up | 404 | |
| Non-admin on admin routes | 401 / 403 | |

## 8. Acceptance Criteria

Backend: `tests/rates/test_investor_leads.py`. Frontend: `e2e/tests/rate-investor.spec.ts`.

- `test_locked_scenario_reproduces_the_prototype`: 14.00 → 10.50 → 17.27, return 172,704,000.
- `test_tier_loan_rates`, `test_loan_rate_follows_the_board_rate`, `test_missing_board_rate_falls_back_instead_of_500`.
- `test_no_reinvestment_six_months_is_seven_percent`.
- `test_rating_and_bank_rate_are_never_returned`, `test_public_responses_never_echo_contact_details`.
- `test_calculate_stores_the_lead`, `test_consents_must_be_literally_true`, `test_invalid_lead_input_rejected`.
- `test_estimate_requires_an_existing_lead`, `test_estimate_writes_nothing`.
- `test_signup_records_the_choices_on_screen`, `test_first_signup_stands`, `test_signup_ignores_a_client_supplied_yield`.
- `test_admin_list_requires_admin`, `test_no_public_alias_for_admin_leads`, `test_admin_list_and_detail`, `test_admin_filters`.
- e2e: default tab, `?for=investor` deep link, gate, hidden-until-calculated then live, sign-up reference, tab state kept, admin listing.

## 9. Open Questions

1. **Legal wording of the contact-consent line** (new; not in the prototype). Needed for Decree 13/2023 because the tab stores name, email and phone. Needs legal sign-off, EN and VI.
2. **Conservative / Growth calibration.** Ratings 90/60 and losses 0.5/2.0 are unmodelled extrapolations, and there is no simulation for a 6-month commitment. Run the simulation before launch.
3. **SME calculator board rate.** `grade_lite` prices at `GradingInput.bank_rate_pct = 12.0`, a hardcoded default, while this tab and admin score runs read `bank_rate_config`. If an admin changes the board rate, the SME tab will drift from both. Edward's call on whether `grade_lite` should take the rate from the table.
4. **Retention period** for leads that never sign up (Decree 13 requires one).

## Changelog

| Date | Version | Change |
|---|---|---|
| 2026-09-27 | 1.0 | Initial; implemented alongside. |
