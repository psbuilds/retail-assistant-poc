# Conversation Service (`app/services/conversation_service.py`)

## Purpose and Responsibility

The Conversation Service (`app/services/conversation_service.py`) is the **central orchestration layer** of the Retail Memory Assistant POC. It directs user queries through a deterministic pipeline: intent extraction → intent routing → mode‑specific handling → response generation → memory update → conversation history update. It is the single point where all integration boundaries (MCP, Product API, LLM) are crossed, and where the precedence rules (current instruction → conversation context → persistent memory → application defaults) are implemented.

**Key design goal**: Only `PRODUCT_DISCOVERY` invokes the Product API and shows product cards. `STORE`, `RECALL`, and `NORMAL_RESPONSE` never call the Product API and never show product cards.

## Place in Overall Architecture

```
Streamlit UI
    |-- chat_input → process_query()
    |-- sidebar controls (memory, clear chat, quick prompts)
    |
Conversation Service (this file)
    |-- _determine_intent_mode()     → STORE / RECALL / NORMAL_RESPONSE / PRODUCT_DISCOVERY
    |-- process_query()              → full pipeline orchestrator
    |-- _store_preference_only()     → store durable preferences
    |-- _generate_recall_response()  → recall-oriented responses
    |-- _generate_normal_response()  → ordinary conversation
    |-- _call_product_api()          → Product API search
    |-- _update_memory_if_appropriate() → store prefs from product searches
    |-- clear_conversation()         → clear only Streamlit state
    |-- _get_relevant_memories()     → fragments for display
    |
Memory Service (Cognee MCP)
    |-- store, recall, delete memories
    |
Product API
    |-- catalogue search, product cards
    |
Ollama + Cognee MCP Server
    |-- embeddings, LLM for response generation
```

## Internal Dependencies

- `app.api.product_api.app` — FastAPI instance for Product API calls
- `app.chatbot.extract_preferences()` — deterministic preference extraction from user query
- `app.services.constraint_resolver.ConstraintResolver` — resolves constraints with memory + query precedence
- `app.services.intent_extraction.extract_intent()` — classifies user intent
- `app.services.memory_service.MemoryService` — memory operations (remember/recall/forget)
- `app.services.response_generation.generate_response()` — LLM or deterministic response
- `app.llm.get_llm_status()` / `LLMStatus` — LLM reachability reporting
- `httpx` — HTTP client for Product API calls
- `app.services.memory_service.DEMO_DATASET` — `"retail_demo"` constant

## Every Class and Its Role

### `ConversationService` (main class, instantiated in `streamlit_app.main()`)

- **Constructor**: `ConversationService(memory_service=None, use_llm=True)`
  - Default memory service: `MemoryService()` (demo dataset)
  - `use_llm=True` enables LLM status as "active"; `False` forces deterministic mode
- **`process_query(query)`** — entry point for every user turn
  - Full pipeline: intent extraction → routing → mode handling → response gen → memory update → history update
- **`set_customer(customer_id)`** — sets the current customer (single-user: always `"customer_001"`)
- **`clear_conversation()`** — clears only Streamlit conversation history; **does NOT** delete Cognee memories
- **`get_llm_status()`** — delegates to `get_llm_status(check_provider=True)` from `app.llm`
- **`_determine_intent_mode(intent, query)`** — rules‑based mode classifier
- **`_store_preference_only(query, intent)`** — STORE mode: extract + persist preferences
- **`_generate_recall_response(query, intent)`** — RECALL mode: retrieve + speak relevant memories
- **`_generate_normal_response(query, intent)`** — NORMAL_RESPONSE mode: pure conversational
- **`_call_product_api(constraints)`** — PRODUCT_DISCOVERY: POST to Product API
- **`_update_memory_if_appropriate(query, constraints)`** — store prefs after product search
- **`_get_relevant_memories(query)`** — return fragments for display in response

## Constructor Parameters and Important Attributes

### `ConversationService.__init__(self, memory_service=None, use_llm=True)`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `memory_service` | `MemoryService` | `None` (→ new instance) | Uses demo dataset `"retail_demo"`; single‑user mode |
| `use_llm` | `bool` | `True` | If `False`, LLM status forced to deterministic; UI cannot claim model is active |
| `self.memory_service` | `MemoryService` | injected or new | `self.memory_service.get_customer()` → `"customer_001"`; `self.memory_service.get_dataset()` → `"retail_demo"` |
| `self.conversation_history` | `List[Dict]` | `[]` | Each entry: `{"role": "user"/"assistant", "content": "..."}` |
| `self.use_llm` | `bool` | `True` | Controls LLM‑active flag in status and response generation |
| `self._product_api_base` | `str` | `"http://127.0.0.1:8000"` | Base URL for Product API calls |

## Every Maintained Function and Method

### `ConversationService.process_query(query)`

**Execution order (full pipeline):**

1. **Get customer ID**: `customer_id = self.memory_service.get_customer()` → always `"customer_001"`
2. **Empty customer check**: if not `customer_id` → return error dict (no products, no memories)
3. **Intent extraction**: 
   ```python
   intent = extract_intent(
       query,
       conversation_history=self.conversation_history,
       use_llm=self.use_llm,
   )
   ```
   - Returns dict with: `source` (`"deterministic"` or `"llm"`), `usage`, `category`, `brand`, `budget`, `wireless`, `noise_cancellation`, etc.
