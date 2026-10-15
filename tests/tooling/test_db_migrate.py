"""Unit tests for scripts/db_migrate.py -- URL handling, password isolation,
prompts, tool discovery, verification and command flags. The pg_dump/pg_restore
round trip itself needs client tools matching the server and is exercised
manually; see scripts/README-db-migrate.md."""

from pathlib import Path

import pytest

import scripts.db_migrate as db_migrate
from scripts.db_migrate import (
    Database,
    MigrationError,
    Tools,
    choose_tools,
    compare_counts,
    discover_tool_installs,
    dump_command,
    main,
    parse_database_url,
    parse_major_version,
    prompt_database,
    quote_ident,
    restore_command,
)

TOOLS = Tools(pg_dump="/bin/pg_dump", pg_restore="/bin/pg_restore", psql="/bin/psql")


@pytest.fixture(autouse=True)
def _not_a_terminal(monkeypatch):
    """Behave as in CI unless a test says otherwise: no greeting, no prompts."""
    monkeypatch.setattr(db_migrate, "is_interactive", lambda: False)


def _fake_install(directory: Path, major: int) -> Path:
    directory.mkdir(parents=True)
    for name in ("pg_dump", "pg_restore", "psql"):
        tool = directory / name
        tool.write_text(f'#!/bin/sh\necho "{name} (PostgreSQL) {major}.2"\n')
        tool.chmod(0o755)
    return directory


def test_parse_url_strips_sqlalchemy_driver_and_password():
    db = parse_database_url("postgresql+asyncpg://fundlok:s3cret@localhost:5433/fundlok_dev")
    assert db.url == "postgresql://fundlok@localhost:5433/fundlok_dev"
    assert db.password == "s3cret"


def test_parse_url_accepts_postgres_scheme_and_psycopg2_driver():
    assert parse_database_url("postgres://u:p@h/db").url == "postgresql://u@h/db"
    assert parse_database_url("postgresql+psycopg2://u:p@h/db").url == "postgresql://u@h/db"


def test_parse_url_decodes_percent_encoded_password_but_keeps_username_encoding():
    db = parse_database_url("postgresql://us%40er:p%40ss%3Aword@h:5432/db")
    assert db.password == "p@ss:word"
    assert db.url.startswith("postgresql://us%40er@h:5432/")


def test_parse_url_without_password_sets_no_pgpassword(monkeypatch):
    monkeypatch.delenv("PGPASSWORD", raising=False)
    db = parse_database_url("postgresql://u@h/db")
    assert db.password is None
    assert "PGPASSWORD" not in db.env


def test_parse_url_translates_asyncpg_ssl_to_sslmode():
    db = parse_database_url("postgresql+asyncpg://u:p@ep-x.neon.tech/db?ssl=require")
    assert db.url == "postgresql://u@ep-x.neon.tech/db?sslmode=require"
    assert "sslmode=require" in parse_database_url("postgresql://u:p@h/db?ssl=true").url


def test_parse_url_explicit_sslmode_wins_over_ssl():
    db = parse_database_url("postgresql://u:p@h/db?ssl=disable&sslmode=verify-full")
    assert db.url == "postgresql://u@h/db?sslmode=verify-full"


def test_parse_url_drops_asyncpg_only_params():
    db = parse_database_url("postgresql+asyncpg://u:p@h/db?prepared_statement_cache_size=0&sslmode=require")
    assert db.url == "postgresql://u@h/db?sslmode=require"


def test_parse_url_keeps_cloud_sql_socket_host():
    db = parse_database_url("postgresql://u:p@/fundlok?host=/cloudsql/fundlok:asia-southeast1:fundlok")
    assert db.url == "postgresql://u@/fundlok?host=/cloudsql/fundlok:asia-southeast1:fundlok"
    assert db.identity == ("/cloudsql/fundlok:asia-southeast1:fundlok", 5432, "fundlok")


