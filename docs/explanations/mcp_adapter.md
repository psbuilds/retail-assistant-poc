# MCP Adapter (`app/memory/mcp_adapter.py`)

## Purpose and Responsibility

The MCP Adapter (`app/memory/mcp_adapter.py`) is the **single integration boundary** between the Retail Memory Assistant application and the Cognee MCP (Model Context Protocol) server. It implements the `MemoryAdapter` abstract base class and mediates all memory operations — `remember`, `recall`, and the newly added `forget` — through the MCP protocol.

**Non-negotiable rule**: The application must never make direct Cognee Python SDK or CLI/subprocess memory calls. All memory operations must go through this MCP adapter.

## Place in Overall Architecture

```
Application Code
    |
McpMemoryAdapter (this file)
    |-- _PersistentMcpSession (thread-based, owns event loop)
    |-- call_tool(name, arguments) → MCP protocol
    |-- _raise_on_tool_error() → error handling
    |-- _fragments_from_result() → memory extraction
    |
Cognee MCP Server (http://127.0.0.1:8101/mcp)
    |-- remember tool: store facts + dataset
    |-- recall tool: query memories + datasets
    |-- forget tool: delete memories from dataset
    |
Cognee API Server (http://127.0.0.1:8102)
    |-- Embedding processing (nomic-embed-text:latest)
    |-- Memory storage/retrieval
    |
Ollama
    |-- nomic-embed-text:latest (embeddings)
    -- llama3.1:latest (LLM)
```

## Internal Dependencies

- `mcp` — Model Context Protocol client library
- `mcp.client.streamable_http` — transport for HTTP-based MCP
- `mcp.client.session.ClientSession` — MCP session management
- `mcp.types` — MCP message types (TextContent, etc.)
- `app.memory.base.MemoryAdapter` — abstract base class
- `app.memory.base.MemoryAdapterError` — adapter‑level errors
- `app.memory.base.MemoryOperationError` — operation‑level errors with context
- `app.memory.base.RememberResult` / `RecallResult` — result dataclasses
- `app.memory.base.dataset_for()` — customer ID → dataset name
- `app.memory.mcp_adapter._PersistentMcpSession` — long‑running MCP session
- `app.memory.mcp_adapter._text_from_result()` — extract text from MCP response
- `app.memory.mcp_adapter._raise_on_tool_error()` — turn tool failures into errors
- `app.memory.mcp_adapter._fragments_from_result()` — extract memories from recall payload
- `app.memory.mcp_adapter._is_system_marker()` — filter system garbage from fragments
- `app.services.memory_service.MemoryService` — higher‑level service wrapper
- `threading` — for `_PersistentMcpSession` background thread
- `asyncio` — for event loop management in `_PersistentMcpSession`

## Every Class and Its Role

### `McpMemoryAdapter` (main class)

- **Responsibility**: One MCP protocol session for all memory operations on this adapter.
- **Integration**: `"mcp"` — the integration identifier used by `MemoryOperationError`.
- **Dataset handling**: The `datasets` parameter in `recall` is a **comma‑separated string**, not a list. One customer maps to exactly one dataset.
- **Tool error handling**: Failures are reported as `"Error: ..."` text results or `isError` flag — this method turns those into `MemoryOperationError` exceptions.
- **Fragment extraction**: The recall payload contains a summary line (`"X memories found..."`) followed by memory blocks — this method extracts only the actual memory fragments.

### `_PersistentMcpSession` (internal, not instantiated directly by application code)

- **Responsibility**: Own an async MCP transport/session on a private event‑loop thread.
- **Lifecycle**:
  1. `__init__(url, timeout_seconds)` — create new event loop, daemon thread, `_ready` event, `_startup_error`
  2. `_run()` — set event loop, start `_serve()` task, run forever
  3. `_serve()` — establish streamable HTTP connection, initialize session, list tools, verify `remember`/`recall` exist
  4. `call_tool(name, arguments)` — submit tool call to session via command queue
  5. `close()` — signal shutdown, wait for thread termination
- **Thread safety**: All cross‑thread communication goes through `self._commands` (asyncio.Queue); submitted via `call_soon_threadsafe`
- **Startup error handling**: If `initialize()` fails, `_startup_error` is set, `_ready` is set, and the constructor re‑raises (or adapter resets `_connection` to None)

### `MemoryAdapter` (abstract base, imported from `app.memory.base`)

- **Purpose**: Define the interface all memory adapters must implement
- **Key members**:
  - `integration` — class attribute, `"mcp"` for this adapter
  - `remember(customer_id, facts)` → `RememberResult`
  - `recall(customer_id, query)` → `RecallResult`
  - `forget(customer_id, dataset)` → `int` (deleted count)
  - `get_customer_dataset()` → dataset name (via `dataset_for()`)
  - `close()` → release resources

