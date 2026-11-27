# Spec: One borrower across its projects

| Field | Value |
|---|---|
| **Status** | v1.0 IMPLEMENTED (Phat, `d2ea9ee`). **v1.1 IMPLEMENTED** (Phat, branch `feature/projects/borrow-again`, migration `d5c1e8a7f302`; D-050). Section 3, rules 1 and 3, and section 8 change; everything not mentioned stays as v1.0 |
| **Owner** | Phat |
| **Implementer(s)** | v1.0: Phat. v1.1 (HANDOFF-06 T6): a Claude Sonnet implementer after Phat and Edward approve this version; Phat reviews the PR (shared file `app/lending/models.py`, `app/projects`) |
| **Module** | `app/projects/borrower.py`, `app/projects/service.py`, `app/market/funding.py`, `app/direct_transfer/windows.py`; migration `6664b74ae183` (v1.0) and a new one (v1.1); `app/lending/models.py` (`Project`), `app/direct_transfer/models.py` (index only) |
| **Version** | 1.2 (approved business verification required before project creation) |
| **Date** | 2026-11-15 |
| **Related ADR** | ADR-009, ADR-011 |
| **Depends on** | D-016, D-040, D-050 (`docs/outstanding-issues.md`), `docs/specs/direct_transfer/funding-window.md`, `docs/specs/direct_transfer/end-to-end-payment-flow.md`, handbook 10.1 and Appendix M 7.7 |

---

## 1. Context & Goal

