# file-under-test: backend/onyx/db/skybase_shared_supabase.py
"""Focused unit coverage for the shared-Supabase fail-closed profile."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from onyx.db.skybase_shared_supabase import assert_pool_request
from onyx.db.skybase_shared_supabase import assert_search_path
from onyx.db.skybase_shared_supabase import assert_worker_app_allowed
from onyx.db.skybase_shared_supabase import is_disabled_native_surface
from onyx.db.skybase_shared_supabase import SEARCH_PATH
from onyx.db.skybase_shared_supabase import SharedSupabaseContractError
from onyx.db.skybase_shared_supabase import validate_shared_supabase_contract


def _profile_env(tmp_path: Path, *, role_profile: str = "runtime") -> dict[str, str]:
    root_cert = tmp_path / "branch-ca.pem"
    root_cert.write_text("test-only-ca", encoding="utf-8")
    return {
        "SKYBASE_ONYX_SHARED_SUPABASE": "true",
        "SKYBASE_ONYX_ROLE_PROFILE": role_profile,
        "SKYBASE_ONYX_DISPOSABLE_BRANCH": "true",
        "MULTI_TENANT": "false",
        "POSTGRES_USER": (
            "skybase_onyx_migrator"
            if role_profile == "migrator"
            else "skybase_onyx_runtime"
        ),
        "POSTGRES_PASSWORD": "a" * 64,
        "POSTGRES_HOST": "db.example.test",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "postgres",
        "POSTGRES_DEFAULT_SCHEMA": "skybase_onyx",
        "POSTGRES_EXTENSION_SCHEMA": "extensions",
        "POSTGRES_SSLMODE": "verify-full",
        "POSTGRES_SSLROOTCERT": str(root_cert),
        "POSTGRES_USE_NULL_POOL": "false",
        "POSTGRES_API_SERVER_POOL_SIZE": "1" if role_profile == "migrator" else "2",
        "POSTGRES_API_SERVER_POOL_OVERFLOW": "0",
        "POSTGRES_API_SERVER_READ_ONLY_POOL_SIZE": "1",
        "POSTGRES_API_SERVER_READ_ONLY_POOL_OVERFLOW": "0",
        "DB_READONLY_USER": "skybase_onyx_kg_ro",
        "DB_READONLY_PASSWORD": "b" * 64,
        "CELERY_WORKER_DOCFETCHING_CONCURRENCY": "1",
        "CELERY_WORKER_DOCPROCESSING_CONCURRENCY": "1",
        "SKIP_WARM_UP": "true",
        "FILE_STORE_BACKEND": "disabled",
        "USE_IAM_AUTH": "false",
    }


def test_valid_runtime_profile_passes(tmp_path: Path) -> None:
    validate_shared_supabase_contract(_profile_env(tmp_path))


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("POSTGRES_DEFAULT_SCHEMA", "public", "POSTGRES_DEFAULT_SCHEMA"),
        ("SKYBASE_ONYX_DISPOSABLE_BRANCH", "false", "DISPOSABLE_BRANCH"),
        ("MULTI_TENANT", "true", "MULTI_TENANT"),
        ("POSTGRES_SSLMODE", "require", "POSTGRES_SSLMODE"),
        ("POSTGRES_API_SERVER_POOL_SIZE", "3", "POSTGRES_API_SERVER_POOL_SIZE"),
        ("FILE_STORE_BACKEND", "s3", "FILE_STORE_BACKEND"),
        ("S3_AWS_SECRET_ACCESS_KEY", "not-allowed", "direct object-storage"),
        ("OPENAI_API_KEY", "not-allowed", "native provider"),
    ],
)
def test_runtime_profile_rejects_each_boundary(
    tmp_path: Path, name: str, value: str, message: str
) -> None:
    environment = _profile_env(tmp_path)
    environment[name] = value
    with pytest.raises(SharedSupabaseContractError, match=message):
        validate_shared_supabase_contract(environment)


def test_migrator_profile_only_permits_one_connection(tmp_path: Path) -> None:
    environment = _profile_env(tmp_path, role_profile="migrator")
    validate_shared_supabase_contract(environment)
    environment["POSTGRES_API_SERVER_POOL_SIZE"] = "2"
    with pytest.raises(SharedSupabaseContractError, match="POOL_SIZE"):
        validate_shared_supabase_contract(environment)


def test_pool_and_worker_guards_fail_before_engine_creation(tmp_path: Path) -> None:
    environment = _profile_env(tmp_path)
    with patch.dict(os.environ, environment, clear=True):
        with pytest.raises(SharedSupabaseContractError, match="api_sync pool"):
            assert_pool_request(pool_size=40, max_overflow=10, purpose="api_sync")
        with pytest.raises(SharedSupabaseContractError, match="not approved"):
            assert_worker_app_allowed("primary")
        assert_worker_app_allowed("docfetching")


def test_search_path_and_native_surface_gates(tmp_path: Path) -> None:
    environment = _profile_env(tmp_path)
    with patch.dict(os.environ, environment, clear=True):
        assert_search_path(SEARCH_PATH)
        with pytest.raises(SharedSupabaseContractError, match="search_path"):
            assert_search_path("skybase_onyx, public")
        assert is_disabled_native_surface("/api/admin/connector")
        assert is_disabled_native_surface("/api/documents/search")
        assert is_disabled_native_surface("/api/chat/send")
        assert not is_disabled_native_surface("/health")


def _run_shared_profile_python(
    tmp_path: Path, source: str
) -> subprocess.CompletedProcess[str]:
    environment = _profile_env(tmp_path)
    environment["PATH"] = os.environ["PATH"]
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[5],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_real_sql_engine_rejects_an_oversized_pool_before_create_engine(
    tmp_path: Path,
) -> None:
    result = _run_shared_profile_python(
        tmp_path,
        """
from onyx.db.engine import sql_engine
from onyx.db.skybase_shared_supabase import SharedSupabaseContractError

sql_engine.create_engine = lambda *args, **kwargs: (_ for _ in ()).throw(
    AssertionError("create_engine must not run")
)
try:
    sql_engine.SqlEngine.init_engine(pool_size=3, max_overflow=0)
except SharedSupabaseContractError:
    pass
else:
    raise AssertionError("shared profile accepted an oversized API pool")
assert sql_engine.SqlEngine._engine is None
""",
    )
    assert result.returncode == 0, result.stderr


def test_real_allowed_worker_import_does_not_initialize_an_engine(
    tmp_path: Path,
) -> None:
    result = _run_shared_profile_python(
        tmp_path,
        """
import onyx.background.celery.apps.docfetching
from onyx.db.engine.sql_engine import SqlEngine

assert SqlEngine._engine is None
""",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "app_name",
    [
        "primary",
        "light",
        "heavy",
        "user_file_processing",
        "scheduled_tasks",
        "monitoring",
        "beat",
        "client",
    ],
)
def test_real_denied_worker_import_fails_before_engine_initialization(
    tmp_path: Path, app_name: str
) -> None:
    result = _run_shared_profile_python(
        tmp_path,
        f"""
import importlib
from onyx.db.skybase_shared_supabase import SharedSupabaseContractError

try:
    importlib.import_module("onyx.background.celery.apps.{app_name}")
except SharedSupabaseContractError:
    pass
else:
    raise AssertionError("denied worker app imported successfully")

from onyx.db.engine.sql_engine import SqlEngine
assert SqlEngine._engine is None
""",
    )
    assert result.returncode == 0, result.stderr
