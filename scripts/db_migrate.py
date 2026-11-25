#!/usr/bin/env python3
"""Copy one PostgreSQL database into another (pg_dump | pg_restore), checked.

Steps:
  1. Pre-flight: client tools present, both databases reachable, pg_dump new
     enough for the source server, source and target not the same database.
  2. Confirm: type the target database name (skip with --yes).
  3. Copy: pg_dump --format=custom | pg_restore --clean --if-exists
     --no-owner --no-privileges, in ONE transaction by default, so a failed
     restore rolls back and leaves the target unchanged.
  4. Verify: exact row count of every table, source vs target.

Usage -- see scripts/README-db-migrate.md for the full guide:
  bun run db:copy                                    # interactive: asks for both URLs
  bun run db:copy --dry-run                          # pre-flight only, changes nothing
  python3 scripts/db_migrate.py --dump-file fl.dump  # dump to a file, then restore it
  bun run db:dump --dump-file fl.dump                # download only: save the source to a file

Without a terminal (CI), pass the URLs via SOURCE_DATABASE_URL / TARGET_DATABASE_URL
(env vars keep passwords out of shell history and `ps`) and --yes.

URLs may be in the app's SQLAlchemy form (postgresql+asyncpg://...?ssl=require);
they are converted to plain libpq URLs. Passwords reach the tools only through
PGPASSWORD and never appear in printed output.

Works on macOS, Linux and Windows (Python 3.9+, standard library only). Needs
PostgreSQL client tools whose major version is >= the source server's. Homebrew,
Postgres.app, Debian, the Windows installer (C:\\Program Files\\PostgreSQL\\N\\bin)
and Scoop installs are found automatically; --pg-bin-dir overrides. On Neon, use
the direct (non "-pooler") host.

Migrating a live database: switch maintenance mode on first so the source
stops changing mid-copy. Paths in MAINTENANCE_ALLOW_PREFIXES (e.g. /auth, /admin)
can still write; anything they write after the dump starts is not copied.
"""

from __future__ import annotations

import argparse
import getpass
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

PREFIX = "[db-migrate]"

# Query params SQLAlchemy/asyncpg accept but libpq rejects with "invalid URI
# query parameter". asyncpg's `ssl` is translated to libpq's `sslmode` instead.
_NON_LIBPQ_PARAMS = frozenset(
    {
        "prepared_statement_cache_size",
        "statement_cache_size",
        "command_timeout",
        "max_cached_statement_lifetime",
        "max_cacheable_statement_size",
    }
)
_ASYNCPG_SSL_TO_SSLMODE = {"true": "require", "1": "require", "false": "disable", "0": "disable"}

# User tables in every non-system schema, skipping extension-owned tables
# (pg_dump does not copy their rows, so they would always look mismatched).
_TABLES_SQL = r"""
SELECT n.nspname, c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\_%'
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend d
      WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e'
  )
ORDER BY 1, 2;
"""


class MigrationError(Exception):
    """A step failed; the message is shown to the user as-is."""


# Shown when a URL cannot be split into its parts. Never include the URL or a
# parser message here: when a symbol in the password broke the URL, both carry
# part of the password.
_UNREADABLE_URL = (
    "invalid database URL: could not read its host, port and database. If the password has "
    "# ? / or other symbols, percent-encode them (# as %23, ? as %3F, / as %2F) or use a "
    "password of letters and digits"
)


@dataclass(frozen=True)
class Database:
    """A libpq connection URL with the password held separately."""

    url: str  # never contains the password -- safe to print and to pass as argv
    password: str | None

    @property
    def identity(self) -> tuple[str, int, str]:
        """(host, port, dbname) -- used to refuse copying a database onto itself."""
        parts = urlsplit(self.url)
        query = dict(parse_qsl(parts.query))
        host = (parts.hostname or query.get("host") or "localhost").lower()
        port = parts.port or int(query.get("port") or 5432)
        return host, port, unquote(parts.path.lstrip("/"))

    @property
    def label(self) -> str:
        host, port, dbname = self.identity
        return f"{host}:{port}/{dbname}"

    @property
    def env(self) -> dict[str, str]:
        env = {**os.environ, "PGAPPNAME": "fundlok-db-migrate"}
        if self.password is not None:
            env["PGPASSWORD"] = self.password
        return env


@dataclass(frozen=True)
class Tools:
    pg_dump: str
    pg_restore: str
    psql: str


