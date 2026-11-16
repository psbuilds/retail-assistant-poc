"""Provider factory with a process-wide cache.

Streamlit reruns the script on every interaction, so constructing a provider
per rerun would re-read configuration and re-open connections needlessly. The
client is cached per configuration fingerprint and rebuilt only when the
environment actually changes (or ``reset_llm_client`` is called by tests).
"""

from __future__ import annotations

import logging
import threading

from app.llm.base import LLMProvider
from app.llm.config import LLMConfig, LLMConfigurationError
from app.llm.ollama import OllamaProvider

logger = logging.getLogger(__name__)

#: Provider name -> constructor. Add new providers here.
_PROVIDER_REGISTRY: dict[str, type[LLMProvider]] = {
    "ollama": OllamaProvider,
}

_cache_lock = threading.Lock()
_cached_client: LLMProvider | None = None
_cached_fingerprint: tuple | None = None


def _fingerprint(config: LLMConfig) -> tuple:
    return (
        config.provider,
        config.model,
        config.base_url,
        config.timeout_seconds,
        config.temperature,
        config.enabled,
        bool(config.api_key),
    )


def _build(config: LLMConfig) -> LLMProvider:
    provider_cls = _PROVIDER_REGISTRY.get(config.provider)
    if provider_cls is None:  # pragma: no cover - guarded by LLMConfig
        raise LLMConfigurationError(
            f"No implementation registered for provider {config.provider!r}."
        )
    return provider_cls(config)


def get_llm_client(config: LLMConfig | None = None) -> LLMProvider | None:
    """Return the cached provider, or ``None`` when the LLM is not usable.

    A misconfigured environment is not fatal: it is logged, and the caller
    keeps working through the deterministic path.
    """
    global _cached_client, _cached_fingerprint

    if config is None:
        try:
            config = LLMConfig.from_env()
        except LLMConfigurationError as exc:
            logger.warning("LLM configuration invalid: %s", exc)
            return None

    fingerprint = _fingerprint(config)

    with _cache_lock:
        if _cached_client is not None and _cached_fingerprint == fingerprint:
            return _cached_client
        client = _build(config)
        _cached_client = client
        _cached_fingerprint = fingerprint
        logger.info(
            "LLM client created: provider=%s model=%s base_url=%s enabled=%s",
            config.provider,
            config.model,
            config.base_url,
            config.enabled,
        )
        return client


def reset_llm_client() -> None:
    """Drop the cached provider. Intended for tests and config reloads."""
    global _cached_client, _cached_fingerprint

    with _cache_lock:
        if _cached_client is not None:
            try:
                _cached_client.close()
            except Exception:  # pragma: no cover - close is best effort
                logger.debug("Provider close failed", exc_info=True)
        _cached_client = None
        _cached_fingerprint = None
