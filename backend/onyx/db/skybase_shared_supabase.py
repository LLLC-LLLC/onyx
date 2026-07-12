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
from typing import Any
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
ROLE_CONNECTION_LIMITS: Final = {
    RUNTIME_ROLE: MAX_RUNTIME_CONNECTIONS,
    MIGRATOR_ROLE: 1,
    READONLY_ROLE: 1,
}
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

# The profile permits only its two database passwords. Everything matching a
# credential-shaped key or a provider/connector/telemetry namespace is refused
# before the normal Onyx configuration module can consume it. This remains
# intentionally broad because connector and provider configuration are owned by
# Skybase, not by the CE runtime.
PROFILE_SECRET_ENV_ALLOWLIST: Final = frozenset(
    {"POSTGRES_PASSWORD", "DB_READONLY_PASSWORD"}
)
FORBIDDEN_SECRET_ENV_TOKENS: Final = frozenset(
    {
        "CREDENTIAL",
        "DSN",
        "PASSWORD",
        "SECRET",
        "TOKEN",
    }
)
FORBIDDEN_NATIVE_ENV_PREFIXES: Final = (
    "AIRTABLE_",
    "AMPLITUDE_",
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "AZURE_OPENAI_",
    "AZURE_STORAGE_",
    "BEDROCK_",
    "CEREBRAS_",
    "COHERE_",
    "CONNECTOR_",
    "DATABRICKS_",
    "DATADOG_",
    "DEEPSEEK_",
    "FIRECRAWL_",
    "FIREWORKS_",
    "GCS_",
    "GITHUB_",
    "GOOGLE_",
    "GROQ_",
    "HF_",
    "HONEYCOMB_",
    "HUGGINGFACE_",
    "LANGFUSE_",
    "LANGSMITH_",
    "LITELLM_",
    "MISTRAL_",
    "MINIO_",
    "MIXPANEL_",
    "NEW_RELIC_",
    "NOTION_",
    "OAUTH_",
    "OIDC_",
    "OPENAI_",
    "OPENROUTER_",
    "OTEL_",
    "PERPLEXITY_",
    "POSTHOG_",
    "PROVIDER_",
    "S3_",
    "SAML_",
    "SCIM_",
    "SEGMENT_",
    "SENTRY_",
    "SLACK_",
    "TELEMETRY_",
    "TOGETHER_",
    "VERTEX_",
    "VOYAGE_",
    "XAI_",
)
SHARED_PROFILE_ALLOWED_PATHS: Final = frozenset({"/health"})


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


def _is_forbidden_native_environment_name(name: str) -> bool:
    """Return whether a populated variable could configure a native secret surface."""

    normalized = name.upper()
    if normalized in PROFILE_SECRET_ENV_ALLOWLIST:
        return False
    tokens = frozenset(normalized.split("_"))
    return (
        normalized.startswith(FORBIDDEN_NATIVE_ENV_PREFIXES)
        or bool(tokens & FORBIDDEN_SECRET_ENV_TOKENS)
        or (
            {"API", "KEY"}.issubset(tokens)
            or {"API", "BASE"}.issubset(tokens)
            or {"ACCESS", "KEY"}.issubset(tokens)
            or {"CLIENT", "SECRET"}.issubset(tokens)
            or {"PRIVATE", "KEY"}.issubset(tokens)
        )
    )


