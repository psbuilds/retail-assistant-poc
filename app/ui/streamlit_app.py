"""Streamlit conversational retail shopping assistant UI.

Responsibilities kept here: rendering chat, collecting input, temporary session
state, and showing connection/LLM status. All memory, constraint, and product
logic lives in the service layer.

The LLM status panel reports what is actually true - configured, reachable,
model installed, last invocation succeeded, or deterministic fallback - and
never shows credentials.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List

import httpx
import streamlit as st

from app.services.conversation_service import ConversationService
from app.services.memory_service import MemoryService

# Page configuration
st.set_page_config(
    page_title="Retail Shopping Assistant",
    page_icon="🛍️",
    layout="centered",
    initial_sidebar_state="expanded",
)

# Constants
DEFAULT_CUSTOMER_ID = "customer_001"
PRODUCT_API_BASE = os.getenv("PRODUCT_API_URL", "http://127.0.0.1:8000")
MCP_BASE_URL = os.getenv("COGNEE_MCP_URL", "http://127.0.0.1:8101/mcp")

#: Health checks are network calls; refresh them at most this often.
STATUS_TTL_SECONDS = 15.0

QUICK_PROMPTS = (
    "I want headphones under ₹2000",
    "Show me headphones without ANC",
    "Now show me running shoes",
    "Something cheaper",
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Status helpers
# --------------------------------------------------------------------------- #


def check_mcp_connection() -> bool:
    """Return True when the Cognee MCP endpoint is reachable.

    A bare GET to an MCP endpoint answers 406 (it needs protocol headers), and
    that still proves the server is listening. Only a transport-level failure
    means the server is down, so any HTTP response counts as reachable.
    """
    try:
        httpx.get(
            MCP_BASE_URL,
            timeout=2.0,
            headers={"Accept": "application/json, text/event-stream"},
        )
        return True
    except httpx.HTTPError:
        return False


def _cached_llm_status(service: ConversationService) -> Dict[str, Any]:
    """Cache the LLM status for a few seconds to keep reruns responsive."""
    now = time.monotonic()
    cached = st.session_state.get("llm_status_cache")
    if cached and now - cached["at"] < STATUS_TTL_SECONDS:
        return cached["value"]

    status = service.get_llm_status()
    value = {
        "configured": status.configured,
        "enabled": status.enabled,
        "provider": status.provider,
        "model": status.model,
        "reachable": status.reachable,
        "model_available": status.model_available,
        "model_invoked": status.model_invoked,
        "mode": status.mode,
        "summary": status.summary(),
        "detail": status.detail,
        "latency_ms": status.last_invocation_latency_ms,
    }
    st.session_state.llm_status_cache = {"at": now, "value": value}
    return value


def render_status_panel(mcp_connected: bool, llm: Dict[str, Any]) -> None:
    """Show connection and response-mode status, without secrets."""
    st.subheader("Status")

    if mcp_connected:
        st.success("Cognee MCP: connected", icon="✅")
    else:
        st.warning("Cognee MCP: unreachable - memory context unavailable", icon="⚠️")

    if llm["mode"] == "llm":
        st.success(f"LLM: {llm['provider']} / {llm['model']}", icon="🧠")
    else:
        st.warning("LLM: deterministic fallback", icon="📝")

    with st.expander("LLM detail", expanded=False):
        st.write(
            {
                "configured": llm["configured"],
                "enabled": llm["enabled"],
                "provider": llm["provider"] or "-",
                "model": llm["model"] or "-",
                "provider_reachable": llm["reachable"],
                "model_available": llm["model_available"],
                "last_invocation_ok": llm["model_invoked"],
            }
        )
        if llm["detail"]:
            st.caption(llm["detail"])
        if llm["model_invoked"]:
            st.caption(f"Last model call: {llm['latency_ms']:.0f} ms")
        if llm["mode"] != "llm":
            st.caption(
                "Responses are generated deterministically from Product API "
                "data. Start the model service to enable LLM responses."
            )


# --------------------------------------------------------------------------- #
# Product rendering
# --------------------------------------------------------------------------- #


def render_product_card(product: Dict[str, Any]) -> None:
    """Render one product card from Product API data."""
    with st.container(border=True):
        header, price_col = st.columns([3, 1])
        with header:
            st.markdown(f"**{product.get('name', 'Unknown product')}**")
            st.caption(str(product.get("brand", "Unknown brand")))
        with price_col:
            st.markdown(f"### ₹{product.get('price', '?')}")

        rating = product.get("rating")
        if isinstance(rating, (int, float)) and rating > 0:
            filled = int(rating)
            st.caption(f"{'★' * filled}{'☆' * (5 - filled)} {rating}")

        badges: List[str] = []
        if product.get("noise_cancellation"):
            badges.append("noise cancellation")
        if product.get("wireless"):
            badges.append("wireless")
        features = [str(feature) for feature in (product.get("features") or [])]
        badges.extend(features[:3])
        if badges:
            st.caption(" · ".join(dict.fromkeys(badges)))


# --------------------------------------------------------------------------- #
# Main app
# --------------------------------------------------------------------------- #


def _initialise_state() -> ConversationService:
    if "conversation_service" not in st.session_state:
        st.session_state.conversation_service = ConversationService(MemoryService())
    if "customer_id" not in st.session_state:
        st.session_state.customer_id = DEFAULT_CUSTOMER_ID
    if "messages" not in st.session_state:
        st.session_state.messages = []

    service: ConversationService = st.session_state.conversation_service
    service.set_customer(st.session_state.customer_id)
    return service


def _run_turn(service: ConversationService, prompt: str) -> None:
    """Process one user turn and append both messages to session history."""
    st.session_state.messages.append({"role": "user", "content": prompt})

    with st.spinner("Finding matching products..."):
        result = service.process_query(prompt)

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": result.get("response", ""),
            "products": result.get("recommended_products", []),
            "follow_up": result.get("follow_up_question", ""),
            "response_mode": result.get("response_mode", "deterministic"),
            "error": result.get("error") if not result.get("success") else None,
        }
    )


def main() -> None:
    """Render the assistant."""
    service = _initialise_state()

    st.title("🛍️ Retail Shopping Assistant")
    st.caption("Conversational shopping with memory-aware recommendations")

    mcp_connected = check_mcp_connection()
    llm_status = _cached_llm_status(service)

    with st.sidebar:
        st.header(f"Customer: {st.session_state.customer_id}")
        render_status_panel(mcp_connected, llm_status)

        st.divider()
        st.caption("Quick requests")
        for index, prompt in enumerate(QUICK_PROMPTS):
            if st.button(prompt, key=f"preset_{index}", use_container_width=True):
                _run_turn(service, prompt)
                st.rerun()

        st.divider()
        with st.expander("Memory status", expanded=False):
            if st.button("Show stored memories"):
                try:
                    recall = service.memory_service.recall("all")
                except Exception as exc:  # memory failure must not crash the UI
                    st.error(f"Memory recall failed: {exc}")
                else:
                    if recall.fragments:
                        for fragment in recall.fragments:
                            st.write(f"• {fragment}")
                    else:
                        st.caption("No memories stored yet.")

        if st.button("🗑️ Clear conversation", help="Chat history only - memories preserved"):
            service.clear_conversation()
            st.session_state.messages = []
            st.rerun()

        st.caption("Clearing the chat never deletes long-term memories.")

        # Clear All Cognee Memories section
        with st.expander("Clear All Cognee Memories", expanded=False):
            st.warning(
                "This will delete ALL memories for customer "
                f"{service.memory_service.get_customer()} from dataset "
                f"{service.memory_service.get_dataset()}."
            )
            if st.button("Confirm Delete All Memories", type="primary"):
                customer_id = service.memory_service.get_customer()
                dataset = service.memory_service.get_dataset()
                if not customer_id or not dataset:
                    st.error("No customer or dataset identified.")
                else:
                    try:
                        # Call the forget tool to delete all memories for this dataset
                        result = service.memory_service.forget(customer_id, dataset)
                        st.success(f"Deleted memories for dataset: {dataset}")
                        # Verify deletion by attempting a fresh recall
                        fresh_recall = service.memory_service.recall("all")
                        if fresh_recall.fragments:
                            st.warning(
                                f"Warning: {len(fresh_recall.fragments)} fragments still remain. "
                                "Deletion may not have been complete."
                            )
                        else:
                            st.success("Verification complete: No memories remain for this dataset.")
                    except Exception as e:
                        st.error(f"Failed to delete memories: {e}")
            st.caption("⚠️ This operation is dataset-scoped and permanent.")

        st.caption("Clearing the chat never deletes long-term memories.")

    # Conversation transcript
    if not st.session_state.messages:
        st.info("Ask for something to get started, for example 'I want headphones under ₹2000'.")

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            if message.get("error"):
                st.error(message["content"])
            else:
                st.markdown(message["content"])

            if message["role"] == "assistant":
                products = message.get("products") or []
                if products:
                    st.caption("Recommendations")
                    for product in products:
                        render_product_card(product)
                if message.get("follow_up"):
                    st.caption(message["follow_up"])
                if message.get("response_mode"):
                    st.caption(f"Response source: {message['response_mode']}")
                if message.get("intent_source"):
                    st.caption(f"Intent: {message['intent_source']}")

    prompt = st.chat_input("Type your shopping request...")
    if prompt:
        _run_turn(service, prompt)
        st.rerun()


if __name__ == "__main__":
    main()
