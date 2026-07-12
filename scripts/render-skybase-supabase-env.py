#!/usr/bin/env python3
"""Render 0600, role-scoped CE v1 files for a disposable Supabase branch.

This runs on the operator host, never in an Onyx container.  It accepts the
privileged branch bootstrap URL from a 0600 file, parses it once, and emits
separate runtime, migrator, and readonly credentials plus private bootstrap
SQL.  No secret is written to stdout.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Final
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlsplit

_HEX_PASSWORD = re.compile(r"^[0-9a-f]{64}$")
_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_ENV_VALUE = re.compile(r"^[^\r\n=]+$")
_EXPECTED_MODE = 0o600
# CE v4.3.1 has no caller for the readonly engine. Its historical KG view
# path is commented out and granted filtered views per request rather than
# base tables. Keep the v1 data allowlist intentionally empty until a Skybase
# adapter defines permission-filtered retrieval views.
KG_READONLY_TABLE_ALLOWLIST: Final[tuple[str, ...]] = ()
KG_READONLY_SEQUENCE_ALLOWLIST: Final[tuple[str, ...]] = ()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _require_mode(path: Path) -> None:
    if _mode(path) != _EXPECTED_MODE:
        raise ValueError(f"{path} must have mode 0600")


def _strict_decode_once(url_file: Path) -> str:
    _require_mode(url_file)
    raw = url_file.read_bytes()
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("bootstrap URL file must be strict UTF-8") from exc
    # A single terminal newline is conventional for a secret file. Other
    # controls, including embedded newlines, make URI parsing ambiguous.
    if value.endswith("\n"):
        value = value[:-1]
    if _CONTROL.search(value):
        raise ValueError("bootstrap URL contains a control character")
    return value


def _unquote_exact(value: str, field: str) -> str:
    if _PERCENT_ESCAPE.search(value):
        raise ValueError(f"bootstrap URL has malformed percent escape in {field}")
    decoded = unquote(value, encoding="utf-8", errors="strict")
    if quote(decoded, safe="") != value:
        raise ValueError(
            f"bootstrap URL uses ambiguous percent encoding in {field}; use canonical UTF-8 escaping"
        )
    return decoded


def parse_bootstrap_url(url_file: Path) -> dict[str, str]:
    """Parse a single canonical PostgreSQL URI without logging its contents."""

    raw_url = _strict_decode_once(url_file)
    if _PERCENT_ESCAPE.search(raw_url):
        raise ValueError("bootstrap URL has malformed percent escape")
    if raw_url.count("@") != 1:
        raise ValueError("bootstrap URL must contain exactly one authority @ separator")
    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("bootstrap URL scheme must be postgres or postgresql")
    if parsed.query or parsed.fragment:
        raise ValueError(
            "bootstrap URL must not include query parameters or a fragment"
        )
    if not parsed.username or parsed.password is None:
        raise ValueError("bootstrap URL must include an explicit username and password")
    if not parsed.hostname or parsed.port is None:
        raise ValueError("bootstrap URL must include an explicit host and port")
    if not 1 <= parsed.port <= 65535:
        raise ValueError("bootstrap URL port must be between 1 and 65535")
    database = parsed.path.lstrip("/")
    if not database or "/" in database:
        raise ValueError("bootstrap URL must include exactly one database path segment")
    if any(_CONTROL.search(item) for item in (parsed.hostname, database)):
        raise ValueError("bootstrap URL host/database contains a control character")

    authority = raw_url.split("://", 1)[1].split("/", 1)[0]
    raw_userinfo = authority.rsplit("@", 1)[0]
    raw_user, raw_password = raw_userinfo.split(":", 1)
    username = _unquote_exact(raw_user, "username")
    password = _unquote_exact(raw_password, "password")
    if username != "postgres":
        raise ValueError(
            "branch bootstrap URL must use the branch-local postgres identity"
        )
    if not password or _CONTROL.search(password):
        raise ValueError(
            "bootstrap URL password is empty or contains a control character"
        )
    return {"host": parsed.hostname, "port": str(parsed.port), "database": database}


def _write_secret(path: Path, contents: str) -> None:
    if not _SAFE_ENV_VALUE.search(contents) and "\n" not in contents:
        raise ValueError("refusing to write an unsafe generated environment value")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _EXPECTED_MODE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(contents)
    finally:
        os.chmod(path, _EXPECTED_MODE)
    _require_mode(path)


def _password() -> str:
    value = secrets.token_hex(32)
    if not _HEX_PASSWORD.fullmatch(value):
        raise RuntimeError("generated a non-hex role password")
    return value


def _env_lines(
    connection: dict[str, str],
    *,
    role: str,
    password: str,
    readonly_password: str,
    ssl_root_cert: Path,
    profile: str,
) -> str:
    pool_size = "1" if profile == "migrator" else "2"
    readonly_pool_size = "1"
    lines = {
        "SKYBASE_ONYX_SHARED_SUPABASE": "true",
        "SKYBASE_ONYX_ROLE_PROFILE": profile,
        "SKYBASE_ONYX_DISPOSABLE_BRANCH": "true",
        "MULTI_TENANT": "false",
        "POSTGRES_USER": role,
        "POSTGRES_PASSWORD": password,
        "POSTGRES_HOST": connection["host"],
        "POSTGRES_PORT": connection["port"],
        "POSTGRES_DB": connection["database"],
        "POSTGRES_DEFAULT_SCHEMA": "skybase_onyx",
        "POSTGRES_EXTENSION_SCHEMA": "extensions",
        "POSTGRES_SSLMODE": "verify-full",
        "POSTGRES_SSLROOTCERT": str(ssl_root_cert),
        "POSTGRES_USE_NULL_POOL": "false",
        "POSTGRES_API_SERVER_POOL_SIZE": pool_size,
        "POSTGRES_API_SERVER_POOL_OVERFLOW": "0",
        "POSTGRES_API_SERVER_READ_ONLY_POOL_SIZE": readonly_pool_size,
        "POSTGRES_API_SERVER_READ_ONLY_POOL_OVERFLOW": "0",
        "DB_READONLY_USER": "skybase_onyx_kg_ro",
        "DB_READONLY_PASSWORD": readonly_password,
        "CELERY_WORKER_DOCFETCHING_CONCURRENCY": "1",
        "CELERY_WORKER_DOCPROCESSING_CONCURRENCY": "1",
        "SKIP_WARM_UP": "true",
        "FILE_STORE_BACKEND": "disabled",
        "USE_IAM_AUTH": "false",
    }
    if any(not _SAFE_ENV_VALUE.fullmatch(value) for value in lines.values()):
        raise ValueError("generated an unsafe environment value")
    return "".join(f"{key}={value}\n" for key, value in lines.items())


def _bootstrap_sql(
    *, runtime_password: str, migrator_password: str, readonly_password: str
) -> str:
    # This file is intentionally host-only and 0600. It is applied with the
    # branch-local bootstrap identity before any Onyx process starts.
    if not all(
        _HEX_PASSWORD.fullmatch(password)
        for password in (runtime_password, migrator_password, readonly_password)
    ):
        raise ValueError("bootstrap SQL only accepts generated hex role passwords")
    template = """DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'skybase_onyx_migrator') THEN
        CREATE ROLE skybase_onyx_migrator LOGIN PASSWORD '__MIGRATOR_PASSWORD__';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'skybase_onyx_runtime') THEN
        CREATE ROLE skybase_onyx_runtime LOGIN PASSWORD '__RUNTIME_PASSWORD__';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'skybase_onyx_kg_ro') THEN
        CREATE ROLE skybase_onyx_kg_ro LOGIN PASSWORD '__READONLY_PASSWORD__';
    END IF;
