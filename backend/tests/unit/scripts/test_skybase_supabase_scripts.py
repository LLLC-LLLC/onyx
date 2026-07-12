# file-under-test: scripts/render-skybase-supabase-env.py
"""Contract tests for the host-only shared-Supabase launch tooling."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RENDERER = REPO_ROOT / "scripts" / "render-skybase-supabase-env.py"
LAUNCHER = REPO_ROOT / "scripts" / "run-skybase-ce-alembic.sh"


def _write_0600(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def test_renderer_emits_private_role_files_without_stdout_secrets(
    tmp_path: Path,
) -> None:
    bootstrap_url = tmp_path / "bootstrap-url"
    _write_0600(
        bootstrap_url,
        "postgresql://postgres:abc%2Fdef@branch.example.test:5432/postgres\n",
    )
    root_cert = tmp_path / "ca.pem"
    root_cert.write_text("test-ca", encoding="utf-8")
    output_dir = tmp_path / "rendered"

    result = subprocess.run(
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

    assert "abc" not in result.stdout
    assert "secret" not in result.stdout.lower()
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
    passwords = re.findall(r"(?:POSTGRES|DB_READONLY)_PASSWORD=([0-9a-f]+)", runtime)
    assert passwords and all(
        re.fullmatch(r"[0-9a-f]{64}", value) for value in passwords
    )


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
