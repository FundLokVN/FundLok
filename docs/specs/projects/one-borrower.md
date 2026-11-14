# Spec: One borrower across its projects

| Field | Value |
|---|---|
| **Status** | IMPLEMENTED (needs Edward's review: it adds a migration, a column on the shared `projects` table and an index on `funding_windows`) |
| **Owner** | Phat |
| **Implementer(s)** | Phat (backend and frontend) |
| **Module** | `app/projects/borrower.py`, `app/projects/service.py`, `app/market/funding.py`; migration `6664b74ae183`; `app/lending/models.py` (`Project.tax_business`), `app/direct_transfer/models.py` (index only) |
| **Version** | 1.0 |
| **Date** | 2026-11-14 |
| **Related ADR** | ADR-009 |
| **Depends on** | D-016 (`docs/outstanding-issues.md`), `docs/specs/direct_transfer/funding-window.md`, `docs/specs/direct_transfer/end-to-end-payment-flow.md` |

---

## 1. Context & Goal

D-016 (Lok, 2026-10-28): at most 100M outstanding per borrower on FundLok, and one listing per SME until its previous loan is repaid in full. Both were checked per **project**: `uq_funding_windows_one_live_per_project` and `check_borrower_cap` (target plus declared outside debt, not FundLok's own records). The pre-launch QA (Appendix M 2.6, 2026-11-14) had an SME repaying 100M open a second 50M listing under a second tax code: 150M outstanding.

**Goal:** the borrower, not the project, carries the limits. The database holds what a column can; the code holds the rest.

## 2. Out of Scope

- A repeat loan for the same business after full repayment. `projects.tax_id` was already unique, so a business can never have a second project; a new application on the existing project is a separate feature (open question 1).
- CIC as the source of outside debt (I-25): still the SME's declaration, taken as zero when a listing opens.
- Merging or cleaning existing rows that break the rules: the migration lists them and stops (section 6, rule 6).

## 3. Data Model

| Where | What |
|---|---|
| `projects.tax_business` (TEXT, generated, stored) | `CASE WHEN tax_id ~ '^[0-9]{12}$' THEN tax_id WHEN tax_id ~ '^[0-9]{10}(-[0-9]{3})?$' THEN left(tax_id, 10) END`: the business a well-formed tax code belongs to; null for any other shape (older rows) |
| `uq_projects_tax_business` | Unique on `projects.tax_business` (nulls do not clash) |
| `uq_funding_windows_one_live_per_sme` | Unique on `funding_windows.sme_user_id` where `status IN ('OPEN','FULL','CLOSING','REPAYING')`, beside the existing one per project |

New projects store the tax code in one form (`borrower.normalize_tax_code`): `0123456789`, `0123456789-001`, or a 12-digit personal number.

## 4. API Contract

### `POST /projects` (shape unchanged)
`tax_id` is now required and normalized. New refusals (`X-Error-Code`):

| Status | Code | When |
|---|---|---|
| 422 | `INVALID_TAX_CODE` | Missing, or not 10 digits, 10-3 digits or 12 digits (spaces and dots ignored) |
| 409 | `TAX_CODE_NOT_VERIFIED_BUSINESS` | The SME's business is verified (approved KYB with a tax code) and this is another business |
| 409 | `SME_HAS_OPEN_PROJECT` | Another of the SME's projects is still in progress (rule 3) |
| 409 | `DUPLICATE_TAX_CODE` | The business already has a project, however its code was written (was 400 without a code) |

### `POST /admin/projects/{project_id}/funding-window`, and opening on its own (`publish_if_ready`)
| Status | Code | When |
|---|---|---|
| 409 | `SME_HAS_LIVE_LISTING` | The SME account already has a listing open or a loan repaying (the database index; existing code) |
| 409 | `TAX_CODE_NOT_VERIFIED_BUSINESS` | An owner verified another business than the project's tax code |

## 5. State Machine

No new states.

## 6. Business Rules

1. **One project per business** (database). A branch is its company; a household business is its 12-digit number.
2. **One live listing per SME account** (database). With nothing else live or owed, the 100M target cap is the borrower's cap.
3. **One project in progress per SME** (code). A project has ended when it is CLOSED or ARCHIVED, every funding round on it FAILED, was CANCELLED or was REPAID, or every application on it was rejected. Any other project, including a draft and an approved one not yet listed, is in progress; one with a live window is always in progress.
4. **The verified business** (code). Once an SME has an approved KYB with a tax code, a project must be that business, when it is created and again when its listing opens (it may have been created first).
5. **Tax code required** (code, new projects only). Older rows keep what they have; one in another shape is not compared by the database.
6. **Migration.** Existing rows that break rule 1 or 2 stop the upgrade with a list (account: windows, or business: projects). Someone decides which window to cancel or which project to change, then upgrades again.

## 7. Error Cases

| Scenario | Behaviour | Response |
|---|---|---|
| Same business, branch code, other account | Insert refused by `uq_projects_tax_business` | 409 `DUPLICATE_TAX_CODE` |
| SME repaying, admin opens its second project | Insert refused by `uq_funding_windows_one_live_per_sme` | 409 `SME_HAS_LIVE_LISTING` |
| SME repaying, creates a project | Refused before insert | 409 `SME_HAS_OPEN_PROJECT` |
| Project created before KYB, for another business | Listing refused | 409 `TAX_CODE_NOT_VERIFIED_BUSINESS` |
| Dev database with one SME holding three live windows (QA data) | `alembic upgrade` stops and lists them | — |

## 8. Acceptance Criteria

`tests/projects/test_one_borrower.py`:

- [x] `test_a_project_needs_a_valid_tax_code_kept_in_one_form`
- [x] `test_a_business_has_one_project_however_its_tax_code_is_written`
- [x] `test_a_project_must_be_the_business_the_sme_verified`
- [x] `test_an_sme_cannot_start_a_second_project_while_one_is_in_progress`
- [x] `test_an_sme_repaying_a_loan_can_neither_start_nor_list_another_project`
- [x] `test_a_listing_cannot_open_for_a_business_other_than_the_one_verified`
- [x] `test_the_database_holds_one_live_listing_per_sme`

## 9. Open Questions

| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | How does a business borrow again after repaying in full, when it can only ever have one project? | Edward, Lok | — |
| 2 | Should `check_borrower_cap` (Edward's) add FundLok's own outstanding records? With rule 2 they are always zero when a listing opens | Edward | — |
| 3 | Should the per-SME rule move into `open_funding_window` itself rather than rest on the index's IntegrityError? | Edward | — |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-11-14 | Phat / Claude | First version, with the implementation |
