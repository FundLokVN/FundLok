---
name: git-workflow-and-versioning
description: >-
  FundLok backend git and PR workflow: `TYPE/MODULE/DESC` branch names,
  `type(module): …` commit messages, spec PRs merged before implementation PRs,
  the PR checklist (spec link, passing pytest, Alembic migration), module
  ownership between Edward, Phat and shared files, and why `main`/`preview`
  are deploy branches you never commit to directly. Use this whenever you are
  about to create a branch, stage or commit, write a commit message or PR
  description, merge or rebase, resolve a conflict, or decide whether a change
  is allowed to touch a given `app/` module in this repo — even when the user
  only says "commit this", "make a branch" or "open a PR".
---

# Git Workflow — FundLok backend

Git here is not just history. Pushing to `main` deploys production and pushing
to `preview` deploys staging, and the ledger is the legal record of real
money. So the workflow is designed to make every change small, attributable to
an owner, traceable to a spec, and reversible.

## The two deploy branches

`.github/workflows/ci.yml` runs on push/PR to `main` and `preview`. The deploy
jobs need `migration-check` (`alembic upgrade head` + `alembic check` on
Postgres 15), `tests` (`pytest -q`) and `dependency-audit` to pass first:

| Push to | Deploys to |
|---|---|
| `preview` | Cloud Run **staging** |
| `main` | Cloud Run **production** |

Because of that:

- Work on a topic branch. Don't commit to `main` or `preview` yourself, even
  for a one-line fix. A direct commit skips review, and the next push ships it.
- Don't push, open, or merge a PR unless the human asks in this session. Those
  are outward-facing actions, and a merge to `main` is a production deploy.
- Never force-push `main` or `preview`. Rewriting a deploy branch makes CI and
  Cloud Run disagree about what is live.

## Branch names

Use `<type>/<module>/<short-kebab-description>`. Branch from an up-to-date
`main`.

| type | for | real examples |
|---|---|---|
| `feature` | new behaviour (CLAUDE.md's canonical form; prefer it over `feat/`) | `feature/banking/brankas-disbursement` |
| `fix` | bug or security fix | `fix/auth/2fa-attempt-limit`, `fix/market/order-eligibility-and-idempotency-scope` |
| `docs` | specs, ADRs, handbook syncs | `docs/repayment/extension-model-v1.4` |
| `chore` / `refactor` | tooling, deps, no behaviour change | `chore/deps/sca-bump` |

`<module>` is the `app/` package (`auth`, `ledger`, `market`, `gverify`, …) or
the docs area (`repayment`, `underwriting`). If a change spans many modules,
name the one that owns the behaviour. Reviewers sort and route PRs by that
segment.

## Commit messages

Use Conventional Commits with the module as scope. Write the subject as the
behaviour change in plain words, the way it would read in a changelog:

```
fix(auth): stop a pre-registered passkey surviving the owner's password reset
fix(market): require a verified, KYC-approved investor to place an order
feat(ledger): post reversing entry for a failed disbursement
docs(repayment): sync extension model to handbook v1.4
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `ci`. Use the body
for the *why*: the incident, the spec section, the invariant being protected.
Commit each green increment separately. A commit that does one thing can be
reverted on its own when it breaks staging.

## Ownership: check before you stage

`CLAUDE.md` → "Module Ownership" is the source of truth. Strict ownership is
how two people and several agents avoid silently rewriting each other's
modules. Summary:

- **Edward (CTO):** `banking`, `verification`, `underwriting`, `contracts`,
  `repayment_schedule`, `share_conversion`, `compliance`, `ledger`,
  `payments`, `system`, `core`, `utils`, `alembic/`, `docs/adr/`, backend
  specs.
- **Phat:** `users/router.py` + `users/schemas.py`, `projects`, `loans`,
  `market`, `sme`, `uploads`, `contact`, `oauth`, `admin`, all frontend code,
  frontend specs.
- **Shared (both must agree):** `app/auth/`, `app/models/__init__.py`,
  `app/lending/models.py`, `app/users/models.py`, the OpenAPI contract.

Before committing, run `git diff --name-only main...` and map each path to an
owner. If the diff touches a module the current user doesn't own, or a shared
file, say so plainly. Name the files and the owner, and put an "Ownership"
note in the PR body so the other person reviews it. Don't move the change
somewhere else to dodge the boundary. Edward doesn't write UI, so never add
frontend code to this repo.

## Spec first, then code

The order is: spec PR (`docs/specs/<module>/<feature>.md`) is reviewed and
merged, then the implementation PR is opened. See `spec-driven-development`.

- If the implementation PR has no spec to link, stop and ask whether the
  change needs one. Bug fixes that restore already-specified behaviour,
  refactors, and test-only changes usually don't.
- If implementation shows the spec is wrong, update the spec in the same PR
  (bump its version and changelog) rather than letting code and spec drift.

## Migrations and the ledger in a PR

`alembic check` in CI fails the build if models and migrations disagree, so
treat these as part of the change, not a follow-up:

- Schema change → new revision via `bun run migrate:new "<message>"`
  (`alembic revision --autogenerate`). It gets a **hash** revision ID chained
  off the current head. Never hand-number `0002+`, and never edit or delete an
  existing migration: they have already run against staging and production.
- Run `alembic heads` before pushing. Two heads mean someone merged a migration
  first. Rebase and re-point `down_revision`, don't merge heads blindly.
- `ledger_entries` / `ledger_transactions` are append-only (app layer and DB
  trigger). A diff that adds an UPDATE or DELETE path for them is wrong by
  construction. Corrections are reversing entries.

## Production hotfixes

Pressure to "just push to main" is when the workflow matters most. A direct
push isn't faster anyway: it runs the same CI gates before deploying.

- If the breakage lines up with a recent deploy, suggest rolling Cloud Run
  traffic back to the last good revision first. That stops the bleeding in
  seconds while the real fix goes through review.
- Fix on `fix/<module>/<desc>` and include a regression test that reproduces
  the failure in the same commit (see `test-driven-development`, prove-it
  pattern). A test deferred to "later" never gets written, and the same bug
  comes back.
- Ask the module owner for an expedited review. Expedited is fine; skipped is
  not.

## Before every commit

```bash
git status && git diff --staged                # exactly what you mean to ship
git diff --staged | grep -inE "secret|password|api_key|token|BEGIN .*PRIVATE"
bun run test                                    # pytest -q; needs Postgres on :5432
bun run docker:test                             # or: CI-identical throwaway Postgres 15
./.venv/bin/alembic check                       # if any model changed
```

`.env`, `.env.*` backups and key files must never be staged. A `.env.bak`
leaked real secrets into this repo once already. If a secret does get
committed, rotating it is the fix. Deleting it in a later commit doesn't help.

## PR description

```markdown
## What / why
<one paragraph; link the incident or handbook section if relevant>

**Spec:** docs/specs/<module>/<feature>.md (vX.Y)   ← or "n/a — bug fix restoring spec'd behaviour"
**Migration:** <revision id> — <what it does> | none
**Ownership:** touches <shared/other-owner files> — needs <Edward|Phat> review | own module only

## Tests
- `pytest -q` green locally / via `bun run docker:test`
- new: tests/<module>/test_<feature>.py::<acceptance-criterion names>

## Not touched (intentionally)
- <adjacent problem noticed but out of scope>
```

The "not touched" list matters. It shows the change stayed in scope and gives
the reviewer a ready-made follow-up list.

## More

- `references/commit-discipline.md`: atomic commits, the save-point loop,
  worktrees for parallel agents, and `git bisect`/`blame` for debugging.
