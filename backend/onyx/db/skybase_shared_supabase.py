"""Fail-closed contract for running Onyx CE beside Skybase in one database.

This module intentionally reads its small configuration surface directly from
the environment.  It is imported by configuration, engines, migrations, and
Celery applications, so it must not import the normal Onyx configuration
module and create an import cycle.  The contract is inactive unless the
operator explicitly enables it with ``SKYBASE_ONYX_SHARED_SUPABASE=true``.

The profile is deliberately narrower than a generic self-hosted Onyx install:
Skybase owns identity, credentials, object storage, and public routing.  Until
the private storage broker is installed, the Onyx FileStore is disabled rather
than allowed to fall back to S3 or PostgreSQL large objects.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlalchemy import text

SHARED_SUPABASE_ENV: Final = "SKYBASE_ONYX_SHARED_SUPABASE"
ROLE_PROFILE_ENV: Final = "SKYBASE_ONYX_ROLE_PROFILE"
SHARED_SCHEMA: Final = "skybase_onyx"
EXTENSION_SCHEMA: Final = "extensions"
RUNTIME_ROLE: Final = "skybase_onyx_runtime"
MIGRATOR_ROLE: Final = "skybase_onyx_migrator"
READONLY_ROLE: Final = "skybase_onyx_kg_ro"
SEARCH_PATH: Final = f"{SHARED_SCHEMA},{EXTENSION_SCHEMA}"
MAX_RUNTIME_CONNECTIONS: Final = 12
ALLOWED_WORKER_APPS: Final = frozenset({"docfetching", "docprocessing"})
DENIED_WORKER_APPS: Final = frozenset(
    {
        "primary",
        "light",
        "heavy",
        "user_file_processing",
        "scheduled_tasks",
        "monitoring",
        "beat",
        "client",
    }
)


class SharedSupabaseContractError(RuntimeError):
    """Raised before a shared-profile process can touch PostgreSQL."""


@dataclass(frozen=True)
class PoolBudget:
    """Reviewed, process-local connection budget for the CE v1 pilot."""

    api_sync: int = 2
    api_async: int = 2
    readonly: int = 1
    docfetching: int = 1
    docprocessing: int = 1
    indexing_child: int = 1

    @property
    def total(self) -> int:
        return (
            self.api_sync
            + self.api_async
            + self.readonly
            + self.docfetching
            + self.docprocessing
            + self.indexing_child
        )


POOL_BUDGET: Final = PoolBudget()


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def is_shared_supabase_profile(
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return whether the explicitly opted-in Skybase profile is active."""

    source = os.environ if env is None else env
    return _is_true(source.get(SHARED_SUPABASE_ENV))


def _require(env: Mapping[str, str], name: str, expected: str) -> None:
    actual = (env.get(name) or "").strip()
    if actual != expected:
        raise SharedSupabaseContractError(
            f"{name} must be {expected!r} when {SHARED_SUPABASE_ENV}=true; "
            f"got {actual!r}."
        )


def _require_int(env: Mapping[str, str], name: str, expected: int) -> None:
    raw_value = (env.get(name) or "").strip()
    try:
        actual = int(raw_value)
    except ValueError as exc:
        raise SharedSupabaseContractError(
            f"{name} must be the integer {expected} when "
            f"{SHARED_SUPABASE_ENV}=true; got {raw_value!r}."
        ) from exc
    if actual != expected:
        raise SharedSupabaseContractError(
            f"{name} must be {expected} when {SHARED_SUPABASE_ENV}=true; got {actual}."
        )


def _require_present_not_default(env: Mapping[str, str], name: str) -> None:
    value = (env.get(name) or "").strip()
    if not value or value.lower() in {"password", "postgres", "db_readonly_user"}:
        raise SharedSupabaseContractError(
            f"{name} must be an explicit non-default secret/identity when "
            f"{SHARED_SUPABASE_ENV}=true."
        )


def _assert_no_direct_object_storage(env: Mapping[str, str]) -> None:
    configured = sorted(
        name
        for name, value in env.items()
        if name.startswith(("S3_", "GCS_")) and value.strip()
    )
    if configured:
        raise SharedSupabaseContractError(
            "The shared-Supabase profile forbids direct object-storage "
            f"configuration before the Skybase broker is installed: {', '.join(configured)}."
        )
    _require(env, "FILE_STORE_BACKEND", "disabled")


