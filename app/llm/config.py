"""Environment-driven LLM configuration.

All names are ``LLM_*`` so the whole LLM surface is discoverable from one
grep, and no provider is privileged in the naming:

===================================  ==========================  =============================
Variable                             Default                     Meaning
===================================  ==========================  =============================
``LLM_ENABLED``                      ``true``                    Hard on/off switch.
``LLM_PROVIDER``                     ``ollama``                  Which provider implementation.
``LLM_MODEL``                        ``llama3.1:latest``         Model name the provider serves.
``LLM_BASE_URL``                     ``http://127.0.0.1:11434``  Provider endpoint.
``LLM_TIMEOUT_SECONDS``              ``60``                      Per-request timeout.
``LLM_TEMPERATURE``                  ``0.2``                     Sampling temperature.
``LLM_API_KEY``                      unset                       Only for hosted providers.
===================================  ==========================  =============================

Secrets are read from the environment only; they are never logged, never
written to disk by this module, and never rendered in the UI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_PROVIDER = "ollama"
DEFAULT_MODEL = "llama3.1:latest"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_TEMPERATURE = 0.2

#: Providers this build can construct. Extend alongside ``app.llm.factory``.
SUPPORTED_PROVIDERS = ("ollama",)


class LLMConfigurationError(ValueError):
    """Raised when the LLM environment configuration is unusable."""


def _as_bool(raw: str, default: bool) -> bool:
    value = raw.strip().casefold()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _as_float(raw: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value < minimum or value > maximum:
        return default
    return value


@dataclass(frozen=True)
class LLMConfig:
    """Resolved LLM configuration for one provider instance."""

    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    temperature: float = DEFAULT_TEMPERATURE
    enabled: bool = True
    #: Presence only. Never log or display the value.
    api_key: str | None = None

    def __post_init__(self) -> None:
        provider = (self.provider or "").strip().casefold()
        if provider not in SUPPORTED_PROVIDERS:
            raise LLMConfigurationError(
                f"Unsupported LLM_PROVIDER={self.provider!r}. "
                f"Supported providers: {', '.join(SUPPORTED_PROVIDERS)}."
            )
        object.__setattr__(self, "provider", provider)

        model = (self.model or "").strip()
        if not model:
            raise LLMConfigurationError("LLM_MODEL must not be empty.")
        object.__setattr__(self, "model", model)

        base_url = (self.base_url or "").strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise LLMConfigurationError(
                f"LLM_BASE_URL must be an http(s) URL, got {self.base_url!r}."
            )
        object.__setattr__(self, "base_url", base_url)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "LLMConfig":
        """Build a config from environment variables.

        Raises:
            LLMConfigurationError: if a required value is missing or malformed.
        """
        source = os.environ if env is None else env
        return cls(
            provider=source.get("LLM_PROVIDER", DEFAULT_PROVIDER),
            model=source.get("LLM_MODEL", DEFAULT_MODEL),
            base_url=source.get("LLM_BASE_URL", DEFAULT_BASE_URL),
            timeout_seconds=_as_float(
                source.get("LLM_TIMEOUT_SECONDS", ""),
                DEFAULT_TIMEOUT_SECONDS,
                minimum=1.0,
                maximum=600.0,
            ),
            temperature=_as_float(
                source.get("LLM_TEMPERATURE", ""),
                DEFAULT_TEMPERATURE,
                minimum=0.0,
                maximum=2.0,
            ),
            enabled=_as_bool(source.get("LLM_ENABLED", ""), True),
            api_key=(source.get("LLM_API_KEY") or "").strip() or None,
        )

    def describe(self) -> dict[str, object]:
        """Secret-free description suitable for logs and the UI."""
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "enabled": self.enabled,
            "timeout_seconds": self.timeout_seconds,
            "temperature": self.temperature,
            "api_key_configured": self.api_key is not None,
        }
