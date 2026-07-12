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
from onyx.db.skybase_shared_supabase import assert_shared_migration_preconditions
from onyx.db.skybase_shared_supabase import assert_worker_app_allowed
from onyx.db.skybase_shared_supabase import is_disabled_native_surface
from onyx.db.skybase_shared_supabase import SEARCH_PATH
from onyx.db.skybase_shared_supabase import SharedSupabaseContractError
from onyx.db.skybase_shared_supabase import validate_shared_supabase_contract
from onyx.file_store.file_store import DisabledFileStore
from onyx.file_store.file_store import get_default_file_store
from onyx.file_store.file_store import get_s3_file_store


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


def test_runtime_profile_allows_the_safe_huggingface_telemetry_disable_flag(
    tmp_path: Path,
) -> None:
    environment = _profile_env(tmp_path)
    environment["HF_HUB_DISABLE_TELEMETRY"] = "true"
    validate_shared_supabase_contract(environment)


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
        ("OPENAI_API_KEY", "not-allowed", "forbidden"),
        ("OPENROUTER_DEFAULT_API_KEY", "not-allowed", "forbidden"),
        ("GITHUB_OAUTH_CLIENT_ID", "not-allowed", "forbidden"),
        ("GITHUB_PERSONAL_ACCESS_TOKEN", "not-allowed", "forbidden"),
        ("SENTRY_DSN", "not-allowed", "forbidden"),
        ("LITELLM_API_BASE", "not-allowed", "forbidden"),
        ("OTEL_EXPORTER_OTLP_HEADERS", "not-allowed", "forbidden"),
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


@pytest.mark.parametrize(
    "path",
    [
        "/admin/web-search",
        "/admin/embedding",
        "/admin/api-key",
        "/auth/login",
        "/password/change-password",
        "/onyx-api/connector-docs/1",
        "/query",
    ],
)
def test_shared_profile_default_denies_all_native_routes(
    tmp_path: Path, path: str
) -> None:
    environment = _profile_env(tmp_path)
    with patch.dict(os.environ, environment, clear=True):
        assert_search_path(SEARCH_PATH)
        with pytest.raises(SharedSupabaseContractError, match="search_path"):
            assert_search_path("skybase_onyx, public")
        assert is_disabled_native_surface(path)
        assert not is_disabled_native_surface("/health")
        assert not is_disabled_native_surface("/health/")


def test_shared_profile_fail_closes_all_file_store_operations(tmp_path: Path) -> None:
    with patch.dict(os.environ, _profile_env(tmp_path), clear=True):
        file_store = get_default_file_store()
        assert isinstance(file_store, DisabledFileStore)
        file_store.initialize()
        with pytest.raises(
            SharedSupabaseContractError, match="FileStore object operations"
        ):
            file_store.list_files_by_prefix("knowledge/")
        with pytest.raises(SharedSupabaseContractError, match="Direct S3"):
            get_s3_file_store()


class _Result:
    def __init__(self, row: object | None = None, scalar_value: str | None = None):
        self._row = row
        self._scalar_value = scalar_value

    def fetchone(self) -> object | None:
        return self._row

    def scalar(self) -> str | None:
        return self._scalar_value


