# Streamlit UI (`app/ui/streamlit_app.md`)

## Purpose and Responsibility

The Streamlit UI (`app/ui/streamlit_app.py`) is the presentation layer of the Retail Memory Assistant POC. It renders the chat interface, collects user input, displays status panels, and provides quick preset prompts. All business logic (memory operations, product discovery, constraint resolution) is delegated to the service layer — the UI never contains direct Cognee or Product API calls.

## Place in Overall Architecture

```
Streamlit UI
    |-- Renders chat history and product cards
    |-- Collects user prompts via chat_input
    |-- Shows status panels (MCP connection, LLM mode)
    |-- Provides memory management controls (inspect, delete all)
    |-- Handles quick preset prompts for demo flow
    |
Conversation Service
    |-- Intent extraction and routing (STORE / RECALL / NORMAL_RESPONSE / PRODUCT_DISCOVERY)
    |-- Memory update (if appropriate)
    |-- Conversation history update
    |
Memory Service
    |-- MCP adapter for all memory operations
    |-- Single demo dataset: retail_demo
    |
Product Service
    |-- Product API integration for catalogue search
    |
Ollama + Cognee MCP Server
    |-- Embedding and LLM capabilities
```

## Internal Dependencies

- `streamlit` — UI framework
- `httpx` — MCP connectivity checks
- `app.services.conversation_service.ConversationService` — main pipeline
- `app.services.memory_service.MemoryService` — memory operations
- `app.services.memory_service.DEMO_DATASET` — constant `"retail_demo"`
- `app.ui.streamlit_app.render_status_panel()` — status display
- `app.ui.streamlit_app.render_product_card()` — product display
- `app.ui.streamlit_app.check_mcp_connection()` — MCP health check

## Every Class and Its Role

### `ConversationService` (instantiated in `main()`)

- **Constructor**: `ConversationService(memory_service=MemoryService())`
  - Initializes with a `MemoryService` using the demo dataset
  - Sets `use_llm=True` by default
- **`process_query(query)`** — main entry point
  - Intent extraction → intent routing → (STORE/RECALL/NORMAL_RECALL/PRODUCT_DISCOVERY) → response generation → memory update → conversation history update
- **`clear_conversation()`** — clears only Streamlit session state, not Cognee memories
- **`get_llm_status()`** — LLM reachability/status reporting

### `MemoryService` (instantiated by `ConversationService`)

- **Constructor**: `MemoryService(memory_adapter=McpMemoryAdapter())`
  - Uses `McpMemoryAdapter` for all Cognee communication through MCP
  - Fixed dataset: `DEMO_DATASET = "retail_demo"`
- **`remember(facts)`** — store durable preferences
- **`recall(query)`** — retrieve relevant memories
- **`forget(customer_id, dataset)`** — delete all memories for the demo dataset
- **`get_customer()`** → always returns `"customer_001"` (single-user)
- **`get_dataset()`** → always returns `"retail_demo"`

## Constructor Parameters and Important Attributes

### `MemoryService.__init__(self, memory_adapter=None)`

- `memory_adapter` — optional `McpMemoryAdapter`; defaults to new instance
- `self.current_dataset` — always `"retail_demo"` in single-user mode
- `self.memory_adapter` — the MCP adapter for all memory operations

### `ConversationService.__init__(self, memory_service=None, use_llm=True)`

- `memory_service` — optional `MemoryService`; defaults to new instance
- `use_llm` — forces deterministic mode if `False`
- `self.memory_service` — the memory service instance
- `self.conversation_history` — list of `{"role": "user"/"assistant", "content": ...}`
- `self.use_llm` — controls whether LLM is considered "active"

## Every Maintained Function and Method

### `ConversationService.process_query(query)`

**Execution order:**

1. Get customer ID via `self.memory_service.get_customer()` — always `"customer_001"`
2. Intent extraction via `extract_intent(query, conversation_history=self.conversation_history, use_llm=self.use_llm)`
3. Determine intent mode via `_determine_intent_mode(intent, query)`
4. Route based on mode:
   - **STORE**: `_store_preference_only()` → `_generate_store_response()`
   - **RECALL**: `_generate_recall_response()`
   - **NORMAL_RESPONSE**: `_generate_normal_response()`
   - **PRODUCT_DISCOVERY**: `_call_product_api()` → `generate_response()`
