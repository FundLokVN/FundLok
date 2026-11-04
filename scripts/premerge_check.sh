#!/usr/bin/env bash
# Pre-merge check for a pull request. Prints GO or NO-GO.
#
#   scripts/premerge_check.sh <PR number>            # merge check (about a minute)
#   scripts/premerge_check.sh <PR number> --tests    # ...and run the whole test suite (about 11 minutes)
#
# Why this exists: main has no branch protection (free GitHub plan), so GitHub does not stop a PR from merging
# on checks that ran against an OLD main. That is how #64 merged green and left main with two Alembic heads.
# This trial-merges the PR into the CURRENT main in a throwaway folder and checks what CI would check, now:
#   1. it merges without conflicts;
#   2. the migrations form ONE chain (a single Alembic head);
#   3. `alembic upgrade head` works on an empty database and `alembic check` finds no drift.
# Run it for ANY PR that adds a migration, right before you click merge. It changes nothing in your working
# folder, the PR, or any database except a temporary one it creates and drops.
#
# Needs: Docker running with the compose postgres up (`docker compose up -d postgres`), the gh CLI (logged in),
# and the project's .venv. Postgres is used only for a throwaway database named premerge_<pid>.
set -uo pipefail

PR="${1:-}"
RUN_TESTS=0
[[ "${2:-}" == "--tests" ]] && RUN_TESTS=1
if [[ -z "$PR" || ! "$PR" =~ ^[0-9]+$ ]]; then
  echo "usage: scripts/premerge_check.sh <PR number> [--tests]" >&2
  exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT" || exit 2
PY="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then  # run from a secondary worktree: use the main worktree's venv
  MAIN_WORKTREE="$(git worktree list --porcelain | awk 'NR==1{print $2}')"
  PY="$MAIN_WORKTREE/.venv/bin/python"
fi
[[ -x "$PY" ]] || { echo "No project venv found ($PY): create it first." >&2; exit 2; }
command -v gh >/dev/null || { echo "The gh CLI is required." >&2; exit 2; }

# Talk to the compose stack of the MAIN folder even when run from a secondary worktree.
COMPOSE_DIR="$(git worktree list --porcelain | awk 'NR==1{print $2}')"
compose() { docker compose --project-directory "$COMPOSE_DIR" "$@"; }

WORK="$(mktemp -d)"
DB="premerge_$$"
FAILS=()
note_fail() { FAILS+=("$1"); echo "  FAIL: $1"; }

cleanup() {
  git worktree remove --force "$WORK" >/dev/null 2>&1 || rm -rf "$WORK"
  git worktree prune >/dev/null 2>&1
  compose exec -T postgres psql -U fundlok -d postgres -c "DROP DATABASE IF EXISTS $DB" >/dev/null 2>&1
}
trap cleanup EXIT

echo "== PR #$PR"
if [[ -n "${PREMERGE_REF:-}" ]]; then
  # Test seam: check a local branch/commit instead of a PR (used to test this script itself).
  git fetch -q origin main || { echo "git fetch failed." >&2; exit 2; }
  PR_HEAD="$(git rev-parse "$PREMERGE_REF")" || exit 2
  echo "   (local ref $PREMERGE_REF, not a real PR)"
else
  TITLE="$(gh pr view "$PR" --json title,state,isDraft -q '"\(.title) [\(.state)\(if .isDraft then ", draft" else "" end)]"' 2>/dev/null)" \
    || { echo "Cannot read PR #$PR (gh login? wrong number?)." >&2; exit 2; }
  echo "   $TITLE"
  # Two SEPARATE fetches: FETCH_HEAD points at the FIRST ref fetched, so fetching main and the PR together made
  # PR_HEAD equal main, and every PR "merged cleanly" into itself (fixed 2026-11-04).
  git fetch -q origin main || { echo "git fetch of main failed." >&2; exit 2; }
  git fetch -q origin "pull/$PR/head" || { echo "git fetch of the PR failed." >&2; exit 2; }
  PR_HEAD="$(git rev-parse FETCH_HEAD)"
  # Guard: the commit we are about to check must be the PR's own head, exactly as GitHub reports it.
  EXPECTED="$(gh pr view "$PR" --json headRefOid -q .headRefOid)"
  if [[ "$PR_HEAD" != "$EXPECTED" ]]; then
    echo "Resolved commit $PR_HEAD but GitHub says the PR head is $EXPECTED. Refusing to report anything." >&2
    exit 2
  fi
  echo "   checking the PR head $(git rev-parse --short "$PR_HEAD")"
