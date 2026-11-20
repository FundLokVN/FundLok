# Spec: SME rate quiz on the public rate page (illustrative)

| Field | Value |
|---|---|
| **Status** | REVIEW |
| **Owner** | Phat |
| **Implementer(s)** | Drafted and built with Claude for Loc; Phat reviews (module owner), Edward reviews the migration |
| **Module** | `app/rates/` (backend), `app/[lang]/rate/` in `fundlok-front-end` (frontend) |
| **Version** | 1.0 |
| **Date** | 2026-11-20 |
| **Related ADR** | ADR-004 (Decree 94 posture) |
| **Depends on** | `docs/specs/rates/investor-rate-calculator.md` (same page, investor tab, untouched) |

Product decision: Loc, 20 Nov 2026.

---

## 1. Context & Goal

The first Facebook campaign for `/rate` (5–10 Oct 2026, about 478k ₫, 442 landing-page views) produced little to no enquiries. Part of the cause was the ad targeting; the other part was the page. The SME tab asked 13 required fields (two years of revenue, cost of goods sold, fixed costs, contact details first) before showing anything, which visitors on a phone do not finish.

The SME tab becomes a five-question tap-through quiz. **The rate it shows is an illustration for first-time visitors.** It comes from a short published formula computed in the browser. It is not the underwriting engine, does not call it, and does not change it. Name and phone are asked on the same screen as the result. When the visitor sends them, the answers, the rate they saw and their contact are saved to the existing `rate_inquiries` table, so they appear in the admin "Rate inquiries" list like calculator inquiries do.

**Goal:** an SME visitor on a phone sees an illustrative rate in about 30 seconds and can leave a name and phone on the same screen, landing in the admin table.

---

## 2. Out of Scope

- **The underwriting engine.** Nothing under `app/underwriting/` or `app/loans/lite_grading.py` changes, and the quiz never calls either. An applicant's real rate still comes from the deterministic 0–100 grade and the handbook 8.3 formula at application time. The quiz figure is never carried into an application.
- The investor tab. Unchanged.
- `POST /api/v1/rates/calculate`. It stays in place and keeps working (old cached pages may still call it), but the new page no longer uses it. Retiring it is a later decision (open question 6).
- Storing quiz results from visitors who do not leave a name and phone. Only submitted leads are saved.
- Editing the quiz numbers from the admin panel. They are a versioned constant in the frontend (rule 3).
- Meta Pixel and the CSP change it needs (needs the pixel ID; separate change).
- A/B testing against the old form. Traffic is too low for a meaningful split.

---

## 3. Data Model

### Modified table: `rate_inquiries`

Quiz leads are rows with `source = 'QUIZ'`. Existing rows and the old calculator keep `source = 'CALCULATOR'`.

```
+ source               VARCHAR(20)   NOT NULL DEFAULT 'CALCULATOR', CHECK IN ('CALCULATOR', 'QUIZ')
+ monthly_revenue      BIGINT        nullable, CHECK > 0          -- Q5
+ operating_band       VARCHAR(10)   nullable, CHECK IN ('LT_1Y', 'Y1_3', 'GT_3Y')   -- Q3
+ quiz_outcome         VARCHAR(10)   nullable, CHECK IN ('QUOTED', 'DECLINED')
+ quiz_params_version  VARCHAR(20)   nullable                     -- e.g. 'sme-quiz-v1'
+ utm_source           VARCHAR(100)  nullable
+ utm_medium           VARCHAR(100)  nullable
+ utm_campaign         VARCHAR(100)  nullable
+ utm_content          VARCHAR(100)  nullable
+ utm_term             VARCHAR(100)  nullable
+ fbclid               VARCHAR(255)  nullable

~ operating_months, employee_count, revenue_l12m, revenue_prev_12m, cogs_l12m,
  fixed_costs_l12m, variable_costs_l12m, estimated_rate_min, estimated_rate_max
                       NOT NULL → nullable (a quiz row has none of the calculator's figures)
```

For a quiz row, `estimated_rate_min = estimated_rate_max` = the monthly rate the visitor saw (NULL when declined), so the existing rate column in the admin list shows it.

Two CHECKs keep each kind of row complete:

- `chk_rate_inquiries_calculator_complete`: `source <> 'CALCULATOR'` OR all nine columns above are NOT NULL (exactly what the table guaranteed before).
- `chk_rate_inquiries_quiz_complete`: `source <> 'QUIZ'` OR (`monthly_revenue`, `operating_band`, `quiz_outcome`, `quiz_params_version`, `full_name`, `phone`, `consented_contact_at` NOT NULL, and the rate is NOT NULL exactly when `quiz_outcome = 'QUOTED'`).

