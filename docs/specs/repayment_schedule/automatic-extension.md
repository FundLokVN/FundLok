# Spec: Automatic extension

| Field | Value |
|---|---|
| **Status** | DRAFT (Phat, 2026-11-19). **Needs Edward's approval** (D-058: schedule, fees, a migration) |
| **Owner** | Phat (repayment engine, D-058); Edward approves |
| **Implementer(s)** | Phat |
| **Module** | `app/direct_transfer/extension.py`; route in `app/market/repayment_router.py`; `extension_filings` table |
| **Version** | 0.1 |
| **Date** | 2026-11-19 |
| **Related ADR** | ADR-011 |
| **Depends on** | `fixed-daily-repayment-mechanism-v1.4.md` (§4.2, §4.5, §7, §8: the base this spec changes), D-041, D-042, D-044, `late-status.md` rule 7, Money flow FINAL v1.0 §2 |

---

## 1. Context & Goal

D-042 (Loc, 14 Nov): extension ships at launch and is automatic. A business whose monthly revenue falls short of its fixed daily repayments gets more working days, so the daily amount never rises; each investor is paid interest for the extra days. No staff step, no fee.

**Goal:** the SME files a month's revenue by the 5th; the system decides at once and, if there is a shortfall, re-spreads every investor's remaining balance over its remaining days plus Δ.

## 2. Out of Scope

- Default and acceleration (TD-03): "a loan in default cannot file" has nothing to check yet (no default state).
- OCR or staff validation of the revenue filed: accepted as filed (D-042; OCR check open with Loc).
- The extension fee (D-27/TD-04, deferred).
- Ledger legs for the extra interest (interest has no claim leg today: claims are principal-only).

## 3. Data Model

### New table `extension_filings`
```
id UUID PK · window_id → funding_windows · sme_user_id → users · period_month DATE (first of the month)
evidence_kind TEXT CHECK IN ('TAX_DECLARATION','BANK_INFLOW') · evidence_file_id → evidence_files
revenue_vnd BIGINT ≥ 0 · revenue_share NUMERIC(7,4) (s, copied from the score run)
due_vnd BIGINT · shortfall_vnd BIGINT · delta_days INT ≥ 0 · start_date DATE nullable
previous_rows JSONB nullable   -- the per-investor rows the extension replaced (the old schedule version)
status TEXT CHECK IN ('EXTENDED','NO_SHORTFALL','NO_ROOM') · filed_at TIMESTAMPTZ
UNIQUE (window_id, period_month)
```
Migration `c2f8a6d4e917` (off `a4d2c7e9b130`). Existing tables change in place: future `repayment_days` rows are re-amounted and Δ rows appended; `facility.interest_vnd` and `total_obligation_vnd` grow by the extra interest; a new `schedule_version` row (facility-level daily, final instalment, day count); `tranches.investor_fee_total_vnd` grows. `facility.backstop_day_index` never moves (immutable trigger).

**Revenue share s.** `score_runs.daily_repayment_rate`, set once at underwriting: `(T / M) / expected monthly revenue` (`underwriting/grading/pricing.py`). Never changed. It is the blank in Contracts 3 and 5 (D-044: to be confirmed with Loc). A loan whose score run has no s cannot file (`409 REVENUE_SHARE_UNKNOWN`).

## 4. API Contract

