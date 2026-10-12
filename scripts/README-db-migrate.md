# `db:copy`: copy one Postgres database into another

`scripts/db_migrate.py` copies a whole PostgreSQL database onto another server, for example local → Neon, Cloud SQL → Neon, or old Neon project → new one. It wraps `pg_dump | pg_restore` and adds checks before and after, so a bad copy fails loudly instead of quietly.

```bash
bun run db:copy
```

> Not the same as `bun run migrate`. That one runs Alembic **schema** migrations (`alembic upgrade head`). `db:copy` moves **data and schema** from one database server to another.

---

## What it does

```
👋 Hello! This copies one PostgreSQL database into another.

   1. Checks both databases and picks matching pg_dump/pg_restore tools
   2. Shows what it found and asks you to confirm before touching the target
   3. Copies everything in one transaction, then compares row counts
```

1. **Asks for the two databases.** Paste each connection URL. Input is hidden because URLs usually contain a password. It then shows what it understood (`user@host:port/db`, never the password). If the URL has no password, it asks for one.
2. **Pre-flight checks:**
   - Both databases are reachable.
   - The source is not the same database as the target.
   - The client tools are new enough for the source server.
   - It shows the PostgreSQL version, table count and Alembic revision on each side.
3. **Asks for confirmation.** You type the target database name. Nothing changes before this.
4. **Copies the database:**
   - It runs `pg_dump --format=custom | pg_restore --clean --if-exists --no-owner --no-privileges`.
   - The restore runs in **one transaction**. If anything fails, the target is rolled back and left exactly as it was.
5. **Verifies.** It compares the exact row count of every table, source vs target, and fails if any differ.

---

## Before you start