5. Update conversation history
6. Build return dict with `response`, `recommended_products`, `memories`, `preferences`, `success`, `error`, `llm_used`, `response_mode`, `intent_source`, `follow_up_question`

**Return dict keys:**

- `response` — assistant's response text
- `recommended_products` — list of product dicts (PRODUCT_DISCOVERY only)
- `memories` — relevant fragments from `_get_relevant_memories()`
- `preferences` — empty dict (populated later if needed)
- `success` — whether operation succeeded
- `error` — error message if failed
- `llm_used` — whether LLM was used for response generation
- `response_mode` — `STORE` / `RECALL` / `NORMAL_RESPONSE` / `PRODUCT_DISCOVERY`
- `intent_source` — `"deterministic"` or `"llm"` from intent extraction
- `follow_up_question` — suggested follow-up

### `ConversationService._store_preference_only(query, intent)`

**Rules:**

- Extract preferences from user query via `extract_preferences(query)`
- Only store what user explicitly stated
- Call `self.memory_service.remember_with_customer(customer_id, facts_to_store)`
- **Do NOT** call resolver, Product API, or `generate_response` with `api_products`
- Only store category (if valid and not default "headphones"), budget > ₹500, wireless, noise_cancellation, brand

### `ConversationService._generate_store_response(query, intent)`

**Rules:**

- Acknowledge preference was stored
- **Do NOT** mention products
- **Do NOT** call Product API
- **Do NOT** show product cards
- Simple deterministic acknowledgement
- Build response from: category, budget, wireless, noise_cancellation, brand parts
- Default follow-up: `"Is there anything else you'd like to tell me about your preferences?"`

### `ConversationService._generate_recall_response(query, intent)`

**Rules:**

- **Do NOT** call Product API
- **Do NOT** show product cards
- **Do NOT** return every saved preference — only what's relevant to the question
- **Do NOT** fabricate missing preferences
- Check query type: headphone budget → recall budget; brand/prefer → recall brand; general → recall fragments
- Only mention first relevant fragment as simple acknowledgement

### `ConversationService._generate_normal_response(query, intent)`

**Rules:**

- **NO** Product API call
- **NO** preference storage
- **NO** memory recall (unless query specifically asks about memories)
- Pure conversational response
- **Zero** MCP calls
- **Zero** Product API calls
- Handles: greetings, gratitude, ANC questions, wireless questions, thinking aloud, defaults

### `ConversationService._call_product_api(resolved_constraints)`

**Execution order:**

1. Build query params from resolved constraints (category, max_price, brand, required_features, excluded_features, sort_by)
2. POST to `http://127.0.0.1:8000/api/products/search` with `httpx.post(timeout=15.0)`
3. If 200: return `data.get("products", [])`
4. If non-200: log warning, return `[]`
5. If `httpx.RequestError`: log error, return `[]` (graceful degradation)
6. If unexpected exception: log error, return `[]`

**Return:** `List[Dict[str, Any]]` of product dicts from the Product API.

### `ConversationService._update_memory_if_appropriate(query, constraints)`

**Rules — persistent only (not temporary):**

- Category preferences are persistent (if category keyword matches headphones, footwear, etc.)
- Budget preferences are persistent only if > ₹500 (distinguishes from one-time constraints)
- Wireless preference stored only if explicitly preferred as a user preference
- Noise cancellation stored only if explicitly preferred
- Brand stored only if explicitly mentioned
- Call `self.memory_service.remember_with_customer(customer_id, facts_to_store)` if any facts

### `ConversationService._determine_intent_mode(intent, query)`

**Returns one of:** `STORE` / `RECALL` / `NORMAL_RESPONSE` / `PRODUCT_DISCOVERY`

**Rules (single-user, no customer isolation):**

- **STORE**: When user explicitly states a preference (`I prefer X`, `I like X`, `I want X`)
  - Check for category, brand, wireless, noise cancellation
  - Also: budget > ₹500 in query (not a product request)
