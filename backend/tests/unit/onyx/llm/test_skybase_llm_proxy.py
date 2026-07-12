# file-under-test: backend/onyx/llm/skybase_llm_proxy.py
"""Contract tests for the private Skybase LLM proxy transport."""

from __future__ import annotations

import hashlib
import hmac
import uuid

import httpx
import pytest
from openai import AuthenticationError
from openai import OpenAI

from onyx.db.skybase_shared_supabase import SKYBASE_LLM_PROXY_BASE_URL_ENV
from onyx.llm.constants import LlmProviderNames
from onyx.llm.skybase_llm_proxy import build_skybase_proxy_openai_client
from onyx.llm.skybase_llm_proxy import create_skybase_proxy_canonical_request
from onyx.llm.skybase_llm_proxy import resolve_skybase_llm_proxy_config
from onyx.llm.skybase_llm_proxy import SKYBASE_LLM_HMAC_NEXT_ID_ENV
from onyx.llm.skybase_llm_proxy import SKYBASE_LLM_HMAC_NEXT_KEY_ENV
from onyx.llm.skybase_llm_proxy import SKYBASE_LLM_HMAC_PRIMARY_ID_ENV
from onyx.llm.skybase_llm_proxy import SKYBASE_LLM_HMAC_PRIMARY_KEY_ENV
from onyx.llm.skybase_llm_proxy import SKYBASE_LLM_PROXY_API_KEY_SENTINEL
from onyx.llm.skybase_llm_proxy import SKYBASE_LLM_PROXY_PATH
from onyx.llm.skybase_llm_proxy import SkybaseLlmProxyConfigurationError
from onyx.llm.skybase_llm_proxy import SkybaseLlmProxySigningConfig
from onyx.llm.skybase_llm_proxy import SkybaseLlmProxySigningTransport

_PROXY_BASE = "http://skybase-server.railway.internal/internal/knowledge/openai/v1"
_PRIMARY_ID = "primary-key"
_PRIMARY_KEY = "primary-signing-material"
_NEXT_ID = "next-key"
_NEXT_KEY = "next-signing-material"
_TIMESTAMP = 1_788_000_000
_NONCE = uuid.UUID("7c0e9ec3-9a22-4700-8c68-7ed5ec553a61")
_KNOWN_BODY = b'{"z":1, "a":2}'
_KNOWN_BODY_SHA256 = "1408bb395bc0ec8a11a20a6d6c63dfa7c9e8fc35e3ae38cdfd7e0bd9c982a1a5"
_KNOWN_SIGNATURE = "e33e47bd961a3f66916d2371a1ed2e91ee426169f83ce6c50b4137f81b2e8b09"


def _proxy_environment() -> dict[str, str]:
    return {
        SKYBASE_LLM_HMAC_PRIMARY_ID_ENV: _PRIMARY_ID,
        SKYBASE_LLM_HMAC_PRIMARY_KEY_ENV: _PRIMARY_KEY,
        SKYBASE_LLM_HMAC_NEXT_ID_ENV: _NEXT_ID,
        SKYBASE_LLM_HMAC_NEXT_KEY_ENV: _NEXT_KEY,
    }


def _shared_proxy_environment() -> dict[str, str]:
    environment = _proxy_environment()
    environment.update(
        {
            "SKYBASE_ONYX_SHARED_SUPABASE": "true",
            SKYBASE_LLM_PROXY_BASE_URL_ENV: _PROXY_BASE,
        }
    )
    return environment


def _proxy_config(
    *,
    environment: dict[str, str] | None = None,
) -> SkybaseLlmProxySigningConfig:
    config = resolve_skybase_llm_proxy_config(
        model_provider=LlmProviderNames.OPENAI_COMPATIBLE,
        api_base=_PROXY_BASE,
        api_key=SKYBASE_LLM_PROXY_API_KEY_SENTINEL,
        env=environment or _proxy_environment(),
    )
    assert config is not None
    return config


def _valid_chat_completion_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-skybase-test",
            "created": _TIMESTAMP,
            "model": "knowledge-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 5,
                "total_tokens": 8,
            },
        },
    )