## Constructor Parameters and Important Attributes

### `McpMemoryAdapter.__init__()`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `mcp_url` | `str` | `env COGNEE_MCP_URL` or `DEFAULT_MCP_URL` (`"http://127.0.0.1:8101/mcp"`) | MCP server URL, trailing `/` stripped |
| `recall_query_type` | `str` | `env COGNEE_RECALL_QUERY_TYPE` or `DEFAULT_QUERY_TYPE` (`"CHUNKS"`) | Search type for recall queries |
| `top_k` | `int` | `DEFAULT_TOP_K` (`10`) | Number of results to return from recall |
| `timeout_seconds` | `float` | `DEFAULT_TIMEOUT_SECONDS` (`180.0`) | Timeout for tool calls and session initialization |
| `mcp_url` (final) | `str` | — | `self.mcp_url` — used for `_get_connection()`, `_remember()`, `_recall()`, `forget()` |
| `recall_query_type` (final) | `str` | — | `self.recall_query_type` — passed in every `_recall()` call |
| `top_k` (final) | `int` | — | `self.top_k` — passed in every `_recall()` call |
| `timeout_seconds` (final) | `float` | — | `self.timeout_seconds` — passed to `_get_connection()` and `call_tool()` |
| `self.mcp_url` | `str` | — | The MCP server URL, used to create `_PersistentMcpSession` |
| `self.recall_query_type` | `str` | — | Query type for recall (CHUNKS by default) |
| `self.top_k` | `int` | — | Maximum results per recall |
| `self.timeout_seconds` | `float` | — | Timeout for all blocking operations |
| `self._connection` | `_PersistentMcpSession` | `None` | Cached session; recreated if thread dies or init fails |
| `self._closed` | `bool` | `False` | Flag — once set, `_get_connection()` raises `MemoryConfigurationError` |
| `self.integration` | `str` | `"mcp"` | Used in `MemoryOperationError` for integration attribution |

### `_PersistentMcpSession.__init__()`

| Parameter | Type | Description |
|---|---|---|
| `url` | `str` | MCP server URL (e.g. `"http://127.0.0.1:8101/mcp"`) |
| `timeout_seconds` | `float` | Initialization timeout; if handshake takes longer, cancellation occurs |
| `self.url` | `str` | Stored URL |
| `self.timeout_seconds` | `float` | Propagated to `call_tool()` and initialization |
| `self._loop` | `asyncio.NewEventLoop()` | Private event loop for this session |
| `self._thread` | `threading.Thread` | Daemon thread that runs `self._loop.run_forever()` |
| `self._ready` | `threading.Event` | Signaled when `_serve()` completes init (success or failure) |
| `self._startup_error` | `BaseException` | Set if `_serve()` fails during init; constructor re‑raises |
| `self._stack` | `AsyncExitStack` | Resource management for streams + session |
| `self._session` | `ClientSession` | Active MCP session (after successful init) |
| `self._commands` | `asyncio.Queue` | Cross‑thread command queue; `call_tool()` submits `(name, args, result_Future)` |
| `self._stopped` | `Future[None]` | Signals the command loop to terminate |
| `self._ready` | `threading.Event` | External waiters (e.g. adapter) can wait on this to know when init is done |

## Every Maintained Function and Method

### `McpMemoryAdapter.__init__()`

**Execution order:**

1. Set `self.mcp_url` — resolve from parameter, env var `COGNEE_MCP_URL`, or `DEFAULT_MCP_URL`; strip trailing `/`
2. Set `self.recall_query_type` — resolve from parameter, env var `COGNEE_RECALL_QUERY_TYPE`, or `DEFAULT_QUERY_TYPE` (`"CHUNKS"`)
3. Set `self.top_k` — resolve from parameter or `DEFAULT_TOP_K` (`10`)
4. Set `self.timeout_seconds` — resolve from parameter or `DEFAULT_TIMEOUT_SECONDS` (`180.0`)
5. Set `self._closed = False`
6. Set `self._connection = None`
7. **Create `_PersistentMcpSession`**:
   - `self._connection = _PersistentMcpSession(self.mcp_url, self.timeout_seconds)`
   - On `CancelledError`: reset `self._connection = None`; raise `MemoryOperationError` with `"MCP session initialization was cancelled"`
   - On other `Exception`: reset `self._connection = None`; raise `MemoryOperationError` with `"MCP session initialization failed: {exc}"`
8. On success: log info `"MCP adapter initialized with url={mcp_url}, query_type={recall_query_type}, top_k={top_k}"`

