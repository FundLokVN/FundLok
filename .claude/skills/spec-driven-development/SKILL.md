---
name: spec-driven-development
description: >-
  Write, update or review a FundLok feature spec in
  `docs/specs/MODULE/FEATURE.md` from `docs/specs/_TEMPLATE.md` (the nine
  sections, Status DRAFT→REVIEW→ACCEPTED→IMPLEMENTED, owner/implementer,
  acceptance criteria that become pytest names) before any implementation
  starts. Use this whenever a new feature, endpoint, table, column, state
  transition, money flow or significant behaviour change in the backend has
  no ACCEPTED spec yet; when the user says "spec", "design doc", "write it
  up", "handoff for Phat/Edward" or "what should the API look like"; when
  requirements are vague; or when implementation shows an existing spec is
  wrong. Use it even if the user says "just build it" for something with no
  spec. Not for typo fixes, pure refactors, or bug fixes that restore
  already-specified behaviour.
---

# Spec-Driven Development — FundLok

At FundLok the spec is the **handoff document between two engineers and
between AI sessions**. Edward writes backend specs, Phat implements the
frontend against them (and the reverse for frontend specs), and neither Claude
nor any other agent remembers anything between sessions. If a decision isn't
in `docs/specs/` or `docs/adr/`, it doesn't exist. The test for a finished
spec: the other engineer could implement their side **with zero chat or
verbal context**. If they'd need to ask, the spec is incomplete. Fix the spec,
not the conversation.

## The sequence

```
1. SPEC      docs/specs/<module>/<feature>.md, from _TEMPLATE.md  (Status: DRAFT)
2. REVIEW    the other engineer reviews via PR comments            (Status: REVIEW)
3. CONTRACT  request/response types agreed, Open Questions closed  (Status: ACCEPTED, spec PR merged)
4. BUILD     backend and frontend implement in parallel, in a separate PR that links the spec
5. TEST      each acceptance criterion becomes a pytest function of the same name
6. ADR       if an architectural decision was made → docs/adr/NNN-<slug>.md
             (once shipped: Status: IMPLEMENTED)
```

Implementation code waits for step 3. Code written against a DRAFT spec
usually gets rewritten when review changes the contract, and the frontend may
already be building against the old shape. If the human wants to prototype
early, that's their call. Label it a spike and don't merge it.

## Before writing: look for what exists

1. Use `search_fl_docs` (the fl-knowledge MCP) or `ls docs/specs/<module>/` and
   `grep -ril "<topic>" docs/` for an existing or overlapping spec. Many areas
   already have one: ledger, repayment_schedule (v1.3 **and** v1.4, so check
   which is current), payments/funding-flow, banking, gverify, underwriting.
   Extending a spec (bump Version, add a Changelog row) beats writing a parallel
   one that contradicts it.
2. Read the `fundlok-domain` skill if the feature touches money, rates,
   grading, schedules, repayment, the ledger, disclosure or audit. Business
   Rules must agree with its invariants. Cite them instead of re-deriving.
3. Check `CLAUDE.md` → "Current Phase / MVP scope". If the feature is
   explicitly out of MVP scope (e.g. arrears, late-rate interest, the D27
   extension fee), say so before writing a spec for it.
4. If anything is still ambiguous, list your assumptions and ask. A short
   "I'm assuming X, Y, Z, correct me now" costs one message, while a wrong
   assumption baked into an ACCEPTED spec costs two PRs.

## Writing the nine sections