@dataclass(frozen=True)
class DatabaseInfo:
    server_major: int
    tables: list[tuple[str, str]]
    alembic_revision: str | None


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #


def parse_database_url(raw: str) -> Database:
    """Turn an app/SQLAlchemy/libpq Postgres URL into a password-free libpq URL."""
    try:
        parts = urlsplit(raw.strip())
    except ValueError:  # e.g. an unclosed IPv6 bracket
        raise MigrationError(_UNREADABLE_URL) from None
    scheme = parts.scheme.split("+", 1)[0]
    if scheme not in ("postgresql", "postgres"):
        raise MigrationError(f"not a PostgreSQL URL (scheme {parts.scheme!r})")
    # A raw # ? or / in the password ends the credentials early: the rest of the
    # password lands in the fragment, query or path, and the user:password part
    # is read as host:port. A Postgres URL has no fragment, and an @ after the
    # host means the split happened inside the credentials.
    if parts.fragment or "@" in parts.path or "@" in parts.query:
        raise MigrationError(_UNREADABLE_URL)

    userinfo, at, hostinfo = parts.netloc.rpartition("@")
    password = None
    if at:
        user, colon, raw_password = userinfo.partition(":")
        if colon:
            password = unquote(raw_password)
        netloc = f"{user}@{hostinfo}" if user else hostinfo
    else:
        netloc = hostinfo

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    ssl = query.pop("ssl", None)
    if ssl is not None and "sslmode" not in query:
        query["sslmode"] = _ASYNCPG_SSL_TO_SSLMODE.get(ssl.lower(), ssl)
    for key in _NON_LIBPQ_PARAMS:
        query.pop(key, None)

    db = Database(
        url=urlunsplit(("postgresql", netloc, parts.path, urlencode(query, safe="/:"), "")),
        password=password,
    )
    try:
        _, _, dbname = db.identity
    except ValueError:  # a non-numeric port; the parser's message would quote it
        raise MigrationError(_UNREADABLE_URL) from None
    if not dbname:
        raise MigrationError("database URL has no database name")
    return db


# --------------------------------------------------------------------------- #
# Client tools
# --------------------------------------------------------------------------- #


def tool_dir_globs() -> list[str]:
    """Where client tools usually live besides PATH. Every match is version-checked."""
    if sys.platform == "win32":
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
        globs = [os.path.join(root, "PostgreSQL", "*", "bin") for root in roots if root]  # EDB installer
        if os.environ.get("USERPROFILE"):  # Scoop
            globs.append(os.path.join(os.environ["USERPROFILE"], "scoop", "apps", "postgresql*", "current", "bin"))
        return globs
    return [
        "/opt/homebrew/opt/postgresql@*/bin",  # Homebrew, Apple silicon
        "/usr/local/opt/postgresql@*/bin",  # Homebrew, Intel
        "/Applications/Postgres.app/Contents/Versions/*/bin",  # Postgres.app
        "/usr/lib/postgresql/*/bin",  # Debian / Ubuntu
    ]


def install_hint(major: int) -> str:
    if sys.platform == "win32":
        return (
            f"Windows: run the PostgreSQL {major} installer from https://www.postgresql.org/download/windows/ "
            "(the 'Command Line Tools' component is enough)"
        )
    if sys.platform == "darwin":
        return f"macOS: brew install postgresql@{major}"
    return f"Debian/Ubuntu: apt install postgresql-client-{major} (from apt.postgresql.org)"


def _tools_in(directory: str) -> Tools | None:
    paths = [shutil.which(name, path=directory) for name in ("pg_dump", "pg_restore", "psql")]
    return Tools(*paths) if all(paths) else None


def discover_tool_installs(bin_dir: str | None) -> list[tuple[int, Tools]]:
    """Client tool installs as (major version, tools), oldest first. With
    --pg-bin-dir only that directory is considered; otherwise PATH first, then
    the usual install locations."""
    if bin_dir:
        directories = [bin_dir]
    else:
        on_path = shutil.which("pg_dump")
        directories = [str(Path(on_path).parent)] if on_path else []
        directories += sorted(d for pattern in tool_dir_globs() for d in glob.glob(pattern))
    installs: dict[int, Tools] = {}
    for directory in directories:
        tools = _tools_in(directory)
        if tools:
            installs.setdefault(tool_major_version(tools.pg_dump), tools)
    if not installs:
        where = f"in {bin_dir}" if bin_dir else "on PATH or in the usual install locations"
        raise MigrationError(
            f"PostgreSQL client tools (pg_dump, pg_restore, psql) not found {where}. "
            f"Install them ({install_hint(16)}) or pass --pg-bin-dir."
        )
    return sorted(installs.items())


