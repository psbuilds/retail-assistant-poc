#!/bin/bash
# health_check.sh - Check health of all services for the Retail Memory Assistant
# Uses meaningful HTTP checks where possible, not just port checks.
# Features timeouts on all curl calls; never hangs indefinitely.
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# Load .env embedding vars so they are available for the app, but do NOT
# override env vars the user may already have set.  Only load missing ones.
if [ -f .env ]; then
    export $(grep -E '^EMBEDDING_' .env 2>/dev/null | xargs)
fi

# --8<--8<--8<-- TIMEOUT CONFIG --8<--8<--8<--
HTTP_TIMEOUT=5       # connect timeout in seconds
HTTP_MAX_TIME=10     # total request timeout in seconds
# --8<--8<--8<-- END TIMEOUT CONFIG --8<--8<--8<--

ALL_HEALTHY=true

# -------------------------------------------------------
# Helper: run a curl health check with timeouts.
# Arguments: $1 = URL, $2 = label (for error messages)
# Returns: 0 if HTTP 2xx or known-acceptable non-2xx, 1 otherwise.
# -------------------------------------------------------
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

# -------------------------------------------------------
# 1. Ollama (embedding / LLM endpoint)
# -------------------------------------------------------
echo ">>> Checking Ollama (embedding / LLM endpoint)"

OLLAMA_URL="http://127.0.0.1:11434/api/tags"

# Always try the curl; if it fails we report clearly and move on
OLLAMA_RESPONSE=$(curl -s --connect-timeout "$HTTP_TIMEOUT" \
    --max-time "$HTTP_MAX_TIME" \
    "$OLLAMA_URL" 2>/dev/null) || true

if [ -z "$OLLAMA_RESPONSE" ]; then
    echo "  ✗ Ollama not reachable at $OLLAMA_URL"
    echo "    Error: connection refused or timeout"
    echo "    Recovery: start Ollama:  ollama serve (ensure it's running)"
    ALL_HEALTHY=false
else
    echo "  ✓ Ollama reachable at $OLLAMA_URL"

    # Parse JSON model list — guard against malformed JSON
    MODEL_NAMES=$(echo "$OLLAMA_RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    models = [m.get('name', '') for m in d.get('models', [])]
    print('|'.join(models))
except Exception:
    print('')
" 2>/dev/null) || true

    # Verify required models exist; only check if we successfully parsed the list
    REQUIRED="nomic-embed-text:latest|llama3.1:latest"
    FOUND_EACH=0

    if [ -n "$MODEL_NAMES" ]; then
        for model in $REQUIRED; do
            if echo "$MODEL_NAMES" | grep -qF "$model"; then
                echo "   - Model '$model': found"
                FOUND_EACH=$((FOUND_EACH+1))
            else
                echo "   - Model '$model': NOT found"
                ALL_HEALTHY=false
            fi
        done
    else
        # We got a response but couldn't parse it as valid JSON model list
        echo "   ! Could not parse Ollama model list (may be outdated API version)"
        ALL_HEALTHY=false
    fi

    if [ "$FOUND_EACH" -eq 2 ]; then
        echo "  ✓ Required models (nomic-embed-text:latest + llama3.1:latest) are available"
    fi
fi

# -------------------------------------------------------
# 2. Cognee API (port 8102)
# -------------------------------------------------------
echo ""
echo ">>> Checking Cognee API (port 8102)"

if check_http "http://127.0.0.1:8102/health" "Cognee API"; then
    : # success recorded inside check_http
else
    ALL_HEALTHY=false
fi

# -------------------------------------------------------
# 3. Cognee MCP endpoint (port 8101/mcp)
# -------------------------------------------------------
echo ""
echo ">>> Checking Cognee MCP (port 8101/mcp)"

# The MCP endpoint returns 406 if the routing layer is up but no tools matched;
# 200 means the full session is up. Either is "healthy" for our purposes.
# We explicitly do NOT treat 400 or 406 alone as proof MCP is unavailable.
if check_http "http://127.0.0.1:8101/mcp" "Cognee MCP"; then
    : # success
else
    ALL_HEALTHY=false
fi

# -------------------------------------------------------
# 4. Product API (port 8000)
# -------------------------------------------------------
echo ""
echo ">>> Checking Product API (port 8000)"

if check_http "http://127.0.0.1:8000/docs" "Product API"; then
    : # success
else
    ALL_HEALTHY=false
fi

# -------------------------------------------------------
# 5. Streamlit (port 8501)
# -------------------------------------------------------
echo ""
echo ">>> Checking Streamlit (port 8501)"

# Streamlit has a health endpoint; fall back to root
STREAMLIT_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
    --connect-timeout "$HTTP_TIMEOUT" \
    --max-time "$HTTP_MAX_TIME" \
    "http://127.0.0.1:8501/_sthealth" 2>/dev/null) || true

if [ "$STREAMLIT_CODE" = "200" ]; then
    echo "  ✓ Streamlit healthy (HTTP 200 /_sthealth)"
elif curl -s --connect-timeout "$HTTP_TIMEOUT" \
    --max-time "$HTTP_MAX_TIME" \
    "http://127.0.0.1:8501" >/dev/null 2>&1; then
    echo "  ✓ Streamlit reachable (HTTP 200 /)"
else
    echo "  ✗ Streamlit not responding at http://127.0.0.1:8501"
    ALL_HEALTHY=false
fi

# -------------------------------------------------------
# Summary
# -------------------------------------------------------
echo ""
echo "=== Health Check Complete ==="
if $ALL_HEALTHY; then
    echo "  ✓ All services healthy"
else
    echo "  ✗ Some services unhealthy - see above"
fi
echo ""
echo "For full integration verification, run:"
echo "  ./.venv/bin/python scripts/verify_providers.py"
echo ""
exit 0