### Alembic migration

New hash-based revision chained off the current head. Downgrade deletes `QUIZ` rows (they cannot satisfy the old NOT NULLs), then restores the constraints and drops the columns.

---

## 4. API Contract

### `POST /api/v1/rates/sme/quiz-leads` → 201

Public, captcha-checked (`turnstile_token`). Saves one `rate_inquiries` row with `source = 'QUIZ'`.

**Request**
```json
{
  "requested_amount": 50000000,
  "tenor_months": 6,
  "operating_band": "Y1_3",
  "industry": "Retail Trade",
  "monthly_revenue": 100000000,
  "quiz_params_version": "sme-quiz-v1",
  "outcome": "QUOTED",
  "monthly_rate_pct": 1.21,
  "full_name": "Nguyễn Văn A",
  "phone": "0901 234 567",
  "consent_contact": true,
  "session_id": "sess_…",
  "turnstile_token": "…",
  "tracking": {
    "utm_source": "facebook", "utm_medium": "paid", "utm_campaign": "sme-2026-11",
    "utm_content": null, "utm_term": null, "fbclid": "IwAR…"
  }
}
```

**Response 201**
```json
{"inquiry_id": "uuid"}
```

The backend does not re-price. It stores the rate the visitor saw, after checking it is within what the published formula can produce (rule 9). The formula lives only in the frontend.

### `GET /admin/rates/inquiries` (existing, ADMIN / SYSTEM_ADMIN)

- New optional query param `source` (`CALCULATOR` | `QUIZ`).
- Each item gains `source`, `monthly_revenue`, `operating_band`, `quiz_outcome`, `quiz_params_version` and the six tracking fields.
- `operating_months`, `employee_count`, the five revenue/cost figures, `yoy_growth_pct`, `estimated_rate_min` and `estimated_rate_max` become nullable. The `*_display` strings are always filled (for a quiz row: "1–3 yrs (quiz)", "100.000.000 ₫ / mo", "—", "1.21% / mo" or "Declined", tier "Quiz").

`GET /admin/rates/inquiries/{id}` returns the same item shape.

---

## 5. State Machine

None. A quiz lead is written once and never changes.

---

## 6. Business Rules

### Questions (one per screen, in this order)

| # | Question (vi) | Answers | Used for |
|---|---|---|---|
| Q1 | Bạn cần bao nhiêu? | chips 20 · 30 · 50 · 80 · 100 triệu | burden (rule 5) |
| Q2 | Trong bao lâu? | chips 1 · 2 · 3 · 4 · 5 · 6 tháng | burden (rule 5) |
| Q3 | Doanh nghiệp đã hoạt động bao lâu? | Dưới 1 năm · 1–3 năm · Trên 3 năm | saved for sales, not priced (open question 1) |
| Q4 | Ngành nghề? | the 15 industries + Khác | industry adjustment (rule 4) |
| Q5 | Doanh thu trung bình mỗi tháng? | number field (numeric keypad) with chips 50tr · 100tr · 200tr · 500tr · 1 tỷ | burden (rule 5) |

Amount chips stop at 100 triệu because of the 100M per-legal-entity cap (D-040).

### Illustrative rate formula (frontend, `lib/rate-quiz.ts`)

1. **Rate** = baseline + industry adjustment + burden adjustment, in % a year.
2. **Baseline** = 14.5% a year.
3. **Versioned.** Baseline, industry table and brackets live in one constant (`SME_QUIZ_PARAMS`, version `sme-quiz-v1`). Every saved lead carries the version. Changing a value means a new version.
4. **Industry adjustment.** The 15 industries ranked by the Sector premium in `sector_reference_v1.yaml` (version `v1-20260728`), +0.1% per rank (Loc, 20 Nov 2026). These are a fixed copy typed into the frontend constant. The quiz does not read that file, and nothing about the engine changes.

   | Rank | Industry (code) | Adjustment |
   |---|---|---|
   | 1 | Dịch vụ CNTT (`IT Services`) | +0.0 |
   | 2 | Y tế & dược phẩm (`Healthcare & Pharmacy`) | +0.1 |
   | 3 | Dịch vụ chuyên môn (`Professional Services`) | +0.2 |
   | 4 | Giáo dục & đào tạo (`Education & Training`) | +0.3 |
   | 5 | Sắc đẹp & chăm sóc cá nhân (`Beauty & Personal Care`) | +0.4 |
   | 6 | Bán lẻ (`Retail Trade`) | +0.5 |
   | 7 | Thực phẩm & đồ uống (`Food & Beverage`) | +0.6 |
   | 8 | Sản xuất (`Manufacturing`) | +0.7 |
   | 9 | Bán lẻ điện tử (`Electronics Retail`) | +0.8 |
   | 10 | Logistics & vận tải (`Logistics & Transport`) | +0.9 |
   | 11 | Du lịch & lưu trú (`Tourism & Hospitality`) | +1.0 |
   | 12 | Nội thất & gỗ (`Furniture & Woodwork`) | +1.1 |
   | 13 | Dệt may (`Textile & Garment`) | +1.2 |
   | 14 | Nông nghiệp & trồng trọt (`Agriculture & Farming`) | +1.3 |
   | 15 | Vật liệu xây dựng (`Construction Materials`) | +1.4 |
   | — | Khác (`Other`) | +0.7 |