- **RECALL**: When query references previous context, usage buckets, or asks for recommendations based on stored preferences
  - Question terms: `"what do you know"`, `"what is my"`, `"my preferences"`, `"my budget"`, `"my brand"`
  - Query ends with `?` and contains recall terms
  - Usage from intent extraction, or conversation history references (`before`, `previous`, `remember`, `earlier`)
- **PRODUCT_DISCOVERY**: When user explicitly requests product discovery
  - Product request terms: `"recommend"`, `"suggest"`, `"search"`, `"compare"`, `"show me"`, `"find"`, `"look for"`
  - Exclude: `"thinking about"`, `"might"`, `"sometime"`, `"explore"`, `"considering"`
- **NORMAL_RESPONSE**: All other queries

### `ConversationService._get_relevant_memories(query)`

**Execution order:**

1. Get customer ID — always `"customer_001"`
2. Call `self.memory_service.recall(query)`
3. If fragments exist: return `list(recall.fragments)`
4. If no fragments: return `[]`
5. On exception: log warning, return `[]`

### `ConversationService.clear_conversation()`

**Execution order:**

1. Set `self.conversation_history = []`
2. Log information
3. **Do NOT** call any memory service methods — memories are preserved

### `MemoryService.remember(facts)`

**Execution order:**

1. Validate facts — strip whitespace, remove empty strings
2. If no valid facts after cleaning: raise `MemoryAdapterError`
3. Call `self.memory_adapter.remember("customer_001", tuple(cleaned_facts))`
4. Log success with fact count
5. Return `RememberResult`

### `MemoryService.recall(query)`

**Execution order:**

1. Call `self.memory_adapter.recall("customer_001", query)`
2. Log success with fragment count
3. Return `RecallResult`

### `MemoryService.forget(customer_id, dataset)`

**Execution order:**

1. Call `self.memory_adapter.forget(customer_id, dataset)`
2. Log success with result count
3. Return result count (extracted from tool response text)

### `MemoryService.get_customer()`

**Returns:** `"customer_001"` (constant for single-user mode)

### `MemoryService.get_dataset()`

**Returns:** `"retail_demo"` (constant demo dataset)

### `MemoryService.remember_with_customer(customer_id, facts)`

**Execution order:**

1. Determine dataset (always `DEMO_DATASET` `"retail_demo"` in single-user mode)
2. Call `self.memory_adapter.remember(customer_id, tuple(facts))`
3. Log success with fact count
4. Return `RememberResult`

### `MemoryService.recall_with_customer(customer_id, query)`

**Execution order:**

1. Determine dataset (always `DEMO_DATASET` `"retail_demo"`)
2. Call `self.memory_adapter.recall(customer_id, query)`
3. Log success with fragment count
4. Return `RecallResult`

### `MemoryService.get_customer_isolation_report(customer_id)`

**Execution order:**

1. Determine dataset (`DEMO_DATASET`)
2. Call `self.memory_adapter.recall(customer_id, "all memories")`
3. Return dict with `customer_id`, `dataset`, `fragment_count`, `fragments`, `isolation_verified=True`

### `MemoryService.close()`

**Execution order:**

1. If adapter has `close()` method, call it
2. Release resources

## Function Parameters and Return Values