4. **Determine intent mode**: `self._determine_intent_mode(intent, query)`
5. **Route based on mode** (four mutually exclusive paths):

   ### a) `STORE` mode

   - **`_store_preference_only(query, intent)`**
     - Extract preferences via `extract_preferences(query)` (also used from `intent`)
     - Build `facts_to_store` list:
       - Category: if valid (not default "headphones") → `"Prefers {category} shopping"`
       - Budget: if `max_budget > 500` → `"Has a budget up to ₹{max_budget}"`
       - Wireless: if `wireless is not None` → `"Prefers wireless headphones"` (or `"Prefers wired headphones"`)
       - Noise cancellation: if `noise_cancellation` → `"Prefers noise-cancelling headphones"`
       - Brand: if `brand` → `"Prefers {brand} headphones"`
     - **Persist**: `self.memory_service.remember_with_customer("customer_001", facts_to_store)`
     - **Generate response**: `_generate_store_response(query, intent)`
       - Build acknowledgement from stored parts (category, budget, wireless, noise_cancellation, brand)
       - Default follow‑up: `"Is there anything else you'd like to tell me about your preferences?"`
   
   ### b) `RECALL` mode

   - **`_generate_recall_response(query, intent)`**
     - Check query type via `query_lower = query.casefold()`:
       - `"headphone budget" in query_lower or "budget" in query_lower` → `_recall_budget()`
       - `"brand" in query_lower or "prefer" in query_lower` → `_recall_brand()`
       - `"colour"/`"color" in query_lower` → fixed response about colour
       - General → `self.memory_service.recall(query)` → fragments → first fragment as acknowledgement
     - Build response:
       - If budget recalled: `"Your preferred headphone budget is ₹{amount}."`
       - If brand recalled: `"You previously mentioned that you prefer {brand} headphones."`
       - If general fragments: `"Based on what you've told me: {first_fragment}."`
       - If nothing recalled: `"You haven't mentioned your headphone budget yet, so I don't have that information."`
     - Default follow‑up: `"Is there anything specific about your preferences you'd like to know more about?"`

   ### c) `NORMAL_RESPONSE` mode

   - **`_generate_normal_response(query, intent)`**
     - Check if query is a memory question (terms: `"remember"`, `"preference"`, `"what do you know"`, `"my budget"`, `"my brand"`)
     - If memory question → simple referral: `"I'd be happy to tell you about your preferences! Could you ask me specifically what you'd like to know?"`
     - Handle greeting terms: `"hi"`, `"hello"`, `"hey"`, `"how are you"` → `"Hello! I'm doing well..."`
     - Handle gratitude: `"thank"`, `"thanks"` → `"You're welcome!"`
     - Handle thinking terms: `"thinking about"`, `"considering"`, `"might"`, `"maybe"` → contextual response
     - Handle `"ANC"` → explanation of Active Noise Cancellation
     - Handle `"wireless"` → explanation of wireless headphones
     - Default: `"I understand. I'm here to help you find the right headphones or answer any questions you have. What's on your mind?"`
     - **CRITICAL RULES**:
       - **NO** Product API call
       - **NO** preference storage
       - **NO** memory recall (unless query specifically asks about memories)
       - **Zero** MCP calls
       - **Zero** Product API calls

   ### d) `PRODUCT_DISCOVERY` mode

   - **Constraint resolution**:
     ```python
     resolver = ConstraintResolver(self.memory_service)
     resolved_constraints = resolver.resolve_constraints(
         customer_id="customer_001",
         current_query=query,
         conversation_context=self.conversation_history,
         intent_hints=intent,
     )
     ```
   - **Normalize constraints**:
     ```python
     constraints = normalize_constraints(resolved_constraints)
     ```
     - Translates: category, brand, max_price, min_price, required_features (comma‑separated), excluded_features (comma‑separated), budget (min/max), sort_preference, wireless boolean, noise_cancellation boolean
   - **Call Product API**:
     ```python
     api_products = self._call_product_api(constraints)
     ```
     - POST to `http://127.0.0.1:8000/api/products/search` with params built from constraints
     - Graceful degradation: if API unavailable → `[]` (empty results, not crash)
   - **Generate natural language response**:
     ```python
     response_result = generate_response(
         original_query=query,
         resolved_constraints=constraints,
         api_products=api_products,
         conversation_context=self.conversation_history,
     )
     ```
     - If LLM available: natural language response with product cards
     - If LLM unavailable: deterministic response from Product data + preference context
   - **Update memory (if appropriate)**:
     ```python
     self._update_memory_if_appropriate(query, resolved_constraints)
     ```
     - Store category, budget (if > ₹500), wireless (if explicitly preferred), noise_cancellation (if explicitly preferred), brand (if explicitly mentioned)
   - **Build return dict**:
     - `recommended_products` from `response_result.get("recommended_product_ids", [])`
     - `response` from `response_result["message"]`
     - `memories` from `self._get_relevant_memories(query)`
     - `response_mode = "PRODUCT_DISCOVERY"`

