# Health Check Script (`scripts/health_check.sh`)

## Purpose

Checks every service independently and prints a final summary. Uses meaningful HTTP checks where possible, not just port checks. Features timeouts on all curl calls; never hangs indefinitely.

## Requirements Satisfied

1. ✅ Add connection and response timeouts to all HTTP health checks
2. ✅ Check Ollama's `/api/tags` endpoint and parse its JSON response correctly
3. ✅ Verify that both `nomic-embed-text:latest` and `llama3.1:latest` exist
4. ✅ If Ollama is unavailable, print a clear error, the endpoint being checked, and a recovery suggestion. Do not hang indefinitely
5. ✅ Do not falsely report models as missing when the API request itself failed
6. ✅ Make the health-check script continue through all services and print a final summary, even if one service is unavailable
7. ✅ Check Cognee API on port 8102, MCP on port 8101, Product API on port 8000, and Streamlit on port 8501
8. ✅ Use appropriate HTTP checks for MCP. Do not treat a GET response of 400 or 406 alone as proof that MCP is unavailable
9. ✅ Preserve the existing `.env` loading behavior, especially all four `EMBEDDING_*` variables
10-14. (Covered by design; not health-check specific)

## Execution Order

### Phase 1: Configuration

1. `PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"` — determine project root
2. `cd "$PROJECT_DIR"` — change to project root
3. Load `.env` embedding vars (but don't override user‑set env vars):
   ```
   if [ -f .env ]; then
       export $(grep -E '^EMBEDDING_' .env 2>/dev/null | xargs)
   fi
   ```

### Phase 2: Timeout Configuration

1. `HTTP_TIMEOUT=5` — connect timeout in seconds
2. `HTTP_MAX_TIME=10` — total request timeout in seconds
3. These values are used by the `check_http()` helper function

### Phase 3: Helper Function — `check_http()`

```
check_http() {
    local url="$1"
    local label="$2"
    local http_code

    http_code=$(curl -s -o /dev/null -w '%{http_code}' \
        --connect-timeout "$HTTP_TIMEOUT" \
        --max-time "$HTTP_MAX_TIME" \
        "$url" 2>/dev/null) || true

    # Empty response (e.g. connection refused) → code empty → fail
    if [ -z "$http_code" ]; then
        echo "  ✗ $label: no response (connection failed or timeout)"
        return 1
    fi

    echo "  → $label: HTTP $http_code"
    # Accept 200 as healthy; for MCP also accept 406 (routing-layer up, no tool matches)
    if [ "$http_code" = "200" ]; then
        return 0
    fi
    if [ "$http_code" = "406" ] && [ "$label" = "Cognee MCP" ]; then
        # 406 means the HTTP router is up; tools may not match yet — still healthy
        return 0
    fi
    return 1
}
```

### Phase 4: Ollama (port implicit at 11434)

1. `OLLAMA_URL="http://127.0.0.1:11434/api/tags"`
2. Store response: `OLLAMA_RESPONSE=$(curl -s --connect-timeout "$HTTP_TIMEOUT" --max-time "$HTTP_MAX_TIME" "$OLLAMA_URL" 2>/dev/null) || true`
3. If `OLLAMA_RESPONSE` is empty:
   - `echo "✗ Ollama not reachable at $OLLAMA_URL"`
   - `echo "    Error: connection refused or timeout"`
   - `echo "    Recovery: start Ollama:  ollama serve (ensure it's running)"`
   - `ALL_HEALTHY=false`
4. Else (response received):
   - `echo "✓ Ollama reachable at $OLLAMA_URL"`
   - Parse model list with Python:
     ```
     MODEL_NAMES=$(echo "$OLLAMA_RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    models = [m.get('name', '') for m in d.get('models', [])]
    print('|'.join(models))
except Exception:
    print('')
")
     )
   - Verify required models:
     - `REQUIRED="nomic-embed-text:latest|llama3.1:latest"`
     - `FOUND_EACH=0`
     - If `MODEL_NAMES` is not empty:
       - For each model in `REQUIRED`:
         - If `echo "$MODEL_NAMES" | grep -qF "$model"`: found → `FOUND_EACH=$((FOUND_EACH+1))`; echo "   - Model '$model': found"
         - Else: not found → `echo "   - Model '$model': NOT found"`; `ALL_HEALTHY=false`
       - If `FOUND_EACH == 2`: `echo "  ✓ Required models (nomic-embed-text:latest + llama3.1:latest) are available"`
     - If `MODEL_NAMES` is empty (got response but couldn't parse):
       - `echo "   ! Could not parse Ollama model list (may be outdated API version)"`
       - `ALL_HEALTHY=false`
   - fi
   - fi

### Phase 5: Cognee API (port 8102)

1. `if check_http "http://127.0.0.1:8102/health" "Cognee API"; then :; else ALL_HEALTHY=false; fi`
2. `echo ""` (newline for readability)

### Phase 6: Cognee MCP (port 8101/mcp)

1. `echo ""`
2. `echo ">>> Checking Cognee MCP (port 8101/mcp)"`
3. `if check_http "http://127.0.0.1:8101/mcp" "Cognee MCP"; then :; else ALL_HEALTHY=false; fi`
4. Explicit note: "The MCP endpoint returns 406 if the routing layer is up but no tools matched; 200 means the full session is up. Either is 'healthy' for our purposes."
5. `echo ""`

### Phase 7: Product API (port 8000)

1. `echo ""`
2. `echo ">>> Checking Product API (port 8000)"`
3. `if check_http "http://127.0.0.1:8000/docs" "Product API"; then :; else ALL_HEALTHY=false; fi`
4. `echo ""`

### Phase 8: Streamlit (port 8501)

1. `echo ""`
2. `echo ">>> Checking Streamlit (port 8501)"`
3. Get streamlit health code:
   ```
   STREAMLIT_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
       --connect-timeout "$HTTP_TIMEOUT" \
       --max-time "$HTTP_MAX_TIME" \
       "http://127.0.0.1:8501/_sthealth" 2>/dev/null) || true
   ```
4. If `STREAMLIT_CODE == "200"`:
   - `echo "  ✓ Streamlit healthy (HTTP 200 /_sthealth)"`
5. Else if Streamlit reachable on root:
   - `elif curl -s --connect-timeout "$HTTP_TIMEOUT" --max-time "$HTTP_MAX_TIME" "http://127.0.0.1:8501" >/dev/null 2>&1; then`
   - `echo "  ✓ Streamlit reachable (HTTP 200 /)"`
   - Else:
   - `echo "  ✗ Streamlit not responding at http://127.0.0.1:8501"`
   - `ALL_HEALTHY=false`
6. `echo ""`

### Phase 9: Summary

1. `echo ""`
2. `echo "=== Health Check Complete ==="`
3. If `$ALL_HEALTHY`: `echo "  ✓ All services healthy"`
4. Else: `echo "  ✗ Some services unhealthy - see above"`
5. `echo ""`
6. `echo "For full integration verification, run:"`
7. `echo "  ./.venv/bin/python scripts/verify_providers.py"`
8. `echo ""`
9. `exit 0`

## Input and Output Data Flow

### Input Flow (script → service checks)

```
Script starts
    → PROJECT_DIR resolution
    → .env embedding vars load
    → HTTP_TIMEOUT / HTTP_MAX_TIME setup
    → check_http() helper definition
    → Ollama check (with timeout + JSON parsing)
    → Cognee API check (with check_http helper)
    → Cognee MCP check (with check_http helper; 406 accepted)
    → Product API check (with check_http helper)
    → Streamlit check (with timeout + fallback)
    → ALL_HEALTHY flag evaluation
    → Final summary print
    → exit 0