### `McpMemoryAdapter._get_connection()`

**Execution order:**

1. If `self._closed`: raise `MemoryConfigurationError("MCP adapter is closed", integration=self.integration, operation="configure")`
2. If `self._connection is not None`:
   - Get `thread = getattr(self._connection, "_thread", None)`
   - If `thread is not None and not thread.is_alive()`:
     - Log warning `"Cached MCP session thread is dead. Resetting connection."`
     - Set `self._connection = None`
   - Else: thread is alive (or mock doesn't have `_thread`) — **reuse the session**
3. If `self._connection is None` (either fresh or reset):
   - Try: `self._connection = _PersistentMcpSession(self.mcp_url, self.timeout_seconds)`
   - On `CancelledError`: set `self._connection = None`; raise `MemoryOperationError(f"MCP session initialization was cancelled: {exc}", integration=self.integration, operation="initialize", customer_id=...)`
   - On other `Exception`: set `self._connection = None`; raise `MemoryOperationError(f"MCP session initialization failed: {exc}", integration=self.integration, operation="initialize", customer_id=...)`
4. Return `self._connection`

**Purpose**: Ensure the underlying thread is alive before re‑using a cached session; reset and recreate if it died (e.g. after a Streamlit rerun cancelled the daemon thread).

### `McpMemoryAdapter._remember(customer_id, facts, dataset)`

**Execution order**:

1. Call `self._call("remember", {"data": "\\n".join(facts), "dataset_name": dataset, "background": False, "self_improvement": True}, customer_id)`
2. `_call()` internally:
   - Gets connection via `_get_connection()`
   - Calls `connection.call_tool("remember", arguments)`
   - `_raise_on_tool_error(result, ...)` — if tool reported error, re‑raise as `MemoryOperationError`
   - Return raw result
3. `_text_from_result(result)` — extract text: `"\\n".join(content.text for content in result.content if isinstance(content, types.TextContent) and content.text)`
4. `_text_from_result()` returns the full stored text (facts newline‑joined)
5. Return `len(_text_from_result(result))` — fact count (number of newline‑separated lines; each fact is one line)

### `McpMemoryAdapter._recall(customer_id, query, dataset)`

**Execution order**:

1. Call `self._call("recall", {"query": query, "datasets": dataset, "search_type": self.recall_query_type, "top_k": self.top_k}, customer_id)`
2. `_call()` internally:
   - Gets connection via `_get_connection()`
   - Calls `connection.call_tool("recall", arguments)`
   - `_raise_on_tool_error(result, ...)` — if tool reported error, re‑raise as `MemoryOperationError`
   - Return raw result
3. `_fragments_from_result(result)` — extract memory fragments from recall payload
4. Return `tuple of str` — the stored memory fragments (empty if none)

**Key detail**: The `datasets` parameter is a **comma‑separated string** (MCP tool schema expects string, not list). For single‑user: `"retail_demo"`.

### `McpMemoryAdapter.forget(customer_id, dataset)` **(newly added)**

**Execution order**:

1. Call `self._call("forget", {"customer_id": customer_id, "dataset_name": dataset}, customer_id)`
2. `_call()` internally:
   - Gets connection via `_get_connection()`
   - Calls `connection.call_tool("forget", arguments)`
   - `_raise_on_tool_error(result, ...)` — if tool reported error, re‑raise as `MemoryOperationError`
   - Return raw result
3. `_text_from_result(result)` — extract text from the forget tool's response
4. Try to parse a number from the text with `re.search(r"(\\d+)", text)`
5. If number found: return `int(match.group(1))` — count of deleted memories
6. If "success" or "deleted" in text (case‑insensitive): return `1` (one deletion unit)
7. Otherwise: return `0` (could not determine count)

### `McpMemoryAdapter.call_tool(name, arguments)`

**Execution order**:

1. If `self._session is None or self._commands is None`: raise `RuntimeError("MCP session is not initialized")`
2. Create `result: Future[Any] = Future()`
3. Submit to event loop: `self._loop.call_soon_threadsafe(self._commands.put_nowait, (name, arguments, result))`
4. Return `result.result(timeout=self.timeout_seconds)` — blocking read with timeout

**Purpose**: The public API for invoking any MCP tool (remember, recall, forget, etc.). Serializes the call into the command queue on the session's event‑loop thread and blocks until the result is ready (or times out).

### `McpMemoryAdapter.close()`

**Execution order**:

1. If `self._thread.is_alive()` is `False`: return immediately (already dead)
2. If `self._commands is not None`: submit `None` as shutdown signal: `self._loop.call_soon_threadsafe(self._commands.put_nowait, None)`
3. Else: submit `self._loop.stop` to the event loop
4. `self._thread.join(timeout=self.timeout_seconds)` — wait for thread termination
5. Set `self._connection = None`; `self._closed = True`

**Purpose**: Gracefully shut down the persistent MCP session, cleaning up the thread and resources.

### `_text_from_result(result)`

**Execution order**:

1. `lines = []`
2. For each `content` in `getattr(result, "content", ())`:
   - If `isinstance(content, types.TextContent) and content.text`: `lines.append(content.text)`
3. Return `"\\n".join(lines)` — all text content newline‑joined

**Purpose**: Normalize MCP response into plain text, regardless of how many `TextContent` blocks the tool returns.

### `_raise_on_tool_error(result, *, integration, operation, customer_id)`

**Execution order**:

1. If `getattr(result, "isError", False)`:
   - `detail = _text_from_result(result).strip() or "tool returned an error result"`
   - Raise `MemoryOperationError(f"MCP {operation} tool reported an error: {detail}", integration=integration, operation=operation, customer_id=customer_id)`
2. `text = _text_from_result(result).strip()`
3. `first_line = text.splitlines()[0].strip() if text else ""`
4. If `first_line.startswith("Error:")`:
   - Raise `MemoryOperationError(f"MCP {operation} tool reported an error: {text}", integration=integration, operation=operation, customer_id=customer_id)`

**Purpose**: Turn Cognee MCP's "soft" failure mode (text result saying `"Error: ..."` or `isError=True`) into a `MemoryOperationError` that the service layer can catch and report to the user.

### `_fragments_from_result(result)`

**Execution order**:

1. `lines = _text_from_result(result).strip().splitlines()`
2. If `lines and _RECALL_SUMMARY_PATTERN.match(lines[0].strip())`: `lines = lines[1:]` — strip the protocol summary line (`"X memories found..."`, `"no matching memories"`, `"still indexing..."`, `"memory indexing failed..."`)
3. `fragments = []`
4. For each `block in "\\n".join(lines).split("\\n\\n")`:
   - `text = _RECALL_SOURCE_PREFIX_PATTERN.sub("", block.strip()).strip()` — strip `[source]` prefix if present
   - If `text and not _is_system_marker(text)`: `fragments.append(text)`
5. Return `tuple(dict.fromkeys(fragments))` — deduplicated, preserving order

**Key patterns**:

- `_RECALL_SUMMARY_PATTERN = re.compile(r"^(?:\d+ memor(?:y|ies) found\\b.*|no matching memories|still indexing\\b.*|memory indexing failed\\b.*)$")`
- `_RECALL_SOURCE_PREFIX_PATTERN = re.compile(r"^\\[a-z_\\]+\\s*")`
- `_is_system_marker(text)`: returns `True` if `text.lower()` starts with `"memory is still"`, `"cognee"`, or `"knowledge graph"`

**Purpose**: Extract only the actual stored memories from the MCP recall payload, filtering out the protocol summary and system garbage.

### `_is_system_marker(text)`

**Execution order**:

1. `lowered = text.lower()`
2. Return `lowered.startswith(("memory is still", "cognee", "knowledge graph"))`

**Purpose**: Filter out system‑level garbage that sometimes appears in recall payloads but is not a stored memory.

### `McpMemoryAdapter._call(name, arguments, customer_id)`

**Execution order** (internal helper used by `_remember`, `_recall`, `forget`, `call_tool`):

1. Try: `result = self._get_connection().call_tool(name, arguments)`
2. Except `MemoryAdapterError`: re‑raise (don't wrap)
3. Except all other `Exception as exc`:
   - Raise `MemoryOperationError(f"MCP {name} tool call failed: {exc}", integration=self.integration, operation=name, customer_id=customer_id) from exc`
4. `_raise_on_tool_error(result, integration=self.integration, operation=name, customer_id=customer_id)`
5. Return `result`

**Purpose**: The single entry point for all MCP tool calls. Handles connection acquisition, tool invocation, and error translation.

## Function Parameters and Return Values

| Method | Key Parameters | Return Value |
|---|---|---|
| `McpMemoryAdapter.__init__(mcp_url, recall_query_type, top_k, timeout_seconds)` | Various | `None` (initializes adapter, creates `_PersistentMcpSession`) |
| `McpMemoryAdapter._get_connection()` | — | `_PersistentMcpSession` or raises `MemoryConfigurationError` |
| `McpMemoryAdapter._remember(customer_id, facts, dataset)` | `customer_id: str`, `facts: tuple[str, ...]`, `dataset: str` | `int` — fact count (number of newline‑separated lines) |
| `McpMemoryAdapter._recall(customer_id, query, dataset)` | `customer_id: str`, `query: str`, `dataset: str` | `tuple[str, ...]` — memory fragments (empty if none) |
| `McpMemoryAdapter.forget(customer_id, dataset)` | `customer_id: str`, `dataset: str` | `int` — count of deleted memories (0 if unknown) |
| `McpMemoryAdapter.call_tool(name, arguments)` | `name: str`, `arguments: dict` | `Any` — result from MCP tool |
| `McpMemoryAdapter.close()` | — | `None` — cleans up thread/resources |
| `McpMemoryAdapter._get_connection()` | — | `_PersistentMcpSession` or error |
| `_text_from_result(result)` | `result: Any` | `str` — all text content newline‑joined |
| `_raise_on_tool_error(...)` | — | Raises `MemoryOperationError` on failure |
| `_fragments_from_result(result)` | `result: Any` | `tuple[str, ...]` — deduplicated memory fragments |
| `_is_system_marker(text)` | `text: str` | `bool` — True if text is system garbage |

## Exceptions, Error Handling and Side Effects

- **`MemoryAdapterError`** — base adapter error; raised by base class methods; caught in service layer
- **`MemoryOperationError`** — the primary error this adapter produces; always includes:
  - `integration` — `"mcp"` (attribution)
  - `operation` — `"remember"` / `"recall"` / `"forget"` / `"initialize"`
  - `customer_id` — the customer whose operation failed
  - `__cause__` — the original exception (e.g. `CancelledError`, connection error)
  - Raised when: tool reports `isError` or `"Error:"` text; MCP init cancelled or fails; tool call fails; connection not available
- **`MemoryConfigurationError`** — raised when adapter is closed or misconfigured
- **`RuntimeError`** — from `call_tool()` if session not initialized
- **`CancelledError`** (asyncio) — from `_PersistentMcpSession` init or from Streamlit reruns cancelling daemon threads; handled by adapter resetting `_connection = None` so next attempt can retry
- **Side effects**:
  - `remember()` always persists to the MCP server's storage (dataset named in `dataset_name` param)
  - `recall()` always queries the MCP server and returns fragments (may be empty)
  - `forget()` deletes memories from the MCP server's dataset
  - `close()` terminates the background thread and releases the HTTP connection
  - `_get_connection()` may reset `_connection` to None if the thread died, forcing re‑initialization on next use

## Actual Implementation Walkthrough in Execution Order

### Initialization (app starts, MemoryService created)

1. `MemoryService.__init__()` → `McpMemoryAdapter()` (no args → defaults)
2. `McpMemoryAdapter.__init__()`:
   - `mcp_url = "http://127.0.0.1:8101/mcp"` (default)
   - `recall_query_type = "CHUNKS"` (default)
   - `top_k = 10` (default)
   - `timeout_seconds = 180.0` (default)
   - `self._connection = _PersistentMcpSession("http://127.0.0.1:8101/mcp", 180.0)`
3. `_PersistentMcpSession._serve()`:
   - `streamable_http_client(url)` → read/write streams
   - `ClientSession(read, write)` → `await session.initialize()`
   - `await session.list_tools()` → verify `remember` and `recall` exist
   - `self._ready.set()` — signal that init is complete
4. Back in `McpMemoryAdapter.__init__()`: success → log info
5. `MemoryService.remember("customer_001", ("fact",))` possible now

### Remember a Fact (user says "I prefer Sony headphones")

1. `MemoryService.remember_with_customer("customer_001", ["I prefer Sony headphones"])`
2. `McpMemoryAdapter.remember("customer_001", ("I prefer Sony headphones",), "retail_demo")`
3. `_remember()`:
   - `_call("remember", {"data": "I prefer Sony headphones", "dataset_name": "retail_demo", "background": False, "self_improvement": True}, "customer_001")`
   - `_call()`:
     - `_get_connection()` — thread alive? yes → return cached connection
     - `connection.call_tool("remember", arguments)`
     - `_raise_on_tool_error(result, ...)` — if tool says "Error:" → raise; else continue
   - Return raw MCP result
   - `_text_from_result(result)` → `"I prefer Sony headphones"`
   - `len(_text_from_result(result))` → `1` (one fact)
4. `RememberResult(fact_count=1)` returned up the call stack

### Recall a Fact (user asks "What do you remember about my preferences?")

1. `MemoryService.recall("preferences")`
2. `McpMemoryAdapter.recall("customer_001", "preferences")`
3. `_recall()`:
   - `_call("recall", {"query": "preferences", "datasets": "retail_demo", "search_type": "CHUNKS", "top_k": 10}, "customer_001")`
   - `_call()`:
     - `_get_connection()` — ensure session alive
     - `connection.call_tool("recall", arguments)`
     - `_raise_on_tool_error(result, ...)` — handle errors
   - Return raw MCP recall result
   - `_fragments_from_result(result)`:
     - `_text_from_result(result)` → extract text from MCP payload
     - Split lines, strip summary pattern (`"3 memories found..."` → remove first line)
     - Split on `"\\n\\n"` to get blocks
     - Strip `[source]` prefix from each block
     - Filter out system markers
     - Deduplicate with `dict.fromkeys()`
     - Return `("Prefers Sony headphones",)` (or similar)
4. `RecallResult(fragments=("Prefers Sony headphones",), result_count=1)` returned

### Forget All Memories (user clicks "Clear All Cognee Memories")

1. `MemoryService.forget("customer_001", "retail_demo")`
2. `McpMemoryAdapter.forget("customer_001", "retail_demo")`
3. `_forget()`:
   - `_call("forget", {"customer_id": "customer_001", "dataset_name": "retail_demo"}, "customer_001")`
   - `_call()`:
     - `_get_connection()`
     - `connection.call_tool("forget", {"customer_id": "customer_001", "dataset_name": "retail_demo"})`
     - `_raise_on_tool_error(result, ...)`
   - Return raw forget result
   - `_text_from_result(result)` → e.g. `"Deleted 3 memories"` or `"Success"` or similar
   - `re.search(r"(\d+)", text)` → if match: return `int(match.group(1))` → `3`
   - If no number but "success"/"deleted" in text: return `1`
   - Otherwise: return `0`
4. `logger.info("Forgot %d memories for customer %s dataset %s", result, "customer_001", "retail_demo")`
5. UI: `st.success(f"Deleted {result} memories from dataset: retail_demo")`

### Typical Flow: Intent Determination

1. `ConversationService.process_query("I prefer Sony headphones.")`:
   - `extract_intent(...)` → dict with `source="deterministic"`, `category="headphones"`, `brand="Sony"`
   - `_determine_intent_mode(intent, query)`:
     - `query_lower.startswith("i prefer")` → `True`
     - Brand `"sony"` detected in query → **return `"STORE"`**
2. `_store_preference_only(query, intent)` → extract preferences → store `"Prefers Sony headphones"` via `McpMemoryAdapter.remember()`
3. `_generate_store_response(query, intent)` → `"Understood! I'll remember that you prefer Sony headphones."`
4. Return dict with `response_mode="STORE"`

## Integration with Memory Service, Cognee, Ollama and Streamlit

### Memory Service → MCP Adapter

- `MemoryService.remember()` → `McpMemoryAdapter.remember("customer_001", tuple(facts), "retail_demo")`
- `MemoryService.recall()` → `McpMemoryAdapter.recall("customer_001", query)`
- `MemoryService.forget()` → `McpMemoryAdapter.forget("customer_001", "retail_demo")`
- `MemoryService.get_dataset()` → always `"retail_demo"` (single-user constant)

### Cognee MCP Server

- Receives MCP JSON‑RPC requests on `http://127.0.0.1:8101/mcp`
- `remember` tool: stores `data` as facts in dataset `dataset_name`; returns success/failure
- `recall` tool: queries dataset `datasets` with `query`, `search_type`, `top_k`; returns payload with summary line + memory blocks
- `forget` tool (new): deletes all memories for `customer_id` from `dataset_name`; returns count

### Ollama

- Embeddings: `nomic-embed-text:latest` on `http://127.0.0.1:11434/api/embed`
- LLM: `llama3.1:latest` on `http://127.0.0.1:11434`
- The MCP adapter **never calls Ollama directly** — Ollama is used by Cognee API (for embeddings) and by the `generate_response()` function (for LLM-based responses). The adapter only handles the MCP protocol.

### Streamlit UI

- `check_mcp_connection()` → `httpx.get(MCP_BASE_URL)` — bare GET proves server is listening (406 still counts)
- `llm_status_cache` → refreshed every 15 seconds; shows configured/enabled/provider/model/reachable/mode
- Memory inspector: `service.memory_service.recall("all")` → fragments displayed
- Delete All: `service.memory_service.forget("customer_001", "retail_demo")` → confirmed via MCP
- Conversation history: `service.conversation_history` list; `clear_conversation()` resets it

## Important Design Decisions and Trade-offs

### MCP `datasets` as String, Not List

- **Decision**: The `recall` tool's `datasets` parameter is a comma‑separated string (`"retail_demo"`), not a Python list `["retail_demo"]`.
- **Trade-off**: Requires `dataset_for(customer_id)` → `f"{dataset_prefix}_{customer_id}"` or similar mapping before passing to the tool; in single‑user mode it's just `"retail_demo"`.
- **Rationale**: The MCP tool schema was designed for multi‑tenant (comma‑separated multiple datasets); our single‑user POC uses one dataset, passed as a string.

### Persistent `_PersistentMcpSession` Thread

- **Decision**: One long‑running thread per adapter instance, owning its own event loop.
- **Trade-off**: Streamlit reruns can cancel daemon threads; the adapter handles this by checking `thread.is_alive()` and resetting `_connection` if dead.
- **Rationale**: The app is built for Streamlit; this pattern keeps the MCP session alive across user turns while tolerating reruns.

### `datasets` String vs List in Remember

- **Decision**: `remember` sends `"data": "\\n".join(facts), "dataset_name": "retail_demo"` — a single dataset string.
- **Trade-off**: If multi‑tenant were needed, the dataset name would need prefix/suffix logic per customer.
- **Rationale**: POC scope is single‑user; one dataset for all preferences.

### `forget` Tool Added to MCP Adapter

- **Decision**: Added `forget()` method assuming the Cognee MCP server version supports a `forget` tool.
- **Trade-off**: If the installed MCP server version does not support `forget`, the method will raise `MemoryAdapterError` with `"MCP forget tool not available"`.
- **Rationale**: The requirement explicitly asks for "Delete All Stored Memories" through MCP; we must provide it and gracefully handle the case where the server doesn't support it.

### Zero Direct Cognee SDK/CLI Calls

- **Decision**: All memory operations go through the MCP adapter; `app/code` never imports `cognee` directly, never runs `cognee api` or `cognee mcp` as subprocess.
- **Trade-off**: The adapter is an extra layer of indirection; if the MCP server is down, the adapter fails gracefully rather than falling back to direct SDK calls.
- **Non-negotiable rule**: "Do not use direct Cognee Python SDK or Cognee CLI/subprocess memory operations in application code."

### Fragment Extraction — Strip Summary + System Garbage

- **Decision**: `_fragments_from_result()` strips the first line (protocol summary like `"3 memories found..."`) and any `[source]` prefixed tags, plus system markers (`"memory is still..."`, `"cognee"`, `"knowledge graph"`).
- **Trade-off**: If the MCP server changes its payload format, this regex may need updating.
- **Rationale**: Ensures the assistant never hands a protocol summary to the user as a memory fragment.

## A Concrete Usage Example

### Session 1: Remember Preferences

| User | Assistant | Internal Action |
|---|---|---|
| `I prefer Sony headphones.` | `Understood! I'll remember that you prefer Sony headphones.` | `McpMemoryAdapter.remember("customer_001", ("Prefers Sony headphones",), "retail_demo")` → `RememberResult(fact_count=1)` |
| `I like over-ear headphones.` | `Got it! I'll remember that you're interested in over-ear shopping.` | `McpMemoryAdapter.remember("customer_001", ("Prefers over-ear shopping",), "retail_demo")` → `RememberResult(fact_count=1)` |
| `My budget is ₹8000.` | `Understood! I'll remember your preferred budget of ₹8000.` | `McpMemoryAdapter.remember("customer_001", ("Has a budget up to ₹8000",), "retail_demo")` → `RememberResult(fact_count=1)` |

### Session 2 (after recall)

| User | Assistant | Internal Action |
|---|---|---|
| `What do you remember about my headphone preferences?` | `Based on what you've told me: You previously mentioned that you prefer Sony headphones. You also have a preferred budget of ₹8000.` | `McpMemoryAdapter.recall("customer_001", "preferences")` → `_fragments_from_result()` → `("Prefers Sony headphones", "Has a budget up to ₹8000")` → `_generate_recall_response()` builds message |
| `Recommend wireless headphones under ₹5000.` | Shows product cards + `"Here are some wireless headphones under ₹5000:"` | `PRODUCT_DISCOVERY` mode → `_call_product_api()` → Product API search → `generate_response()` with product cards |
| `What do you remember now?` | `Based on what you've told me: You previously mentioned that you prefer Sony headphones. You also have a preferred budget of ₹8000.` | Same recall as before (memories persist) |

### Session 3: Delete All Memories

| User | Assistant | Internal Action |
|---|---|---|
| (clicks **Clear All Cognee Memories**) | (warning dialog: "This will delete ALL persistent memories from the demo dataset. This operation is irreversible.") | User confirms |
| (clicks **Confirm Delete All Memories**) | `Deleted 3 memories from dataset: retail_demo` | `McpMemoryAdapter.forget("customer_001", "retail_demo")` → text `"Deleted 3 memories"` → `re.search(r"(\d+)", text)` → `3` → return `3` |
| `What do you remember about my headphone preferences?` | `You haven't mentioned any preferences yet, so I don't have that information.` | `McpMemoryAdapter.recall("customer_001", "all")` → `_fragments_from_result()` → `()` (empty) → `_generate_recall_response()` → not‑found message |
| `I prefer Bose headphones.` | `Understood! I'll remember that you prefer Bose headphones.` | New `remember` → fresh preference stored |

## Tests Covering the Module

- `tests/test_mcp_adapter.py` — 15 unit tests, including:
  - `test_endpoint_is_explicit_and_session_is_reused` — verifies session reuse and that `datasets` is passed as string not list
  - `test_environment_endpoint_is_used` — verifies `COGNEE_MCP_URL` env var
  - `test_context_manager_closes_persistent_session` — verifies `close()` works via context manager
  - `test_recall_strips_protocol_summary_from_fragments` — verifies `_fragments_from_result()` strips summary line
  - `test_recall_without_matches_is_an_empty_success` — verifies empty recall returns `result_count=0`, `fragments=()`
  - `test_recall_tool_error_is_not_reported_as_empty_memory` — verifies error text raises `MemoryOperationError`
  - `test_recall_error_flag_is_not_reported_as_empty_memory` — verifies `isError=True` raises `MemoryOperationError`
  - `test_remember_tool_error_is_not_reported_as_success` — verifies remember error raises `MemoryOperationError`
  - `test_tool_errors_are_wrapped_and_chained` — verifies `MemoryOperationError` wraps original cause
  - `test_session_initialization_failure_is_wrapped` — verifies init failure wraps as `MemoryOperationError(operation="initialize")`
  - `test_cancelled_session_initialization_is_wrapped` — verifies `CancelledError` wraps as `MemoryOperationError(operation="initialize")`
  - And others for environment, tool errors, etc.
- `tests/test_memory.py` — memory‑related tests (full suite: 26 passing tests)
- Test style: most use `unittest.mock.patch` to mock `McpMemoryAdapter._PersistentMcpSession`, testing logic in isolation without a running MCP server
- Integration: the 26 tests pass after `.env` embedding fix, `fragments[1:]` → `fragments` fix, and script rewrites

## Actual Known Limitations

1. **MCP persistent session event loop** — `_PersistentMcpSession` has cancellation issues when the adapter is instantiated outside Streamlit's event loop. The app is designed for Streamlit; running in pure CLI may fail with `CancelledError`. Tests mock the session.

2. **No multi‑user support** — One shared `retail_demo` dataset for all preferences. Customer‑specific dataset naming or isolation is not implemented.

3. **MCP session survival across Streamlit reruns** — Streamlit reruns can cancel daemon threads. The adapter checks `thread.is_alive()` and resets `_connection` if dead, but fully seamless recovery across reruns is not guaranteed without re‑initialization.

4. **MCP `forget` tool availability** — The `forget` method assumes the Cognee MCP server version supports a `forget` tool. If the installed version does not, the method will raise `MemoryAdapterError` with `"MCP forget tool not available"`. The health check and UI should gracefully handle this.

5. **Fragment format dependence** — `_fragments_from_result()` relies on the MCP recall payload format (summary line + `\\n\\n`-separated blocks + optional `[source]` prefix). If the Cognee MCP server changes its payload format, this regex may need updating.

6. **Thread‑based session management** — The `_PersistentMcpSession` uses a daemon thread with `run_forever()`. If the main process exits without clean shutdown, the thread may be killed without calling `close()`, potentially leaving the MCP connection in an unspecified state.

7. **No built‑in retry logic** — If a tool call times out or fails, the adapter raises `MemoryOperationError` immediately. No automatic retry on transient failures. The service layer (`ConversationService`) catches this and gives a user‑friendly response.

8. **No support for batch remember/recall** — Each `remember`/`recall` call is a single MCP tool invocation. Remembering/recalling N facts requires N separate calls (or one `remember` with N newline‑joined facts, which is what we do).

9. **Dataset name hard‑coded as `retail_demo`** in the adapter's default configuration. If a different dataset name is needed, the `McpMemoryAdapter` must be instantiated with a custom `mcp_url` or the `dataset` parameter overridden per‑call.

10. **`call_tool` blocking read** — `call_tool()` blocks the calling thread with `future.result(timeout=self.timeout_seconds)`. For a timeout of 180 seconds, this is a long block; in Streamlit's synchronous execution model this is acceptable, but in async contexts it would need `await asyncio.wait_for()`.

---