6. **Update conversation history**:
   ```python
   self.conversation_history.append({"role": "user", "content": query})
   self.conversation_history.append({"role": "assistant", "content": response_result["message"]})
   ```
   - Keep history manageable: `if len > 20: self.conversation_history = self.conversation_history[-20:]`

7. **Build final return dict**:
   ```python
   return {
       "response": response_result["message"],
       "recommended_products": recommended_products,
       "memories": self._get_relevant_memories(query),
       "preferences": {},
       "success": True,
       "error": None,
       "llm_used": bool(response_result.get("llm_used", False)),
       "response_mode": intent_mode,
       "intent_source": intent.get("source", "deterministic"),
       "follow_up_question": response_result.get("follow_up_question", "How can I help you further?"),
   }
   ```

### `ConversationService._determine_intent_mode(intent, query)`

**Returns one of**: `STORE` / `RECALL` / `NORMAL_RESPONSE` / `PRODUCT_DISCOVERY`

**Rules (single‑user, no customer isolation)**:

- **STORE**: User explicitly states a preference
  - `query_lower.startswith("i prefer")`, `query_lower.startswith("i like")`, `query_lower.startswith("i love")`, `query_lower.startswith("i want")`
  - Category detected in query (headphones, earbuds, audio, professional, footwear, apparel, bags, smartphones, laptops, smartwatches)
  - Brand detected (sony, bose, jabra, beats, sennheiser)
  - Wireless mentioned: `" wireless"` in query or starts with `"wireless "`
  - Noise cancellation / ANC mentioned: `"noise cancellation"` or `"anc"` in query
  - **Budget** > ₹500 **AND** not a product request (`"recommend"`, `"suggest"`, `"search"`, etc. not in query)
- **RECALL**: User asks about stored preferences or context
  - Question terms: `"what do you know"`, `"what is my"`, `"my preferences"`, `"my budget"`, `"my brand"`
  - Query ends with `?` and contains recall terms
  - `intent.get("usage")` is set
  - Conversation history references: `"before"`, `"previous"`, `"like last"`, `"remember"`, `"earlier"` in last 3 entries
- **PRODUCT_DISCOVERY**: User explicitly requests product discovery
  - Product request terms: `"recommend"`, `"suggest"`, `"search"`, `"compare"`, `"show me"`, `"find"`, `"look for"`
  - **NOT** if exclusion terms present: `"thinking about"`, `"might"`, `"sometime"`, `"explore"`, `"considering"`
  - Must not be a STORE or RECALL pattern first
- **NORMAL_RESPONSE**: All other queries
  - Greetings, gratitude, general conversation, ANC questions, wireless questions, thinking aloud
  - **CRITICAL**: Never a product request without the above inclusion/exclusion patterns

### `ConversationService._store_preference_only(query, intent)`

**Execution order** (see `process_query` STORE path for full context):

1. Get customer ID → always `"customer_001"`
2. Extract preferences: `extract_preferences(query)` → dict with `category`, `budget`, `wireless`, `noise_cancellation`, `brand`
3. Build `facts_to_store: List[str] = []`
4. **Category**: if `category` and `category != "headphones"` and `category in valid_categories` → `facts_to_store.append(f"Prefers {category} shopping")`
5. **Budget**: `max_budget = budget.get("max") if isinstance(budget, dict) else None`; if `max_budget is not None and max_budget > 500` → `facts_to_store.append(f"Has a budget up to ₹{max_budget}")`
6. **Wireless**: if `query_prefs.get("wireless") is not None` → if `True`: `facts_to_store.append("Prefers wireless headphones")`; if `False`: `facts_to_store.append("Prefers wired headphones")`
7. **Noise cancellation**: if `query_prefs.get("noise_cancellation")` → `facts_to_store.append("Prefers noise-cancelling headphones")`
8. **Brand**: if `brand` → `facts_to_store.append(f"Prefers {brand} headphones")`
9. **If facts_to_store not empty**: `self.memory_service.remember_with_customer("customer_001", facts_to_store)`; log info
10. **If no facts**: silently do nothing (no error, no storage)

### `ConversationService._generate_store_response(query, intent)`

**Execution order**:

1. Get customer ID → always `"customer_001"`
2. Extract intent parts: `category = intent.get("category")`, `budget = intent.get("budget", {})`, `max_budget = budget.get("max") if isinstance(budget, dict) else None`, `brand = intent.get("brand")`, `wireless = intent.get("wireless")`, `noise_cancellation = intent.get("noise_cancellation")`
3. Build `parts: List[str] = []`
4. If `category and category != "headphones"` → `parts.append("Got it! I'll remember that you're interested in {category}.")`
5. If `max_budget is not None and max_budget > 500` → `parts.append("Understood! I'll remember your preferred budget of ₹{max_budget}.")`
6. If `wireless is not None and wireless` → `parts.append("Got it! I'll remember that you prefer wireless headphones.")`
7. If `noise_cancellation is not None and noise_cancellation` → `parts.append("Got it! I'll remember that you prefer headphones with active noise cancellation.")`
8. If `brand` → `parts.append("Understood! I'll remember that you prefer {brand} headphones.")`
9. If `not parts` → `response_text = "Got it! I'll remember that."`
10. Else → `response_text = " ".join(parts) + "."`
11. **Default follow‑up**: `"Is there anything else you'd like to tell me about your preferences?"`
12. **Return dict**: `{"message": response_text, "llm_used": False, "response_mode": "STORE", "follow_up_question": follow_up}`

