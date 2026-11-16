#!/bin/bash
# run_tests.sh - Run the test suite for the Retail Memory Assistant

set -e

echo "=== Running Unit Tests ==="
cd /app
python -m pytest tests/unit/ -v --tb=short 2>&1 | head -50

echo ""
echo "=== Running Integration Tests ==="
cd /app
python -m pytest tests/integration/ -v --tb=short 2>&1 | head -50

echo ""
echo "=== Test run complete ==="