@pytest.mark.parametrize(
    "raw", ["mysql://u:p@h/db", "postgresql://u:p@h", "postgresql://u:p@h/", "postgresql://u:p@h:abc/db"]
)
def test_parse_url_rejects_invalid(raw):
    with pytest.raises(MigrationError):
        parse_database_url(raw)


def test_identity_treats_spellings_of_one_database_as_equal():
    a = parse_database_url("postgresql+asyncpg://u:p@LocalHost/db?ssl=require")
    b = parse_database_url("postgres://other:x@localhost:5432/db")
    assert a.identity == b.identity


def test_password_reaches_tools_only_via_env():
    db = parse_database_url("postgresql://u:topsecret@h/db")
    commands = dump_command(TOOLS, db, verbose=True) + restore_command(TOOLS, db, single_transaction=True, verbose=True)
    assert all("topsecret" not in part for part in commands)
    assert db.env["PGPASSWORD"] == "topsecret"
    assert "topsecret" not in db.label


def test_restore_is_single_transaction_unless_errors_allowed():
    db = Database(url="postgresql://u@h/db", password=None)
    assert "--single-transaction" in restore_command(TOOLS, db, single_transaction=True, verbose=False)
    assert "--single-transaction" not in restore_command(TOOLS, db, single_transaction=False, verbose=False)


def test_restore_uses_clean_if_exists_no_owner_no_privileges_and_dump_file():
    db = Database(url="postgresql://u@h/db", password=None)
    cmd = restore_command(TOOLS, db, single_transaction=True, verbose=False, dump_file=Path("fl.dump"))
    assert {"--clean", "--if-exists", "--no-owner", "--no-privileges"} <= set(cmd)
    assert cmd[-1] == "fl.dump"


def test_dump_uses_custom_format():
    db = Database(url="postgresql://u@h/db", password=None)
    assert "--format=custom" in dump_command(TOOLS, db, verbose=False)


@pytest.mark.parametrize(
    ("output", "major"),
    [
        ("pg_dump (PostgreSQL) 14.20 (Homebrew)\n", 14),
        ("pg_restore (PostgreSQL) 16.4 (Debian 16.4-1.pgdg120+1)\n", 16),
        ("psql (PostgreSQL) 17.0\n", 17),
    ],
)
def test_parse_major_version(output, major):
    assert parse_major_version(output) == major


def test_parse_major_version_rejects_garbage():
    with pytest.raises(MigrationError):
        parse_major_version("command not found")


def test_compare_counts_matching():
    assert compare_counts({"public.users": 3}, {"public.users": 3}) == ([], [])


def test_compare_counts_reports_mismatch_and_missing_as_errors_extra_as_warning():
    errors, warnings = compare_counts(
        {"public.users": 3, "public.ledger_entries": 10},
        {"public.users": 2, "public.leftover": 1},
    )
    assert errors == [
        "public.ledger_entries: missing on target (source has 10 rows)",
        "public.users: source 3 rows, target 2 rows",
    ]
    assert warnings == ["public.leftover: only on target (1 rows), not part of the copy"]


def test_quote_ident_escapes_double_quotes():
    assert quote_ident('we"ird') == '"we""ird"'


def test_main_refuses_same_database_before_touching_anything(capsys):
    rc = main(["--source", "postgresql://u:p@h/db", "--target", "postgresql+asyncpg://u:q@h:5432/db", "--yes"])
    assert rc == 1
    assert "same database" in capsys.readouterr().err


def test_main_requires_urls_when_not_in_a_terminal(monkeypatch):
    monkeypatch.delenv("SOURCE_DATABASE_URL", raising=False)
    monkeypatch.delenv("TARGET_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--target", "postgresql://u@h/db"])
    assert exc.value.code == 2


