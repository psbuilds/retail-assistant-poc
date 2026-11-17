# Ollama Configuration (`llm_ollama.md`)

## Expected Configuration

The application requires two Ollama models to be installed and running:

### Embedding Model (for Cognee embeddings)

- **Name**: `nomic-embed-text:latest`
- **Endpoint**: `http://127.0.0.1:11434/api/embed`
- **Purpose**: Generates text embeddings for Cognee memory operations
- **Configuration in `.env`**:
  ```
  EMBEDDING_PROVIDER=ollama
  EMBEDDING_MODEL=nomic-embed-text:latest
  EMBEDDING_ENDPOINT=http://127.0.0.1:11434/api/embed
  EMBEDDING_DIMENSIONS=768
  ```

### LLM Model (for natural language responses)

- **Name**: `llama3.1:latest`
- **Endpoint**: `http://127.0.0.1:11434`
- **Purpose**: Generates natural language responses when LLM mode is active
- **Configuration in `.env`**:
  ```
  LLM_PROVIDER=ollama
  LLM_MODEL=llama3.1:latest
  LLM_ENDPOINT=http://127.0.0.1:11434
  LLM_API_KEY=ollama
  ```

## Why `LLM_API_KEY=ollama`

Historical root cause of the HTTP 409 Conflict bug:

1. Without `EMBEDDING_*` vars in `.env`, Cognee silently defaulted to OpenAI embeddings
2. LiteLLM used `LLM_API_KEY=ollama` as the OpenAI API key
3. OpenAI returned `401 AuthenticationError: Incorrect API key provided: ollama`
4. Cognee wrapped the 401 as HTTP `409 Conflict`

**Fix**: Adding `EMBEDDING_*` vars to `.env` makes Cognee use Ollama embeddings directly, so LiteLLM doesn't need to forward OpenAI keys for embedding operations. The `LLM_API_KEY=ollama` is still needed for LLM generation calls through LiteLLM, but it no longer causes the 409 because embeddings are handled locally.

## Verification

Verify Ollama is running with required models:

```bash
curl -s http://127.0.0.1:11434/api/tags
```

Expected output (example):
```json
{"models": [{"name": "nomic-embed-text:latest", "size": 503055680, "modified_at": "2024-01-01T00:00:00Z"}, {"name": "llama3.1:latest", "size": 20186132480, "modified_at": "2024-01-01T00:00:00Z"}, {"name": "codegemma:7b", "size": 5177824256, "modified_at": "2024-01-01T00:00:00Z"}]}
```

Both `nomic-embed-text:latest` and `llama3.1:latest` must appear in the list.

## Llm Status Reporting

The UI status panel reports LLM state via `app.llm.status.LLMStatus`. Key attributes:

| Attribute | Description |
|---|---|
| `configured` | `True` if provider/model/endpoint are set in `.env` |
| `enabled` | `True` if the provider is `ollama` and the endpoint is reachable |
| `provider` | `"ollama"` or `None` |
| `model` | `"llama3.1:latest"` or `None` |
| `reachable` | `True` if `http://127.0.0.1:11434/api/tags` returns 200 with both required models |
| `model_available` | `True` if the specific model name is in the `/api/tags` response |
| `model_invoked` | `True` if the last model call succeeded (recorded latency + success) |
| `mode` | `"llm"` if LLM responses are active, `"deterministic"` if falling back to Product API data |
| `summary()` | Human-readable string, e.g. `"ollama / llama3.1:latest (deterministic)"` |
| `detail()` | Additional detail string, e.g. `"model installed"` or `"endpoint unreachable"` |
| `last_invocation_latency_ms` | Latency of the last successful model call in milliseconds |

## LLM Disabled Mode

When `use_llm=False` in `ConversationService()`:

- `LLMStatus` is forced to `detail="LLM disabled for this conversation service."`
- `mode` is `"deterministic"`
- UI shows `"LLM: deterministic fallback"` warning
- All responses are generated from Product API data and stored preferences only
- No MCP or Ollama calls are made for response generation

## When LLM Is Unavailable

If Ollama is running but models are not yet pulled, or the endpoint is unreachable:

1. `reachable` = `False`
2. `model_available` = `False`
3. `mode` = `"deterministic"`
4. Assistant continues to function with deterministic responses from:
   - Product API catalogue (product search, filtering, pricing)
   - Stored preferences (brand, budget, features)
   - Rule‑based conversation responses (greetings, ANC explanations, etc.)
5. UI shows `"LLM: deterministic fallback"` with caption explaining why

## Common Ollama Issues and Fixes

### Issue: `/api/tags` times out

**Cause**: Ollama not fully started, or model download in progress.

**Fix**:
```bash
ollama serve
ollama pull nomic-embed-text:latest
ollama pull llama3.1:latest
```

### Issue: Models show in `/api/tags` but embeddings fail

**Cause**: `EMBEDDING_ENDPOINT` misconfigured, or wrong model name.

**Fix**: Verify `.env`:
```
EMBEDDING_ENDPOINT=http://127.0.0.1:11434/api/embed
EMBEDDING_MODEL=nomic-embed-text:latest
```

### Issue: LLM calls return errors

**Cause**: `LLM_API_KEY` not set, or endpoint wrong.

**Fix**: Verify `.env`:
```
LLM_ENDPOINT=http://127.0.0.1:11434
LLM_API_KEY=ollama
LLM_MODEL=llama3.1:latest
```

### Issue: Only one model is showing

**Cause**: The other model hasn't been pulled yet.

**Fix**:
```bash
ollama pull nomic-embed-text:latest
ollama pull llama3.1:latest
```

## Integration with Cognee MCP

- Cognee API uses the embedding model (`nomic-embed-text:latest`) to convert user text into vectors before storing/retrieving in the knowledge graph
- The MCP adapter (`McpMemoryAdapter`) sends recall queries to Cognee API; Cognee uses embeddings to find matching memories
- If embeddings fail (wrong model, wrong endpoint), Cognee returns no memories, and the assistant falls back to deterministic responses based on Product API data
- The `EMBEDDING_DIMENSIONS=768` constant ensures compatibility between the embedding model's output and Cognee's internal expectations

## Integration with Conversation Service

- `ConversationService.get_llm_status()` calls `get_llm_status(check_provider=True)` from `app.llm`
- The status is cached for 15 seconds (`STATUS_TTL_SECONDS`) to avoid thrashing the Ollama endpoint on every Streamlit rerun
- If `use_llm=False`, status is forced deterministic regardless of actual Ollama state
- `generate_response()` in `app/services/response_generation.py` uses the LLM when `llm_used=True`; otherwise it falls back to deterministic generation from Product data and preferences

## Integration with Startup Script

The `scripts/start_services.sh` script:

1. Activates `.venv`
2. Loads `.env` EMBEDDING_* and LLM_* variables
3. Checks Ollama at `http://127.0.0.1:11434/api/tags`
4. Waits for both required models to appear
5. Starts Cognee API with correct environment
6. Starts Cognee MCP server
7. Starts Product API
8. Starts Streamlit

The script includes 30-second health waits for each service, and timeout config on all curl calls (5s connect, 10s max) to prevent hanging.

---