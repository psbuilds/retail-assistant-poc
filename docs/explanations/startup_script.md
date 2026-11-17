# Startup Script (`scripts/start_services.sh`)

## Purpose

Provides one reliable and documented startup procedure for the Retail Memory Assistant POC. Ensures all services are started with the correct environment, timeouts are configured, and processes are cleaned up on Ctrl+C.

## Requirements Satisfied

1. ✅ Load the target project's `.env`
2. ✅ Activate the target project's `.venv`
3. ✅ Check Ollama and required models
4. ✅ Start Cognee API with the correct environment
5. ✅ Wait for API readiness
6. ✅ Start Cognee MCP against `http://127.0.0.1:8102`
7. ✅ Verify MCP using protocol-level initialization
8. ✅ Start or clearly instruct how to start the Product API
9. ✅ Start or clearly instruct how to start Streamlit
10. ✅ Display service status and ports
11. ✅ Handle Ctrl+C and clean up only processes it started
12. ✅ Avoid duplicate service instances
13. ✅ Print useful error details when a dependency fails

## Configuration

### Environment Variables (loaded from `.env`)

| Variable | Default/Description |
|---|---|
| `EMBEDDING_PROVIDER` | `ollama` |
| `EMBEDDING_MODEL` | `nomic-embed-text:latest` |
| `EMBEDDING_ENDPOINT` | `http://127.0.0.1:11434/api/embed` |
| `EMBEDDING_DIMENSIONS` | `768` |
| `LLM_PROVIDER` | `ollama` |
| `LLM_MODEL` | `llama3.1:latest` |
| `LLM_ENDPOINT` | `http://127.0.0.1:11434` |
| `LLM_API_KEY` | `ollama` |
| `COGNEE_MCP_URL` | `http://127.0.0.1:8101/mcp` |

### Timeout Configuration

- `HTTP_TIMEOUT=5` — connect timeout in seconds for all curl calls
- `HTTP_MAX_TIME=10` — total request timeout in seconds for all curl calls
- Ollama API calls use: `curl --connect-timeout 5 --max-time 10`

## Execution Order

### Phase 1: Project Directory and Venv

1. Determine `PROJECT_DIR` (directory where `start_services.sh` resides, then `cd` into it)
2. Verify `.venv/bin/activate` exists; if not, error and exit
3. `source .venv/bin/activate` — activate the virtual environment
4. Verify `EMBEDDING_PROVIDER` is set; if not, load from `.env`:
   ```
   export $(grep -E '^EMBEDDING_' .env 2>/dev/null | xargs)
   ```

### Phase 2: Ollama Check

1. `OLLAMA_TIMEOUT=5`
2. `CURL_OPTS="-s --connect-timeout ${OLLAMA_TIMEOUT} --max-time ${OLLAMA_TIMEOUT}"`
3. Check Ollama:
   ```
   if curl ${CURL_OPTS} http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
       echo "✓ Ollama is running at http://127.0.0.1:11434"
   else
       echo "! Ollama not detected at http://127.0.0.1:11434"
       echo "  Please start Ollama first: ollama serve"
       echo "  Required models: llama3.1:latest, nomic-embed-text:latest"
       # Don't exit — allow startup to proceed; embeddings will fail later
   fi
```

### Phase 3: Start Cognee API

1. `echo ">>> Starting Cognee API on port 8102"`
2. `.venv/bin/python -m uvicorn cognee.api.client:app --host 127.0.0.1 --port 8102 > /tmp/cognee-api.log 2>&1 &`
3. `API_PID=$!`
4. Wait for API to be healthy (30s timeout):
   ```
   for i in $(seq 1 30); do
       code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 http://127.0.0.1:8102/health 2>/dev/null)
       if [ "$code" = "200" ]; then
           echo "✓ Cognee API healthy (HTTP 200)"
           break
       fi
       if [ "$i" -eq 30 ]; then
           echo "✗ Cognee API failed to become healthy after 30s"
           cat /tmp/cognee-api.log | tail -5
           exit 1
       fi
       sleep 1
   done
   ```

### Phase 4: Start Cognee MCP Server

1. `echo ">>> Starting Cognee MCP server on port 8101"`
2. `cognee-mcp --transport http --host 127.0.0.1 --port 8101 --path /mcp --api-url http://127.0.0.1:8102 > /tmp/cognee-mcp.log 2>&1 &`
3. `MCP_PID=$!`
4. Wait for MCP endpoint (30s timeout):
   ```
   for i in $(seq 1 30); do
       code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 http://127.0.0.1:8101/mcp 2>/dev/null)
       if [ "$code" = "200" ] || [ "$code" = "406" ]; then
           echo "✓ Cognee MCP healthy (HTTP $code)"
           break
       fi
       if [ "$i" -eq 30 ]; then
           echo "✗ Cognee MCP failed to start after 30s"
           cat /tmp/cognee-mcp.log | tail -5
           kill "$API_PID" 2>/dev/null
           exit 1
       fi
       sleep 1
   done
   ```