5. **Burden** = monthly principal ÷ monthly revenue = Q1 ÷ (Q2 × Q5) (Loc, 20 Nov 2026). Compared in integers (`amount × 100` against `threshold × tenor × revenue`), so a boundary value is never misread.
6. **Burden adjustment** (% a year; boundaries confirmed by Loc):

   | Burden | Adjustment |
   |---|---|
   | < 10% | −0.5 |
   | 10% to 20% (both inclusive) | 0 |
   | over 20% up to 25% | +0.5 |
   | over 25% up to 30% | +1.0 |
   | over 30% | **declined**, no rate |

7. **Display.** Monthly = annual ÷ 12, rounded half-up to 2 decimals, Vietnamese decimal comma, as the headline ("**1,21%/tháng**"), with the annual under it ("14,50%/năm"). Range: 1,17%/tháng (14.0%) to 1,41%/tháng (16.9%).
8. **Declined suggestion.** The largest amount chip that passes at the visitor's term and revenue, and the shortest term that passes at their amount; either may be absent.
9. **Server bounds check.** A `QUOTED` lead must carry `monthly_rate_pct` between 1.16 and 1.41 (the formula's floor and ceiling, with rounding room); a `DECLINED` lead must carry none. Anything else is a 422.

Worked examples:

| Q4 | Q1 | Q2 | Q5 | Burden | Industry | Burden adj. | Rate |
|---|---|---|---|---|---|---|---|
| Bán lẻ | 50tr | 6 | 100tr | 8.3% | +0.5 | −0.5 | 14.50%/năm = 1,21%/tháng |
| Bán lẻ | 30tr | 2 | 100tr | 15.0% | +0.5 | 0 | 15.00%/năm = 1,25%/tháng |
| Bán lẻ | 100tr | 5 | 100tr | 20.0% | +0.5 | 0 | 15.00%/năm = 1,25%/tháng |
| Nông nghiệp | 100tr | 3 | 150tr | 22.2% | +1.3 | +0.5 | 16.30%/năm = 1,36%/tháng |
| Bán lẻ | 60tr | 2 | 100tr | 30.0% | +0.5 | +1.0 | 16.00%/năm = 1,33%/tháng |
| Dịch vụ CNTT | 50tr | 6 | 100tr | 8.3% | +0.0 | −0.5 | 14.00%/năm = 1,17%/tháng (floor) |
| Vật liệu xây dựng | 60tr | 2 | 100tr | 30.0% | +1.4 | +1.0 | 16.90%/năm = 1,41%/tháng (ceiling) |
| Bán lẻ | 100tr | 2 | 120tr | 41.7% | — | — | declined; suggest 50tr, or 3 tháng |

### Result screen and lead capture

10. Name and phone sit **on the result screen, directly under the rate**, with no extra button or screen in between (Loc).
11. Fields: Họ và tên, Số điện thoại / Zalo, the consent checkbox (open question 2), button "Gửi". No email, no company name.
12. Under the rate, always: "Lãi suất minh họa cho lần đầu tìm hiểu, không phải đề nghị cho vay. Lãi suất thực tế được xác định sau khi thẩm định hồ sơ." Wording follows the `fundlok-domain` skill: no rating language, no promise.
13. Declined: no rate. "Khoản vay này lớn so với doanh thu hiện tại", the rule 8 suggestions as chips that update the answer and re-show the result, and the same name and phone fields (open question 3).
14. After sending: a confirmation and a short reference (first 8 characters of `inquiry_id`). The fields lock.

### Screen behaviour

15. One question per screen. Tapping an answer moves on. Progress "Câu n/5" and a back button.
16. No typing before Q5.
17. Answers and tracking parameters are kept in `sessionStorage`, so a reload resumes at the same question.
18. `/rate?for=sme` opens straight on Q1 with no tab choice needed; plain `/rate` opens on the SME tab as today; `?for=investor` keeps working.
19. On first load, `utm_*` and `fbclid` are read from the URL and sent with the lead.
20. Turnstile renders only when Cloudflare needs an interaction (`appearance: interaction-only`). If it errors, a message with a retry is shown. The send button never sits disabled without a reason on screen.

### Data protection

21. Nothing is stored until the visitor presses "Gửi" with consent ticked (Decree 13/2023). Consent must be literally `true`.

---

## 7. Error Cases

| Scenario | System behaviour | Response to caller |
|---|---|---|
| Amount, tenor, band, industry or version outside the allowed values | Nothing stored | 422 naming the field (never echoing the value) |
| `monthly_revenue` ≤ 0 or above `FIGURE_MAX_VND` | Nothing stored | 422 |
| `QUOTED` without a rate, a rate outside 1.16–1.41, or `DECLINED` with a rate | Nothing stored | 422 |
| Consent not literally true, or invalid name / phone | Nothing stored | 422 |
| Captcha missing or failed (secret configured) | Nothing stored | 400; provider unreachable: 503, fails closed |
| Tracking value longer than its column | Truncated, not rejected | 201 |

---

## 8. Acceptance Criteria

Backend: `tests/rates/test_sme_quiz_leads.py`.

- [ ] `test_quiz_lead_quoted_is_stored_as_quiz_row`
- [ ] `test_quiz_lead_declined_is_stored_without_rate`
- [ ] `test_quiz_lead_stores_tracking_and_truncates_long_values`
- [ ] `test_quiz_lead_requires_literal_consent`
- [ ] `test_quiz_lead_rejects_rate_outside_formula_bounds`
- [ ] `test_quiz_lead_rejects_declined_with_rate`
- [ ] `test_quiz_lead_rejects_unknown_industry_and_band`
- [ ] `test_quiz_lead_rejects_amount_above_cap`
- [ ] `test_quiz_lead_requires_captcha_when_configured`
- [ ] `test_admin_list_shows_quiz_rows_with_display_strings`
- [ ] `test_admin_list_filters_by_source`
- [ ] `test_calculator_rows_unchanged` (existing calculator path still stores and lists as before)
- [ ] `test_db_rejects_incomplete_quiz_row` and `test_db_rejects_incomplete_calculator_row`

Frontend: `lib/rate-quiz.test.ts` covers every worked example above, all four bracket boundaries, every industry's adjustment, the declined suggestions, and that monthly rounding is half-up. Component behaviour (auto-advance, back, reload resume, result plus fields on one screen) is checked by hand in the browser before merge.

---

## 9. Open Questions

| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | Q3 (years operating) is not in the formula. Keep it for sales (as built), drop it, or price it? | Loc | — |
| 2 | Consent: an unticked checkbox (as built), or a "Bằng việc bấm Gửi, bạn đồng ý…" line? Decree 13 sign-off. | Loc | — |
| 3 | Declined screen as in rule 13 (no rate, suggestion chips, still ask for phone). | Loc | — |
| 4 | Ad copy: the ad said "từ 1,33%/tháng"; the quiz ranges 1,17–1,41%/tháng. The next ad should quote the quiz's real range. | Loc / Thùy | — |
| 5 | Who in sales works the quiz leads in the admin table, and what follow-up time the confirmation screen may promise. | Loc | — |
| 6 | When to retire `POST /api/v1/rates/calculate` and the SME use of `grade_lite` (the engine itself stays as it is). | Phat / Edward | — |

Resolved on 20 Nov 2026 (Loc): burden = Q1 ÷ (Q2 × Q5); industry adjustments by Sector-premium rank, 0.1% per step; "Khác" = +0.7; bracket boundaries as in rule 6; the rate is computed in the browser and is illustrative only; leads go to the existing admin table.

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 0.1–0.4 | 2026-11-20 | Drafted with Claude for Loc | Server-side quote design, pricing settled. |
| 1.0 | 2026-11-20 | Drafted with Claude for Loc | Reframed as an illustrative mock computed in the browser; no engine involvement; leads saved to `rate_inquiries` (`source = 'QUIZ'`) via one new endpoint. |