| Method | Key Parameters | Return Value |
|---|---|---|
| `ConversationService.process_query(query)` | `query: str` | `Dict` with keys: `response`, `recommended_products`, `memories`, `preferences`, `success`, `error`, `llm_used`, `response_mode`, `intent_source`, `follow_up_question` |
| `ConversationService._store_preference_only(query, intent)` | `query: str`, `intent: Dict` | `None` (stores memories side effect) |
| `ConversationService._generate_store_response(query, intent)` | `query: str`, `intent: Dict` | `Dict` with keys: `message`, `llm_used`, `response_mode`, `follow_up_question` |
| `ConversationService._generate_recall_response(query, intent)` | `query: str`, `intent: Dict` | `Dict` with keys: `message`, `llm_used`, `response_mode`, `follow_up_question` |
| `ConversationService._generate_normal_response(query, intent)` | `query: str`, `intent: Dict` | `Dict` with keys: `message`, `llm_used`, `response_mode`, `follow_up_question` |
| `ConversationService._call_product_api(constrained)` | `resolved_constraints: Dict` | `List[Dict[str, Any]]` of product dicts |
| `ConversationService._update_memory_if_appropriate(query, constraints)` | `query: str`, `constraints: Dict` | `None` (stores memories side effect) |
| `ConversationService._determine_intent_mode(intent, query)` | `intent: Dict`, `query: str` | One of: `STORE` / `RECALL` / `NORMAL_RESPONSE` / `PRODUCT_DISCOVERY` |
| `ConversationService._get_relevant_memories(query)` | `query: str` | `List[str]` of memory fragments |
| `MemoryService.remember(facts)` | `facts: List[str]` | `RememberResult` with `fact_count` |
| `MemoryService.recall(query)` | `query: str` | `RecallResult` with `fragments`, `result_count` |
| `MemoryService.forget(customer_id, dataset)` | `customer_id: str`, `dataset: str` | `int` — count of deleted memories |
| `MemoryService.get_customer()` | — | `str` `"customer_001"` |
| `MemoryService.get_dataset()` | — | `str` `"retail_demo"` |
| `ConversationService.clear_conversation()` | — | `None` |

## Exceptions, Error Handling and Side Effects

- **`MemoryAdapterError`** — raised when memory operations fail at the adapter level
  - Caught in `MemoryService.remember/recall/forget` and re-raised as `MemoryAdapterError` with contextual info
  - Caught in `ConversationService._store_preference_only` — logged as warning, no crash
  - Caught in `ConversationService._generate_recall_response` — simplified response given

- **`MemoryOperationError`** — raised when MCP session initialization fails or tool call fails
  - Propagates up from `McpMemoryAdapter._call()`
  - In Streamlit UI: caught in `try/except` block, displayed as `st.error()`

- **`httpx.RequestError`** — raised when Product API is unreachable
  - Handled in `_call_product_api()`: returns `[]` (empty results, not crash)
  - UI shows no product cards gracefully

- **Streamlit component errors** — all memory/recall/product API calls wrapped in `try/except` within UI components
  - Never allow a single failed operation to crash the entire app
  - User always sees actionable error message

- **Side effects:**
  - `process_query()` always updates `self.conversation_history`
  - `remember()` always persists to the demo dataset via MCP
  - `forget()` always deletes from the demo dataset via MCP
  - `clear_conversation()` only clears Streamlit state, NOT Cognee memories
  - Memory operations use the single `retail_demo` dataset — no customer isolation

## Actual Implementation Walkthrough in Execution Order

### Typical User Flow: Store a Preference

1. User types: `"I prefer Sony headphones."`
2. `st.chat_input` triggers `service.process_query("I prefer Sony headphones.")`
3. `process_query` extracts intent → `starts_with_preference` is `True` → category `"headphones"` detected → **mode = `"STORE"`**
4. `_store_preference_only()` extracts preferences: brand=`"Sony"`, category=`"headphones"` (but default, so not stored since `category == "headphones"`), no budget, wireless not mentioned, no noise_cancellation
5. Only brand fact stored: `"Prefers Sony headphones"`
6. `self.memory_service.remember_with_customer("customer_001", ["Prefers Sony headphones"])` → MCP `remember` tool
7. `_generate_store_response()` builds acknowledgement: `"Understood! I'll remember that you prefer Sony headphones."`
8. Return dict: `response="Understood! I'll remember that you prefer Sony headphones.", response_mode="STORE", follow_up_question="Is there anything else you'd like to tell me about your preferences?"`
9. Assistant message rendered in chat, follow-up shown as caption

### Typical User Flow: Product Recommendation

