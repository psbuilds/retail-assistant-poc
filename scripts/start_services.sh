#!/bin/bash
# start_services.sh - Start all required services for the Retail Memory Assistant

set -e

echo "=== Starting Cognee API ==="
# Start Cognee API - this should be started first
cognee api start --port 8102 || echo "Cognee API may already be running"

echo "=== Starting Cognee MCP HTTP server ==="
# Start Cognee MCP server
cognee mcp start --port 8101 || echo "Cognee MCP may already be running"

echo "=== Starting Product API ==="
# Start product API (assuming it's a simple uvicorn app)
cd /app
uvicorn app.api.product_api:app --host 127.0.0.1 --port 8000 &
PRODUCT_PID=$!

echo "=== Starting Streamlit application ==="
cd /app
streamlit run app/ui/streamlit_app.py --port 8501 &
STREAMLIT_PID=$!

echo ""
echo "=== Services running ==="
echo "  - Cognee API: http://127.0.0.1:8102"
echo "  - Cognee MCP: http://127.0.0.1:8101/mcp"
echo "  - Product API: http://127.0.0.1:8000"
echo "  - Streamlit: http://127.0.0.1:8501"
echo ""
echo "Press Ctrl+C to stop all services"

# Wait for interrupt
trap "echo 'Stopping services...'; kill $PRODUCT_PID $STREAMLIT_PID 2>/dev/null; echo 'Done'" INT

wait