def _assert_no_native_credentials(env: Mapping[str, str]) -> None:
    direct_values = sorted(
        name
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_DEFAULT_API_KEY",
            "COHERE_API_KEY",
            "COHERE_DEFAULT_API_KEY",
            "ENCRYPTION_KEY_SECRET",
            "GEN_AI_API_KEY",
            "GOOGLE_API_KEY",
            "OPENAI_API_KEY",
            "OPENAI_DEFAULT_API_KEY",
            "VERTEXAI_DEFAULT_CREDENTIALS",
            "VOYAGE_API_KEY",
        )
        if (env.get(name) or "").strip()
    )
    if direct_values:
        raise SharedSupabaseContractError(
            "The shared-Supabase profile accepts no native provider or credential "
            f"secrets: {', '.join(direct_values)}. Use the Skybase proxy later."
        )


def _validate_tls(env: Mapping[str, str]) -> None:
    _require(env, "POSTGRES_SSLMODE", "verify-full")
    root_cert = (env.get("POSTGRES_SSLROOTCERT") or "").strip()
    if not root_cert:
        raise SharedSupabaseContractError(
            "POSTGRES_SSLROOTCERT is required by the shared-Supabase profile."
        )
    path = Path(root_cert)
    if not path.is_file() or not os.access(path, os.R_OK):
        raise SharedSupabaseContractError(
            "POSTGRES_SSLROOTCERT must name a readable CA bundle in the shared-Supabase profile."
        )


def _validate_runtime_budget(env: Mapping[str, str], role_profile: str) -> None:
    if role_profile == "migrator":
        _require_int(env, "POSTGRES_API_SERVER_POOL_SIZE", 1)
        _require_int(env, "POSTGRES_API_SERVER_POOL_OVERFLOW", 0)
        return

    _require_int(env, "POSTGRES_API_SERVER_POOL_SIZE", POOL_BUDGET.api_sync)
    _require_int(env, "POSTGRES_API_SERVER_POOL_OVERFLOW", 0)
    _require_int(env, "POSTGRES_API_SERVER_READ_ONLY_POOL_SIZE", POOL_BUDGET.readonly)
    _require_int(env, "POSTGRES_API_SERVER_READ_ONLY_POOL_OVERFLOW", 0)
    _require_int(env, "CELERY_WORKER_DOCFETCHING_CONCURRENCY", 1)
    _require_int(env, "CELERY_WORKER_DOCPROCESSING_CONCURRENCY", 1)
    if not _is_true(env.get("SKIP_WARM_UP")):
        raise SharedSupabaseContractError(
            "SKIP_WARM_UP must be true in the shared-Supabase profile; the "
            "upstream 20-connection warmup exceeds the reviewed budget."
        )


def validate_shared_supabase_contract(
    env: Mapping[str, str] | None = None,
) -> None:
    """Validate every value that protects the shared Skybase database.

    The validation is pure except for checking that the explicit CA bundle is
    readable.  It is called at normal configuration import and again by the
    engines/launcher, so callers cannot bypass it by importing a lower-level
    module directly.
    """

    source = os.environ if env is None else env
    if not is_shared_supabase_profile(source):
        return

    _require(source, "POSTGRES_DEFAULT_SCHEMA", SHARED_SCHEMA)
    _require(source, "POSTGRES_EXTENSION_SCHEMA", EXTENSION_SCHEMA)
    _require(source, "SKYBASE_ONYX_DISPOSABLE_BRANCH", "true")
    _require(source, "MULTI_TENANT", "false")
    _require(source, "POSTGRES_USE_NULL_POOL", "false")
    _require(source, "USE_IAM_AUTH", "false")
    _require_present_not_default(source, "POSTGRES_PASSWORD")
    _require(source, "DB_READONLY_USER", READONLY_ROLE)
    _require_present_not_default(source, "DB_READONLY_PASSWORD")
    _validate_tls(source)
    _assert_no_direct_object_storage(source)
    _assert_no_native_credentials(source)

    role_profile = (source.get(ROLE_PROFILE_ENV) or "runtime").strip()
    if role_profile not in {"runtime", "migrator"}:
        raise SharedSupabaseContractError(
            f"{ROLE_PROFILE_ENV} must be 'runtime' or 'migrator'; got {role_profile!r}."
        )
    _require(
        source,
        "POSTGRES_USER",
        MIGRATOR_ROLE if role_profile == "migrator" else RUNTIME_ROLE,
    )
    _validate_runtime_budget(source, role_profile)

    if POOL_BUDGET.total > MAX_RUNTIME_CONNECTIONS:
        raise SharedSupabaseContractError(
            "The checked-in shared-Supabase connection budget exceeds its approved maximum."
        )


