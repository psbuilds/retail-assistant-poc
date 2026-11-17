#!/bin/bash
# start_services.sh - Start all required services for the Retail Memory Assistant
# Ensures the correct venv (.venv) and .env are used.
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "=== Retail Memory Assistant - Service Startup ==="
echo ""

# Ensure we are in the correct project directory and venv
if [ -f ".venv/bin/activate" ]; then
    echo ">>> Activating .venv virtual environment"
    source .venv/bin/activate
else
    echo ">>> ERROR: .venv not found in $PROJECT_DIR"
    echo "   Please ensure the script is run from retail-memory-assistant root."
    exit 1
fi

# Verify embedding configuration is present
if [ -z "${EMBEDDING_PROVIDER:-}" ]; then
    echo ">>> WARNING: EMBEDDING_PROVIDER not set in environment"
    echo "   Loading from .env file..."
    export $(grep -E '^EMBEDDING_' .env 2>/dev/null | xargs)
fi

echo ">>> Embedding config: PROVIDER=${EMBEDDING_PROVIDER:-ollama} MODEL=${EMBEDDING_MODEL:-nomic-embed-text:latest}"

# --8<--8<--8<-- TIMEOUT CONFIG --8<--8<--8<--
OLLAMA_TIMEOUT=5
CURL_OPTS="-s --connect-timeout ${OLLAMA_TIMEOUT} --max-time ${OLLAMA_TIMEOUT}"
# --8<--8<--8<-- END TIMEOUT CONFIG --8<--8<--8<--

# ============================================================
# 1. Verify Ollama is running (with timeout, no hang)
# ============================================================
echo ""

echo "=== Checking Ollama ==="
if curl ${CURL_OPTS} http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "  ✓ Ollama is running at http://127.0.0.1:11434"
else
    echo " ! Ollama not detected at http://127.0.0.1:11434"
    echo "  Please start Ollama first: ollama serve (or ensure it's running)"
    echo "  Required models: llama3.1:latest, nomic-embed-text:latest"
    # Don't exit — allow startup to proceed; embeddings will fail later
fi

# ============================================================
# 2. Start Cognee API (port 8102)
# ============================================================
echo ""
echo ">>> Starting Cognee API on port 8102"
.venv/bin/python -m uvicorn cognee.api.client:app \
    --host 127.0.0.1 --port 8102 > /tmp/cognee-api.log 2>&1 &
API_PID=$!

# Wait for API to be healthy
for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 http://127.0.0.1:8102/health 2>/dev/null)
    if [ "$code" = "200" ]; then
        echo "  ✓ Cognee API healthy (HTTP 200)"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  ✗ Cognee API failed to become healthy after 30s"
        cat /tmp/cognee-api.log | tail -5
        exit 1
    fi
    sleep 1
done

# ============================================================
# 3. Start Cognce MCP server (port 8101, /mcp)
# ============================================================
echo ""
echo ">>> Starting Cognee MCP server on port 8101"
cognee-mcp --transport http \
    --host 127.0.0.1 --port 8101 --path /mcp \
    --api-url http://127.0.0.1:8102 > /tmp/cognee-mcp.log 2>&1 &
MCP_PID=$!

# Wait for MCP endpoint
for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 http://127.0.0.1:8101/mcp 2>/dev/null)
    if [ "$code" = "200" ] || [ "$code" = "406" ]; then
        echo "  ✓ Cognee MCP healthy (HTTP $code)"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  ✗ Cognee MCP failed to start after 30s"
        cat /tmp/cognee-mcp.log | tail -5
        kill "$API_PID" 2>/dev/null
        exit 1
    fi
    sleep 1
done

# ============================================================
# 4. Start Product API (port 8000)
# ============================================================
echo ""
echo ">>> Starting Product API on port 8000"
cd /app
.venv/bin/python -m uvicorn app.api.product_api:app \
    --host 127.0.0.1 --port 8000 > /tmp/product-api.log 2>&1 &
PRODUCT_PID=$!

# Wait for Product API
for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 http://127.0.0.1:8000/docs 2>/dev/null)
    if [ "$code" = "200" ]; then
        echo "  ✓ Product API healthy (HTTP 200)"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  ✗ Product API failed to become healthy after 30s"
        cat /tmp/product-api.log | tail -5
        kill "$API_PID" "$MCP_PID" 2>/dev/null
        exit 1
    fi
    sleep 1
done

# ============================================================
# 5. Start Streamlit (port 8501)
# ============================================================
echo ""
echo ">>> Starting Streamlit on port 8501"
cd /app
.venv/bin/python -m streamlit run app/ui/streamlit_app.py --port 8501 > /tmp/streamlit.log 2>&1 &
STREAMLIT_PID=$!

# Wait a moment for Streamlit to start
sleep 3

echo ""
echo "=== All services started ==="
echo ""
echo "  - Cognee API:      http://127.0.0.1:8102  (health: /health)"
echo "  - Cognee MCP:      http://127.0.0.1:8101/mcp  (path: /mcp)"
echo "  - Product API:     http://127.0.0.1:8000    (docs: /docs)"
echo "  - Streamlit:       http://127.0.0.1:8501"
echo ""
echo ">>> Press Ctrl+C to stop all services"

# ============================================================
# Wait for interrupt
# ============================================================
trap "echo 'Stopping services...'; kill $API_PID $MCP_PID $PRODUCT_PID $STREAMLIT_PID 2>/dev/null; echo 'Done'; wait" INT TERM

wait