### `ConversationService._generate_recall_response(query, intent)`

**Execution order**:

1. Get customer ID → always `"customer_001"`
2. If not `customer_id` → return error dict with default memory‑unavailable response
3. `query_lower = query.casefold()`
4. **Budget query**: if `"headphone budget" in query_lower or "budget" in query_lower`:
   - `_recall_budget(customer_id)` → if budget fact found → `response = budget_fact`; else → `response = "You haven't mentioned your headphone budget yet, so I don't have that information."`
5. **Brand query**: elif `"brand" in query_lower or "prefer" in query_lower`:
   - `_recall_brand(customer_id)` → if brand fact found → `response = "You previously mentioned that you prefer {brand} headphones."`; else → `response = "You haven't mentioned your preferred headphone brand yet, so I don't have that information."`
6. **Colour query**: elif `"colour" in query_lower or "color" in query_lower` → `response = "You haven't mentioned your preferred headphone colour yet, so I don't have that information."`
7. **General recall**: else:
   - `recall = self.memory_service.recall(query)`
   - `fragments = list(recall.fragments) if recall.fragments else []`
   - If not fragments: `response = "You haven't mentioned any preferences yet, so I don't have that information."`
   - Else: `response = f"Based on what you've told me: {fragments[0]}."`
8. **Return dict**: `{"message": response, "llm_used": False, "response_mode": "RECALL", "follow_up_question": "Is there anything specific about your preferences you'd like to know more about?"}`

### `ConversationService._recall_budget(self, customer_id)`

**Execution order**:

1. `recall = self.memory_service.recall("preferences")`
2. If `recall.fragments`:
   - `from app.chatbot import extract_preferences`
   - For each `frag in recall.fragments`:
     - `prefs = extract_preferences(frag)`
     - `max_budget = prefs.get("max_budget")`
     - If `max_budget`: return `f"Your preferred headphone budget is ₹{max_budget}."`
3. If no budget found across all fragments: return `None`
4. **Exception**: log warning, return `None`

### `ConversationService._recall_brand(self, customer_id)`

**Execution order**:

1. `recall = self.memory_service.recall("preferences")`
2. If `recall.fragments`:
   - `from app.chatbot import extract_preferences`
   - For each `frag in recall.fragments`:
     - `prefs = extract_preferences(frag)`
     - `brand = prefs.get("preferred_brand")`
     - If `brand`: return `f"You previously mentioned that you prefer {brand} headphones."`
3. If no brand found: return `None`
4. **Exception**: log warning, return `None`

### `ConversationService._generate_normal_response(query, intent)`

**Execution order**:

1. `query_lower = query.casefold()`
2. **Memory question check**: `memory_terms = ["remember", "preference", "what do you know", "my budget", "my brand"]`; `is_memory_question = any(term in query_lower for term in memory_terms)`
   - If `is_memory_question`: return `{"message": "I'd be happy to tell you about your preferences! Could you ask me specifically what you'd like to know?", "llm_used": False, "response_mode": "NORMAL_RESPONSE", "follow_up_question": "What would you like to know about your preferences?"}`
3. **Greeting check**: `greeting_terms = ["hi", "hello", "hey", "how are you"]`; if any → `response = "Hello! I'm doing well, thank you for asking. How can I help you today?"`
4. **Gratitude check**: `gratitude_terms = ["thank", "thanks"]`; if any → `response = "You're welcome! I'm here whenever you need help with headphones or other shopping."`
5. **Thinking terms**: `thinking_terms = ["thinking about", "considering", "might", "maybe"]`; if any:
   - If `"headphones"` in query_lower or `"gaming"` in query_lower → `response = "That sounds interesting! Are you looking for recommendations, or just thinking aloud?"`
   - Else → `response = "I see you're thinking about that. I'm here to help whenever you're ready!"`
6. **ANC check**: if `"anc" in query_lower` → `response = "ANC stands for Active Noise Cancellation. It uses microphones to detect and cancel ambient noise, providing a quieter listening experience. Would you like to know more about headphones with ANC?"`
7. **Wireless check**: if `"wireless" in query_lower` → `response = "Wireless headphones connect via Bluetooth rather than a physical cable, giving you more freedom of movement. They're great for exercising, commuting, or just relaxing. Would you like to know more about wireless options?"`
8. **Default**: `response = "I understand. I'm here to help you find the right headphones or answer any questions you have. What's on your mind?"`
9. **Return dict**: `{"message": response, "llm_used": False, "response_mode": "NORMAL_RESPONSE", "follow_up_question": "Is there anything else I can help you with?"}`

### `ConversationService._call_product_api(self, resolved_constraints)`

**Execution order**:

