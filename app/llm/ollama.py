"""Ollama provider implementation.

Talks to the Ollama HTTP API (``/api/chat`` and ``/api/tags``). Kept thin on
purpose: it only translates the provider-neutral :class:`LLMProvider` contract
into Ollama HTTP calls and converts every failure into :class:`LLMChatError`.
"""

from __future__ import annotations

import logging
import time
from typing import Sequence

import httpx

from app.llm.base import (
    ChatMessage,
    InvocationRecord,
    LLMChatError,
    LLMProvider,
    ProviderHealth,
)

logger = logging.getLogger(__name__)

#: Health check is cheap and must never hang the UI.
HEALTH_TIMEOUT_SECONDS = 3.0


def _model_names_match(configured: str, reported: str) -> bool:
    """Treat ``llama3.1`` and ``llama3.1:latest`` as the same model."""
    configured = configured.strip().casefold()
    reported = reported.strip().casefold()
    if configured == reported:
        return True
    if reported == f"{configured}:latest":
        return True
    if configured == f"{reported}:latest":
        return True
    return False


class OllamaProvider(LLMProvider):
    """Local Ollama server provider."""

    @property
    def name(self) -> str:
        return "ollama"

    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        if not self.config.enabled:
            raise LLMChatError("LLM is disabled via LLM_ENABLED=false.")

        options: dict[str, object] = {
            "temperature": (
                self.config.temperature if temperature is None else temperature
            )
        }
        if max_tokens is not None:
            options["num_predict"] = max_tokens

        payload = {
            "model": self.config.model,
            "messages": list(messages),
            "stream": False,
            "options": options,
        }

        started = time.perf_counter()
        try:
            response = httpx.post(
                f"{self.config.base_url}/api/chat",
                json=payload,
                timeout=timeout if timeout is not None else self.config.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            self._record_failure(started, f"request failed: {exc}")
            raise LLMChatError(
                f"Ollama request to {self.config.base_url} failed: {exc}"
            ) from exc

        if response.status_code != 200:
            self._record_failure(started, f"HTTP {response.status_code}")
            raise LLMChatError(
                f"Ollama returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            self._record_failure(started, "malformed JSON response")
            raise LLMChatError("Ollama returned a non-JSON response.") from exc

        content = ""
        message = body.get("message") if isinstance(body, dict) else None
        if isinstance(message, dict):
            content = str(message.get("content") or "")
        content = content.strip()

        if not content:
            self._record_failure(started, "empty completion")
            raise LLMChatError("Ollama returned an empty completion.")

        latency_ms = (time.perf_counter() - started) * 1000.0
        self.last_invocation = InvocationRecord(
            attempted=True,
            succeeded=True,
            latency_ms=latency_ms,
            error="",
        )
        logger.info(
            "LLM invocation succeeded provider=%s model=%s latency_ms=%.0f",
            self.name,
            self.config.model,
            latency_ms,
        )
        return content

    def health(self) -> ProviderHealth:
        if not self.config.enabled:
            return ProviderHealth(
                reachable=False,
                model_available=False,
                detail="LLM disabled via LLM_ENABLED=false.",
            )
        try:
            response = httpx.get(
                f"{self.config.base_url}/api/tags", timeout=HEALTH_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            return ProviderHealth(
                reachable=False,
                model_available=False,
                detail=f"Cannot reach {self.config.base_url}: {exc}",
            )

        if response.status_code != 200:
            return ProviderHealth(
                reachable=False,
                model_available=False,
                detail=f"Ollama health check returned HTTP {response.status_code}.",
            )

        try:
            body = response.json()
        except ValueError:
            return ProviderHealth(
                reachable=False,
                model_available=False,
                detail="Ollama health check returned non-JSON output.",
            )

        models = body.get("models") if isinstance(body, dict) else None
        names: tuple[str, ...] = tuple(
            str(entry.get("name"))
            for entry in (models or [])
            if isinstance(entry, dict) and entry.get("name")
        )

        model_available = any(
            _model_names_match(self.config.model, name) for name in names
        )
        detail = "" if model_available else (
            f"Model {self.config.model!r} is not installed. "
            f"Available: {', '.join(names) or 'none'}. "
            f"Run: ollama pull {self.config.model}"
        )
        return ProviderHealth(
            reachable=True,
            model_available=model_available,
            available_models=names,
            detail=detail,
        )

    def _record_failure(self, started: float, error: str) -> None:
        latency_ms = (time.perf_counter() - started) * 1000.0
        self.last_invocation = InvocationRecord(
            attempted=True,
            succeeded=False,
            latency_ms=latency_ms,
            error=error,
        )
        logger.warning(
            "LLM invocation failed provider=%s model=%s error=%s",
            self.name,
            self.config.model,
            error,
        )