1. User types: `"Recommend wireless headphones under ₹5000."`
2. `process_query` extracts intent → product request terms detected (`"recommend"`) → no recall terms → **mode = `"PRODUCT_DISCOVERY"`**
3. Constraint resolver resolves constraints from customer ID, current query, conversation history, intent hints
4. `normalize_constraints()` translates to API shape: `max_price`, `brand`, `required_features`, `excluded_features`, `budget`
5. `_call_product_api(constraints)` → POST to `http://127.0.0.1:8000/api/products/search` with `max_price=5000`, `wireless` as feature
6. Product API returns matching products (filtered by budget and wireless)
7. `generate_response()` creates natural language response with product cards
8. `_update_memory_if_appropriate()` may store: wireless preference, budget if > ₹500, brand if mentioned
9. Return dict with `response`, `recommended_products` (product IDs), `response_mode="PRODUCT_DISCOVERY"`
10. Assistant message with product cards rendered in chat

### Typical User Flow: Recall Preference

1. User types: `"What do you remember about my preferences?"`
2. `process_query` extracts intent → recall question terms detected → **mode = `"RECALL"`**
3. `_generate_recall_response()` checks query type
4. General recall → `self.memory_service.recall("preferences")` → MCP `recall` tool
5. Fragments extracted, first fragment used as acknowledgement
6. Return dict with `response`, `response_mode="RECALL"`, `follow_up_question`
7. Assistant message rendered

### Typical User Flow: Clear Chat

1. User clicks **"🗑️ Clear conversation"** in sidebar
2. `service.clear_conversation()` sets `self.conversation_history = []`
3. Chat reruns with empty history
4. Memoies in Cognee `retail_demo` dataset are **preserved** — confirmed by memory inspector still showing stored preferences

### Typical User Flow: Delete All Memories

1. User clicks **"Clear All Cognee Memories"** in sidebar expander
2. Warning displayed: `"This will delete ALL persistent memories from the demo dataset. This operation is irreversible."`
3. User clicks **"Confirm Delete All Memories"**
4. `service.memory_service.forget("customer_001", "retail_demo")` → MCP `forget` tool
5. Success confirmation: `st.success(f"Deleted {result} memories from dataset: retail_demo")`
6. Verification: `fresh_recall = service.memory_service.recall("all")` → if `fresh_recall.fragments` is empty → `st.success("Verification complete: No memories remain for this dataset.")`
7. If fragments still remain → `st.warning(f"Warning: {len(fresh_recall.fragments)} fragments still remain. Deletion may not have been complete.")`
8. Memoery inspector refreshes — no fragments shown
9. Assistant remembers nothing about previous preferences

## Input and Output Data Flow

### Input Flow (user → assistant)

```
User prompt
    → ConversationService.process_query()
        → extract_intent()  [deterministic, LLM‑optional]
            → intent dict with: source, usage, etc.
        → _determine_intent_mode()  [rules-based]
            → one of: STORE / RECALL / NORMAL_RESPONSE / PRODUCT_DISCOVERY
        → mode‑specific handler
            → _store_preference_only / _generate_recall_response / _generate_normal_response / _call_product_api → generate_response
        → update conversation_history
        → build return dict
    → Streamlit renders assistant message
        → st.markdown(message)
        → if products: render_product_card(product) × N
        → if follow_up: st.caption(follow_up)
        → if response_mode: st.caption(f"Response source: {response_mode}")
        → if intent_source: st.caption(f"Intent: {intent_source}")

### Output Flow (assistant → user)

```
Return dict
    → assistant message text: st.markdown(response)
    → product cards: for product in recommended_products: render_product_card(product)
    → follow-up question: st.caption(follow_up_question)
    → response source: st.caption(f"Response source: {response_mode}")
    → intent: st.caption(f"Intent: {intent_source}")
```

## Integration with MCP, Cognee, Ollama and Product API

### MCP Integration

- **All memory operations** go through `McpMemoryAdapter`, which manages one persistent `_PersistentMcpSession`
- **`remember` tool**: sends `data` (newline‑joined facts), `dataset_name` (`retail_demo`), `background=False`, `self_improvement=True`
- **`recall` tool**: sends `query`, `datasets` (comma‑separated string, here just `"retail_demo"`), `search_type`, `top_k`
- **`forget` tool** (new): sends `customer_id` and `dataset_name` for deletion
- **`_raise_on_tool_error()`**: inspects tool results — if `isError` or text starts with `"Error:"`, raises `MemoryOperationError`
- **`_fragments_from_result()`**: extracts stored memories from recall payload, stripping the protocol summary line (`"X memories found..."`)

### Cognee Integration

- Cognee API started from correct `.venv` with `EMBEDDING_*` vars from `.env`
- Embedding provider: `ollama`, model: `nomic-embed-text:latest`, endpoint: `http://127.0.0.1:11434/api/embed`, dimensions: `768`
- All memory operations (store, recall, delete) go through the MCP adapter — **no direct Cognee Python SDK or CLI calls** from application code