`POST /repayment/rounds/{window_id}/extension-filings` · SME (the round's business) · multipart:
`period_month` (`YYYY-MM`), `revenue_vnd` (int ≥ 0), `evidence_kind` (`TAX_DECLARATION` | `BANK_INFLOW`), `file` (PDF/PNG/JPEG/WEBP ≤ 10 MB) → `201 ExtensionFilingOut`:
```json
{ "id": "uuid", "window_id": "uuid", "period_month": "2026-11-01", "evidence_kind": "TAX_DECLARATION",
  "revenue_vnd": 30000000, "due_vnd": 23825392, "shortfall_vnd": 10253963,
  "status": "EXTENDED", "delta_days": 10, "start_date": "2026-12-04", "filed_at": "iso" }
```

| Status | Code | Condition |
|---|---|---|
| 409 | `FILING_CLOSED` | not between the 1st and the 5th (ICT) of the month after `period_month` |
| 409 | `ALREADY_FILED` | that month was filed already |
| 409 | `LATE_DAY_BLOCKS_FILING` | a repayment day of the round is `LATE` or `REPORTED_MISSED` |
| 409 | `REVENUE_SHARE_UNKNOWN` | the score run has no s |
| 409 | `NOT_REPAYING` | no investment of the round is repaying |
| 409 | `EXTENSION_RAISES_DAILY` | an investor's new daily amount would exceed its current one (stops, nothing saved) |
| 404 | — | not the round's business |

## 5. State Machine

None. A filing is decided when filed: `EXTENDED`, `NO_SHORTFALL` (U ≤ 0) or `NO_ROOM` (no days left before the backstop).

## 6. Business Rules

1. **Filing.** By the 5th (ICT) of month m+1, with month m's tax declaration or bank inflow statement. No filing, no extension. Repeatable every month; no staff step; no fee.
2. **Blocked while late.** A round with any day `LATE` or `REPORTED_MISSED` cannot file (`late-status.md` rule 7, D-041). See open question 1.
3. **Shortfall.** `U = instalments due in m to all investors − s × evidenced revenue`. A partial first or last month counts `s × revenue × (the loan's instalment days in m ÷ the month's working days)`. Nearest đồng, 0.5 up.
4. **Extra days.** `Δ = ⌈U ÷ due × 21⌉`, rounded up to the next multiple of 5, the same for every investor; capped at the room to the backstop `N_bs − N0 − days already granted` (the smallest room over the round's facilities). See open question 2.
5. **Per investor.** `extra = P × r × Δ / 252` (nearest đồng, 0.5 up); `new T = T + extra`; `new daily = ⌊(new T − paid) ÷ (days left + Δ)⌋` from the first working day after the filing, where *paid* is everything due before that day; the last instalment takes the remainder. Each day's interest part follows the existing rule (`gross × I / T` rounded down, last day takes the rest) and withholding is recomputed so the total is 5% of the investor's total interest.
6. **Guard.** The new daily amount must not exceed the old one; if it does the filing stops with `EXTENSION_RAISES_DAILY` and nothing is saved.
7. **Records.** A new schedule version; the replaced rows are kept on the filing (`previous_rows`). `investor_fee_total += P × 2.5% × Δ / 252` (nearest đồng, 0.5 up).
8. **Notifications.** `EXTENSION_APPLIED` to the SME (who is also the guarantor) and to every investor.
9. **Backstop.** Never moves.

**Worked example (Money flow §2).** 100M over 2 months at 15%; investors A 40M, B 30M, C 20M, D 10M; month 1 paid at the original dailies; month-1 revenue 205,000,000, s = 0.2. Due 51,249,954, U = 10,249,954, Δ = 5. A: extra 119,048, new T 41,119,048, new daily 793,040; B: 89,286 / 594,780; C: 59,524 / 396,520; D: 29,762 / 198,260. Fee increases 19,841 / 14,881 / 9,921 / 4,960.

## 7. Error Cases

| Scenario | Behaviour |
|---|---|
| Revenue covers the month | `NO_SHORTFALL`, recorded, nothing changes |
| Δ would pass the backstop | Δ capped at the room; `NO_ROOM` when the room is 0 |
| Filed on the 6th | `409 FILING_CLOSED` |
| One day LATE | `409 LATE_DAY_BLOCKS_FILING` |
| New daily above the old | `409 EXTENSION_RAISES_DAILY`, nothing saved |

## 8. Acceptance Criteria

`tests/direct_transfer/test_automatic_extension.py`:
- [x] `test_money_flow_worked_example_exact_figures`
- [x] `test_delta_rounds_up_to_a_multiple_of_five_and_is_capped_at_the_backstop_room`
- [x] `test_partial_month_counts_revenue_for_the_loan_s_working_days_only`
- [x] `test_new_daily_amount_never_exceeds_the_old_one`
- [x] `test_filing_extends_every_investor_by_the_same_days_and_keeps_the_old_version`
- [x] `test_no_shortfall_records_the_filing_without_extending`
- [x] `test_filing_window_is_the_first_five_days_of_the_next_month`
- [x] `test_a_late_or_disputed_day_blocks_the_filing`
- [x] `test_filing_needs_the_revenue_share_set_at_underwriting`
- [x] route wiring: `tests/market/test_extension_filing.py`

## 9. Open Questions

| # | Question | Owner | Default used |
|---|---|---|---|
| 1 | P1 brief: "a filing is **held** while any day is Late". D-041, `late-status.md` rule 7 and the P1-1 brief say a Late day **blocks** filing; Loc (16 Nov, open PR #117) says a Late day refuses filing and a disputed day is accepted but held. Which? | Loc | Refused while any day is LATE or REPORTED_MISSED (rule 7 on main) |
| 2 | v1.4 §7.3 and v1.3 EC-08 refuse an extension past the backstop ("never grant a reduced Δ"); the P1 brief caps Δ at the room. | Loc, Edward | Capped (P1 brief, D-042 "within the backstop room") |
| 3 | v1.4 §7.3 uses the closed month's e-invoice revenue, validated; D-042 and the brief accept a tax declaration or bank inflow statement as filed. | Loc | As filed (D-042) |
| 4 | v1.4 D10 escalates a second extension to a human; D-042 and the brief: no staff step. | Loc | No staff step (D-042) |
| 5 | The investor fee increase `P × 2.5% × Δ / 252` is in no spec or agreement (6.1: `P × 2.5% × M / 12`). | Edward | Applied (P1 brief) |
| 6 | s = the engine's daily repayment rate (D-044) is still "to be confirmed" with Loc. | Loc | `score_runs.daily_repayment_rate` |
| 7 | Partial-month pro-rating: by the loan's instalment days over the month's working days? | Loc | Yes |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 0.1 | 2026-11-19 | Phat / Claude | First draft from D-042, the P1 brief (P1-3) and v1.4 |
