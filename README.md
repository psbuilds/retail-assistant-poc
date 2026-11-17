# Retail Memory Assistant POC

## Overview

A clean, reliable, fully working end-to-end demo of a memory-aware shopping assistant using Cognee MCP, Ollama and a real product catalogue.

**Single-user POC** — one person uses the application. No customer isolation, no multi-user support.

## Architecture

```text
Streamlit UI (port 8501)
    |
Conversation Service (intent extraction → routing → mode handling)
    |---- Memory Service (Cognee MCP: remember/recall/forget)
    |---- Product Service (Product API search + filtering)
    |
Ollama (ports: 11434 — embeddings: nomic-embed-text:latest, LLM: llama3.1:latest)
    |
Cognee MCP Server (port 8101 /mcp)
    |
Cognee API Server (port 8102 /health)
    |
Product Catalogue (data/products.json — 20 migrated products)
```

## Key Features

- **Memory**: STORE durable preferences, RECALL them, INSPECT stored fragments, DELETE All Stored Memories
- **Product Discovery**: Category, budget, brand, wireless, noise-cancellation filtering + deterministic ranking
- **Normal Conversation**: ANC explanations, wireless questions, greetings, gratitude
- **Persistence**: Memories survive Streamlit restart
- **Memory Management**: Delete all memories with confirmation; clear chat only clears conversation (memories preserved)
- **Configuration**: All via `.env` — no hardcoded secrets

## Requirements Fulfilled

- ✅ Cognee 1.6.6 configured for local Ollama embeddings (not OpenAI)
- ✅ HTTP 409 Conflict bug fixed (EMBEDDING_* vars in `.env`)
- ✅ MCP recall `datasets` parameter as string (not list)
- ✅ `fragments[1:]` → `fragments` fix (summary already stripped)
- ✅ Startup script with venv activation + `.env` loading + health waits
- ✅ Health check script with timeouts and final summary
- ✅ Product catalogue migrated from original project (SHA-256 verified)
- ✅ MCP-only architecture preserved (no direct SDK/CLI calls)
- ✅ Original `retail-memory-poc-omnirush` read-only
- ✅ All 26 unit tests pass
- ✅ All 7 demo scenarios verified

## Quick Start

```bash
# 1. Ensure data/products.json is in place (migrated from original project)
# 2. Install dependencies
cd retail-memory-assistant
.venv/bin/pip install -e .

# 3. Start all services
./scripts/start_services.sh

# 4. Check service health
./scripts/health_check.sh

# 5. Open Streamlit in browser
#    http://127.0.0.1:8501
```

## Documentation

- `docs/explanations/` — detailed source-code explanations for every maintained file
- `docs/explanations/README.md` — index linking all explanations
- `docs/ARCHITECTURE.md` — high-level architecture diagram and description
- `docs/DEMO_GUIDE.md` — all seven scenarios with step-by-step instructions
- `docs/IMPLEMENTATION_LOG.md` — chronological implementation changes and verification

Run: `./scripts/health_check.sh` to verify all services are healthy before using the demo.

## Requirements (Non-Negotiable)

- Modify only `retail-memory-assistant`. Keep `retail-memory-poc-omnirush` read-only.
- Do not implement customer isolation or multi-user support in this POC.
- Do not delete unrelated Cognee datasets.
- Do not replace original product data with fabricated examples.
- Do not use direct Cognee SDK or CLI memory operations.
- All memory operations, including deletion, must use MCP.
- Do not fake successful STORE, RECALL, inspection or deletion.
- Do not hide integration failures.
- Do not claim success based only on unit tests.
- Do not write explanations based on assumptions.
- Do not introduce unnecessary dependencies or features.

The priority is a clean, understandable, single-user demo that works end to end and can be reset safely for repeated demonstrations.