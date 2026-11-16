# Retail Memory Assistant - Architecture

## Overview

The application is a conversational AI shopping assistant with persistent, personalized user memory. It demonstrates how persistent memory can improve shopping conversations by remembering durable user preferences and using them to provide grounded product recommendations.

## Core Architecture

The application follows a clean separation of concerns with these main layers:

```
Streamlit UI
    ↓
Conversation Service (orchestration + intent routing)
    ↓
Intent Classification → Appropriate Service Route
    ├─ STORE → Memory Service → MCP Adapter → Cognee MCP Server
    ├─ RECALL → Memory Service → MCP Adapter → Cognee MCP Server
    ├─ NORMAL_RESPONSE → Deterministic response (no MCP/Product API)
    └─ PRODUCT_DISCOVERY → Constraint Resolver → Product API → Response Generation
```

## Component Responsibilities

### Streamlit UI (`app/ui/streamlit_app.py`)
- Customer selection and chat interface
- Status indicators (MCP connection, LLM status)
- Conversation history display
- Memory inspection tools
- Clear Chat option (preserves Cognee memories)
- "Clear All Cognee Memories" admin operation

### Conversation Service (`app/services/conversation_service.py`)
- Coordinates each user interaction through the pipeline
- Intent routing: STORE / RECALL / NORMAL_RESPONSE / PRODUCT_DISCOVERY
- Only PRODUCT_DISCOVERY invokes Product API and shows product cards
- STORE, RECALL, and NORMAL_RESPONSE never call Product API

### Intent Classification (`app/services/intent_extraction.py`)
- Deterministic intent extraction from user queries
- Distinguishes between preference statements, recall requests, normal conversation, and product discovery

### Memory Service (`app/services/memory_service.py`)
- Wraps MCP adapter for all memory operations
- Customer isolation via dataset naming (`retail_{customer_id}`)
- `remember_with_customer()` and `recall_with_customer()` for isolated operations
- `forget()` for administrative memory deletion

### MCP Adapter (`app/memory/mcp_adapter.py`)
- **Single integration boundary** between application and Cognee persistent memory
- Manages persistent MCP session on a daemon thread
- `_remember()`: Calls MCP `remember` tool with `{data, dataset_name, background, self_improvement}`
- `_recall()`: Calls MCP `recall` tool with `{query, datasets: [dataset], search_type, top_k}`
- Thread liveness check for Streamlit rerun recovery
- Error wrapping and chaining

### Product API (`app/api/product_api.py`)
- FastAPI service on port 8000
- Provides product catalogue search and filtering
- Validated product data with names, prices, ratings, features

### Recommendation Service (`app/services/constraint_resolver.py`)
- Resolves constraints with proper precedence:
  1. Current explicit user instruction
  2. Relevant current conversation context
  3. Relevant persistent memory
  4. Product defaults
- `normalize_constraints()` translates to API/prompt shape

### Response Generation (`app/services/response_generation.py`)
- Generates natural language response using LLM (or deterministic fallback)
- If Ollama unavailable, deterministic recommendation logic still works
- Never fabricates product names, prices, ratings, or specifications

## MCP Integration

### Communication Flow

All application-level communication with Cognee memory **MUST** happen through MCP:

**Allowed:**
- MCP `remember` tool call
- MCP `recall` tool call
- MCP session management

**Forbidden:**
- Direct Cognee Python memory operations from application code
- Cognee CLI memory operations from application code
- Subprocess-based Cognee memory calls
- A second memory adapter that bypasses MCP

### Recall Tool Schema

The MCP `recall` tool expects the following argument format:

```python
{
    "query": "<search query>",
    "datasets": ["<dataset_name>"],  # LIST format [FIX 1]
    "search_type": "CHUNKS",
    "top_k": 10,
}
```

**Important**: `datasets` must be a **list** `[dataset]`, not a string `dataset`. This format was fixed in migration to match Cognee API 1.6.6 schema and avoid 409 Conflict errors.

### Remember Tool Schema

The MCP `remember` tool expects:

```python
{
    "data": "<facts joined by newlines>",
    "dataset_name": "<dataset_name>",  e.g., "retail_customer_001"
    "background": False,
    "self_improvement": True,
}
```

## Conversation Flow

### STORE Mode
- User explicitly states a preference ("I prefer Sony headphones")
- Conversation Service extracts preferences via `extract_preferences()`
- Stores facts as durable memories via `memory_service.remember_with_customer()`
- Generates deterministic acknowledgement response
- No Product API call, no product cards

### RECALL Mode
- User asks about existing preferences ("What do you know about my preferences?")
- Conversation Service retrieves relevant memories via `memory_service.recall()`
- Returns actual stored preferences, never fabricates missing information
- No Product API call, no product cards

### NORMAL_RESPONSE Mode
- Greetings, general knowledge, explanations
- Zero MCP calls, zero Product API calls
- Deterministic explanations (e.g., "ANC stands for Active Noise Cancellation")

### PRODUCT_DISCOVERY Mode
- User explicitly requests product discovery ("Recommend headphones under ₹3,000")
- Constraint Resolver merges preferences with proper precedence
- Product API searched with resolved constraints
- Response Generation produces grounded recommendation
- Products displayed from actual catalogue data

## Customer Isolation

- Each customer gets their own dataset: `retail_{customer_id}` (e.g., `retail_customer_001`)
- Memories are scoped by dataset - Customer A's preferences are isolated from Customer B's
- `remember_with_customer()` and `recall_with_customer()` allow operations for specific customers without changing current context
- Switching customers does not leak memories

## Persistent vs Short-Term Context

### Persistent Memory (through Cognee MCP)
- Preferred brands ("Prefers Sony headphones")
- General product preferences ("Prefers wireless headphones")
- Long-term budget preferences ("Has a budget up to ₹5000")
- Preferred product categories

### Short-Term Context (Streamlit session)
- Recent messages (last 20 turns)
- Temporary budget constraints (for current query only)
- Current shopping request (category, budget for this search)
- Temporary product exclusions
- Follow-up context

**Critical**: `clear_conversation()` only clears conversation history, not Cognee memories.

## Product Recommendation Flow

1. Understand current request
2. Resolve current explicit instructions (highest precedence)
3. Incorporate conversation context
4. Retrieve relevant persistent memories when personalization is useful
5. Query Product API with resolved constraints
6. Filter and rank actual products from catalogue
7. Generate grounded response with real product data

If Ollama is unavailable, deterministic recommendation logic still works using actual catalogue data.

## Configuration and Service Dependencies

| Service | Port | Environment Variable |
|---------|------|---------------------|
| Cognee API | 8102 | `COGNEE_API_URL=http://127.0.0.1:8102` |
| Cognee MCP HTTP | 8101 | `COGNEE_MCP_URL=http://127.0.0.1:8101/mcp` |
| Product API | 8000 | `PRODUCT_API_URL=http://127.0.0.1:8000` |
| Streamlit | 8501 | (default, no env var required) |
| Ollama (optional) | 11434 | `OLLAMA_BASE_URL=http://127.0.0.1:11434` |

## Known Issues

- **MCP initialization cancellation**: During Streamlit reruns, daemon threads may be cancelled. The adapter includes recovery logic (_connection = None reset on CancellationError), but in rare cases may require restarting the Streamlit session.
- **409 Conflict during recall**: Fixed by passing `datasets` as a list (`datasets: [dataset]`). This matches Cognee API 1.6.6 schema.

## Setup Instructions

See README.md for complete setup, startup, and testing instructions.