1. `try:`:
   - Build `params: Dict[str, Any] = {}`
   - `category = resolved_constraints.get("category")`; if → `params["category"] = category`
   - `budget = resolved_constraints.get("budget", {})`; `max_budget = budget.get("max")`; if → `params["max_price"] = max_budget`
   - `brand = resolved_constraints.get("brand")`; if → `params["brand"] = brand`
   - `required_features = resolved_constraints.get("required_features")`; if → `params["required_features"] = ", ".join(required_features)`
   - `excluded_features = resolved_constraints.get("excluded_features")`; if → `params["excluded_features"] = ", ".join(excluded_features)`
   - `sort_by = resolved_constraints.get("sort_preference")`; if → `params["sort_by"] = sort_by`
   - `resp = httpx.post(f"{self._product_api_base}/api/products/search", params=params, timeout=15.0)`
   - If `resp.status_code == 200`: `data = resp.json()`; `products = data.get("products", [])`; log info; **return** `products`
   - Else: log warning `"Product API returned status {resp.status_code}, returning empty results"`; **return** `[]`
2. **Except `httpx.RequestError`**: log error `"Product API request failed: {exc}"`; **return** `[]` (graceful degradation)
3. **Except Exception**: log error `"Product API unexpected error: {exc}"`; **return** `[]`

**Return**: `List[Dict[str, Any]]` of product dicts (possibly empty)

### `ConversationService._update_memory_if_appropriate(query, constraints)`

**Execution order**:

1. `customer_id = self.memory_service.get_customer()` → always `"customer_001"`; if not → return
2. `facts_to_store: List[str] = []`
3. **Category**: `category = constraints.get("category")`; if `category`:
   - `category_keywords = ["headphones", "footwear", "apparel", "running", "shoes"]`
   - If any `kw in category` → `facts_to_store.append(f"Prefers {category} shopping")`
4. **Budget**: `max_budget = constraints.get("budget", {}).get("max")`; if `max_budget and max_budget > 0`:
   - If `max_budget > 500` → `facts_to_store.append(f"Has a budget up to ₹{max_budget}")` (only substantial budgets are durable)
5. **Wireless**: if `constraints.get("wireless") is not None`:
   - If `constraints["wireless"]` → `facts_to_store.append("Prefers wireless headphones")` (only store if explicitly preferred)
6. **Noise cancellation**: if `constraints.get("noise_cancellation")` → `facts_to_store.append("Prefers noise-cancelling headphones")`
7. **Brand**: if `constraints.get("brand")` → `facts_to_store.append(f"Prefers {constraints['brand']} headphones")`
8. **If facts_to_store not empty**: `self.memory_service.remember_with_customer("customer_001", facts_to_store)`; log info
9. **If no facts**: silently do nothing

### `ConversationService.clear_conversation()`

**Execution order**:

1. `self.conversation_history = []`
2. `logger.info("Conversation history cleared for customer %s", self.memory_service.get_customer())`
3. **Do NOT** call any memory service methods — memories are preserved
4. **Return**: `None`

### `ConversationService._get_relevant_memories(query)`

**Execution order**:

1. `customer_id = self.memory_service.get_customer()` → always `"customer_001"`; if not → return `[]`
2. Try: `recall = self.memory_service.recall(query)`; if `recall.fragments`: return `list(recall.fragments)`
3. Except Exception: log warning `"Failed to retrieve memories: {exc}"`; return `[]`
4. **Return**: `List[str]` of memory fragments (possibly empty)

## Function Parameters and Return Values

| Method | Key Parameters | Return Value |
|---|---|---|
| `process_query(query)` | `query: str` | `Dict` with: `response`, `recommended_products`, `memories`, `preferences`, `success`, `error`, `llm_used`, `response_mode`, `intent_source`, `follow_up_question` |
| `_determine_intent_mode(intent, query)` | `intent: Dict`, `query: str` | One of: `STORE` / `RECALL` / `NORMAL_RESPONSE` / `PRODUCT_DISCOVERY` |
| `_store_preference_only(query, intent)` | `query: str`, `intent: Dict` | `None` (stores memories side effect) |
| `_generate_store_response(query, intent)` | `query: str`, `intent: Dict` | `Dict` with: `message`, `llm_used`, `response_mode`, `follow_up_question` |
| `_generate_recall_response(query, intent)` | `query: str`, `intent: Dict` | `Dict` with: `message`, `llm_used`, `response_mode`, `follow_up_question` |
| `_generate_normal_response(query, intent)` | `query: str`, `intent: Dict` | `Dict` with: `message`, `llm_used`, `response_mode`, `follow_up_question` |
| `_call_product_api(constraints)` | `resolved_constraints: Dict` | `List[Dict[str, Any]]` of product dicts |
| `_update_memory_if_appropriate(query, constraints)` | `query: str`, `constraints: Dict` | `None` (stores memories side effect) |
| `_get_relevant_memories(query)` | `query: str` | `List[str]` of memory fragments |
| `_determine_intent_mode(...)` | — | Intent mode string |
| `clear_conversation()` | — | `None` |

## Exceptions, Error Handling and Side Effects

- **`MemoryAdapterError`** — raised by `MemoryService` when adapter operations fail
  - Caught in `ConversationService._store_preference_only` → logged as warning, no crash
  - Caught in `ConversationService._generate_recall_response` → simplified response given
  - Caught in `ConversationService._update_memory_if_appropriate` → logged as warning

