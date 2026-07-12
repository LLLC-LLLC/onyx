# file-under-test: scripts/render-skybase-supabase-env.py
"""Contract tests for the host-only shared-Supabase launch tooling."""

from __future__ import annotations

import os
import re
import runpy
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RENDERER = REPO_ROOT / "scripts" / "render-skybase-supabase-env.py"
LAUNCHER = REPO_ROOT / "scripts" / "run-skybase-ce-alembic.sh"
POST_MIGRATION_GRANTS_LAUNCHER = (
    REPO_ROOT / "scripts" / "apply-skybase-ce-post-migration-grants.sh"
)


def _write_0600(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _render_private_files(tmp_path: Path) -> Path:
    bootstrap_url = tmp_path / "bootstrap-url"
    _write_0600(
        bootstrap_url,
        "postgresql://postgres:abc%2Fdef@branch.example.test:5432/postgres\n",
    )
    root_cert = tmp_path / "ca.pem"
    root_cert.write_text("test-ca", encoding="utf-8")
    output_dir = tmp_path / "rendered"
    subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "--bootstrap-url-file",
            str(bootstrap_url),
            "--ssl-root-cert",
            str(root_cert),
            "--output-dir",
            str(output_dir),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return output_dir


def test_renderer_emits_private_role_files_without_stdout_secrets(
    tmp_path: Path,
) -> None:
    output_dir = _render_private_files(tmp_path)

    for filename in (
        "runtime.env",
        "migrator.env",
        "readonly.env",
        "bootstrap.sql",
        "post-migrate-grants.sql",
    ):
        path = output_dir / filename
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    runtime = (output_dir / "runtime.env").read_text(encoding="utf-8")
    migrator = (output_dir / "migrator.env").read_text(encoding="utf-8")
    assert "POSTGRES_USER=skybase_onyx_runtime" in runtime
    assert "POSTGRES_USER=skybase_onyx_migrator" in migrator
    assert "FILE_STORE_BACKEND=disabled" in runtime
    assert "IN SCHEMA skybase_onyx" in (
        output_dir / "post-migrate-grants.sql"
    ).read_text(encoding="utf-8")
    bootstrap_sql = (output_dir / "bootstrap.sql").read_text(encoding="utf-8")
    assert (
        "skybase_onyx_migrator LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 1 PASSWORD '"
        in bootstrap_sql
    )
    assert (
        "skybase_onyx_runtime LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 12 PASSWORD '"
        in bootstrap_sql
    )
    assert (
        "skybase_onyx_kg_ro LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 1 PASSWORD '"
        in bootstrap_sql
    )
    assert "GRANT USAGE ON SCHEMA public" not in bootstrap_sql
    assert "ALTER SCHEMA skybase_onyx OWNER TO skybase_onyx_migrator" in bootstrap_sql
    assert (
        "GRANT EXECUTE ON FUNCTION public.gen_random_uuid() TO "
        "skybase_onyx_migrator, skybase_onyx_runtime" in bootstrap_sql
    )
    passwords = re.findall(r"(?:POSTGRES|DB_READONLY)_PASSWORD=([0-9a-f]+)", runtime)
    assert passwords and all(
        re.fullmatch(r"[0-9a-f]{64}", value) for value in passwords
    )


def test_renderer_keeps_the_v1_readonly_data_allowlist_empty(tmp_path: Path) -> None:
    renderer_module = runpy.run_path(str(RENDERER))
    assert renderer_module["KG_READONLY_TABLE_ALLOWLIST"] == ()
    assert renderer_module["KG_READONLY_SEQUENCE_ALLOWLIST"] == ()

    grants = (_render_private_files(tmp_path) / "post-migrate-grants.sql").read_text(
        encoding="utf-8"
    )
    assert "GRANT SELECT ON ALL TABLES" not in grants
    assert "GRANT USAGE, SELECT ON ALL SEQUENCES" not in grants
    assert (
        re.findall(r"GRANT SELECT ON TABLE ([a-z_]+) TO skybase_onyx_kg_ro", grants)
        == []
    )
    assert (
        "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA skybase_onyx FROM skybase_onyx_kg_ro"
        in grants
    )
    assert "REVOKE ALL ON TABLES FROM skybase_onyx_kg_ro" in grants
    for sensitive_table in ("api_key", "credential", "oauth_account", "user"):
        assert f"ON TABLE {sensitive_table} TO skybase_onyx_kg_ro" not in grants


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://postgres:password@branch.example.test/postgres",
        "postgresql://postgres:password@branch.example.test:5432/postgres?sslmode=require",
        "postgresql://postgres:password@branch.example.test:5432/first/second",
        "postgresql://postgres:password@branch.example.test:0/postgres",
        "postgresql://postgres:bad%zz@branch.example.test:5432/postgres",
        "mysql://postgres:password@branch.example.test:5432/postgres",
    ],
)
def test_renderer_rejects_ambiguous_bootstrap_urls(tmp_path: Path, url: str) -> None:
    bootstrap_url = tmp_path / "bootstrap-url"
    _write_0600(bootstrap_url, url)
    root_cert = tmp_path / "ca.pem"
    root_cert.write_text("test-ca", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "--bootstrap-url-file",
            str(bootstrap_url),
            "--ssl-root-cert",
            str(root_cert),
            "--output-dir",
            str(tmp_path / "rendered"),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0


def test_launcher_rejects_any_arbitrary_alembic_arguments() -> None:
    result = subprocess.run(
        [str(LAUNCHER), "downgrade", "base"],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 64
    assert "usage:" in result.stderr


def test_launcher_exports_the_private_profile_to_alembic(tmp_path: Path) -> None:
    env_file = tmp_path / "migrator.env"
    _write_0600(
        env_file,
        "\n".join(
            (
                "SKYBASE_ONYX_SHARED_SUPABASE=true",
                "SKYBASE_ONYX_DISPOSABLE_BRANCH=true",
                "SKYBASE_ONYX_ROLE_PROFILE=migrator",
                "POSTGRES_USER=skybase_onyx_migrator",
            )
        )
        + "\n",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    alembic = bin_dir / "alembic"
    alembic.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$SKYBASE_ONYX_SHARED_SUPABASE,$SKYBASE_ONYX_ROLE_PROFILE,$POSTGRES_USER\"\n",
        encoding="utf-8",
    )
    alembic.chmod(0o755)

    result = subprocess.run(
        [str(LAUNCHER), "--env-file", str(env_file)],
        text=True,
        capture_output=True,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
    assert "true,migrator,skybase_onyx_migrator" in result.stdout


def test_launcher_rejects_shell_syntax_in_private_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "migrator.env"
    _write_0600(
        env_file,
        "SKYBASE_ONYX_SHARED_SUPABASE=true\n"
        "SKYBASE_ONYX_DISPOSABLE_BRANCH=true\n"
        "SKYBASE_ONYX_ROLE_PROFILE=migrator\n"
        "POSTGRES_USER=skybase_onyx_migrator\n"
        "UNSAFE=$(touch should-not-run)\n",
    )
    result = subprocess.run(
        [str(LAUNCHER), "--env-file", str(env_file)],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 65
    assert "unexpected key UNSAFE" in result.stderr
    assert not (tmp_path / "should-not-run").exists()


def test_post_migration_grants_launcher_allows_only_reviewed_grants_after_head(
    tmp_path: Path,
) -> None:
    output_dir = _render_private_files(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "alembic").write_text(
        "#!/usr/bin/env bash\nprintf 'deadbeef (head)\\n'\n",
        encoding="utf-8",
    )
    (bin_dir / "psql").write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "if [[ \"$*\" == *'SELECT version_num'* ]]; then\n"
        "  printf 'deadbeef\\n'\n"
        "else\n"
        "  [[ \"${PGUSER}\" == 'skybase_onyx_migrator' ]]\n"
        "  [[ \"${PGSSLMODE}\" == 'verify-full' ]]\n"
        "fi\n",
        encoding="utf-8",
    )
    for executable in bin_dir.iterdir():
        executable.chmod(0o755)

    result = subprocess.run(
        [
            str(POST_MIGRATION_GRANTS_LAUNCHER),
            "--env-file",
            str(output_dir / "migrator.env"),
            "--grants-file",
            str(output_dir / "post-migrate-grants.sql"),
        ],
        text=True,
        capture_output=True,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    assert "Applied reviewed shared-Supabase post-migration grants." in result.stdout


def test_post_migration_grants_launcher_refuses_altered_sql(tmp_path: Path) -> None:
    output_dir = _render_private_files(tmp_path)
    grants_file = output_dir / "post-migrate-grants.sql"
    grants_file.write_text("SELECT 1;\n", encoding="utf-8")
    grants_file.chmod(0o600)

    result = subprocess.run(
        [
            str(POST_MIGRATION_GRANTS_LAUNCHER),
            "--env-file",
            str(output_dir / "migrator.env"),
            "--grants-file",
            str(grants_file),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 65
    assert "does not match the reviewed renderer output" in result.stderr
