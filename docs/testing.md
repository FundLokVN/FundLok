# Running the tests

Written 2026-10-30 after a session of chasing test-setup problems; every command here was run.

The suite is about 1,700 tests and takes about 11 minutes in full. It needs a real PostgreSQL 15 (it builds the schema with `alembic upgrade head`, then truncates tables between tests) and mocks all external services (no real bank, Didit, R2 or email calls).

## Option 1 (recommended): everything in Docker

```bash
./scripts/docker_test.sh                                  # the whole suite
./scripts/docker_test.sh tests/direct_transfer -q         # any pytest arguments pass through
./scripts/docker_test.sh tests/users/test_payment_accounts_api.py -x -q
```

(`bun run docker:test` does the same.) It starts a throwaway `postgres:15` and a `python:3.11` container, installs `requirements-dev.txt`, runs `alembic upgrade head`, then pytest, and removes both containers afterwards. It mirrors the CI "Run Test Suite" job.

- **Needs:** Docker Desktop running. Nothing else: no venv, no `.env`, no database setup, no ports (so it never clashes with the dev stack on :5433).
- **Speed:** a cold subset takes about 2 minutes (the first run downloads the images and installs packages); later runs reuse a pip cache volume.
- It tests the files in your folder as they are, so check out the branch you want first.

## Option 2: from your own venv (faster to iterate)

Use this when you re-run a few tests many times.

```bash
docker compose up -d postgres                              # the dev Postgres, port 5433
# a clean, empty database just for tests (recreate it whenever you switch branches):
docker compose exec -T postgres psql -U fundlok -d postgres \
  -c "DROP DATABASE IF EXISTS fundlok_test" -c "CREATE DATABASE fundlok_test"

DATABASE_URL=postgresql+asyncpg://fundlok:fundlok@localhost:5433/fundlok_test \
SECRET_KEY=test \
TOTP_ENCRYPTION_KEY=n5W7jlQOzlCKwRHfeCH4cpb6vUvai3kOX0GnEgEh9i4= \
.venv/bin/python -m pytest -q                              # or add a path: tests/direct_transfer
```

- **Never point the tests at `fundlok_dev`.** They wipe tables. That is why they get their own database, `fundlok_test`.
- The `.venv/bin/python` path works without activating the venv. Alternatively run `source .venv/bin/activate` once per terminal, after which `pytest` works as a bare command.
- Why the long environment line: `conftest.py` defaults to `test_user:test_password@localhost:5432/test_db`, which is what CI's Postgres uses. Locally that port belongs to whatever else you run, so override `DATABASE_URL` as above.
- **Recreate `fundlok_test` when you switch branches.** The tests run `alembic upgrade head`; a database left at a newer migration than your code knows fails with "Can't locate revision".

## Your dev database

`scripts/reset_db.sh` drops and recreates `fundlok_dev`, migrates it to the current head, and loads the seed users (password `Password123!`). It destroys local dev data, by design. Use it after switching to a branch with different migrations.

## When it goes wrong

| What you see | Cause and fix |
|---|---|
| Every test shows `E` (error at setup) | One environment problem repeated. Re-run with `-x` and read the first error: `... pytest -x -q 2>&1 \| tail -40` |
| `Multiple head revisions are present` | Two migrations chain off the same parent, so the migration chain has two ends. Add a merge migration, or re-point one. `scripts/premerge_check.sh <PR>` catches this before merging |
| `Can't locate revision identified by ...` | The test database is at a newer migration than your code. Recreate `fundlok_test` (above) |
| `password authentication failed for user "test_user"` | The tests are pointing at the default `test_user` on port 5432. Set `DATABASE_URL` as in option 2, or use option 1 |
| `No module named 'tests.uploads'` (or similar) during collection | A package installed in your venv ships its own top-level `tests/` folder, which shadows the repo's. Find it with `.venv/bin/pip list` and `ls .venv/lib/python3.11/site-packages/tests`; it was `icecream` once. Remove the stray folder or the package, or use option 1 |
| `Cannot connect to the Docker daemon` | Start Docker Desktop |
| Collection works but a few tests fail on `main` | Check you are on current `main` (`git pull`) and the test database is freshly recreated |

## Before you merge

Run `scripts/premerge_check.sh <PR number>` for any PR that adds a migration, and whenever `main` has moved since the PR's checks ran. `main` has no branch protection (free GitHub plan), so GitHub will not stop a merge on stale checks. Add `--tests` to also run the whole suite.
