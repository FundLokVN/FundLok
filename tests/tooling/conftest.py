"""scripts/db_migrate.py (`bun run db:copy`) is a local developer tool, not part
of the app, so its tests are left out of the default suite: CI, `bun run test`
and `bun run docker:test` skip them and never fail on them.

Run them on purpose:

    bun run db:copy:test
    DB_MIGRATE_TESTS=1 .venv/bin/pytest tests/tooling -q
"""

import os

if not os.environ.get("DB_MIGRATE_TESTS"):
    collect_ignore = ["test_db_migrate.py"]
