"""Provider interface for the LLM layer.

Every provider implements :meth:`LLMProvider.chat` (one blocking completion)
and :meth:`LLMProvider.health` (an honest reachability/model check). Nothing
above this layer knows how a provider talks over the wire, which is what makes
Ollama swappable for Groq or anything else later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence, TypedDict


class ChatMessage(TypedDict):
    """One message in a chat completion request."""

    role: str  # "system" | "user" | "assistant"
    content: str


class LLMChatError(RuntimeError):
    """Raised when a provider cannot complete a chat request.

    Callers must treat this as "no LLM output available" and fall back, not as
    an application error.
    """


@dataclass(frozen=True)
class ProviderHealth:
    """Result of asking a provider whether it can actually serve requests."""

    #: The provider's HTTP endpoint answered.
    reachable: bool
    #: The configured model appears in the provider's model list.
    model_available: bool
    #: Model names the provider reported (empty when unreachable).
    available_models: tuple[str, ...] = ()
    #: Human-readable reason when something is wrong.
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.reachable and self.model_available


@dataclass
class InvocationRecord:
    """Bookkeeping for the most recent completion attempt.

    Used so the UI can distinguish "configured" from "actually invoked".
    """

    attempted: bool = False
    succeeded: bool = False
    latency_ms: float = 0.0
    error: str = ""


class LLMProvider(ABC):
    """Minimal interface the application depends on."""

    def __init__(self, config):  # noqa: ANN001 - LLMConfig, kept loose for test doubles
        self.config = config
        self.last_invocation = InvocationRecord()

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier, e.g. ``"ollama"``."""

    @abstractmethod
    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """Return the assistant reply text.

        Raises:
            LLMChatError: on connection, timeout, HTTP, or malformed-response
                failures, and when the reply is empty.
        """

    @abstractmethod
    def health(self) -> ProviderHealth:
        """Report reachability and whether the configured model is present."""

    def close(self) -> None:
        """Release resources. Providers without state need not override."""