### Ollama Integration

- Two required models: `nomic-embed-text:latest` (embeddings) and `llama3.1:latest` (LLM)
- Embedding endpoint: `http://127.0.0.1:11434/api/embed`
- LLM endpoint: `http://127.0.0.1:11434`
- `LLM_API_KEY=ollama` (LiteLLM uses this as the API key; OpenAI returns 401 which Cognee wraps as 409 — fixed by `.env` configuration)
- LLM status panel in UI reports: configured, enabled, provider, model, reachable, model_available, mode, summary, detail, latency

### Product API Integration

- Catalog loaded from `data/products.json` (20 products, migrated from original project)
- All product discovery goes through `http://127.0.0.1:8000/api/products/search`
- Endpoints: `/api/products/categories`, `/api/products/{product_id}`, `/api/products/search`
- Filters: category, max_price, min_price, brand, required_features, excluded_features, excluded_product_ids, sort_by, limit
- Ranking: rating (desc), price (asc/desc), product ID (alpha tiebreaker)
- Graceful degradation: if API unavailable, returns empty results, not crash

## Important Design Decisions and Trade-offs

### Single‑User Dataset (`retail_demo`)

- **Decision**: One shared dataset for all preferences — no customer isolation.
- **Trade-off**: Simpler architecture, no need for dataset naming or isolation verification. Not suitable for multi‑tenant deployment.
- **Rationale**: POC scope is single‑user; customer isolation would add unnecessary complexity.

### Preference Storage — Only Explicit Statements

- **Decision**: Only store preferences the user explicitly states; do not infer or persist temporary constraints.
- **Trade-off**: Fewer stored memories, but avoids "polluting" the demo dataset with one‑time budget requests or accidental inferences.
- **Rationale**: Matches the "current explicit user instruction" precedence rule; keeps demo focused on durable preferences.

### Budget Persistence Threshold (₹500)

- **Decision**: Only store budget preferences > ₹500 as durable; smaller budgets are treated as one‑time constraints.
- **Trade-off**: Some low‑budget preferences won't persist across sessions, but the threshold avoids storing trivial constraints.
- **Rationale**: Matches the memory rules: "Temporary information: Current budget" — substantial budgets are durable, tiny ones are not.

### Zero MCP Calls in NORMAL_RESPONSE

- **Decision**: Ordinary conversation never invokes MCP or Product API.
- **Trade-off**: Some contextual memory could be available, but the rule "Do not persist every message or one-time instruction" supports keeping MCP calls explicit and intentional.
- **Rationale**: Clear separation of concerns; `NORMAL_RESPONSE` is for greetings, explanations, general knowledge.

### Deterministic Fallback When LLM Unavailable

- **Decision**: If Ollama/models not available, all responses are deterministic from Product API data and preference storage.
- **Trade-off**: No natural‑language generation from the LLM, but the assistant remains fully functional for product discovery and preference management.
- **Rationale**: POC requirement for a "reliable, fully working end‑to‑end demo" — deterministic fallback guarantees functionality even when models aren't running.

### Product API as Source of Truth for Products

- **Decision**: Never invent products, prices, ratings, or specifications. Always query the migrated Product API catalogue.
- **Trade-off**: If the API is down, no products are shown (empty state), rather than showing fabricated products.
- **Rationale**: Non‑negotiable rule: "Never invent products, prices, ratings, specifications or availability."

## A Concrete Usage Example

### Session 1: Storing Preferences