class _PreconditionBind:
    def __init__(
        self,
        *,
        extension_schemas: dict[str, str] | None = None,
        roles: set[str] | None = None,
        search_path: str = SEARCH_PATH,
        role_attributes: dict[str, dict[str, object]] | None = None,
        membership: dict[str, dict[str, str]] | None = None,
        schema_owner: str = "skybase_onyx_migrator",
        protected_schema_create: dict[str, str] | None = None,
        public_table_privilege: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.extension_schemas = extension_schemas or {
            "pg_trgm": "extensions",
            "pgcrypto": "public",
        }
        self.roles = roles or {
            "skybase_onyx_runtime",
            "skybase_onyx_migrator",
            "skybase_onyx_kg_ro",
        }
        self.search_path = search_path
        self.role_attributes = role_attributes or {}
        self.membership = membership or {}
        self.schema_owner = schema_owner
        self.protected_schema_create = protected_schema_create or {}
        self.public_table_privilege = public_table_privilege or {}

    def execute(
        self, statement: object, parameters: dict[str, str] | None = None
    ) -> _Result:
        query = str(statement)
        if "pg_catalog.pg_extension" in query:
            extension = (parameters or {})["extension_name"]
            schema = self.extension_schemas.get(extension)
            return _Result((schema,) if schema is not None else None)
        if "pg_catalog.pg_auth_members" in query:
            role = (parameters or {})["role"]
            return _Result(self.membership.get(role))
        if "rolcanlogin" in query:
            role = (parameters or {})["role"]
            if role not in self.roles:
                return _Result()
            row = {
                "rolname": role,
                "rolcanlogin": True,
                "rolinherit": False,
                "rolsuper": False,
                "rolcreaterole": False,
                "rolcreatedb": False,
                "rolreplication": False,
                "rolbypassrls": False,
                "rolconnlimit": {
                    "skybase_onyx_runtime": 12,
                    "skybase_onyx_migrator": 1,
                    "skybase_onyx_kg_ro": 1,
                }[role],
            }
            row.update(self.role_attributes.get(role, {}))
            return _Result(row)
        if "has_schema_privilege" in query:
            role = (parameters or {})["role"]
            schema = self.protected_schema_create.get(role)
            return _Result({"schema_name": schema} if schema else None)
        if "has_table_privilege" in query:
            role = (parameters or {})["role"]
            return _Result(self.public_table_privilege.get(role))
        if "pg_catalog.pg_namespace" in query:
            return _Result({"owner_role": self.schema_owner})
        if "SHOW search_path" in query:
            return _Result(scalar_value=self.search_path)
        raise AssertionError(f"unexpected precondition query: {query}")


@pytest.mark.parametrize(
    ("bind", "message"),
    [
        (
            _PreconditionBind(
                extension_schemas={"pg_trgm": "public", "pgcrypto": "public"}
            ),
            "pg_trgm",
        ),
        (
            _PreconditionBind(roles={"skybase_onyx_runtime", "skybase_onyx_migrator"}),
            "skybase_onyx_kg_ro",
        ),
        (
            _PreconditionBind(
                role_attributes={"skybase_onyx_runtime": {"rolbypassrls": True}}
            ),
            "rolbypassrls",
        ),
        (
            _PreconditionBind(
                membership={
                    "skybase_onyx_runtime": {
                        "member_role": "skybase_onyx_runtime",
                        "parent_role": "pg_read_all_data",
                    }
                }
            ),
            "memberships",
        ),
        (_PreconditionBind(schema_owner="postgres"), "must be owned"),
        (
            _PreconditionBind(
                protected_schema_create={"skybase_onyx_runtime": "public"}
            ),
            "must not have CREATE",
        ),
        (
            _PreconditionBind(
                public_table_privilege={
                    "skybase_onyx_runtime": {
                        "relname": "skybase_contract_sentinel",
                        "privilege_name": "SELECT",
                    }
                }
            ),
            "public-table privileges",
        ),
        (_PreconditionBind(search_path="public"), "search_path"),
    ],
)
def test_migration_preconditions_reject_catalog_drift(
    tmp_path: Path, bind: _PreconditionBind, message: str
) -> None:
    with patch.dict(
        os.environ, _profile_env(tmp_path, role_profile="migrator"), clear=True
    ):
        with pytest.raises(SharedSupabaseContractError, match=message):
            assert_shared_migration_preconditions(bind)


def test_migration_preconditions_accept_the_reviewed_catalog(tmp_path: Path) -> None:
    with patch.dict(
        os.environ, _profile_env(tmp_path, role_profile="migrator"), clear=True
    ):
        assert_shared_migration_preconditions(_PreconditionBind())


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
