"""Honest, secret-free reporting of LLM state.

The application must never claim the LLM is active just because environment
variables exist. This module separates the five states that actually matter:

1. ``configured`` - the environment parsed into a usable config.
2. ``reachable`` - the provider endpoint answered.
3. ``model_available`` - the configured model is installed/served.
4. ``model_invoked`` - the most recent completion attempt succeeded.
5. ``mode == "deterministic"`` - the fallback is what produced the response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.llm.config import LLMConfig, LLMConfigurationError
from app.llm.factory import get_llm_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMStatus:
    """Snapshot of LLM availability and the response mode it implies."""

    configured: bool = False
    enabled: bool = False
    provider: str = ""
    model: str = ""
    base_url: str = ""
    reachable: bool = False
    model_available: bool = False
    model_invoked: bool = False
    last_invocation_latency_ms: float = 0.0
    last_error: str = ""
    detail: str = ""

    @property
    def active(self) -> bool:
        """True only when a completion could actually be served."""
        return self.configured and self.enabled and self.reachable and self.model_available

    @property
    def mode(self) -> str:
        """``"llm"`` or ``"deterministic"`` - which path responses take."""
        return "llm" if self.active else "deterministic"

    def summary(self) -> str:
        """One-line, secret-free description for the UI."""
        if not self.configured:
            return "LLM: not configured - using deterministic responses"
        if not self.enabled:
            return "LLM: disabled (LLM_ENABLED=false) - using deterministic responses"
        if not self.reachable:
            return (
                f"LLM: {self.provider}/{self.model} unreachable - "
                "using deterministic responses"
            )
        if not self.model_available:
            return (
                f"LLM: model {self.model!r} not installed - "
                "using deterministic responses"
            )
        if self.model_invoked:
            return f"LLM: {self.provider}/{self.model} active (verified)"
        return f"LLM: {self.provider}/{self.model} ready"


def get_llm_status(
    config: LLMConfig | None = None, *, check_provider: bool = True
) -> LLMStatus:
    """Build a status snapshot.

    Args:
        config: Optional pre-resolved config; read from the environment in tests
            and normal use it is omitted.
        check_provider: When False, skips the network health check and reports
            only configuration plus the last known invocation.

    Returns:
        A status object; never raises.
    """
    if config is None:
        try:
            config = LLMConfig.from_env()
        except LLMConfigurationError as exc:
            logger.warning("LLM configuration invalid: %s", exc)
            return LLMStatus(detail=str(exc))

    client = get_llm_client(config)
    if client is None:
        return LLMStatus(
            provider=config.provider,
            model=config.model,
            base_url=config.base_url,
            enabled=config.enabled,
            detail="No provider instance could be created.",
        )

    invocation = client.last_invocation

    if not config.enabled:
        return LLMStatus(
            configured=True,
            enabled=False,
            provider=config.provider,
            model=config.model,
            base_url=config.base_url,
            detail="LLM disabled via LLM_ENABLED=false.",
        )

    if not check_provider:
        return LLMStatus(
            configured=True,
            enabled=True,
            provider=config.provider,
            model=config.model,
            base_url=config.base_url,
            model_invoked=invocation.succeeded,
            last_invocation_latency_ms=invocation.latency_ms,
            last_error=invocation.error,
            detail="Provider health check skipped.",
        )

    health = client.health()
    return LLMStatus(
        configured=True,
        enabled=True,
        provider=config.provider,
        model=config.model,
        base_url=config.base_url,
        reachable=health.reachable,
        model_available=health.model_available,
        model_invoked=invocation.succeeded,
        last_invocation_latency_ms=invocation.latency_ms,
        last_error=invocation.error,
        detail=health.detail,
    )