fi
BEHIND="$(git rev-list --count "$PR_HEAD..origin/main")"
echo "   main is now $(git rev-parse --short origin/main); this PR is $BEHIND commit(s) behind it"
if [[ -z "${PREMERGE_REF:-}" ]]; then
  echo "   (what CI last said about it:)"
  gh pr checks "$PR" 2>/dev/null | awk -F'\t' '{printf "     %-28s %s\n", $1, $2}' | grep -v "Deploy" || true
fi

echo "== 1. Trial merge into the current main"
git worktree add -q --detach "$WORK" origin/main || { echo "worktree failed" >&2; exit 2; }
if (cd "$WORK" && git merge -q --no-commit --no-ff "$PR_HEAD" >/dev/null 2>&1); then
  echo "  ok: merges cleanly"
else
  note_fail "merge conflicts in: $(cd "$WORK" && git diff --name-only --diff-filter=U | tr '\n' ' ')"
fi

if [[ ${#FAILS[@]} -eq 0 ]]; then
  export SECRET_KEY="${SECRET_KEY:-premerge-check}"
  export TOTP_ENCRYPTION_KEY="${TOTP_ENCRYPTION_KEY:-n5W7jlQOzlCKwRHfeCH4cpb6vUvai3kOX0GnEgEh9i4=}"
  export DATABASE_URL="postgresql+psycopg2://fundlok:fundlok@localhost:5433/$DB"

  echo "== 2. One migration chain"
  HEADS="$(cd "$WORK" && "$PY" -m alembic heads 2>&1)"
  N_HEADS="$(grep -c "(head)" <<<"$HEADS")"
  if [[ "$N_HEADS" == "1" ]]; then
    echo "  ok: single head: $(grep "(head)" <<<"$HEADS" | awk '{print $1}')"
  else
    note_fail "$N_HEADS Alembic heads after the merge ($(grep "(head)" <<<"$HEADS" | awk '{print $1}' | tr '\n' ' ')): one migration must chain off the other (merge migration)"
  fi

  echo "== 3. Empty database: upgrade head, then check for drift"
  if ! compose exec -T postgres psql -U fundlok -d postgres -c "CREATE DATABASE $DB" >/dev/null 2>&1; then
    echo "  skipped: cannot create a database (is Docker running and `docker compose up -d postgres` done in the main folder?)"
    FAILS+=("could not run the database step")
  elif [[ "$N_HEADS" == "1" ]]; then
    if (cd "$WORK" && "$PY" -m alembic upgrade head >/dev/null 2>&1); then
      echo "  ok: alembic upgrade head"
      if (cd "$WORK" && "$PY" -m alembic check >/dev/null 2>&1); then
        echo "  ok: alembic check (models and migrations agree)"
      else
        note_fail "alembic check: the models and the migrations disagree (a missing migration?)"
      fi
    else
      note_fail "alembic upgrade head fails on an empty database"
    fi
  else
    echo "  skipped (fix the heads first)"
  fi

  if [[ $RUN_TESTS -eq 1 && ${#FAILS[@]} -eq 0 ]]; then
    echo "== 4. Full test suite (about 11 minutes)"
    export DATABASE_URL="postgresql+asyncpg://fundlok:fundlok@localhost:5433/$DB"
    if (cd "$WORK" && "$PY" -m pytest -q -p no:cacheprovider 2>&1 | tail -3); then :; fi
    echo "  (read the summary line above: 'failed' or 'error' means NO-GO)"
  fi
fi

echo
if [[ ${#FAILS[@]} -eq 0 ]]; then
  echo "GO: PR #$PR merges cleanly into the current main with a single migration chain."
  [[ $BEHIND -gt 0 ]] && echo "    (CI's earlier results are from before $BEHIND newer main commit(s); this check covers the migration risk, not the tests: use --tests if unsure.)"
  exit 0
else
  echo "NO-GO: PR #$PR."
  for f in "${FAILS[@]}"; do echo "  - $f"; done
  exit 1
fi