D-016 (Lok, 2026-10-28): at most 100M outstanding per borrower on FundLok, and one listing per SME until its previous loan is repaid in full. Both were checked per **project**: `uq_funding_windows_one_live_per_project` and `check_borrower_cap` (target plus declared outside debt, not FundLok's own records). The pre-launch QA (Appendix M 2.6, 2026-11-14) had an SME repaying 100M open a second 50M listing under a second tax code: 150M outstanding.

**Goal (v1.0):** the borrower, not the project, carries the limits. The database holds what a column can; the code holds the rest.

**v1.1 addition.** v1.0 made a business's project unique forever, so a business that repaid in full could never borrow again. Loc's rule (handbook 10.1, Appendix M 7.7, decisions of 2026-11-15, D-040): **a business that has repaid in full must be able to borrow again**, and it may re-apply straight away after a listing that failed or was refused. Edward decided (2026-11-15, D-050): **one new project row per loan**, and the uniqueness becomes *one project in progress per business*.

## 2. Out of Scope

- CIC as the source of outside debt (I-25): still the SME's declaration, taken as zero when a listing opens.
- The scope of the borrower cap (FundLok loans only, or also declared outside debt; `cross_platform_cap_vnd` is 400M, provisional): a separate decision, still open (open question 2).
- Merging or cleaning existing rows that break the rules: the migration lists them and stops (section 6, rule 6).
- A "returned to SME" application state (TD-02).
- ~~Copying a closed project's details into a new one~~: built as the repeat request (section 4, `POST /projects/repeat`, Phat 2026-11-19).

## 3. Data Model

### v1.0 (as built)

| Where | What |
|---|---|
| `projects.tax_business` (TEXT, generated, stored) | `CASE WHEN tax_id ~ '^[0-9]{12}$' THEN tax_id WHEN tax_id ~ '^[0-9]{10}(-[0-9]{3})?$' THEN left(tax_id, 10) END`: the business a well-formed tax code belongs to; null for any other shape (older rows) |
| `uq_funding_windows_one_live_per_sme` | Unique on `funding_windows.sme_user_id` where `status IN ('OPEN','FULL','CLOSING','REPAYING')`, beside the existing one per project |

New projects store the tax code in one form (`borrower.normalize_tax_code`): `0123456789`, `0123456789-001`, or a 12-digit personal number.

### v1.1 (changes)

**No new column.** `projects.status` already has the values that mean a project has ended (`CLOSED`, `ARCHIVED`; `borrower.ENDED_PROJECT_STATUSES`), and the code already treats them as ended. v1.1 keeps that one source of truth and makes it the thing the database reads. An open project is one whose `status` is not `CLOSED` or `ARCHIVED` (so `DRAFT`, `ACTIVE` and `SUSPENDED` are open).

| Where | v1.0 | v1.1 |
|---|---|---|
| `uq_projects_tax_business` | Unique on `projects.tax_business` | **Dropped.** Replaced by `uq_projects_one_open_per_business`: unique on `projects.tax_business` `WHERE status NOT IN ('CLOSED','ARCHIVED')` |
| `projects_tax_id_key` | Unique constraint on `projects.tax_id`, created in migration `0001` (frozen). It also stops a second row with the same code | **Dropped** in the new migration. Replaced by `uq_projects_one_open_per_tax_id`: unique on `projects.tax_id` `WHERE status NOT IN ('CLOSED','ARCHIVED')`, so older codes that fit neither shape stay unique among open projects. The implementer reads the constraint's real name from the database before dropping it |
| `uq_funding_windows_one_live_per_sme` | One live window per SME account | Unchanged. With one account per business (D-040) it is also one live listing per business |

Result: a business has at most one project in progress, and any number of ended projects, one per loan, each keeping its own application, listing and repayment history.

The new migration is a new hash-based revision off the current single head. It never edits `0001` or `6664b74ae183`.

## 4. API Contract

No change to request or response shapes.

### `POST /projects`
`tax_id` is required and normalized (v1.0). Refusals (`X-Error-Code`):

| Status | Code | When (v1.1) |
|---|---|---|
| 422 | `INVALID_TAX_CODE` | Missing, or not 10 digits, 10-3 digits or 12 digits (spaces and dots ignored) |
| 409 | `BUSINESS_VERIFICATION_REQUIRED` | The SME has no approved business verification with a valid tax code |
| 409 | `TAX_CODE_NOT_VERIFIED_BUSINESS` | The approved verification identifies a different business |
| 409 | `SME_HAS_OPEN_PROJECT` | The SME has a project in progress (rule 3). The friendly pre-check |
| 409 | `DUPLICATE_TAX_CODE` | The business already has a project **in progress** under another account, however its code was written (the database index). A business whose earlier projects have all ended is **not** refused |

### `POST /projects/repeat` (added 2026-11-19)

A business borrowing again sends only `{requested_amount, duration_months}` (the same bounds as `POST /projects`; the
term defaults to the last request's). The new project copies the company details of the SME's newest ended project
whose application staff approved; the application copies its purpose, repayment preference, self-reported figures,
guarantor address, and its uploaded documents (each file copied to the new application's own key) with their parsed
summaries, and is `SUBMITTED` at once. The signed listing documents are not copied: they carry the approved terms and
are signed again after this request's approval. Staff review and the score run are unchanged.

| Status | Code | When |
|---|---|---|
| 409 | `SME_HAS_OPEN_PROJECT` | The SME has a project in progress |
| 409 | `NO_APPROVED_REQUEST` | No ended project with an approved application to copy: the business uses the full application |
| 422 | | Amount or term outside the engine's bounds |

### `POST /admin/projects/{project_id}/funding-window`, and opening on its own (`publish_if_ready`)

| Status | Code | When |
|---|---|---|
| 409 | `SME_HAS_LIVE_LISTING` | The SME account already has a listing open or a loan repaying. Raised by `open_funding_window` itself (v1.1, open question 3); the database index stays as the backstop |
| 409 | `TAX_CODE_NOT_VERIFIED_BUSINESS` | An owner verified another business than the project's tax code |

## 5. State Machine

`projects.status` gains one deliberate transition: **any open status to `CLOSED` when the project has ended** (rule 3). No other state is added. `ARCHIVED` and staff-set `CLOSED` stay as they are.

## 6. Business Rules

1. **One project in progress per business** (database; v1.0 said "one project per business"). A branch is its company; a household business is its 12-digit number. Ended projects do not count and are kept.
2. **One live listing per SME account** (database). With nothing else live or owed, the 100M target cap is the borrower's cap.
3. **When a project ends** (code). The code sets `status = 'CLOSED'`, once and idempotently, through one function (`close_project_if_finished(db, project_id, reason)`), called at every transition below, with an audit row giving the reason:
   - its last funding round is `REPAID` (full repayment): `REPAID`;
   - its last funding round is `FAILED` or `CANCELLED` and no round on it is live: the business may re-apply straight away (Loc, 2026-11-15): `ROUND_FAILED` or `ROUND_CANCELLED`;
   - every application on it is rejected: `REJECTED`;
   - staff close or archive it (existing behaviour): `STAFF`.

   A project with a live round, or with one not yet decided, is in progress. A closed project is not reopened; a failed round means the SME starts a **new project** (a new row, Edward 2026-11-15), which Phat's UI may prefill.
4. **The verified business** (code). A new project requires an approved KYB with a matching tax code. For older projects the listing check still rejects a mismatch with any approved KYB.
5. **Tax code required** (code, new projects only). Older rows keep what they have; one in another shape is not compared by `tax_business`, but is compared by the open-project index on `tax_id`.
6. **Migration.** In this order, in one revision: (a) set `status = 'CLOSED'` for every existing project that rule 3 says has ended (the same derivation `unfinished_project` uses today), so the new index does not count them; (b) stop with a list if any business or tax code still has more than one *open* project, or any SME account more than one live window (as v1.0): someone decides, then upgrades again; (c) drop `uq_projects_tax_business` and `projects_tax_id_key`; (d) create the two partial unique indexes. `downgrade` recreates the old indexes and refuses if two projects now share a business or code.
7. **Existing code.** `borrower.unfinished_project` keeps its derivation as the one definition of "ended", and `close_project_if_finished` uses it. `check_new_project` and `check_can_open` keep their checks.

## 7. Error Cases

| Scenario | Behaviour | Response |
|---|---|---|
| Same business, branch code, other account, first project still in progress | Insert refused by `uq_projects_one_open_per_business` | 409 `DUPLICATE_TAX_CODE` |
| Business repaid in full, creates a new project (same tax code) | Allowed: the old project is `CLOSED` | 201 |
| Round below 50% (`FAILED`), SME creates a new project straight away | Allowed: the old project was closed when its round failed | 201 |
| Every application rejected, SME creates a new project | Allowed | 201 |
| SME repaying, creates a project | Refused before insert | 409 `SME_HAS_OPEN_PROJECT` |
| SME repaying, admin opens its second project | `open_funding_window` refuses; the index is the backstop | 409 `SME_HAS_LIVE_LISTING` |
| Project created before KYB, for another business | Listing refused | 409 `TAX_CODE_NOT_VERIFIED_BUSINESS` |
| Migration finds two open projects for one business, or three live windows for one SME | `alembic upgrade` stops and lists them | — |

## 8. Acceptance Criteria

v1.0, `tests/projects/test_one_borrower.py` (kept; the two marked are reworded by v1.1):

- [x] `test_a_project_needs_a_valid_tax_code_kept_in_one_form`
- [x] `test_a_business_has_one_project_however_its_tax_code_is_written` (v1.1: *one open project*; the assertions run while the first is still open)
- [x] `test_a_project_must_be_the_business_the_sme_verified`
- [x] `test_an_sme_cannot_start_a_second_project_while_one_is_in_progress`
- [x] `test_an_sme_repaying_a_loan_can_neither_start_nor_list_another_project`
- [x] `test_a_listing_cannot_open_for_a_business_other_than_the_one_verified`
- [x] `test_the_database_holds_one_live_listing_per_sme`

v1.1, as built (`tests/projects/test_one_borrower.py`, `tests/projects/test_one_borrower_migration.py`):

- [x] `test_business_can_borrow_again_after_full_repayment`: facility SETTLED, round REPAID, project CLOSED; the same
  business starts, submits and gets approved a new request under its own tax code; still refused while repaying
- [x] `test_business_can_reapply_straight_away_after_a_failed_round` (the SME declines: FAILED)
- [x] `test_project_closes_when_its_round_fails_below_the_minimum_or_is_cancelled`
- [x] `test_business_can_reapply_after_its_application_is_rejected`: also closing twice writes one audit row, and a
  second account for the same business is refused by the database while the new project is open
- [x] `test_migration_closes_ended_projects_and_downgrade_refuses_shared_codes`
- [x] `test_a_repeat_request_copies_the_last_approved_request_and_goes_to_review`
- [x] `test_a_repeat_request_takes_a_new_term_and_needs_an_approved_request_to_copy`
- A second live listing for one SME: `test_an_sme_repaying_a_loan_can_neither_start_nor_list_another_project` and
  `test_the_database_holds_one_live_listing_per_sme` (v1.0, unchanged)

Not built from this version: the migration's check for two open projects of one business (every project was unique
when it runs, so it cannot find one), and the per-SME live-listing check inside `open_funding_window` (open question
3): the index still refuses it, reported as SME_HAS_LIVE_LISTING. `close_project_if_finished` is called where a round
becomes REPAID, FAILED (SME declines or nobody pays, below the minimum, nothing repays from approval) or CANCELLED, and
where staff reject an application. A closed project's signed documents still read as published
(`app/loans/signing.py`), and GET /projects lists newest first.

## 9. Open Questions

| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | How does a business borrow again after repaying in full, when it can only ever have one project? | Edward, Lok | **Resolved v1.1.** Loc: it must be able to (handbook 10.1, App. M 7.7, 2026-11-15). Edward: one new project row per loan; one project in progress per business (D-050); the business may re-apply straight away after a failed or refused listing |
| 2 | Should `check_borrower_cap` (Edward's) add FundLok's own outstanding records, and does the 100M cap count declared outside debt? With rule 2 FundLok's own are always zero when a listing opens | Edward | Open. Depends on the cap-scope decision (D-040 says 100M per legal entity; the code still carries `cross_platform_cap_vnd` 400M provisional) |
| 3 | Should the per-SME rule move into `open_funding_window` itself rather than rest on the index's IntegrityError? | Edward | **Resolved v1.1.** Yes; the index stays as the backstop (section 4) |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-11-14 | Phat / Claude | First version, with the implementation |
| 1.1 | 2026-11-19 | Phat / Claude | Implemented: migration `d5c1e8a7f302`, `borrower.close_project_if_finished`, section 8 as built |
| 1.1 | 2026-11-15 | Edward / Claude | One project in progress per business, not one ever (D-040, D-050). Drops `uq_projects_tax_business` and `projects_tax_id_key`; adds two partial unique indexes on open projects; `projects.status = 'CLOSED'` is set when a project ends (rule 3); migration backfills and checks first. Resolves open questions 1 and 3. Draft: needs Phat's review |