def assert_worker_app_allowed(app_name: str) -> None:
    """Reject manually launched Celery apps before their engine can initialize."""

    if not is_shared_supabase_profile():
        return
    validate_shared_supabase_contract()
    if app_name not in ALLOWED_WORKER_APPS:
        raise SharedSupabaseContractError(
            f"Celery app {app_name!r} is not approved for the shared-Supabase profile. "
            f"Allowed apps: {', '.join(sorted(ALLOWED_WORKER_APPS))}."
        )


def assert_pool_request(
    *,
    pool_size: int,
    max_overflow: int,
    purpose: str,
) -> None:
    """Prevent any engine path from silently inflating the reviewed budget."""

    if not is_shared_supabase_profile():
        return
    validate_shared_supabase_contract()
    expected_by_purpose = {
        "api_sync": (POOL_BUDGET.api_sync, 0),
        "api_async": (POOL_BUDGET.api_async, 0),
        "readonly": (POOL_BUDGET.readonly, 0),
        "docfetching": (POOL_BUDGET.docfetching, 0),
        "docprocessing": (POOL_BUDGET.docprocessing, 0),
        "indexing_child": (POOL_BUDGET.indexing_child, 0),
        "migrator": (1, 0),
    }
    expected = expected_by_purpose.get(purpose)
    if expected is None:
        raise SharedSupabaseContractError(
            f"Unreviewed database engine purpose {purpose!r} in the shared-Supabase profile."
        )
    if (pool_size, max_overflow) != expected:
        raise SharedSupabaseContractError(
            f"{purpose} pool must be {expected[0]}+{expected[1]} in the shared-Supabase "
            f"profile; got {pool_size}+{max_overflow}."
        )


def shared_search_path() -> str:
    """Return the only search path allowed to Onyx shared-profile connections."""

    return SEARCH_PATH


def assert_search_path(value: str | None) -> None:
    """Check a server-reported search path without normalizing it permissively."""

    if not is_shared_supabase_profile():
        return
    if (value or "").replace(" ", "") != SEARCH_PATH:
        raise SharedSupabaseContractError(
            f"Shared-profile connection search_path must be {SEARCH_PATH!r}; got {value!r}."
        )


def assert_shared_migration_preconditions(bind: object) -> None:
    """Verify shared resources without creating or modifying global objects."""

    if not is_shared_supabase_profile():
        return
    validate_shared_supabase_contract()
    execute = getattr(bind, "execute", None)
    if execute is None:
        raise SharedSupabaseContractError(
            "Alembic shared-profile precondition check requires a SQLAlchemy connection."
        )

    for extension_name, expected_schema in (
        ("pg_trgm", EXTENSION_SCHEMA),
        ("pgcrypto", "public"),
    ):
        row = execute(
            text(
                "SELECT namespace.nspname "
                "FROM pg_catalog.pg_extension AS extension "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = extension.extnamespace "
                "WHERE extension.extname = :extension_name"
            ),
            {"extension_name": extension_name},
        ).fetchone()
        actual = str(row[0]) if row else None
        if actual != expected_schema:
            raise SharedSupabaseContractError(
                f"Required extension {extension_name!r} must already exist in "
                f"schema {expected_schema!r}; got {actual!r}."
            )

    for role in (RUNTIME_ROLE, MIGRATOR_ROLE, READONLY_ROLE):
        row = execute(
            text("SELECT rolname FROM pg_catalog.pg_roles WHERE rolname = :role"),
            {"role": role},
        ).fetchone()
        if row is None:
            raise SharedSupabaseContractError(
                f"Required pre-managed shared-profile role {role!r} does not exist."
            )

    current_path = execute(text("SHOW search_path")).scalar()
    assert_search_path(str(current_path) if current_path is not None else None)


def native_surface_enabled() -> bool:
    """Native credential, upload, chat, tenant, and FileStore APIs are v1-disabled."""

    return not is_shared_supabase_profile()


def is_disabled_native_surface(path: str) -> bool:
    """Return whether a native CE v1 endpoint could mutate Skybase-owned data.

    The shared profile keeps Onyx behind Skybase's private boundary.  Cited
    retrieval is introduced by a later adapter; native credential, identity,
    upload, chat, tenant, tool, and FileStore routes are not a public shortcut
    around that boundary.
    """

    if native_surface_enabled():
        return False
    parts = {part.lower() for part in path.split("/") if part}
    blocked_parts = {
        "auth",
        "chat",
        "connector",
        "document",
        "documents",
        "credential",
        "credentials",
        "file",
        "files",
        "ingestion",
        "llm",
        "mcp",
        "oauth",
        "persona",
        "personas",
        "project",
        "projects",
        "skill",
        "skills",
        "tenant",
        "tool",
        "tools",
        "upload",
        "user",
        "users",
    }
    return bool(parts & blocked_parts)