- **`MemoryOperationError`** — raised by `McpMemoryAdapter` when MCP session init or tool call fails
  - In Streamlit UI: caught in `try/except` within chat message renderers; displayed as `st.error()`
  - Does **not** propagate to crash the entire app

- **`httpx.RequestError`** — Product API unreachable
  - Handled in `_call_product_api()`: returns `[]` (empty results, not crash)
  - UI shows no product cards; assistant continues with deterministic response

- **`ValueError` / `TypeError`** — from constraint resolution or preference extraction
  - Caught in `process_query()` outer `try/except` → return error dict with `"I encountered an error while processing your request. Please try again."`
  - `error` key in return dict contains the exception message

- **Side effects (must‑know)**:
  - `process_query()` **always** updates `self.conversation_history` (append user + assistant messages, truncate to 20)
  - `remember()` **always** persists to the demo dataset `retail_demo` via MCP `remember` tool
  - `forget()` **always** deletes from the demo dataset via MCP `forget` tool
  - `clear_conversation()` **only** clears Streamlit state; Cognee memories in `retail_demo` are **preserved**
  - `process_query()` routes through one of four modes: `STORE`, `RECALL`, `NORMAL_RESPONSE`, `PRODUCT_DISCOVERY` — **exactly one** per turn
  - `PRODUCT_DISCOVERY` is the **only** mode that calls the Product API or shows product cards
  - `STORE`, `RECALL`, `NORMAL_RESPONSE` **never** call the Product API
  - Memory operations use the single `retail_demo` dataset — no customer isolation
  - LLM status: `use_llm=True` (default) → UI can show "LLM mode"; `use_llm=False` → UI shows "deterministic fallback"

## Actual Implementation Walkthrough in Execution Order

### Typical Flow 1: User Stores a Preference

1. User types: `"I prefer Sony headphones."`
2. Streamlit `chat_input` triggers `service.process_query("I prefer Sony headphones.")`
3. `process_query` → `extract_intent(...)` → intent dict: `source="deterministic"`, `category="headphones"`, `brand="Sony"`, no budget, no wireless/noise_cancellation mentioned explicitly as preference
4. `_determine_intent_mode(...)` → `query_lower.startswith("i prefer")` is `True`; brand `"sony"` detected → **mode = `"STORE"`**
5. `_store_preference_only("I prefer Sony headphones.", intent)`:
   - `extract_preferences(...)` → `{"brand": "Sony", "category": "headphones", ...}`
   - `category == "headphones"` → **not stored** (it's the default category, so we don't persist "Prefers headphones shopping")
   - `brand == "Sony"` → `facts_to_store.append("Prefers Sony headphones")`
   - `self.memory_service.remember_with_customer("customer_001", ["Prefers Sony headphones"])` → MCP `remember` tool → success
   - Log: `"Stored 1 facts for customer customer_001 (STORE mode)"`
6. `_generate_store_response("I prefer Sony headphones.", intent)`:
   - `brand = "Sony"` → `parts.append("Understood! I'll remember that you prefer Sony headphones.")`
   - `response_text = "Understood! I'll remember that you prefer Sony headphones."`
   - `follow_up = "Is there anything else you'd like to tell me about your preferences?"`
   - Return: `{"message": "Understood! I'll remember that you prefer Sony headphones.", "llm_used": False, "response_mode": "STORE", "follow_up_question": follow_up}`
7. Return dict built in `process_query`:
   - `response = "Understood! I'll remember that you prefer Sony headphones."`
   - `recommended_products = []` (STORE mode)
   - `memories = self._get_relevant_memories(query)` → may return empty or last fragment
   - `response_mode = "STORE"`
   - `intent_source = "deterministic"`
   - `follow_up_question = "Is there anything else you'd like to tell me about your preferences?"`
8. Streamlit renders assistant message: `st.markdown("Understood! I'll remember that you prefer Sony headphones.")`
9. Follow‑up caption: `st.caption("Is there anything else you'd like to tell me about your preferences?")`

### Typical Flow 2: User Requests Product Recommendation

1. User types: `"Recommend wireless headphones under ₹5000."`
2. `process_query("Recommend wireless headphones under ₹5000.")`:
   - `extract_intent(...)` → `source="deterministic"`, `usage` may be set, no explicit category/brand/budget in intent (budget info in free‑text)
   - `_determine_intent_mode(...)`:
     - `starts_with_preference`? No (starts with "recommend")
     - Product request terms: `"recommend"` is in `query_lower` → check exclusions: `"thinking about"` etc. not present
     - **mode = `"PRODUCT_DISCOVERY"`**
3. Constraint resolver:
   - `resolved_constraints = resolver.resolve_constraints("customer_001", "Recommend wireless headphones under ₹5000.", conversation_history=[], intent_hints=intent)`
   - Returns something like: `{"category": "headphones", "max_budget": {"max": 5000}, "wireless": True, "sort_preference": "rating"}`
4. `normalize_constraints(resolved_constraints)`:
   - `category = "headphones"`
   - `max_price = 5000`
   - `wireless = True`
   - `required_features = ["wireless"]` (or `"wireless"` as comma‑separated)
   - `budget = {"max": 5000, "currency": "INR"}`
   - `sort_preference = "rating"`