def _has_valid_signature(request: httpx.Request) -> bool:
    body_sha256 = hashlib.sha256(request.content).hexdigest()
    canonical = create_skybase_proxy_canonical_request(
        key_id=request.headers.get("X-Skybase-Key-Id", ""),
        timestamp=request.headers.get("X-Skybase-Timestamp", ""),
        nonce=request.headers.get("X-Skybase-Nonce", ""),
        body_sha256=body_sha256,
    )
    expected_signature = hmac.new(
        _PRIMARY_KEY.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return (
        request.method == "POST"
        and request.url.raw_path == SKYBASE_LLM_PROXY_PATH.encode("ascii")
        and request.headers.get("X-Skybase-Key-Id") == _PRIMARY_ID
        and request.headers.get("X-Skybase-Body-Sha256") == body_sha256
        and hmac.compare_digest(
            request.headers.get("X-Skybase-Signature", ""), expected_signature
        )
    )


def test_canonical_request_matches_the_frozen_typescript_vector() -> None:
    canonical = create_skybase_proxy_canonical_request(
        key_id=_PRIMARY_ID,
        timestamp=str(_TIMESTAMP),
        nonce=str(_NONCE),
        body_sha256=_KNOWN_BODY_SHA256,
    )

    assert hashlib.sha256(_KNOWN_BODY).hexdigest() == _KNOWN_BODY_SHA256
    assert canonical == (
        "POST\n"
        "/internal/knowledge/openai/v1/chat/completions\n"
        "primary-key\n"
        "1788000000\n"
        "7c0e9ec3-9a22-4700-8c68-7ed5ec553a61\n"
        "1408bb395bc0ec8a11a20a6d6c63dfa7c9e8fc35e3ae38cdfd7e0bd9c982a1a5"
    )
    assert (
        hmac.new(
            _PRIMARY_KEY.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        == _KNOWN_SIGNATURE
    )


def test_openai_client_signs_the_actual_serialized_request_bytes() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _valid_chat_completion_response()

    client = build_skybase_proxy_openai_client(
        _proxy_config(),
        timeout=10.0,
        inner_transport=httpx.MockTransport(handler),
        clock=lambda: float(_TIMESTAMP),
        nonce_factory=lambda: _NONCE,
    )
    try:
        response = client.chat.completions.create(
            model="knowledge-model",
            messages=[{"role": "user", "content": "hello"}],
        )
    finally:
        client.close()

    assert response.id == "chatcmpl-skybase-test"
    assert len(captured) == 1
    request = captured[0]
    body_sha256 = hashlib.sha256(request.content).hexdigest()
    canonical = create_skybase_proxy_canonical_request(
        key_id=_PRIMARY_ID,
        timestamp=str(_TIMESTAMP),
        nonce=str(_NONCE),
        body_sha256=body_sha256,
    )
    expected_signature = hmac.new(
        _PRIMARY_KEY.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    assert request.url.raw_path == SKYBASE_LLM_PROXY_PATH.encode("ascii")
    assert request.headers["X-Skybase-Key-Id"] == _PRIMARY_ID
    assert request.headers["X-Skybase-Timestamp"] == str(_TIMESTAMP)
    assert request.headers["X-Skybase-Nonce"] == str(_NONCE)
    assert request.headers["X-Skybase-Body-Sha256"] == body_sha256
    assert request.headers["X-Skybase-Signature"] == expected_signature


def test_transport_replaces_caller_supplied_signature_headers() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = SkybaseLlmProxySigningTransport(
        _proxy_config(),
        inner_transport=httpx.MockTransport(handler),
        clock=lambda: float(_TIMESTAMP),
        nonce_factory=lambda: _NONCE,
    )
    with httpx.Client(transport=transport) as client:
        response = client.post(
            f"{_PROXY_BASE}/chat/completions",
            content=_KNOWN_BODY,
            headers={
                "X-Skybase-Key-Id": "attacker-key",
                "X-Skybase-Timestamp": "0",
                "X-Skybase-Nonce": str(uuid.uuid4()),
                "X-Skybase-Body-Sha256": "0" * 64,
                "X-Skybase-Signature": "0" * 64,
                "Authorization": "Bearer provider-secret-must-not-leave-onyx",
            },
        )

    assert response.status_code == 200
    assert len(captured) == 1
    assert (
        captured[0].headers["Authorization"]
        == f"Bearer {SKYBASE_LLM_PROXY_API_KEY_SENTINEL}"
    )
    assert _has_valid_signature(captured[0])


def test_direct_openai_request_cannot_authenticate_to_the_proxy() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _has_valid_signature(request):
            return _valid_chat_completion_response()
        return httpx.Response(
            401,
            json={
                "error": {
                    "message": "invalid proxy request",
                    "type": "authentication_error",
                }
            },
        )

    transport = httpx.MockTransport(handler)
    signed_client = build_skybase_proxy_openai_client(
        _proxy_config(),
        timeout=10.0,
        inner_transport=transport,
        clock=lambda: float(_TIMESTAMP),
        nonce_factory=lambda: _NONCE,
    )
    direct_client = OpenAI(
        api_key=SKYBASE_LLM_PROXY_API_KEY_SENTINEL,
        base_url=_PROXY_BASE,
        http_client=httpx.Client(transport=transport, trust_env=False),
        max_retries=0,
    )
    try:
        signed_client.chat.completions.create(
            model="knowledge-model",
            messages=[{"role": "user", "content": "hello"}],
        )
        with pytest.raises(AuthenticationError):
            direct_client.chat.completions.create(
                model="knowledge-model",
                messages=[{"role": "user", "content": "hello"}],
            )
    finally:
        signed_client.close()
        direct_client.close()

    assert len(captured) == 2
    assert _has_valid_signature(captured[0])
    assert not _has_valid_signature(captured[1])


@pytest.mark.parametrize(
    ("api_base", "api_key", "model_provider", "environment"),
    [
        (
            _PROXY_BASE,
            SKYBASE_LLM_PROXY_API_KEY_SENTINEL,
            LlmProviderNames.OPENAI_COMPATIBLE,
            {},
        ),
        (
            _PROXY_BASE,
            "not-the-sentinel",
            LlmProviderNames.OPENAI_COMPATIBLE,
            _proxy_environment(),
        ),
        (
            _PROXY_BASE,
            SKYBASE_LLM_PROXY_API_KEY_SENTINEL,
            LlmProviderNames.OPENAI,
            _proxy_environment(),
        ),
        (
            "https://api.openai.com/v1",
            SKYBASE_LLM_PROXY_API_KEY_SENTINEL,
            LlmProviderNames.OPENAI_COMPATIBLE,
            _proxy_environment(),
        ),
        (
            "http://skybase-server.railway.internal/internal/knowledge/openai/v1?unexpected=true",
            SKYBASE_LLM_PROXY_API_KEY_SENTINEL,
            LlmProviderNames.OPENAI_COMPATIBLE,
            _proxy_environment(),
        ),
        (
            _PROXY_BASE,
            SKYBASE_LLM_PROXY_API_KEY_SENTINEL,
            LlmProviderNames.OPENAI_COMPATIBLE,
            {
                SKYBASE_LLM_HMAC_PRIMARY_ID_ENV: _PRIMARY_ID,
                SKYBASE_LLM_HMAC_PRIMARY_KEY_ENV: _PRIMARY_KEY,
                SKYBASE_LLM_HMAC_NEXT_ID_ENV: _NEXT_ID,
            },
        ),
        (
            _PROXY_BASE,
            SKYBASE_LLM_PROXY_API_KEY_SENTINEL,
            LlmProviderNames.OPENAI_COMPATIBLE,
            {
                SKYBASE_LLM_HMAC_PRIMARY_ID_ENV: _PRIMARY_ID,
                SKYBASE_LLM_HMAC_PRIMARY_KEY_ENV: " ",
            },
        ),
    ],
)
def test_proxy_configuration_fails_closed_before_request_construction(
    api_base: str,
    api_key: str,
    model_provider: LlmProviderNames,
    environment: dict[str, str],
) -> None:
    with pytest.raises(SkybaseLlmProxyConfigurationError):
        resolve_skybase_llm_proxy_config(
            model_provider=model_provider,
            api_base=api_base,
            api_key=api_key,
            env=environment,
        )


def test_non_proxy_provider_configuration_stays_unchanged() -> None:
    assert (
        resolve_skybase_llm_proxy_config(
            model_provider=LlmProviderNames.OPENAI_COMPATIBLE,
            api_base="https://normal-proxy.example.test/v1",
            api_key="ordinary-provider-key",
            env=_proxy_environment(),
        )
        is None
    )


def test_shared_profile_requires_the_exact_deployment_owned_proxy_base() -> None:
    config = resolve_skybase_llm_proxy_config(
        model_provider=LlmProviderNames.OPENAI_COMPATIBLE,
        api_base=_PROXY_BASE,
        api_key=SKYBASE_LLM_PROXY_API_KEY_SENTINEL,
        env=_shared_proxy_environment(),
    )
    assert config is not None
    assert config.api_base == _PROXY_BASE

    with pytest.raises(SkybaseLlmProxyConfigurationError, match="deployment-owned"):
        resolve_skybase_llm_proxy_config(
            model_provider=LlmProviderNames.OPENAI_COMPATIBLE,
            api_base="http://attacker.railway.internal/internal/knowledge/openai/v1",
            api_key=SKYBASE_LLM_PROXY_API_KEY_SENTINEL,
            env=_shared_proxy_environment(),
        )

    with pytest.raises(SkybaseLlmProxyConfigurationError, match="deployment-owned"):
        resolve_skybase_llm_proxy_config(
            model_provider=LlmProviderNames.OPENAI,
            api_base="https://api.openai.com/v1",
            api_key="upstream-provider-key",
            env=_shared_proxy_environment(),
        )


def test_hmac_rotation_requires_explicit_primary_promotion_and_old_key_removal() -> (
    None
):
    overlap_config = _proxy_config()
    assert overlap_config.primary_key_id == _PRIMARY_ID
    assert overlap_config.next_key_id == _NEXT_ID

    promoted_config = _proxy_config(
        environment={
            SKYBASE_LLM_HMAC_PRIMARY_ID_ENV: _NEXT_ID,
            SKYBASE_LLM_HMAC_PRIMARY_KEY_ENV: _NEXT_KEY,
        }
    )
    assert promoted_config.primary_key_id == _NEXT_ID
    assert promoted_config.next_key_id is None


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", f"{_PROXY_BASE}/chat/completions"),
        ("POST", f"{_PROXY_BASE}/responses"),
        (
            "POST",
            "http://skybase-server.railway.internal:8080/internal/knowledge/openai/v1/chat/completions",
        ),
        ("POST", "https://api.openai.com/v1/chat/completions"),
    ],
)
def test_signing_transport_refuses_an_unexpected_request_before_egress(
    method: str, url: str
) -> None:
    inner_called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal inner_called
        inner_called = True
        return httpx.Response(200)

    transport = SkybaseLlmProxySigningTransport(
        _proxy_config(),
        inner_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SkybaseLlmProxyConfigurationError):
        transport.handle_request(httpx.Request(method, url, content=b"{}"))

    assert not inner_called


def test_signing_transport_rejects_a_streamed_body_before_egress() -> None:
    class OneShotStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"{}"

        def close(self) -> None:
            return None

    inner_called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal inner_called
        inner_called = True
        return httpx.Response(200)

    transport = SkybaseLlmProxySigningTransport(
        _proxy_config(),
        inner_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SkybaseLlmProxyConfigurationError, match="complete byte"):
        transport.handle_request(
            httpx.Request(
                "POST",
                f"{_PROXY_BASE}/chat/completions",
                stream=OneShotStream(),
            )
        )

    assert not inner_called
