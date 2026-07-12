# file-under-test: backend/onyx/shared_supabase_health.py
"""Executable isolated-process coverage for the shared health entrypoint."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND_ROOT = REPO_ROOT / "backend"


def _shared_profile_env(tmp_path: Path) -> dict[str, str]:
    root_cert = tmp_path / "branch-ca.pem"
    root_cert.write_text("test-only-ca", encoding="utf-8")
    return {
        "SKYBASE_ONYX_SHARED_SUPABASE": "true",
        "SKYBASE_ONYX_ROLE_PROFILE": "runtime",
        "SKYBASE_ONYX_DISPOSABLE_BRANCH": "true",
        "MULTI_TENANT": "false",
        "POSTGRES_USER": "skybase_onyx_runtime",
        "POSTGRES_PASSWORD": "a" * 64,
        "POSTGRES_HOST": "db.example.test",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "postgres",
        "POSTGRES_DEFAULT_SCHEMA": "skybase_onyx",
        "POSTGRES_EXTENSION_SCHEMA": "extensions",
        "POSTGRES_SSLMODE": "verify-full",
        "POSTGRES_SSLROOTCERT": str(root_cert),
        "POSTGRES_USE_NULL_POOL": "false",
        "POSTGRES_API_SERVER_POOL_SIZE": "2",
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
        "PYTHONPATH": str(BACKEND_ROOT),
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
    }


def _run_profile_process(
    tmp_path: Path, source: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=BACKEND_ROOT,
        env=_shared_profile_env(tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )


def test_shared_profile_boots_only_the_health_application(tmp_path: Path) -> None:
    result = _run_profile_process(
        tmp_path,
        "\n".join(
            (
                "import sys",
                "from starlette.testclient import TestClient",
                "from onyx.shared_supabase_health import app",
                "with TestClient(app) as client:",
                "    health = client.get('/health')",
                "    normalized = client.get('/health/')",
                "    disabled = client.post('/auth/login')",
                "    invalid_health_method = client.post('/health')",
                "assert health.status_code == normalized.status_code == 200",
                "assert health.json() == {'success': True, 'message': 'ok', 'data': None}",
                "assert disabled.status_code == 503",
                "assert invalid_health_method.status_code == 503",
                "assert not any(name.startswith('onyx.auth') for name in sys.modules)",
                "assert not any(name.startswith('onyx.background.celery') for name in sys.modules)",
                "assert not any(name.startswith('onyx.server.manage.voice') for name in sys.modules)",
                "assert 'onyx.main' not in sys.modules",
            )
        ),
    )
    assert result.returncode == 0, result.stderr


def test_shared_profile_denies_websocket_scopes_in_clean_process(
    tmp_path: Path,
) -> None:
    result = _run_profile_process(
        tmp_path,
        "\n".join(
            (
                "from starlette.testclient import TestClient",
                "from starlette.websockets import WebSocketDisconnect",
                "from onyx.shared_supabase_health import app",
                "with TestClient(app) as client:",
                "    try:",
                "        with client.websocket_connect('/manage/voice/stream'):",
                "            raise AssertionError('WebSocket unexpectedly connected')",
                "    except WebSocketDisconnect as exc:",
                "        assert exc.code == 1008",
            )
        ),
    )
    assert result.returncode == 0, result.stderr


def test_shared_profile_allows_only_literal_health_paths(tmp_path: Path) -> None:
    result = _run_profile_process(
        tmp_path,
        "\n".join(
            (
                "import asyncio",
                "from onyx.shared_supabase_health import app",
                "async def status_for(path):",
                "    messages = []",
                "    async def receive():",
                "        return {'type': 'http.request', 'body': b'', 'more_body': False}",
                "    async def send(message):",
                "        messages.append(message)",
                "    await app({'type': 'http', 'method': 'GET', 'path': path}, receive, send)",
                "    return messages[0]['status']",
                "assert asyncio.run(status_for('/health')) == 200",
                "assert asyncio.run(status_for('/health/')) == 200",
                "assert asyncio.run(status_for('//health')) == 503",
                "assert asyncio.run(status_for('///health')) == 503",
                "assert asyncio.run(status_for('///health///')) == 503",
            )
        ),
    )

    assert result.returncode == 0, result.stderr


def test_health_entrypoint_rejects_a_non_shared_profile(tmp_path: Path) -> None:
    environment = _shared_profile_env(tmp_path)
    environment["SKYBASE_ONYX_SHARED_SUPABASE"] = "false"
    result = subprocess.run(
        [sys.executable, "-c", "import onyx.shared_supabase_health"],
        cwd=BACKEND_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "requires SKYBASE_ONYX_SHARED_SUPABASE=true" in result.stderr


def test_native_main_rejects_the_shared_profile_before_native_imports(
    tmp_path: Path,
) -> None:
    result = _run_profile_process(tmp_path, "import onyx.main")

    assert result.returncode != 0
    assert "onyx.shared_supabase_health:app, not onyx.main:app" in result.stderr
    assert "Celery app 'client'" not in result.stderr