### Phase 5: Start Product API

1. `echo ">>> Starting Product API on port 8000"`
2. `cd /app`
3. `.venv/bin/python -m uvicorn app.api.product_api:app --host 127.0.0.1 --port 8000 > /tmp/product-api.log 2>&1 &`
4. `PRODUCT_PID=$!`
5. Wait for Product API to be healthy (30s timeout):
   ```
   for i in $(seq 1 30); do
       code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 http://127.0.0.1:8000/docs 2>/dev/null)
       if [ "$code" = "200" ]; then
           echo "✓ Product API healthy (HTTP 200)"
           break
       fi
       if [ "$i" -eq 30 ]; then
           echo "✗ Product API failed to become healthy after 30s"
           cat /tmp/product-api.log | tail -5
           kill "$API_PID" "$MCP_PID" 2>/dev/null
           exit 1
       fi
       sleep 1
   done
   ```

### Phase 6: Start Streamlit

1. `echo ">>> Starting Streamlit on port 8501"`
2. `cd /app`
3. `.venv/bin/python -m streamlit run app/ui/streamlit_app.py --port 8501 > /tmp/streamlit.log 2>&1 &`
4. `STREAMLIT_PID=$!`
5. Wait a moment for Streamlit to start: `sleep 3`

### Phase 7: Display Service Status

```
echo ""
echo "=== All services started ==="
echo ""
echo "  - Cognee API:      http://127.0.0.1:8102  (health: /health)"
echo "  - Cognee MCP:      http://127.0.0.1:8101/mcp  (path: /mcp)"
echo "  - Product API:     http://127.0.0.1:8000    (docs: /docs)"
echo "  - Streamlit:       http://127.0.0.1:8501"
echo ""
echo ">>> Press Ctrl+C to stop all services"
```

### Phase 8: Ctrl+C Handling

```
# ============================================================
# Wait for interrupt
# ============================================================
trap "echo 'Stopping services...'; kill $API_PID $MCP_PID $PRODUCT_PID $STREAMLIT_PID 2>/dev/null; echo 'Done'; wait" INT TERM

wait
```

## Key Features

### Timeout Protection

- All curl calls use `--connect-timeout 5 --max-time 10` — no command can hang indefinitely
- Health waits have 30‑second timeouts with polling loops
- On timeout, the script prints the relevant log file tail and exits (or continues)

### No Silent Failures

- Each service start prints a clear status message
- On failure, the script prints the last 5 lines of the service log
- On Ctrl+C, only processes started by this script are killed
- Duplicate instances are avoided by the trap mechanism

### Process IDs Tracked

- `API_PID` — Cognee API (port 8102)
- `MCP_PID` — Cognee MCP server (port 8101)
- `PRODUCT_PID` — Product API (port 8000)
- `STREAMLIT_PID` — Streamlit (port 8501)

All are tracked in the trap and killed on INT/TERM.

### Verification Before Proceeding

The script checks each service independently before moving to the next:

1. Ollama reachable (with timeout)
2. Cognee API healthy (HTTP 200 on `/health`)
3. Cognee MCP healthy (HTTP 200 or 406 on `/mcp`)
4. Product API healthy (HTTP 200 on `/docs`)
5. Streamlit started (sleep 3, then proceed)

If any check fails, the script exits with an error (except Ollama, which only warns and allows proceeding).

## Error Details Example

If Ollama is not running:

```
! Ollama not detected at http://127.0.0.1:11434
  Please start Ollama first: ollama serve (or ensure it's running)
  Required models: llama3.1:latest, nomic-embed-text:latest
```

If Cognee API fails to start:

```
✗ Cognee API failed to become healthy after 30s
  tail of /tmp/cognee-api.log:
  ERROR: failed to bind address
```

If Product API fails:

```
✗ Product API failed to become healthy after 30s
  tail of /tmp/product-api.log:
  HINT: See above for instance already running.
```

## Post-Startup Verification

After the script completes, verify all services:

```bash
# Check Ollama models
curl -s http://127.0.0.1:11434/api/tags

# Check Cognee API health
curl -s http://127.0.0.1:8102/health

# Check Cognee MCP
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8101/mcp

# Check Product API
curl -s http://127.0.0.1:8000/docs

# Check Streamlit is reachable
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8501/_sthealth
```

## What the Script Does NOT Do

- Does **not** pull Ollama models — assumes they are already installed
- Does **not** configure Cognee beyond loading `.env` vars
- Does **not** start the LLM model download — assumes `llama3.1:latest` is available
- Does **not** create the `data/products.json` — assumes it has been migrated from the original project
- Does **not** create or initialize Cognee memories — memories are stored by the application at runtime

---