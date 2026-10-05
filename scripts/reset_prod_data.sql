-- RESET PRODUCTION DATA  --  DESTRUCTIVE, NOT REVERSIBLE.
--
-- Empties every table of user and business data and keeps the schema, so the app
-- starts clean without re-running migrations. What it does NOT touch:
--   * the schema and alembic_version  (the app keeps working, no migration needed)
--   * reference / config tables: see `keep` below
--   * ADMIN and SYSTEM_ADMIN accounts (when :keep_admins = 1; the accounts you sign in with)
--
-- It does NOT clear anything outside Postgres: uploaded documents in the R2
-- bucket and any queued jobs in Redis are left alone and become orphaned.
--
-- HOW TO RUN (psql, against the production database):
--   1. TAKE A BACKUP FIRST and check that it restores:
--        pg_dump "$PROD_DATABASE_URL" -Fc -f fundlok-before-reset.dump
--   2. Run, typing the database name you mean to wipe. The script refuses to
--      continue if it is connected to a different one:
--        psql "$PROD_DATABASE_URL" -v ON_ERROR_STOP=1 \
--             -v confirm_db=<database name> -v keep_admins=1 \
--             -f scripts/reset_prod_data.sql
--      It runs in ONE transaction and ends with a ROLLBACK. Read the counts it
--      prints. To really apply it, re-run with  -v commit=1.
\set ON_ERROR_STOP on
\if :{?keep_admins}
\else
  \set keep_admins 1
\endif
\if :{?commit}
\else
  \set commit 0
\endif

BEGIN;

-- Guard: wrong database, wrong server, wrong terminal tab -> stop here.
DO $$
BEGIN
  IF current_database() <> :'confirm_db' THEN
    RAISE EXCEPTION 'Refusing to run: connected to "%" but confirm_db is "%"',
      current_database(), :'confirm_db';
  END IF;
END $$;

SELECT current_database() AS database, current_user AS db_user,
       (SELECT count(*) FROM users) AS users_before,
       (SELECT count(*) FROM loan_applications) AS applications_before;

-- Tables that survive the reset. Keep this list in step with new reference
-- tables: anything not listed here is emptied.
CREATE TEMP TABLE keep (name text PRIMARY KEY) ON COMMIT DROP;
INSERT INTO keep VALUES
  ('alembic_version'),      -- migration state
  ('bank_rate_config'),     -- reference rate, seeded by a migration
  ('system_settings'),      -- maintenance mode etc.
  ('custodial_accounts'),   -- platform custodial account, seeded by a migration
  ('users');                -- handled below, so admin accounts can stay

-- Empty everything else in one statement (one statement, so tables that
-- reference each other are cleared together and no CASCADE is needed).
DO $$
DECLARE tbls text;
BEGIN
  SELECT string_agg(format('%I.%I', schemaname, tablename), ', ')
    INTO tbls
    FROM pg_tables
   WHERE schemaname = 'public'
     AND tablename NOT IN (SELECT name FROM keep);
  EXECUTE 'TRUNCATE TABLE ' || tbls || ' RESTART IDENTITY';
END $$;

-- Users: drop everyone, or everyone except the admin accounts.
DELETE FROM users
 WHERE :keep_admins = 0
    OR role NOT IN ('ADMIN', 'SYSTEM_ADMIN');

-- Sessions of the admins that stayed were just truncated with refresh_tokens,
-- so they sign in again. That is expected.

SELECT (SELECT count(*) FROM users) AS users_after,
       (SELECT count(*) FROM loan_applications) AS applications_after,
       (SELECT count(*) FROM projects) AS projects_after,
       (SELECT count(*) FROM alembic_version) AS alembic_rows_kept;

\if :commit
  \echo '>>> COMMITTING'
  COMMIT;
\else
  \echo '>>> DRY RUN: rolling back. Re-run with -v commit=1 to apply.'
  ROLLBACK;
\endif