Copy `docs/specs/_TEMPLATE.md`, fill in the header table (Status `DRAFT`,
Owner = whoever is accountable, Implementer(s), Module, Version `1.0`,
today's date, Related ADR, Depends on), then the sections below.
`references/section-guide.md` has a filled-in example for each section.

1. **Context & Goal**: 2–4 sentences on why, plus the business or regulatory
   driver (Decree 94, Circular 64, handbook section). End with a one-sentence
   **Goal**.
2. **Out of Scope**: as important as the scope. Name what a future spec must
   handle so nobody "helpfully" builds it now.
3. **Data Model**: typed pseudocode, not prose. FundLok rules:
   - UUID primary keys and `TIMESTAMPTZ` timestamps.
   - **Money is integer VND**: `Numeric(20,0)` / `BIGINT`, Python `int`, no
     floats and no 2-dp decimals (accepted spec
     `docs/specs/ledger/money-integer-vnd.md`). The template's
     `DECIMAL(15,2)` example predates that spec. Follow the spec, and mention
     the template drift to the human rather than editing the template
     yourself.
   - The migration gets an Alembic **hash** revision ID at implementation time.
     Write "new Alembic revision (hash assigned on generation)", not
     `XXXX_…`/`0002_…`. Existing migrations are frozen.
   - Anything touching `ledger_entries`/`ledger_transactions` is append-only.
     Model corrections as reversing entries, never as UPDATE/DELETE.
4. **API Contract**: every endpoint with method/path, auth roles (`SME`,
   `INVESTOR`, `ADMIN`, public), typed request and response (no
   `object`/`any`), and errors as `{"detail": "<message>"}`. Any endpoint that
   creates a financial record takes an `Idempotency-Key` header and says what a
   replay returns. Responses are Pydantic models, so name them. This section is
   what lets Phat build the UI in parallel. Make it precise enough to generate
   TypeScript types from.
5. **State Machine** (if a status field is added or changed): states,
   triggers, who may trigger each, and the error for an invalid transition.
   Reconcile with the core machines in `CLAUDE.md` (LoanApplication, ScoreRun,
   Contract, Listing, Order) and the MVP repayment machine.
6. **Business Rules**: numbered, each one testable, citing the handbook
   (`fundlok-domain`) or regulation where it comes from.
7. **Error Cases**: table of scenario / system behaviour / what the caller
   sees. Always cover duplicate delivery (idempotency), wrong role, wrong
   state, not found, and for anything external (bank feed, Didit, email) the
   partner being down or replying twice.
8. **Acceptance Criteria**: checkboxes whose text *is* a pytest name, e.g.
   `test_funding_order_idempotent_replay_returns_existing`. Each must be
   binary. `test-driven-development` turns these into failing tests first,
   which only works if they're precise.
9. **Open Questions**: table with an **Owner (Edward / Phat)** for every
   question. A spec can't move to ACCEPTED with open rows, because each one is
   a decision the implementer would otherwise make silently.

Remove the template's `<!-- INSTRUCTIONS -->` block before review, and add a
Changelog row.

## Ownership and the spec PR

- Backend behaviour → Edward owns the spec. Frontend behaviour → Phat. If the
  current user is writing a spec in the other person's area, mark it DRAFT,
  set Owner correctly, and flag that the owner has to review it.
- Anything changing `app/auth/`, the shared ORM models or the OpenAPI contract
  needs both to agree. List both as reviewers in the Open Questions.
- The spec ships alone on a `docs/<module>/<desc>` branch (see
  `git-workflow-and-versioning`) and is merged before the implementation PR
  is opened.

## When the spec is wrong mid-build

Stop and update the spec first: edit the section, bump the Version (1.0 →
1.1), add a Changelog row saying what changed and why, and tell the human the
contract moved. If the API contract changed, the other engineer needs to know
before they ship against the old one. Code that quietly diverges from an
ACCEPTED spec is the failure this whole process exists to prevent.

## If it's an architectural decision

Choosing a partner, a data-flow shape, a storage model, or anything costly to
reverse belongs in an ADR (`docs/adr/NNN-<slug>.md`, next number after the
highest existing, plus an entry in the `CLAUDE.md` ADR index). Reference it in
the spec's header. See `documentation-and-adrs`.

## Done when

- [ ] File is at `docs/specs/<module>/<feature>.md`, all nine sections present (State Machine may say "n/a")
- [ ] Money is integer VND, PKs are UUID, timestamps are TIMESTAMPTZ, no hand-numbered migration
- [ ] Every financial write has `Idempotency-Key` semantics and every error is `{"detail": …}`
- [ ] Business Rules agree with `fundlok-domain` and MVP scope
- [ ] Acceptance criteria are pytest-name-shaped and binary
- [ ] Open Questions each have an owner; Status reflects reality
- [ ] The other engineer could implement their side from this file alone