5. `_call_product_api(constraints)`:
   - `params = {"category": "headphones", "max_price": 5000, "required_features": "wireless"}`
   - `httpx.post("http://127.0.0.1:8000/api/products/search", params=params, timeout=15.0)`
   - API returns matching products (wireless headphones, price ≤ 5000)
   - Return list of product dicts
6. `generate_response(original_query, resolved_constraints, api_products, conversation_context)`:
   - If LLM available: natural language response referencing specific products
   - If LLM unavailable: deterministic response e.g. `"Here are some wireless headphones under ₹5000: ..."` with product cards
7. `_update_memory_if_appropriate("Recommend wireless headphones under ₹5000.", constraints)`:
   - `category = "headphones"` → `"Prefers headphones shopping"` (but "headphones" is default, may or may not store depending on code path)
   - `max_budget = 5000 > 500` → `"Has a budget up to ₹5000"`
   - `wireless = True` → `"Prefers wireless headphones"`
   - These are stored in Cognee `retail_demo` via `remember_with_customer`
8. Build return dict:
   - `response` from `generate_response`
   - `recommended_products` from `response_result.get("recommended_product_ids", [])`
   - `memories = self._get_relevant_memories(query)` → may include newly stored preferences
   - `response_mode = "PRODUCT_DISCOVERY"`
   - `intent_source = intent.get("source", "deterministic")`
   - `follow_up_question = "How can I help you further?"`
9. Streamlit renders:
   - Assistant message with natural language response
   - Product cards via `render_product_card(product)` for each recommended product
   - Follow‑up caption

### Typical Flow 3: User Recalls a Preference

1. User types: `"What do you remember about my headphone budget?"`
2. `process_query("What do you remember about my headphone budget?")`:
   - `extract_intent(...)` → `source="deterministic"`, intent has `usage` or recall terms detected
   - `_determine_intent_mode(...)` → recall question terms detected → **mode = `"RECALL"`**
3. `_generate_recall_response("What do you remember about my headphone budget?", intent)`:
   - `query_lower = "what do you remember about my headphone budget?"`
   - `"headphone budget" in query_lower` → `True`
   - `_recall_budget("customer_001")`:
     - `self.memory_service.recall("preferences")` → MCP recall tool
     - `recall.fragments` exists → iterate
     - `extract_preferences(frag)` → get `max_budget`
     - If found: return `f"Your preferred headphone budget is ₹{max_budget}."`
   - If budget not in memory: return `"You haven't mentioned your headphone budget yet, so I don't have that information."`
4. Return dict:
   - `response = "Your preferred headphone budget is ₹8000."` (or the not‑found message)
   - `response_mode = "RECALL"`
   - `follow_up_question = "Is there anything specific about your preferences you'd like to know more about?"`
5. Streamlit renders assistant message with the budget information

### Typical Flow 4: Normal Conversation (No Memory, No Products)

1. User types: `"What is Active Noise Cancellation?"`
2. `process_query("What is ANC?")`:
   - `extract_intent(...)` → `source="deterministic"`
   - `_determine_intent_mode(...)`:
     - Not STORE (no "I prefer/like/want" start)
     - Not RECALL (no recall question terms)
     - Not PRODUCT_DISCOVERY (no recommend/suggest/search/show me/find/look for — "ANC" is not a product request)
     - **mode = `"NORMAL_RESPONSE"`**
3. `_generate_normal_response("What is ANC?", intent)`:
   - `query_lower = "what is anc?"`
   - Not memory question (no `remember`, `preference`, `what do you know`, `my budget`, `my brand`)
   - Not greeting
   - Not gratitude
   - Not thinking terms
   - `"anc" in query_lower` → **True**
   - `response = "ANC stands for Active Noise Cancellation. It uses microphones to detect and cancel ambient noise, providing a quieter listening experience. Would you like to know more about headphones with ANC?"`
4. Return dict:
   - `response = "ANC stands for Active Noise Cancellation... "`
   - `response_mode = "NORMAL_RESPONSE"`
   - `llm_used = False`
   - `follow_up_question = "Is there anything else I can help you with?"`
5. Streamlit renders assistant message with ANC explanation, no product cards, no memory operations

### Typical Flow 5: User Clears Conversation

1. User clicks **"🗑️ Clear conversation"** in sidebar
2. `service.clear_conversation()` → `self.conversation_history = []`
3. Streamlit reruns with empty chat area
4. Cognee memories in `retail_demo` are **preserved** — memory inspector still shows stored preferences
5. Assistant starts fresh: `st.info("Ask for something to get started...")`

### Typical Flow 5: User Deletes All Memories

1. User clicks **"Clear All Cognee Memories"** in sidebar expander
2. Warning displayed: `"This will delete ALL persistent memories from the demo dataset. This operation is irreversible."`
3. User clicks **"Confirm Delete All Memories"**
4. `service.memory_service.forget("customer_001", "retail_demo")` → MCP `forget` tool
5. Success: `st.success(f"Deleted {result} memories from dataset: retail_demo")`
6. Verification: `fresh_recall = service.memory_service.recall("all")` → if `fresh_recall.fragments` empty → `st.success("Verification complete: No memories remain for this dataset.")`
7. If fragments remain → `st.warning(f"Warning: {len(fresh_recall.fragments)} fragments still remain. Deletion may not have been complete.")`
8. Memory inspector refreshes — no fragments shown
9. Assistant "remembers" nothing — `recall("anything")` returns no fragments