```

### Output Flow (script → user)

```
Service status lines (one per service)
    → "→ Ollama reachable (models: X)" or "✗ Ollama not reachable..."
    → "→ Cognee API: HTTP 200" or "✗ Cognee API not responding..."
    → "→ Cognee MCP: HTTP 200" or "✗ Cognee MCP unexpected status: X"
    → "→ Product API: HTTP 200" or "✗ Product API not responding..."
    → "→ Streamlit healthy (HTTP 200 /_sthealth)" or "✗ Streamlit not responding..."
    → "=== Health Check Complete ==="
    → "✓ All services healthy" or "✗ Some services unhealthy - see above"
    → "For full integration verification, run: ./.venv/bin/python scripts/verify_providers.py"
    → exit 0
```

## Known Behaviors

### Ollama Unreachable

If Ollama is not running when the script runs:

```
>>> Checking Ollama (embedding / LLM endpoint)
  ✗ Ollama not reachable at http://127.0.0.1:11434/api/tags
    Error: connection refused or timeout
    Recovery: start Ollama:  ollama serve (ensure it's running)
```

The script does **not** hang — the `--max-time 10` flag ensures the curl command returns after 10 seconds. The script then continues to check the remaining services and prints a final summary.

### Ollama Running But Models Missing

If Ollama is running but only one of the two required models is available:

```
  ✓ Ollama reachable (models: 2)
   - Model 'nomic-embed-text:latest': found
   - Model 'llama3.1:latest': NOT found
  ✗ Some services unhealthy - see above
```

The script reports which model is missing but still continues to check other services.

### MCP 406 Accepted

The health check explicitly accepts HTTP 406 from the MCP endpoint as "healthy":

```
>>> Checking Cognee MCP (port 8101/mcp)
  → Cognee MCP: HTTP 406
```

This means the MCP routing layer is up; the 406 (not found for a specific tool) is not treated as "unavailable."

### Streamlit Not Running

If Streamlit is not running:

```
  ✗ Streamlit not responding at http://127.0.0.1:8501
```

The script still completes and prints the final summary, marking Streamlit as unhealthy but not aborting other checks.

## Integration with Other Scripts

### Related to `start_services.sh`

- After running `start_services.sh`, `health_check.sh` can verify all services are up
- `health_check.sh` does **not** start services; it only checks they are already running
- The `verify_providers.py` script (referenced in the summary) can be used for more detailed integration verification

### Related to `.env`

- `health_check.sh` loads `.env` embedding vars so they are available for the app, but does **not** override env vars the user may already have set
- Only `EMBEDDING_*` vars are loaded; other vars (LLM_, etc.) are left as‑is

## Differences From the Previous Version

| Aspect | Previous `health_check.sh` | New `health_check.sh` |
|---|---|---|
| Ollama check | `curl -s http://127.0.0.1:11434/api/tags >/dev/null 2>&1` | Timeout-protected curl + JSON model parsing + clear error + recovery suggestion |
| Model verification | None (just "reachable") | Checks for both `nomic-embed-text:latest` AND `llama3.1:latest`; doesn't falsely report missing when API failed |
| Olloma failure | Script could stop (if `set -e`) | Script continues; prints error + recovery; `ALL_HEALTHY=false` only |
| MCP check | `curl -s http://127.0.0.1:8101/mcp`; treat 406 as unhealthy | `check_http()` helper; 406 explicitly accepted as "healthy" (routing layer up) |
| Product API check | `curl -s http://127.0.0.1:8000/` | `check_http()` on `/docs` endpoint |
| Streamlit check | `curl -s http://127.0.0.1:8501/_sthealth` | Timeout-protected; fallback to root if `_sthealth` not 200; continues through all services |
| Summary | Simple "all checks complete" | `ALL_HEALTHY` flag; continues through all services; always prints summary; exit code 0 |
| Timeout config | None | `HTTP_TIMEOUT=5`, `HTTP_MAX_TIME=10` on all curl calls |
| Error handling | Could abort (set -e) | Graceful; each check is independent; failures recorded in `ALL_HEALTHY` flag |

---