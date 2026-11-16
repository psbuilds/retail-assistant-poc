"""Provider-agnostic LLM layer.

Public surface:

- ``LLMConfig``: environment-driven configuration.
- ``LLMProvider``: the interface every provider implements.
- ``get_llm_client`` / ``reset_llm_client``: cached provider access.
- ``get_llm_status``: what the application can honestly report about the LLM.

The rest of the application must depend only on these, never on a provider's
HTTP details, so the provider can be swapped (Ollama, Groq, ...) without
touching the conversation pipeline.
"""

from app.llm.base import ChatMessage, LLMChatError, LLMProvider, ProviderHealth
from app.llm.config import LLMConfig, LLMConfigurationError
from app.llm.factory import get_llm_client, reset_llm_client
from app.llm.status import LLMStatus, get_llm_status

__all__ = [
    "ChatMessage",
    "LLMChatError",
    "LLMProvider",
    "ProviderHealth",
    "LLMConfig",
    "LLMConfigurationError",
    "get_llm_client",
    "reset_llm_client",
    "LLMStatus",
    "get_llm_status",
]