Works on **macOS, Linux and Windows**. It needs [Bun](https://bun.sh), **Python 3.9+** (standard library only; the project `.venv` is used if present) and the PostgreSQL client tools.

**1. Install client tools that match your server.** `pg_dump` must be the same major version as the source server or newer. The local Docker DB runs **16**. For hosted ones, check with `SHOW server_version;`, or just run `bun run db:copy --dry-run`, which tells you what it needs.

| OS | Install | Found automatically in |
|---|---|---|
| macOS | `brew install postgresql@16` | `/opt/homebrew/opt/postgresql@*`, `/usr/local/opt/postgresql@*`, Postgres.app |
| Windows | Installer from [postgresql.org/download/windows](https://www.postgresql.org/download/windows/). Choosing just the **Command Line Tools** component is enough. | `C:\Program Files\PostgreSQL\<version>\bin`, Scoop |
| Debian / Ubuntu | `apt install postgresql-client-16` (add the [apt.postgresql.org](https://wiki.postgresql.org/wiki/Apt) repo for versions newer than your distro's) | `/usr/lib/postgresql/*/bin` |

You don't need to put the tools on your PATH. The tool searches those locations, version-checks every install it finds, and picks the oldest one that can dump the source. Use `--pg-bin-dir` to force a specific install, e.g. `--pg-bin-dir "C:\Program Files\PostgreSQL\16\bin"`.

**2. Stop writes to the source.** Turn on maintenance mode (admin → System, system admins only) before copying a live database. Rows written after the copy starts are not copied, and the row-count check would flag them. The Didit KYC webhook (`/kyc/webhook`) is still allowed through during maintenance, so avoid copying while a KYC verification is in progress.

**3. Get the right connection URLs:**

| Host | Use |
|---|---|
| **Neon** | The **direct** connection string, not the pooled one (no `-pooler` in the host). `pg_dump` doesn't work through the pooler. The tool warns you if it sees `-pooler`. |
| **Cloud SQL (staging)** | Through `cloud-sql-proxy` (`postgresql://user:pass@127.0.0.1:5432/db`), or a socket URL `postgresql://user:pass@/db?host=/cloudsql/PROJECT:REGION:INSTANCE`. |
| **Local Docker** | `postgresql://fundlok:fundlok@localhost:5433/fundlok_dev` |

App-style URLs work as-is: `postgresql+asyncpg://…?ssl=require` and `postgresql+psycopg2://…` are converted automatically. See [Which URLs work](#which-urls-work) for the full list.

**4. The target database must already exist.** Create an empty one first (Neon: new database or new branch; locally: `CREATE DATABASE …`). The tool fills it but does not create it.

---

## Which URLs work

Almost any PostgreSQL connection URL:

| URL | Works? |
|---|---|
| Neon, including the app's `postgresql+asyncpg://…?ssl=require` form | ✅ |
| Supabase (direct), AWS RDS / Aurora (incl. `sslmode=verify-full&sslrootcert=…`), Heroku `postgres://`, Railway, Render | ✅ |
| Any `postgresql+<driver>://` (asyncpg, psycopg2, psycopg) | ✅ the driver part is dropped |
| Cloud SQL unix socket (`postgresql://u:p@/db?host=/cloudsql/…`), IPv6 hosts (`[::1]`) | ✅ |
| JDBC: `jdbc:postgresql://…` | ❌ delete the `jdbc:` prefix |
| Key-value string: `host=… dbname=… user=…` | ❌ rewrite as `postgresql://user:pass@host:port/dbname` |
| Multi-host failover: `postgresql://u:p@h1:5432,h2:5432/db` | ❌ use a single host (the primary) |
| No database name: `postgresql://u:p@host:5432` | ❌ add `/dbname` |

A URL the tool can't use is rejected straight away with a clear message, before anything connects.

**A valid URL is not the whole story.** A copy can still fail for reasons outside the URL. `--dry-run` catches most of these:

- **Poolers in transaction mode** (Neon `-pooler`, Supabase port 6543, PgBouncer) don't work with `pg_dump`. Use the direct or session connection. The tool warns about Neon's.
- **Permissions:** the source user must be able to read every table, and the target user must be able to create and drop objects in the target database. Managed hosts sometimes block extension changes; see `--allow-errors`.
- **Network:** your machine must reach both servers. RDS and Cloud SQL usually need your IP allow-listed or `cloud-sql-proxy`.
- **Extensions:** the target server must offer the extensions the source uses. FundLok only uses `pgcrypto`, which every host has.
- **Tool version:** client tools must be at least the source's major version.
- **Real PostgreSQL only.** Postgres-compatible products with their own engines (CockroachDB, Redshift) can't be copied with `pg_dump`.

**What gets copied:** one database, with all schemas inside it, as a snapshot of the moment the copy starts. Not copied: users/roles, other databases on the same server, or anything written after the copy starts. It is a one-off copy, not ongoing sync.

---

## Usage

### Interactive (recommended)

```bash
bun run db:copy
```

It asks for the source URL, the target URL, and then the confirmation.

### Check first, change nothing (`--dry-run`)

```bash
bun run db:copy --dry-run
```

A rehearsal: it runs every safe step and stops before anything could change.

| Does | Doesn't |
|---|---|
| Connects to both databases (read-only) | Ask for confirmation |
| Checks your `pg_dump` is new enough for the source | Copy anything |
| Shows the PostgreSQL version, table count and Alembic revision on each side | Write to either database |
| Prints warnings (e.g. "target already has 44 tables", "Neon pooler host") | |
| Prints the exact `pg_dump` / `pg_restore` commands, with passwords removed | |

Use it to check that a URL works, that your client tools are right, or what's already in the target, before the real run. It's safe to run any time.

### Non-interactive (scripts / CI)

Provide the URLs as environment variables (not as flags, which end up in shell history) and skip the confirmation with `--yes`:

```bash
SOURCE_DATABASE_URL='postgresql://…' TARGET_DATABASE_URL='postgresql://…' bun run db:copy --yes
```

On Windows (PowerShell):

```powershell
$env:SOURCE_DATABASE_URL = 'postgresql://…'; $env:TARGET_DATABASE_URL = 'postgresql://…'; bun run db:copy --yes
```

Without a terminal and without both URLs, it exits with an error instead of prompting.

### Options

| Flag | What it does |
|---|---|
| `--dry-run` | Pre-flight checks only; prints the commands, changes nothing |
| `--dump-file PATH` | Dump to a file first, then restore from it. Use this if the network drops mid-copy, or to keep a backup. The file is never overwritten, and on macOS/Linux it is readable only by you (see Safety notes for Windows). |
| `--pg-bin-dir DIR` | Use the client tools in `DIR` (also `$PG_BIN_DIR`) |
| `--allow-errors` | Restore without the single transaction: objects that fail are skipped and the rest continues. Row counts are still checked. See Troubleshooting. |
| `--skip-verify` | Skip the row-count comparison |
| `-y`, `--yes` | Don't ask for confirmation |
| `-v`, `--verbose` | Show `pg_dump` / `pg_restore` progress |
| `--source URL` / `--target URL` | Instead of the prompt or env vars (avoid: shell history) |

Extra flags pass through bun: `bun run db:copy --dry-run --verbose`.

---

## What happens to the target

- Every table, index, constraint, trigger and so on **that exists in the source** is dropped on the target and recreated from the dump.
- Objects that exist **only** on the target are left alone. The row-count check lists them as warnings ("only on target").
- Ownership and grants are not copied (`--no-owner --no-privileges`). Everything ends up owned by the user in the target URL.
- `alembic_version` is copied too, so the target ends at the same migration revision as the source. After switching the app's `DATABASE_URL`, `alembic upgrade head` on boot is a no-op.
- The ledger's append-only triggers are copied as well. They don't block the copy, because they guard `UPDATE`/`DELETE` and the restore only drops and inserts.

---

## Troubleshooting

| Message | Fix |
|---|---|
| `the source runs PostgreSQL 16, but the newest pg_dump found is 14` | Install the client tools for the version in the message (the message includes the right command for your OS), then run again |
| `PostgreSQL client tools … not found` | Install them (see *Before you start*) or pass `--pg-bin-dir` |
| `Python 3 not found` | Install Python 3.9+ from [python.org](https://www.python.org/downloads/). On Windows, keep the `py` launcher option ticked in the installer. |
| Windows, **Git Bash**: `required when not running in a terminal`, or the URL you paste is visible | Git Bash's terminal (mintty) isn't a real Windows console, so prompts can't hide input. Run it from **PowerShell** or **Windows Terminal**, or use `winpty bun run db:copy`, or pass the URLs as env vars with `--yes`. |
| `source and target are the same database` | Check the URLs; the tool won't copy a database onto itself |
| `not a PostgreSQL URL (scheme 'jdbc')` / `(scheme '')` | Remove the `jdbc:` prefix, or turn a `host=… dbname=…` string into `postgresql://user:pass@host:port/dbname` |
| `invalid database URL: Port could not be cast …` | Multi-host or malformed port; use a single `host:port` |
| `database URL has no database name` | Add `/dbname` after the host |
| `pg_restore failed … must be owner of extension …` (or another permission error) | Common on managed Postgres. Re-run with `--allow-errors`; the skipped objects are listed, and the row counts still have to match. |
| `N table(s) differ` | The source was probably still being written to. Turn on maintenance mode and run again. |
| `looks like a Neon pooled endpoint` | Use Neon's direct connection string (no `-pooler`) |
| `refusing to overwrite the target without confirmation` | You're not in a terminal; add `--yes` |
| `… already exists; choose a new --dump-file path` | Pick a new file name; existing dumps are never overwritten |

---

## Safety notes

- **Passwords** reach `pg_dump`/`pg_restore`/`psql` only through `PGPASSWORD`. They never appear in a command line (visible in `ps`) or in anything printed.
- **Dump files contain the whole database**, including personal and financial data. Keep them off shared drives and delete them when done (`rm fl.dump`). On macOS/Linux the file is created readable only by you. On Windows it takes the folder's permissions, so write it inside your user folder, not a shared one.
- **A failed copy changes nothing on the target** (single transaction), unless you used `--allow-errors`.
- The source is only read.

---

## Tests

Unit tests (URL parsing, password handling, prompts, tool discovery, row-count comparison):

```bash
.venv/bin/pytest tests/tooling -q
```

The full copy was tested on macOS against local Docker Postgres 16 in these cases:
- empty target;
- re-copy onto a populated target;
- `--dump-file`;
- an interactive run;
- a copy to a second server on another port, checked by row checksums and a schema diff;
- a deliberately broken restore, which confirmed the rollback.

It was also run with Python 3.9. The Windows-specific parts (install locations, install hints, the `py`/`python` launcher fallback) are covered by unit tests that simulate Windows, but **have not yet been run on a real Windows machine**. If you're the first to try, run `bun run db:copy --dry-run` first.