## Input and Output Data Flow

### Input Flow (user → conversation service)

```
User prompt
    → ConversationService.process_query()
        → extract_intent() [deterministic, LLM‑optional]
            → intent dict: source, category, brand, budget, wireless, noise_cancellation, usage, etc.
        → _determine_intent_mode() [rules‑based]
            → one of: STORE / RECALL / NORMAL_RESPONSE / PRODUCT_DISCOVERY
        → mode‑specific handler
            → _store_preference_only / _generate_recall_response / _generate_normal_response / 
              (_call_product_api → generate_response)
        → update conversation_history [always: append user + assistant messages, truncate to 20]
        → build return dict
    → Streamlit renders assistant message
        → st.markdown(response)
        → if products: for product in recommended_products: render_product_card(product)
        → if follow_up: st.caption(follow_up_question)
        → if response_mode: st.caption(f"Response source: {response_mode}")
        → if intent_source: st.caption(f"Intent: {intent_source}")

### Output Flow (conversation service → user)

```
Return dict
    → assistant message text: st.markdown(response)
    → product cards: for product in recommended_products: render_product_card(product)
    → follow‑up question: st.caption(follow_up_question)
    → response source: st.caption(f"Response source: {response_mode}")
    → intent: st.caption(f"Intent: {intent_source}")
```

## Integration with MCP, Cognee, Ollama and Product API

### MCP Integration

- **All memory operations** go through `McpMemoryAdapter`, which manages one persistent `_PersistentMcpSession` (thread‑based, owns its event loop)
- **`remember` tool**: sends `data` (newline‑joined facts), `dataset_name` (`retail_demo`), `background=False`, `self_improvement=True`
- **`recall` tool**: sends `query`, `datasets` (comma‑separated string — here just `"retail_demo"`), `search_type`, `top_k`
- **`forget` tool** (added): sends `customer_id` and `dataset_name` for deletion
- **`_raise_on_tool_error()`**: inspects tool results — if `isError` or text starts with `"Error:"`, raises `MemoryOperationError`
- **`_fragments_from_result()`**: extracts stored memories from recall payload, stripping the protocol summary line (`"X memories found..."`) and any `[source]` prefixed tags
- **MCP session survival**: `McpMemoryAdapter._get_connection()` checks if the underlying thread is alive; if not (e.g., after Streamlit rerun), resets `_connection` so the next attempt re‑initializes

### Cognee Integration

- Cognee API started from correct `.venv` with `EMBEDDING_*` vars from `.env`
- Embedding provider: `ollama`, model: `nomic-embed-text:latest`, endpoint: `http://127.0.0.1:11434/api/embed`, dimensions: `768`
- All memory operations (store, recall, delete) go through the MCP adapter — **no direct Cognee Python SDK or CLI calls** from application code
- Fixed dataset: `retail_demo` — one shared dataset for the entire POC

### Ollama Integration

- Two required models: `nomic-embed-text:latest` (embeddings) and `llama3.1:latest` (LLM)
- Embedding endpoint: `http://127.0.0.1:11434/api/embed`
- LLM endpoint: `http://127.0.0.1:11434`
- `LLM_API_KEY=ollama` (LiteLLM uses this as the API key)
- LLM status panel in UI reports: configured, enabled, provider, model, reachable, model_available, mode, summary, detail, latency
- When LLM unavailable: all responses are deterministic from Product API data and stored preferences

### Product API Integration

- Catalog loaded from `data/products.json` (20 products, SHA-256 verified migration)
- All product discovery goes through `http://127.0.0.1:8000/api/products/search`
- Preferences (brand, budget, features) extracted from Cognee MCP recall are translated into API query parameters by the conversational service
- The Product API is **stateless** w.r.t. memory — it only returns products matching the applied filters
- Pipeline: `User → Conversation Service → (translate preferences) → Product API → (product cards) → Conversation Service → (natural language response) → User`

## Important Design Decisions and Trade-offs

### Single‑User Dataset (`retail_demo`)

- **Decision**: One shared dataset for all preferences — no customer isolation.
- **Trade-off**: Simpler architecture, no dataset naming or isolation verification. Not suitable for multi‑tenant deployment.
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

### Feature Tags via Required/Excluded Features

- **Decision**: The `wireless` and `noise-cancellation` boolean columns in each Product are translated into `required_features`/`excluded_features` strings for the search API.
- **Trade-off**: The search API uses comma‑separated feature strings; other potential features in the product data are not exposed as searchable API params.
- **Rationale**: Keeps the Product API simple (one search endpoint) while allowing the conversational service to express complex preference combinations.

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
| `Recommend wireless headphones.` | Shows product cards for wireless headphones from the catalogue (filtered by budget, wireless feature) |
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
- `tests/test_memory.py` — memory‑related tests (the full test suite has 26 total passing tests covering both test files)
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