def choose_tools(installs: list[tuple[int, Tools]], source_major: int) -> tuple[int, Tools]:
    """The oldest install that can still dump the source: pg_dump must be >= the
    server, and staying close to it keeps newer-only syntax out of the dump."""
    for major, tools in installs:
        if major >= source_major:
            return major, tools
    found = ", ".join(str(major) for major, _ in installs)
    raise MigrationError(
        f"the source runs PostgreSQL {source_major}, but the newest pg_dump found is {installs[-1][0]} "
        f"(found: {found}). Install client tools >= {source_major} ({install_hint(source_major)}); "
        "they are picked up automatically, or pass --pg-bin-dir."
    )


def parse_major_version(version_output: str) -> int:
    """'pg_dump (PostgreSQL) 16.4 (Debian 16.4-1)' -> 16."""
    match = re.search(r"\(PostgreSQL\)\s+(\d+)", version_output)
    if not match:
        raise MigrationError(f"could not read a version from {version_output.strip()!r}")
    return int(match.group(1))


def tool_major_version(path: str) -> int:
    result = subprocess.run([path, "--version"], capture_output=True, text=True, check=False)
    return parse_major_version(result.stdout)


def run_sql(tools: Tools, db: Database, sql: str) -> list[list[str]]:
    """Run SQL with psql and return tab-separated rows (SQL goes in via stdin)."""
    result = subprocess.run(
        [
            tools.psql,
            "--no-psqlrc",
            "--no-align",
            "--tuples-only",
            "--quiet",
            "--field-separator=\t",
            "--set=ON_ERROR_STOP=1",
            f"--dbname={db.url}",
        ],
        input=sql,
        env=db.env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MigrationError(f"query on {db.label} failed: {result.stderr.strip()}")
    return [line.split("\t") for line in result.stdout.splitlines() if line]


# --------------------------------------------------------------------------- #
# Inspection and verification
# --------------------------------------------------------------------------- #


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def inspect_database(tools: Tools, db: Database) -> DatabaseInfo:
    (server_num,), *_ = run_sql(tools, db, "SELECT current_setting('server_version_num');")
    tables = [(schema, name) for schema, name in run_sql(tools, db, _TABLES_SQL)]
    revision = None
    if ("public", "alembic_version") in tables:
        rows = run_sql(tools, db, "SELECT string_agg(version_num, ',') FROM public.alembic_version;")
        revision = rows[0][0] if rows else None
    return DatabaseInfo(server_major=int(server_num) // 10000, tables=tables, alembic_revision=revision)


def row_counts(tools: Tools, db: Database) -> dict[str, int]:
    tables = run_sql(tools, db, _TABLES_SQL)
    if not tables:
        return {}
    sql = "\nUNION ALL\n".join(
        f"SELECT {quote_literal(f'{schema}.{name}')}, count(*) FROM {quote_ident(schema)}.{quote_ident(name)}"
        for schema, name in tables
    )
    return {name: int(count) for name, count in run_sql(tools, db, sql + ";")}


def compare_counts(source: dict[str, int], target: dict[str, int]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Tables only on the target are a warning: they
    were there before and are not part of the copy (--clean leaves them alone)."""
    errors, warnings = [], []
    for name in sorted(source.keys() | target.keys()):
        if name not in target:
            errors.append(f"{name}: missing on target (source has {source[name]:,} rows)")
        elif name not in source:
            warnings.append(f"{name}: only on target ({target[name]:,} rows), not part of the copy")
        elif source[name] != target[name]:
            errors.append(f"{name}: source {source[name]:,} rows, target {target[name]:,} rows")
    return errors, warnings


# --------------------------------------------------------------------------- #
# Copy
# --------------------------------------------------------------------------- #


def dump_command(tools: Tools, source: Database, *, verbose: bool) -> list[str]:
    cmd = [tools.pg_dump, "--format=custom", "--no-owner", "--no-privileges", f"--dbname={source.url}"]
    return cmd + ["--verbose"] if verbose else cmd


def restore_command(
    tools: Tools,
    target: Database,
    *,
    single_transaction: bool,
    verbose: bool,
    dump_file: Path | None = None,
) -> list[str]:
    cmd = [tools.pg_restore, "--clean", "--if-exists", "--no-owner", "--no-privileges", f"--dbname={target.url}"]
    if single_transaction:
        cmd.append("--single-transaction")  # implies --exit-on-error
    if verbose:
        cmd.append("--verbose")
    if dump_file is not None:
        cmd.append(str(dump_file))
    return cmd


def stream_copy(dump_cmd: list[str], restore_cmd: list[str], source: Database, target: Database) -> tuple[int, int]:
    dump = subprocess.Popen(dump_cmd, env=source.env, stdout=subprocess.PIPE)
    try:
        restore = subprocess.Popen(restore_cmd, env=target.env, stdin=dump.stdout)
    except BaseException:
        dump.kill()
        dump.wait()
        raise
    dump.stdout.close()  # pg_restore owns the read end; pg_dump gets SIGPIPE if it dies
    restore_rc = restore.wait()
    return dump.wait(), restore_rc


def dump_to_file(dump_cmd: list[str], source: Database, path: Path) -> None:
    try:
        # O_BINARY: no newline translation on Windows (0 elsewhere). 0o600 is owner-only on POSIX.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        raise MigrationError(f"{path} already exists; choose a new --dump-file path") from None
    with os.fdopen(fd, "wb") as fh:
        rc = subprocess.run(dump_cmd, env=source.env, stdout=fh, check=False).returncode
    if rc != 0:
        path.unlink(missing_ok=True)
        raise MigrationError(f"pg_dump failed (exit {rc}); the target was not touched")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def log(message: str) -> None:
    print(f"{PREFIX} {message}", flush=True)


def warn(message: str) -> None:
    print(f"{PREFIX} WARNING: {message}", file=sys.stderr, flush=True)


def is_interactive() -> bool:
    return sys.stdin.isatty()


def greet(dump_only: bool = False) -> None:
    if dump_only:
        print(
            "\n👋 Hello! This downloads one PostgreSQL database into a file.\n\n"
            "   1. Checks the database and picks a matching pg_dump\n"
            "   2. Saves everything to the file (readable only by you)\n"
            "   3. Checks the file can be read back\n\n"
            "   The database is only read. Ctrl+C quits.\n",
            flush=True,
        )
        return
    print(
        "\n👋 Hello! This copies one PostgreSQL database into another.\n\n"
        "   1. Checks both databases and picks matching pg_dump/pg_restore tools\n"
        "   2. Shows what it found and asks you to confirm before touching the target\n"
        "   3. Copies everything in one transaction, then compares row counts\n\n"
        "   Nothing changes until you type the target database name. Ctrl+C quits.\n",
        flush=True,
    )


def prompt_database(role: str, purpose: str) -> Database:
    """Ask for a URL without echoing it (it usually contains a password), then
    show the parsed, password-free result so the user can check it."""
    print(f"{role} database: {purpose}")
    while True:
        raw = getpass.getpass("  Paste its URL (input hidden): ").strip()
        if not raw:
            continue
        try:
            db = parse_database_url(raw)
        except MigrationError as exc:
            print(f"  ✗ {exc}. Try again.")
            continue
        user = urlsplit(db.url).username
        if db.password is None and user:
            password = getpass.getpass(f"  Password for {user} (Enter for none / ~/.pgpass): ")
            if password:
                db = Database(url=db.url, password=password)
        print(f"  ✓ {user + '@' if user else ''}{db.label}\n")
        return db


def resolve_database(raw: str | None, role: str, purpose: str) -> Database:
    return parse_database_url(raw) if raw else prompt_database(role, purpose)


def confirm(target: Database) -> None:
    _, _, dbname = target.identity
    if not is_interactive():
        raise MigrationError(
            "refusing to overwrite the target without confirmation; pass --yes to run non-interactively"
        )
    answer = input(
        f"{PREFIX} This DROPS and recreates every object from the dump in {target.label}.\n"
        f"{PREFIX} Type the target database name ({dbname}) to continue: "
    )
    if answer.strip() != dbname:
        raise MigrationError("confirmation did not match; nothing was changed")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="db_migrate.py",
        description="Copy one PostgreSQL database into another with pg_dump | pg_restore, then verify row counts.",
        epilog="Run with no URLs in a terminal and it asks for them. Prefer the prompt or the env vars over "
        "--source/--target: URLs on the command line end up in shell history.",
    )
    parser.add_argument(
        "--source",
        default=os.environ.get("SOURCE_DATABASE_URL"),
        help="source URL (default: $SOURCE_DATABASE_URL, else asked)",
    )
    parser.add_argument(
        "--target",
        default=os.environ.get("TARGET_DATABASE_URL"),
        help="target URL (default: $TARGET_DATABASE_URL, else asked)",
    )
    parser.add_argument(
        "--dump-file", type=Path, help="dump to this new file (mode 600) and restore from it, instead of streaming"
    )
    parser.add_argument(
        "--dump-only",
        action="store_true",
        help="only download the source into --dump-file; no target, nothing is restored",
    )
    parser.add_argument(
        "--pg-bin-dir",
        default=os.environ.get("PG_BIN_DIR"),
        help="directory with pg_dump/pg_restore/psql (default: $PG_BIN_DIR, else found automatically)",
    )
    parser.add_argument(
        "--allow-errors",
        action="store_true",
        help="restore without --single-transaction and skip objects that fail (e.g. extension ownership on managed hosts); row counts are still verified",
    )
    parser.add_argument("--skip-verify", action="store_true", help="skip the row-count comparison after the copy")
    parser.add_argument(
        "--dry-run", action="store_true", help="run the pre-flight checks and print the commands; change nothing"
    )
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("-v", "--verbose", action="store_true", help="pass --verbose to pg_dump and pg_restore")
    args = parser.parse_args(argv)
    if args.dump_only:
        if not args.dump_file:
            parser.error("--dump-only needs --dump-file PATH: the file to save the download in")
        if not args.source and not is_interactive():
            parser.error("--source (or SOURCE_DATABASE_URL) is required when not running in a terminal")
        return args
    if (not args.source or not args.target) and not is_interactive():
        parser.error(
            "--source and --target (or SOURCE_DATABASE_URL / TARGET_DATABASE_URL) are required "
            "when not running in a terminal"
        )
    return args


def dump_only(args: argparse.Namespace) -> int:
    """Download the source into a new file and check it reads back. No target."""
    source = resolve_database(args.source, "Source", "the database to download (it is only read)")
    if args.dump_file.exists():
        raise MigrationError(f"{args.dump_file} already exists; choose a new --dump-file path")

    installs = discover_tool_installs(args.pg_bin_dir)
    _, inspector = installs[-1]
    log("Pre-flight checks")
    src = inspect_database(inspector, source)
    log(
        f"  source: {source.label}  (PostgreSQL {src.server_major}, {len(src.tables)} tables, alembic {src.alembic_revision or '-'})"
    )
    tools_major, tools = choose_tools(installs, src.server_major)
    log(f"  tools:  PostgreSQL {tools_major} client  ({Path(tools.pg_dump).parent})")
    if "-pooler." in source.identity[0]:
        warn(f"{source.label} looks like a Neon pooled endpoint; pg_dump needs the direct host (drop '-pooler')")
    if not src.tables:
        warn("source has no tables; the file will be empty")

    dump_cmd = dump_command(tools, source, verbose=args.verbose)
    if args.dry_run:
        log(f"Would run: {shlex.join(dump_cmd)} > {shlex.quote(str(args.dump_file))}")
        log("Dry run: nothing was saved.")
        return 0

    log(f"Downloading {source.label} to {args.dump_file} ...")
    dump_to_file(dump_cmd, source, args.dump_file)
    listing = subprocess.run(
        [tools.pg_restore, "--list", str(args.dump_file)], capture_output=True, text=True, check=False
    )
    if listing.returncode != 0:
        raise MigrationError(f"the file was saved but pg_restore cannot read it (exit {listing.returncode})")
    data_entries = sum(" TABLE DATA " in line for line in listing.stdout.splitlines())
    size_mb = args.dump_file.stat().st_size / (1024 * 1024)
    log(f"Saved {args.dump_file} ({size_mb:,.1f} MB, data for {data_entries} tables). It reads back cleanly.")
    log("It contains the full database, including personal and financial data: keep it private and delete it when done.")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.dump_only:
        return dump_only(args)
    source = resolve_database(args.source, "Source", "the database to copy FROM (it is only read)")
    target = resolve_database(args.target, "Target", "the database to copy INTO (its objects get replaced)")
    if source.identity == target.identity:
        raise MigrationError(f"source and target are the same database ({source.label})")
    if args.dump_file and args.dump_file.exists():
        raise MigrationError(f"{args.dump_file} already exists; choose a new --dump-file path")

    installs = discover_tool_installs(args.pg_bin_dir)
    _, inspector = installs[-1]  # the newest psql can read any server

    log("Pre-flight checks")
    src = inspect_database(inspector, source)
    dst = inspect_database(inspector, target)
    log(
        f"  source: {source.label}  (PostgreSQL {src.server_major}, {len(src.tables)} tables, alembic {src.alembic_revision or '-'})"
    )
    log(
        f"  target: {target.label}  (PostgreSQL {dst.server_major}, {len(dst.tables)} tables, alembic {dst.alembic_revision or '-'})"
    )
    tools_major, tools = choose_tools(installs, src.server_major)
    log(f"  tools:  PostgreSQL {tools_major} client  ({Path(tools.pg_dump).parent})")

    if dst.server_major < src.server_major:
        warn(
            f"target runs PostgreSQL {dst.server_major}, older than the source ({src.server_major}); the restore may fail"
        )
    for db in (source, target):
        if "-pooler." in db.identity[0]:
            warn(
                f"{db.label} looks like a Neon pooled endpoint; pg_dump/pg_restore need the direct host (drop '-pooler')"
            )
    if dst.tables:
        warn(
            f"target already has {len(dst.tables)} tables; objects in the dump are dropped and recreated, others are left as they are"
        )
    if not src.tables:
        warn("source has no tables; the copy will be empty")

    dump_cmd = dump_command(tools, source, verbose=args.verbose)
    restore_cmd = restore_command(
        tools,
        target,
        single_transaction=not args.allow_errors,
        verbose=args.verbose,
        dump_file=args.dump_file,
    )

    if args.dry_run:
        if args.dump_file:
            log(f"Would run: {shlex.join(dump_cmd)} > {shlex.quote(str(args.dump_file))}")
            log(f"Then:      {shlex.join(restore_cmd)}")
        else:
            log(f"Would run: {shlex.join(dump_cmd)} | {shlex.join(restore_cmd)}")
        log("Dry run: nothing was changed.")
        return 0

    if not args.yes:
        confirm(target)

    if args.dump_file:
        log(f"Dumping {source.label} to {args.dump_file} ...")
        dump_to_file(dump_cmd, source, args.dump_file)
        log(f"Restoring {args.dump_file} into {target.label} ...")
        dump_rc, restore_rc = 0, subprocess.run(restore_cmd, env=target.env, check=False).returncode
    else:
        log(f"Copying {source.label} -> {target.label} ...")
        dump_rc, restore_rc = stream_copy(dump_cmd, restore_cmd, source, target)

    atomic = "it ran in one transaction, so the target was rolled back and is unchanged"
    if dump_rc != 0:
        raise MigrationError(f"pg_dump failed (exit {dump_rc})" + ("" if args.allow_errors else f"; {atomic}"))
    if restore_rc != 0:
        if not args.allow_errors:
            raise MigrationError(
                f"pg_restore failed (exit {restore_rc}); {atomic}. If the error above is about extension "
                "ownership or permissions on a managed host, re-run with --allow-errors."
            )
        warn("pg_restore skipped objects that failed (see above); checking row counts")

    if args.skip_verify:
        log("Copy finished (verification skipped).")
    else:
        log("Verifying row counts ...")
        source_counts = row_counts(tools, source)
        errors, warnings = compare_counts(source_counts, row_counts(tools, target))
        for line in warnings:
            warn(line)
        if errors:
            for line in errors:
                print(f"{PREFIX}   MISMATCH {line}", file=sys.stderr)
            raise MigrationError(
                f"{len(errors)} table(s) differ. If the source was still taking writes, switch "
                "maintenance mode on and run the migration again."
            )
        log(f"Verified {len(source_counts)} tables, {sum(source_counts.values()):,} rows: source and target match.")

    if args.dump_file:
        log(f"Dump kept at {args.dump_file}. It contains the full database, so delete it when you no longer need it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Older Windows console code pages can't encode 👋 ✓ ✗; print "?" instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = parse_args(argv)
    if is_interactive():
        greet(dump_only=args.dump_only)
    try:
        return run(args)
    except MigrationError as exc:
        print(f"{PREFIX} ERROR: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(f"\n{PREFIX} Stopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