| User | Assistant |
|---|---|
| `I prefer Sony headphones.` | `Understood! I'll remember that you prefer Sony headphones.` |
| `I like over‑ear headphones.` | `Got it! I'll remember that you're interested in over‑ear shopping.` |
| `My budget is ₹8000.` | `Understood! I'll remember your preferred budget of ₹8000.` |

### Session 2 (after restart): Recall + Recommend

| User | Assistant |
|---|---|
| `What do you remember about my headphone preferences?` | `Based on what you've told me: You previously mentioned that you prefer Sony headphones. You also have a preferred budget of ₹8000.` |
| `Recommend wireless headphones.` | Shows product cards for wireless headphones from the catalogue (filtered by Sony preference if applicable, otherwise just wireless + budget) |
| `What is Active Noise Cancellation?` | `ANC stands for Active Noise Cancellation. It uses microphones to detect and cancel ambient noise...` |

### Session 3: Delete Memories

| User | Assistant |
|---|---|
| (clicks **Clear All Cognee Memories**) | (warning dialog) |
| (clicks **Confirm Delete All Memories**) | `Deleted 3 memories from dataset: retail_demo` |
| `What do you remember about my headphone preferences?` | `You haven't mentioned any preferences yet, so I don't have that information.` |
| `I prefer Bose headphones.` | `Understood! I'll remember that you prefer Bose headphones.` (new preference stored) |

## Tests Covering the Module

- `tests/test_mcp_adapter.py` — 15 unit tests for MCP adapter: recall, store, error handling, dataset as string vs list, context manager close, environment endpoint, tool errors wrapped, cancelled initialization
- `tests/test_memory.py` — memory‑related tests (if any; currently the test suite has 26 total passing tests covering both test files)
- Unit‑test style: most tests use `unittest.mock.patch` to mock `McpMemoryAdapter._PersistentMcpSession`, testing logic in isolation without requiring a running MCP server
- Integration‑style: the existing test suite confirms all 26 tests pass after the `.env` embedding fix, `fragments[1:]` → `fragments` fix, and `start_services.sh`/`health_check.sh` rewrites

## Actual Known Limitations

1. **MCP persistent session event loop** — `_PersistentMcpSession` has cancellation issues when the adapter is instantiated outside Streamlit's event loop. The app is designed for Streamlit; running the memory service in a pure CLI script may fail with `CancelledError`. The tests work because they mock the session.

2. **No multi‑user support** — One shared `retail_demo` dataset for all preferences. Customer‑specific dataset naming or isolation is not implemented.

3. **MCP session survival across Streamlit reruns** — Streamlit reruns can cancel daemon threads. The `McpMemoryAdapter` has a thread‑alive check and resets `_connection` if the thread is dead, but a fully seamless recovery across reruns is not guaranteed without re‑initialization.

4. **Product API downtime** — If the Product API is not running, all product‑discovery routes return empty results (`[]`) and the assistant falls back to deterministic responses about "no products found matching your criteria."

5. **Ollama model availability** — Both `nomic-embed-text:latest` and `llama3.1:latest` must be installed and running in Ollama for embeddings and LLM responses. If either is missing, embedding‑dependent operations fail and the assistant falls back to deterministic responses.

6. **No vector database** — Similarity‑based search is not implemented. Memory search uses Cognee's keyword/text‑based `recall` tool only.

7. **Single‑turn preference extraction** — `extract_preferences()` in `app/chatbot.py` handles simple keyword extraction but may miss complex or negated statements (e.g., "I don't want wireless headphones"). Future improvement: LLM‑based extraction.

8. **Budget threshold hard‑coded at ₹500** — The persistence threshold for budget preferences is a fixed constant. Adjusting it requires code change and redeployment.

9. **MCP `forget` tool availability** — The `forget` method was added to the MCP adapter assuming the Cognee MCP server version supports a `forget` tool. If the installed version does not, the method will raise `MemoryAdapterError` with the message "MCP forget tool not available". The health check and UI should gracefully handle this.

10. **Chat history not persisted across restarts** — Streamlit's `st.session_state` is ephemeral. Clearing conversation or closing the browser loses the chat history, but Cognee memories in the `retail_demo` dataset persist.

---