def test_prompt_database_retries_until_valid_and_asks_for_missing_password(monkeypatch, capsys):
    answers = iter(["", "mysql://u@h/db", "postgresql+asyncpg://fundlok@localhost:5433/fundlok_dev", "s3cret"])
    monkeypatch.setattr(db_migrate.getpass, "getpass", lambda prompt="": next(answers))
    db = prompt_database("Source", "copy FROM")
    assert db.url == "postgresql://fundlok@localhost:5433/fundlok_dev"
    assert db.password == "s3cret"
    out = capsys.readouterr().out
    assert "Try again" in out
    assert "fundlok@localhost:5433/fundlok_dev" in out
    assert "s3cret" not in out


def test_prompt_database_does_not_ask_for_password_already_in_url(monkeypatch):
    answers = iter(["postgresql://u:inline@h/db"])
    monkeypatch.setattr(db_migrate.getpass, "getpass", lambda prompt="": next(answers))
    assert prompt_database("Target", "copy INTO").password == "inline"


def test_choose_tools_picks_oldest_install_new_enough_for_source():
    t14, t16, t17 = (Tools(f"/{v}/pg_dump", f"/{v}/pg_restore", f"/{v}/psql") for v in (14, 16, 17))
    assert choose_tools([(14, t14), (16, t16), (17, t17)], 16) == (16, t16)
    assert choose_tools([(14, t14), (17, t17)], 16) == (17, t17)


def test_choose_tools_explains_how_to_fix_when_none_is_new_enough():
    with pytest.raises(MigrationError, match="Install client tools >= 16") as exc:
        choose_tools([(14, TOOLS)], 16)
    # The install command depends on the OS running the test (brew / apt / Windows
    # installer), so compare with whatever this platform's hint is, not a fixed one.
    assert db_migrate.install_hint(16) in str(exc.value)


def test_discover_tool_installs_reads_versions_from_bin_dir(tmp_path):
    bin_dir = _fake_install(tmp_path / "pg16" / "bin", 16)
    [(major, tools)] = discover_tool_installs(str(bin_dir))
    assert major == 16
    assert tools.pg_dump == str(bin_dir / "pg_dump")


def test_discovery_finds_installs_outside_path_and_sorts_them(monkeypatch, tmp_path):
    _fake_install(tmp_path / "PostgreSQL" / "17" / "bin", 17)
    _fake_install(tmp_path / "PostgreSQL" / "16" / "bin", 16)
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-on-path"))
    monkeypatch.setattr(db_migrate, "tool_dir_globs", lambda: [str(tmp_path / "PostgreSQL" / "*" / "bin")])
    installs = discover_tool_installs(None)
    assert [major for major, _ in installs] == [16, 17]
    assert choose_tools(installs, 16)[1].pg_dump == str(tmp_path / "PostgreSQL" / "16" / "bin" / "pg_dump")


def test_windows_searches_installer_and_scoop_locations(monkeypatch):
    import ntpath

    monkeypatch.setattr(db_migrate.sys, "platform", "win32")
    monkeypatch.setattr(db_migrate.os, "path", ntpath)
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\phat")
    assert db_migrate.tool_dir_globs() == [
        r"C:\Program Files\PostgreSQL\*\bin",
        r"C:\Program Files (x86)\PostgreSQL\*\bin",
        r"C:\Users\phat\scoop\apps\postgresql*\current\bin",
    ]


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("win32", "postgresql.org/download/windows"),
        ("darwin", "brew install postgresql@16"),
        ("linux", "apt install postgresql-client-16"),
    ],
)
def test_install_hint_matches_platform(monkeypatch, platform, expected):
    monkeypatch.setattr(db_migrate.sys, "platform", platform)
    assert expected in db_migrate.install_hint(16)


def test_discover_tool_installs_errors_on_incomplete_bin_dir(tmp_path):
    (tmp_path / "pg_dump").write_text("#!/bin/sh\n")
    (tmp_path / "pg_dump").chmod(0o755)
    with pytest.raises(MigrationError, match="not found"):
        discover_tool_installs(str(tmp_path))
