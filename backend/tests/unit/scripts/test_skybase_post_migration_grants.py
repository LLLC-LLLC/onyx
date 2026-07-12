# file-under-test: scripts/apply-skybase-ce-post-migration-grants.sh
"""Regression coverage for sealed post-migration PostgreSQL invocations."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
RENDERER = REPO_ROOT / "scripts" / "render-skybase-supabase-env.py"
GRANTS_LAUNCHER = REPO_ROOT / "scripts" / "apply-skybase-ce-post-migration-grants.sh"


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


def test_post_grants_psql_receives_only_reviewed_environment(tmp_path: Path) -> None:
    output_dir = _render_private_files(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    psql_observation = tmp_path / "psql-observation"
    (bin_dir / "alembic").write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        '[[ -z "${OPENAI_API_KEY+x}" ]]\n'
        '[[ -z "${PGSERVICE+x}" ]]\n'
        '[[ -z "${PGOPTIONS+x}" ]]\n'
        "printf 'deadbeef (head)\\n'\n",
        encoding="utf-8",
    )
    (bin_dir / "psql").write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "printf '%s|%s|%s\\n' "
        '"${OPENAI_API_KEY:-<unset>}" '
        '"${PGSERVICE:-<unset>}" '
        '"${PGOPTIONS:-<unset>}" >> '
        f"{shlex.quote(str(psql_observation))}\n"
        '[[ "${PGUSER}" == "skybase_onyx_migrator" ]]\n'
        '[[ "${PGDATABASE}" == "postgres" ]]\n'
        '[[ "${PGSSLMODE}" == "verify-full" ]]\n'
        'if [[ "$*" == *"SELECT version_num"* ]]; then\n'
        "  printf 'deadbeef\\n'\n"
        "fi\n",
        encoding="utf-8",
    )
    for executable in bin_dir.iterdir():
        executable.chmod(0o755)

    result = subprocess.run(
        [
            str(GRANTS_LAUNCHER),
            "--env-file",
            str(output_dir / "migrator.env"),
            "--grants-file",
            str(output_dir / "post-migrate-grants.sql"),
        ],
        cwd=REPO_ROOT,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "OPENAI_API_KEY": "host-provider-secret",
            "PGSERVICE": "host-service",
            "PGOPTIONS": "-c search_path=public",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Applied reviewed shared-Supabase post-migration grants." in result.stdout
    assert psql_observation.read_text(encoding="utf-8").splitlines() == [
        "<unset>|<unset>|<unset>",
        "<unset>|<unset>|<unset>",
    ]
