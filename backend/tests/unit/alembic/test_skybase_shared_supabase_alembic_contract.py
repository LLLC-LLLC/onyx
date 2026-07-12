# file-under-test: backend/alembic/env.py
"""Pin the shared-profile Alembic precondition call sites."""

from pathlib import Path


def test_shared_profile_checks_catalog_before_running_migrations() -> None:
    env_file = Path(__file__).resolve().parents[3] / "alembic" / "env.py"
    source = env_file.read_text(encoding="utf-8")
    assert source.count("assert_shared_migration_preconditions(connection)") == 2
