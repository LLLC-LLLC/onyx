"""Fail-closed HMAC transport for Skybase's private LLM proxy.

The provider row stores only a private Railway base URL and the literal
``skybase-private-proxy`` sentinel. Signing material remains in the Railway
service environment and is applied after the OpenAI SDK has serialized the
exact request bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
import uuid
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field

import httpx
from openai import OpenAI

from onyx.db.skybase_shared_supabase import get_shared_llm_proxy_base
from onyx.db.skybase_shared_supabase import is_shared_supabase_profile
from onyx.db.skybase_shared_supabase import SharedSupabaseContractError
from onyx.db.skybase_shared_supabase import SKYBASE_LLM_PROXY_API_KEY_SENTINEL
from onyx.db.skybase_shared_supabase import SKYBASE_LLM_PROXY_BASE_PATH
from onyx.llm.constants import LlmProviderNames

SKYBASE_LLM_PROXY_PATH = f"{SKYBASE_LLM_PROXY_BASE_PATH}/chat/completions"

SKYBASE_LLM_HMAC_PRIMARY_ID_ENV = "SKYBASE_LLM_HMAC_PRIMARY_ID"
SKYBASE_LLM_HMAC_PRIMARY_KEY_ENV = "SKYBASE_LLM_HMAC_PRIMARY_KEY"
SKYBASE_LLM_HMAC_NEXT_ID_ENV = "SKYBASE_LLM_HMAC_NEXT_ID"
SKYBASE_LLM_HMAC_NEXT_KEY_ENV = "SKYBASE_LLM_HMAC_NEXT_KEY"

_KEY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SIGNATURE_HEADER_NAMES = (
    "X-Skybase-Key-Id",
    "X-Skybase-Timestamp",
    "X-Skybase-Nonce",
    "X-Skybase-Body-Sha256",
    "X-Skybase-Signature",
)


class SkybaseLlmProxyConfigurationError(RuntimeError):
    """Raised before an untrusted proxy configuration can make a request."""


@dataclass(frozen=True)
class SkybaseLlmProxySigningConfig:
    """In-memory configuration for one trusted Skybase proxy destination."""

    api_base: str
    host: str
    port: int | None
    scheme: str
    primary_key_id: str
    primary_key: str = field(repr=False)
    next_key_id: str | None = None


def create_skybase_proxy_canonical_request(
    *,
    key_id: str,
    timestamp: str,
    nonce: str,
    body_sha256: str,
) -> str:
    """Build the canonical string shared with the Skybase proxy verifier."""

    return "\n".join(
        (
            "POST",
            SKYBASE_LLM_PROXY_PATH,
            key_id,
            timestamp,
            nonce,
            body_sha256,
        )
    )


def resolve_skybase_llm_proxy_config(
    *,
    model_provider: str,
    api_base: str | None,
    api_key: str | None,
    env: Mapping[str, str] | None = None,
) -> SkybaseLlmProxySigningConfig | None:
    """Return trusted proxy signing config or preserve a normal provider path.

    This intentionally recognizes one exact private route. A caller cannot use
    the sentinel against another endpoint or use a different provider/key with
    the Skybase route.
    """

    source = os.environ if env is None else env
    if is_shared_supabase_profile(source):
        try:
            trusted_base = get_shared_llm_proxy_base(source)
        except SharedSupabaseContractError as exc:
            raise SkybaseLlmProxyConfigurationError(str(exc)) from exc
        if api_base != trusted_base:
            raise SkybaseLlmProxyConfigurationError(
                "The shared-Supabase profile requires the deployment-owned private proxy base URL."
            )
        proxy_url = _parse_exact_proxy_url(trusted_base)
        if proxy_url is None:
            raise SkybaseLlmProxyConfigurationError(
                "The deployment-owned private proxy base URL is invalid."
            )
    else:
        proxy_url = _parse_exact_proxy_url(api_base)
        looks_like_proxy = _looks_like_skybase_proxy_url(api_base)
        if proxy_url is None:
            if api_key == SKYBASE_LLM_PROXY_API_KEY_SENTINEL:
                raise SkybaseLlmProxyConfigurationError(
                    "The Skybase LLM proxy sentinel requires the exact private proxy base URL."
                )
            if looks_like_proxy:
                raise SkybaseLlmProxyConfigurationError(
                    "The Skybase LLM proxy base URL must be the exact private v1 route."
                )
            return None

    if model_provider != LlmProviderNames.OPENAI_COMPATIBLE:
        raise SkybaseLlmProxyConfigurationError(
            "The Skybase LLM proxy requires the openai_compatible provider."
        )
    if api_key != SKYBASE_LLM_PROXY_API_KEY_SENTINEL:
        raise SkybaseLlmProxyConfigurationError(
            "The Skybase LLM proxy requires its literal non-secret API-key sentinel."
        )

    primary_id = source.get(SKYBASE_LLM_HMAC_PRIMARY_ID_ENV)
    primary_key = source.get(SKYBASE_LLM_HMAC_PRIMARY_KEY_ENV)
    next_id = source.get(SKYBASE_LLM_HMAC_NEXT_ID_ENV)
    next_key = source.get(SKYBASE_LLM_HMAC_NEXT_KEY_ENV)
    if primary_id is None or primary_key is None:
        raise SkybaseLlmProxyConfigurationError(
            "Skybase LLM proxy HMAC environment is incomplete or invalid."
        )
    has_next_id = _has_nonempty(next_id)
    has_next_key = _has_nonempty(next_key)
    if (
        not _has_nonempty(primary_id)
        or not _has_nonempty(primary_key)
        or not _KEY_ID_PATTERN.fullmatch(primary_id)
        or has_next_id != has_next_key
        or (next_id is not None and not _KEY_ID_PATTERN.fullmatch(next_id))
        or primary_id == next_id
    ):
        raise SkybaseLlmProxyConfigurationError(
            "Skybase LLM proxy HMAC environment is incomplete or invalid."
        )

    # The signer always uses primary. During key rotation, Skybase accepts both
    # keys, then the operator promotes next to primary and removes the old key.
    return SkybaseLlmProxySigningConfig(
        api_base=str(proxy_url),
        host=proxy_url.host,
        port=proxy_url.port,
        scheme=proxy_url.scheme,
        primary_key_id=primary_id,
        primary_key=primary_key,
        next_key_id=next_id,
    )


class SkybaseLlmProxySigningTransport(httpx.BaseTransport):
    """Sign the final OpenAI SDK request immediately before it is sent."""

    def __init__(
        self,
        config: SkybaseLlmProxySigningConfig,
        *,
        inner_transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self._config = config
        self._inner_transport = inner_transport or httpx.HTTPTransport(
            retries=0,
            trust_env=False,
        )
        self._clock = clock
        self._nonce_factory = nonce_factory

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._assert_exact_proxy_request(request)
        try:
            body = request.content
        except (httpx.RequestNotRead, httpx.StreamConsumed) as exc:
            raise SkybaseLlmProxyConfigurationError(
                "Skybase LLM proxy requires a complete byte request body."
            ) from exc
        if not isinstance(body, bytes):
            raise SkybaseLlmProxyConfigurationError(
                "Skybase LLM proxy requires a complete byte request body."
            )

        timestamp_value = int(self._clock())
        if timestamp_value < 0:
            raise SkybaseLlmProxyConfigurationError(
                "Skybase LLM proxy clock must produce Unix seconds."
            )
        timestamp = str(timestamp_value)
        nonce = str(self._nonce_factory())
        body_sha256 = hashlib.sha256(body).hexdigest()
        canonical = create_skybase_proxy_canonical_request(
            key_id=self._config.primary_key_id,
            timestamp=timestamp,
            nonce=nonce,
            body_sha256=body_sha256,
        )
        signature = hmac.new(
            self._config.primary_key.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        for header_name in _SIGNATURE_HEADER_NAMES:
            request.headers.pop(header_name, None)
        request.headers["Authorization"] = (
            f"Bearer {SKYBASE_LLM_PROXY_API_KEY_SENTINEL}"
        )
        request.headers["X-Skybase-Key-Id"] = self._config.primary_key_id
        request.headers["X-Skybase-Timestamp"] = timestamp
        request.headers["X-Skybase-Nonce"] = nonce
        request.headers["X-Skybase-Body-Sha256"] = body_sha256
        request.headers["X-Skybase-Signature"] = signature
        return self._inner_transport.handle_request(request)

    def close(self) -> None:
        self._inner_transport.close()

    def _assert_exact_proxy_request(self, request: httpx.Request) -> None:
        if (
            request.method != "POST"
            or request.url.scheme != self._config.scheme
            or request.url.host != self._config.host
            or request.url.port != self._config.port
            or request.url.raw_path != SKYBASE_LLM_PROXY_PATH.encode("ascii")
        ):
            raise SkybaseLlmProxyConfigurationError(
                "Skybase LLM proxy signer refuses an unexpected outbound request."
            )


def build_skybase_proxy_openai_client(
    config: SkybaseLlmProxySigningConfig,
    timeout: float,
    *,
    inner_transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.time,
    nonce_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> OpenAI:
    """Build one non-retrying OpenAI client bound to the signing transport."""

    http_client = httpx.Client(
        transport=SkybaseLlmProxySigningTransport(
            config,
            inner_transport=inner_transport,
            clock=clock,
            nonce_factory=nonce_factory,
        ),
        timeout=timeout,
        trust_env=False,
    )
    return OpenAI(
        api_key=SKYBASE_LLM_PROXY_API_KEY_SENTINEL,
        base_url=config.api_base,
        http_client=http_client,
        max_retries=0,
    )


def _parse_exact_proxy_url(api_base: str | None) -> httpx.URL | None:
    if not api_base:
        return None
    try:
        url = httpx.URL(api_base)
    except (TypeError, ValueError):
        return None
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or not url.host.endswith(".railway.internal")
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.raw_path != SKYBASE_LLM_PROXY_BASE_PATH.encode("ascii")
    ):
        return None
    return url


def _looks_like_skybase_proxy_url(api_base: str | None) -> bool:
    if not api_base:
        return False
    try:
        url = httpx.URL(api_base)
    except (TypeError, ValueError):
        return False
    return url.path.startswith("/internal/knowledge/openai")


def _has_nonempty(value: str | None) -> bool:
    return value is not None and bool(value.strip())
