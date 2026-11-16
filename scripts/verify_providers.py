"""Ad-hoc runtime probe: does the configured provider pair pass Cognee's
preflight, and does the Ollama embedding engine return real vectors?

Run from the project root with the project's .env loaded:

    .venv/bin/python scripts/verify_providers.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def main() -> int:
    from cognee.infrastructure.databases.vector.embeddings.config import (
        get_embedding_context_config,
    )
    from cognee.infrastructure.llm.config import get_llm_context_config
    from cognee.modules.preflight import check_provider_config, validate_provider_config

    llm = get_llm_context_config()
    embedding = get_embedding_context_config()

    print("LLM     :", llm.llm_provider, llm.llm_model, llm.llm_endpoint)
    print("Embed   :", embedding.embedding_provider, embedding.embedding_model,
          embedding.embedding_dimensions, embedding.embedding_endpoint)
    print("OpenAI key present:", bool(os.getenv("OPENAI_API_KEY")))

    problems = check_provider_config(llm, embedding)
    print("Preflight problems:", problems or "none")
    validate_provider_config(force=True)
    print("validate_provider_config(): PASSED")

    from cognee.infrastructure.databases.vector.embeddings.get_embedding_engine import (
        get_embedding_engine,
    )

    engine = get_embedding_engine()
    print("Engine  :", type(engine).__name__, "vector size:", engine.get_vector_size())

    vector = asyncio.run(engine.embed_text(["prefers sony headphones"]))
    print("embed_text dims:", len(vector[0]), "sample:", [round(v, 4) for v in vector[0][:3]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