END $$;
ALTER ROLE skybase_onyx_migrator LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 1 PASSWORD '__MIGRATOR_PASSWORD__';
ALTER ROLE skybase_onyx_runtime LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 12 PASSWORD '__RUNTIME_PASSWORD__';
ALTER ROLE skybase_onyx_kg_ro LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 1 PASSWORD '__READONLY_PASSWORD__';
CREATE SCHEMA IF NOT EXISTS skybase_onyx AUTHORIZATION skybase_onyx_migrator;
ALTER SCHEMA skybase_onyx OWNER TO skybase_onyx_migrator;
REVOKE CREATE ON SCHEMA public FROM skybase_onyx_migrator, skybase_onyx_runtime, skybase_onyx_kg_ro;
REVOKE CREATE ON SCHEMA extensions FROM skybase_onyx_migrator, skybase_onyx_runtime, skybase_onyx_kg_ro;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM skybase_onyx_migrator, skybase_onyx_runtime, skybase_onyx_kg_ro;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM skybase_onyx_migrator, skybase_onyx_runtime, skybase_onyx_kg_ro;
GRANT USAGE ON SCHEMA extensions TO skybase_onyx_migrator, skybase_onyx_runtime, skybase_onyx_kg_ro;
GRANT EXECUTE ON FUNCTION public.gen_random_uuid() TO skybase_onyx_migrator, skybase_onyx_runtime;
"""
    return (
        template.replace("__MIGRATOR_PASSWORD__", migrator_password)
        .replace("__RUNTIME_PASSWORD__", runtime_password)
        .replace("__READONLY_PASSWORD__", readonly_password)
    )


def _post_migrate_grants_sql() -> str:
    """Return schema-local grants applied only after a successful upgrade."""

    readonly_table_grants = "\n".join(
        f"GRANT SELECT ON TABLE {table} TO skybase_onyx_kg_ro;"
        for table in KG_READONLY_TABLE_ALLOWLIST
    )
    readonly_sequence_grants = "\n".join(
        f"GRANT USAGE, SELECT ON SEQUENCE {sequence} TO skybase_onyx_kg_ro;"
        for sequence in KG_READONLY_SEQUENCE_ALLOWLIST
    )
    static_readonly_grants = "\n".join(
        grant for grant in (readonly_table_grants, readonly_sequence_grants) if grant
    )
    return """GRANT USAGE ON SCHEMA skybase_onyx TO skybase_onyx_runtime, skybase_onyx_kg_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA skybase_onyx TO skybase_onyx_runtime;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA skybase_onyx TO skybase_onyx_runtime;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA skybase_onyx FROM skybase_onyx_kg_ro;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA skybase_onyx FROM skybase_onyx_kg_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE skybase_onyx_migrator IN SCHEMA skybase_onyx
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO skybase_onyx_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE skybase_onyx_migrator IN SCHEMA skybase_onyx
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO skybase_onyx_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE skybase_onyx_migrator IN SCHEMA skybase_onyx
    REVOKE ALL ON TABLES FROM skybase_onyx_kg_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE skybase_onyx_migrator IN SCHEMA skybase_onyx
    REVOKE ALL ON SEQUENCES FROM skybase_onyx_kg_ro;
