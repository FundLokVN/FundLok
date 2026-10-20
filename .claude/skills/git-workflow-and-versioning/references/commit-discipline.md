# Commit discipline (general practice)

Generic git habits that sit underneath the FundLok-specific rules in `SKILL.md`.

## Atomic commits, one concern each
- Each commit does one logical thing: implement a slice, test it, commit, move on.
- Keep refactors, formatting and behaviour changes in separate commits (ideally separate PRs). Mixed commits can't be reverted cleanly.
- Size guide: ~100 changed lines is easy to review, ~300 is fine for one logical change, and ~1000 should be split (see `code-review-and-quality` for splitting strategies).

## The save-point loop
```
change → pytest green? → commit → next change
                └─ red → `git restore`/`git reset --hard HEAD` to the last commit → investigate
```
You never lose more than one increment. Don't pile up uncommitted work across several slices.

## Change summary (end of a task)
```
CHANGES MADE:
- app/market/service.py: reject orders from investors without approved KYC
THINGS I DIDN'T TOUCH (intentionally):
- app/market/router.py: pagination bug noticed, separate fix
POTENTIAL CONCERNS:
- Existing PENDING_PAYMENT orders from unverified investors are not back-filled
```

## Worktrees for parallel agent work
```bash
git worktree add ../FundLok-ledger-fix fix/ledger/reversal-idempotency
git worktree remove ../FundLok-ledger-fix   # after merge
```
Each worktree has its own branch, so agents don't trip over each other's checkout. Every worktree still shares the one Postgres on :5432 when running tests. Run suites one at a time, or use `bun run docker:test`, which uses its own container.

## Git as a debugger
```bash
git bisect start; git bisect bad HEAD; git bisect good <sha>   # then run the failing test at each step
git log --oneline -- app/ledger/                                # history of one module
git log -S "Idempotency-Key" --oneline                          # when a string appeared or disappeared
git blame app/ledger/service.py
```

## Generated files
Commit `alembic/versions/*` and `requirements.txt` pins. Never commit `.venv/`, `__pycache__/`, `.env*` (other than `.env.example`), keys, or local DB dumps.
