"""Migration 5e1f0b7c9a24: sign-in alerts on by default.

Existing accounts are switched on only if their owner never chose: an owner
who switched alerts off (a SIGNIN_ALERTS_CHANGED audit row) must keep them off.

Runs against its own throwaway database, like tests/ledger/test_migration.py,
so stepping the schema back and forth cannot disturb the shared `test_db`.
"""
import os
import subprocess
import uuid

import psycopg2
from psycopg2.extras import Json
from sqlalchemy.engine import make_url

from conftest import ALEMBIC_COMMAND, ALEMBIC_DATABASE_URL, ROOT
from tests.ledger.test_migration import _admin_connect_kwargs

BEFORE = "17ec24f17dba"
THIS = "5e1f0b7c9a24"
CHECK_DB = "test_db_signin_alerts_default"


def _alembic(env, *args):
    result = subprocess.run(
        [*ALEMBIC_COMMAND, *args], cwd=str(ROOT), env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"


def _add_user(cur, *, alerts: bool | None = None) -> uuid.UUID:
    user_id = uuid.uuid4()
    if alerts is None:
        cur.execute(
            "INSERT INTO users (id, email) VALUES (%s, %s)",
            (str(user_id), f"{user_id.hex[:12]}@example.com"),
        )
    else:
        cur.execute(
            "INSERT INTO users (id, email, signin_alerts_enabled) VALUES (%s, %s, %s)",
            (str(user_id), f"{user_id.hex[:12]}@example.com", alerts),
        )
    return user_id


def _record_choice(cur, user_id: uuid.UUID, *, before: bool, after: bool) -> None:
    # The row PATCH /users/me/security-preferences writes.
    cur.execute(
        "INSERT INTO audit_logs (id, entity_type, entity_id, action, actor_id, before_state, after_state) "
        "VALUES (%s, 'AUTH', %s, 'SIGNIN_ALERTS_CHANGED', %s, %s, %s)",
        (
            str(uuid.uuid4()),
            str(user_id),
            str(user_id),
            Json({"signin_alerts_enabled": before}),
            Json({"signin_alerts_enabled": after}),
        ),
    )


def _alerts(cur, user_id: uuid.UUID) -> bool:
    cur.execute("SELECT signin_alerts_enabled FROM users WHERE id = %s", (str(user_id),))
    return cur.fetchone()[0]


def test_existing_accounts_follow_the_new_default_unless_the_owner_chose():
    admin_conn = psycopg2.connect(**_admin_connect_kwargs())
    admin_conn.autocommit = True
    try:
        with admin_conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{CHECK_DB}"')
            cur.execute(f'CREATE DATABASE "{CHECK_DB}"')

        base_url = make_url(ALEMBIC_DATABASE_URL)
        url = ALEMBIC_DATABASE_URL.replace(f"/{base_url.database}", f"/{CHECK_DB}", 1)
        env = {**os.environ, "DATABASE_URL": url}
        connect_kwargs = {**_admin_connect_kwargs(), "dbname": CHECK_DB}

        _alembic(env, "upgrade", BEFORE)
        conn = psycopg2.connect(**connect_kwargs)
        conn.autocommit = True
        with conn.cursor() as cur:
            never_chose = _add_user(cur)
            opted_in = _add_user(cur, alerts=True)
            _record_choice(cur, opted_in, before=False, after=True)
            opted_out = _add_user(cur, alerts=False)
            _record_choice(cur, opted_out, before=False, after=True)
            _record_choice(cur, opted_out, before=True, after=False)
            assert _alerts(cur, never_chose) is False

        _alembic(env, "upgrade", THIS)
        with conn.cursor() as cur:
            assert _alerts(cur, never_chose) is True
            assert _alerts(cur, opted_in) is True
            assert _alerts(cur, opted_out) is False, "an owner's opt-out must survive"
            created_after = _add_user(cur)
            assert _alerts(cur, created_after) is True

        _alembic(env, "downgrade", BEFORE)
        with conn.cursor() as cur:
            assert _alerts(cur, never_chose) is False
            assert _alerts(cur, created_after) is False
            assert _alerts(cur, opted_in) is True
            assert _alerts(cur, opted_out) is False
        conn.close()
    finally:
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (CHECK_DB,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{CHECK_DB}"')
        admin_conn.close()
