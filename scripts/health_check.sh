#!/bin/bash
# health_check.sh - Check health of all services

echo "=== Checking Service Health ==="
echo ""

# Check Cognee API
echo "Checking Cognee API (port 8102):"
curl -s http://127.0.0.1:8102/health 2>/dev/null && echo "  ✓ Healthy" || echo "  ✗ Not responding"

# Check Cognee MCP
echo "Checking Cognee MCP (port 8101):"
curl -s http://127.0.0.1:8101/mcp 2>/dev/null && echo "  ✓ Healthy" || echo "  ✗ Not responding"

# Check Product API
echo "Checking Product API (port 8000):"
curl -s http://127.0.0.1:8000/ 2>/dev/null && echo "  ✓ Healthy" || echo "  ✗ Not responding"

# Check Streamlit
echo "Checking Streamlit (port 8501):"
curl -s http://127.0.0.1:8501/_sthealth 2>/dev/null && echo "  ✓ Healthy" || echo "  ✗ Not responding"

echo ""
echo "=== All checks complete ==="