def _assert_no_native_credentials(env: Mapping[str, str]) -> None:
    """Fail closed for provider, connector, identity, storage, and telemetry inputs.

    The runtime necessarily inherits ordinary host variables such as ``PATH`` and
    ``LANG``, so this is a security allowlist for *secret-shaped and native
    service namespaces*, not a brittle allowlist of every POSIX environment
    variable. The two reviewed database passwords are the only populated
    credential values accepted by this profile.
    """

    configured = sorted(
        name
        for name, value in env.items()
        if value.strip() and _is_forbidden_native_environment_name(name)
    )
    if configured:
        raise SharedSupabaseContractError(
            "The shared-Supabase profile accepts only its reviewed database "
            "passwords; native provider, connector, identity, storage, and "
            f"telemetry inputs are forbidden: {', '.join(configured)}."
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


def _row_mapping(row: object, *, context: str) -> Mapping[str, Any]:
    """Get a SQLAlchemy result mapping while rejecting unstructured test doubles."""

    candidate = getattr(row, "_mapping", row)
    if not isinstance(candidate, Mapping):
        raise SharedSupabaseContractError(
            f"Shared-profile precondition returned an invalid {context} row."
        )
    return candidate


def _assert_role_security_attributes(execute: Any, role: str, connlimit: int) -> None:
    row = execute(
        text(
            "SELECT rolname, rolcanlogin, rolinherit, rolsuper, rolcreaterole, "
            "rolcreatedb, rolreplication, rolbypassrls, rolconnlimit "
            "FROM pg_catalog.pg_roles WHERE rolname = :role"
        ),
        {"role": role},
    ).fetchone()
    if row is None:
        raise SharedSupabaseContractError(
            f"Required pre-managed shared-profile role {role!r} does not exist."
        )
    actual = _row_mapping(row, context="role")
    expected: Mapping[str, Any] = {
        "rolcanlogin": True,
        "rolinherit": False,
        "rolsuper": False,
        "rolcreaterole": False,
        "rolcreatedb": False,
        "rolreplication": False,
        "rolbypassrls": False,
        "rolconnlimit": connlimit,
    }
    for field, expected_value in expected.items():
        if actual.get(field) != expected_value:
            raise SharedSupabaseContractError(
                f"Role {role!r} must have {field}={expected_value!r}; "
                f"got {actual.get(field)!r}."
            )

    membership = execute(
        text(
            "SELECT parent.rolname AS parent_role, member.rolname AS member_role "
            "FROM pg_catalog.pg_auth_members AS membership "
            "JOIN pg_catalog.pg_roles AS parent ON parent.oid = membership.roleid "
            "JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member "
            "WHERE parent.rolname = :role OR member.rolname = :role LIMIT 1"
        ),
        {"role": role},
    ).fetchone()
    if membership is not None:
        values = _row_mapping(membership, context="role membership")
        raise SharedSupabaseContractError(
            f"Role {role!r} must have no role memberships; found "
            f"{values.get('member_role')!r} in {values.get('parent_role')!r}."
        )


def _assert_no_protected_schema_create(execute: Any, role: str) -> None:
    row = execute(
        text(
            "SELECT protected_schema.schema_name "
            "FROM (VALUES ('public'), ('extensions')) "
            "AS protected_schema(schema_name) "
            "WHERE has_schema_privilege(:role, protected_schema.schema_name, 'CREATE') "
            "LIMIT 1"
        ),
        {"role": role},
    ).fetchone()
    if row is not None:
        protected_schema = _row_mapping(row, context="protected schema").get(
            "schema_name"
        )
        raise SharedSupabaseContractError(
            f"Role {role!r} must not have CREATE on protected schema "
            f"{protected_schema!r}."
        )


def _assert_no_public_table_privileges(execute: Any, role: str) -> None:
    row = execute(
        text(
            "SELECT relation.relname, privilege.privilege_name "
            "FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = relation.relnamespace "
            "CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'), "
            "('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) "
            "AS privilege(privilege_name) "
            "WHERE namespace.nspname = 'public' "
            "AND relation.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "AND has_table_privilege(:role, relation.oid, privilege.privilege_name) "
            "LIMIT 1"
        ),
        {"role": role},
    ).fetchone()
    if row is not None:
        values = _row_mapping(row, context="public table privilege")
        raise SharedSupabaseContractError(
            f"Role {role!r} must not have public-table privileges; found "
            f"{values.get('privilege_name')!r} on {values.get('relname')!r}."
        )


def _assert_shared_role_and_schema_contract(execute: Any) -> None:
    for role, connlimit in ROLE_CONNECTION_LIMITS.items():
        _assert_role_security_attributes(execute, role, connlimit)
        _assert_no_protected_schema_create(execute, role)
        _assert_no_public_table_privileges(execute, role)

    owner = execute(
        text(
            "SELECT role.rolname AS owner_role "
            "FROM pg_catalog.pg_namespace AS namespace "
            "JOIN pg_catalog.pg_roles AS role ON role.oid = namespace.nspowner "
            "WHERE namespace.nspname = :schema"
        ),
        {"schema": SHARED_SCHEMA},
    ).fetchone()
    actual_owner = (
        _row_mapping(owner, context="schema owner").get("owner_role")
        if owner is not None
        else None
    )
    if actual_owner != MIGRATOR_ROLE:
        raise SharedSupabaseContractError(
            f"Schema {SHARED_SCHEMA!r} must be owned by {MIGRATOR_ROLE!r}; "
            f"got {actual_owner!r}."
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

    _assert_shared_role_and_schema_contract(execute)

    current_path = execute(text("SHOW search_path")).scalar()
    assert_search_path(str(current_path) if current_path is not None else None)


def native_surface_enabled() -> bool:
    """Native credential, upload, chat, tenant, and FileStore APIs are v1-disabled."""

    return not is_shared_supabase_profile()


def is_disabled_native_surface(path: str) -> bool:
    """Default-deny every native HTTP route except the health probe in v1."""

    if native_surface_enabled():
        return False
    normalized_path = "/" + path.split("?", 1)[0].strip().strip("/")
    return normalized_path not in SHARED_PROFILE_ALLOWED_PATHS