""" + (f"{static_readonly_grants}\n" if static_readonly_grants else "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap-url-file", required=True, type=Path)
    parser.add_argument("--ssl-root-cert", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    connection = parse_bootstrap_url(args.bootstrap_url_file)
    if not args.ssl_root_cert.is_file() or not os.access(args.ssl_root_cert, os.R_OK):
        raise ValueError("--ssl-root-cert must name a readable CA bundle")
    if args.output_dir.exists():
        raise ValueError("--output-dir must not already exist")
    args.output_dir.mkdir(mode=0o700, parents=True)
    os.chmod(args.output_dir, 0o700)

    runtime_password = _password()
    migrator_password = _password()
    readonly_password = _password()
    _write_secret(
        args.output_dir / "runtime.env",
        _env_lines(
            connection,
            role="skybase_onyx_runtime",
            password=runtime_password,
            readonly_password=readonly_password,
            ssl_root_cert=args.ssl_root_cert,
            profile="runtime",
        ),
    )
    _write_secret(
        args.output_dir / "migrator.env",
        _env_lines(
            connection,
            role="skybase_onyx_migrator",
            password=migrator_password,
            readonly_password=readonly_password,
            ssl_root_cert=args.ssl_root_cert,
            profile="migrator",
        ),
    )
    _write_secret(
        args.output_dir / "readonly.env",
        "DB_READONLY_USER=skybase_onyx_kg_ro\n"
        f"DB_READONLY_PASSWORD={readonly_password}\n",
    )
    _write_secret(
        args.output_dir / "bootstrap.sql",
        _bootstrap_sql(
            runtime_password=runtime_password,
            migrator_password=migrator_password,
            readonly_password=readonly_password,
        ),
    )
    _write_secret(
        args.output_dir / "post-migrate-grants.sql", _post_migrate_grants_sql()
    )
    # Deliberately contain no host/role/secret material in stdout.
    print("Rendered private shared-Supabase role files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
