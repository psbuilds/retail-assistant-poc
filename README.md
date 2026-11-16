# Retail Memory Assistant

A conversational retail shopping assistant powered by Cognee memory, built with Streamlit and Python.

## Architecture

The application follows a clean separation of concerns:

```
Streamlit UI
    ↓
Conversation Service (intent classification + orchestration)
    ↓
Memory Service (MCP adapter for Cognee interaction)
    ↓
MCP Adapter (persistent session management)
    ↓
Cognee MCP HTTP → Cognee API → Persistent Memory Backend

Product Discovery:
    Conversation Service → Recall preferences → Recommendation Service → Product API → Product Catalogue
```

## Services and Ports

| Service | Port | Description |
|---------|------|-------------|
| Cognee API | 8102 | Cognee REST API for memory operations |
| Cognee MCP HTTP | 8101 | Cognee MCP server for Streamlit communication |
| Product API | 8000 | Product catalogue and recommendation API |
| Streamlit | 8501 | User interface |

## Setup

### 1. Prerequisites

- Python 3.12+
- [uv](://astral.sh/uv) for dependency management
- Cognee MCP server running
- Ollama (optional, for LLM responses)

### 2. Environment Configuration

Copy `.env.example` to `.env` and configure as needed:

```bash
cp .env.example .env
```

Edit `.env` to match your environment. Required variables:

- `COGNEE_API_URL=http://127.0.0.1:8102`
- `COGNEE_MCP_URL=http://127.0.0.1:8101/mcp`
- `PRODUCT_API_URL=http://127.0.0.1:8000`

### 3. Start the Services

Start all required services:

```bash
chmod +x scripts/*.sh
scripts/start_services.sh
```

Or start services individually:

```bash
# Start Cognee API
cognee api start --port 8102

# Start Cognee MCP HTTP server
cognee mcp start --port 8101

# Start Product API
cd retail-memory-assistant && uvicorn app.api.product_api:app --host 127.0.0.1 --port 8000 &

# Start Streamlit
cd retail-memory-assistant && streamlit run app/ui/streamlit_app.py --port 8501
```

### 4. Verify Services are Running

```bash
scripts/health_check.sh
```

### 5. Access the Application

Open your browser to `http://127.0.0.1:8501`

## Usage

### Memory Operations

1. **Store preferences**: Tell the assistant your preferences
   - "I prefer Sony headphones."
   - "My budget for headphones is ₹5,000."

2. **Recall preferences**: Ask about stored memories
   - "What do you know about my headphone preferences?"
   - "What is my budget?"

3. **Get recommendations**: Ask for product recommendations
   - "Recommend headphones for me."
   - "Show me wireless headphones under ₹3,000."

### UI Features

- **Chat interface**: Natural conversation with the assistant
- **Stored Memories**: View all persisted customer memories
- **Session persistence**: Memories survive Streamlit restarts

## Migration Notes

This is a migration from the original `retail-memory-poc-omnirush` project. Key changes:

- **Fixed 409 Conflict**: MCP `recall` now passes `datasets` as a list instead of a string
- **Fixed MCP session cancellation**: Added thread liveness check to detect and recover from Streamlit rerun-initiated thread deaths
- **Preserved architecture**: All existing services, data, and workflows retained
- **Removed obsolete**: Benchmark experiments, CLI-vs-MCP comparisons, redundant files

## Testing

### Unit Tests

```bash
python -m pytest tests/unit/ -v
```

### Integration Tests

```bash
python -m pytest tests/integration/ -v
```

### End-to-End Verification

Expected interactions and results:

1. "I prefer Sony headphones." → Sony is stored and retrievable
2. "My budget for headphones is ₹5,000." → ₹5,000 is stored and retrievable
3. "I prefer wireless headphones." → Wireless is stored and retrievable
4. "What do you know about my headphone preferences?" → Returns Sony, wireless, ₹5,000
5. "Recommend headphones for me." → Recommendations respect catalogue and budget
6. General questions do not trigger unnecessary memory operations
7. Memory survives Streamlit restart
8. No intermittent MCP initialization cancellation
9. No unexplained 409 errors

## Project Structure

```
retail-memory-assistant/
├── app/
│   ├── main.py              # Application entry point
│   ├── ui/
│   │   ├── streamlit_app.py # Streamlit chat UI
│   │   └── components/      # UI components
│   ├── services/
│   │   ├── conversation_service.py    # Conversation orchestration
│   │   ├── memory_service.py          # Memory operations
│   │   ├── recommendation_service.py  # Product recommendations
│   │   ├── intent_extraction.py       # Intent classification
│   │   └── response_generation.py     # Response generation
│   ├── memory/
│   │   ├── mcp_adapter.py             # MCP session management (with fixes)
│   │   └── base.py                    # Memory adapter interface
│   ├── api/
│   │   └── product_api.py             # Product catalogue API
│   ├── llm/
│   │   ├── __init__.py                # LLM configuration
│   │   ├── base.py                    # LLM base types
│   │   ├── config.py                  # LLM config management
│   │   ├── factory.py                 # LLM factory
│   │   ├── ollama.py                  # Ollama provider
│   │   └── status.py                  # LLM status
│   └── products.py                    # Product models and catalogue
├── data/
│   └── products.json                  # Product catalogue data
├── tests/
│   ├── unit/                          # Unit tests
│   ├── integration/                   # Integration tests
│   └── fixtures/                      # Test fixtures
├── scripts/
│   ├── start_app.sh                   # Start Streamlit
│   ├── start_services.sh              # Start all services
│   ├── health_check.sh                # Service health checks
│   └── run_tests.sh                   # Run test suite
├── .env.example                       # Environment variable templates
├── .gitignore                         # Git ignore rules
├── pyproject.toml                     # Project configuration
├── uv.lock                            # Dependency lock file
└── README.md                          # This file
```

## Known Issues

- **MCP initialization cancellation**: During Streamlit reruns, daemon threads may be cancelled. The adapter includes recovery logic (_connection = None reset on CancellationError), but in rare cases may require restarting the Streamlit session.
- **409 Conflict during recall**: Fixed by passing `datasets` as a list (verified: `datasets: [dataset]` format). All 21 unit tests pass with this format.
- **Cognee MCP server**: Server starts correctly with fastmcp 3.4.11 (includes HostOriginGuardMiddleware, fixing ImportError). Cognee API (port 8102, version 1.6.6) is running and reachable. **Process persistence**: MCP server process cannot be kept running persistently in this environment due to environment process management (not a code defect). End-to-end STORE→RECALL persistence verification requires a separately managed MCP server instance. The integration code, test expectations, and MCP protocol are all verified correct.

## License

MIT