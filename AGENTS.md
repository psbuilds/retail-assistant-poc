# AGENTS.md — Retail Memory Assistant

## 1. Project Identity

Retail Memory Assistant is a conversational AI shopping assistant with persistent, personalized user memory.

Its purpose is to demonstrate how persistent memory can improve shopping conversations by remembering durable user preferences and using them to provide grounded product recommendations.

The application uses:

- Python
- Streamlit
- Cognee
- Model Context Protocol (MCP)
- Cognee MCP / FastMCP
- Ollama
- FastAPI
- Pydantic
- uv
- pytest

The active repository is the root directory containing this file.

All relative paths in development instructions refer to this project root.

The original project `retail-memory-poc-omnirush` is separate and must remain untouched.

---

## 2. Core Engineering Objective

Build a reliable conversational shopping assistant that:

1. Understands user requests.
2. Identifies durable personal preferences.
3. Stores relevant information in persistent memory.
4. Retrieves relevant memories when needed.
5. Uses remembered preferences for product discovery.
6. Recommends actual products from the catalogue.
7. Preserves memories across application and conversation restarts.

The central objective is persistent personalization, not generic RAG.

---

## 3. Non-Negotiable Architectural Rules

### 3.1 MCP-only memory communication

All application-level communication with Cognee memory MUST happen through MCP.

Allowed:
- MCP remember.
- MCP recall.
- Other explicitly approved MCP tools when genuinely necessary.
- MCP connection and session lifecycle management.

Forbidden:
- Direct Cognee Python memory operations from application code.
- Cognee CLI memory operations from application code.
- Subprocess-based Cognee memory calls.
- CLI fallback when MCP fails.
- A second memory adapter that bypasses MCP.

The Cognee API server may be used internally by the Cognee MCP server. The application itself must not bypass MCP.

The MCP adapter is the single integration boundary between the application and persistent memory.

### 3.2 Separation of responsibilities

Keep the following responsibilities separate:

- UI: presentation and user interaction.
- Conversation service: orchestration.
- Intent extraction: request classification.
- Memory service: application-level memory operations.
- MCP adapter: MCP communication.
- Recommendation service: product filtering and ranking.
- Constraint resolver: conflict resolution.
- Response generation: conversational response creation.
- Product API: catalogue access.

Do not move business logic into the Streamlit UI.

Do not make the UI communicate directly with Cognee.

### 3.3 Persistent memory and conversation context

Persistent memory and short-term conversation context MUST remain separate.

Persistent memory:
- Durable user preferences.
- Recurring requirements.
- Long-term facts relevant to shopping.

Short-term context:
- Recent messages.
- Temporary budgets.
- Current shopping requests.
- Temporary product exclusions.
- Follow-up context.

Clearing a chat must not delete persistent memories.

Do not store every user message.

---

## 4. Conversation Routing

The primary conversation router must distinguish:

1. STORE
2. RECALL
3. NORMAL_RESPONSE

Product discovery is a separate action/route.

### STORE

Use for durable user preferences and relevant long-term facts.

Examples:
- "I prefer Sony headphones."
- "I generally prefer over-ear headphones."
- "I prefer headphones with active noise cancellation."

Store semantic facts through MCP.

Do not store temporary instructions as permanent preferences unless the user clearly indicates durability.

### RECALL

Use when the user asks about existing memories or requests personalization that requires remembered preferences.

Examples:
- "What do you know about my preferences?"
- "Which headphone brand do I prefer?"
- "Recommend headphones based on my preferences."

Retrieve relevant information rather than unrelated memories.

### NORMAL_RESPONSE

Use for greetings, general knowledge, explanations, and ordinary conversation.

Examples:
- "Hi."
- "What is ANC?"
- "Explain Bluetooth codecs."

NORMAL_RESPONSE MUST NOT invoke MCP or Product API.

### PRODUCT_DISCOVERY

Use the Product API for explicit product searches, recommendations, comparisons, and discovery.

For personalized recommendations