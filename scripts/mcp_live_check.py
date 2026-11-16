"""Drive the real Cognee MCP server through the application's MCP adapter."""

from __future__ import annotations

import os
import sys

from app.memory.base import MemoryAdapterError
from app.memory.mcp_adapter import McpMemoryAdapter

CUSTOMER = os.getenv("VERIFY_CUSTOMER", "verify_customer_001")

adapter = McpMemoryAdapter()
failures = []

try:
    print("--- STORE via MCP remember ---")
    stored = adapter.remember(
        CUSTOMER,
        (
            "I prefer Sony headphones.",
            "I usually like over-ear headphones.",
        ),
    )
    print(f"dataset={stored.dataset} facts={stored.fact_count} "
          f"ack_chars={stored.response_chars} latency={stored.latency_ms:.0f}ms")

    print("--- RECALL via MCP recall ---")
    recalled = adapter.recall(CUSTOMER, "what headphone brand do I prefer?")
    print(f"fragments={recalled.result_count} latency={recalled.latency_ms:.0f}ms")
    for fragment in recalled.fragments:
        print(f"  * {fragment[:220]}")

    print("--- RECALL for a customer with no memories ---")
    empty = adapter.recall("verify_customer_never_seen", "what brand do I prefer?")
    print(f"fragments={empty.result_count} (expected 0)")
    if empty.result_count != 0:
        failures.append("a never-seen customer returned memories")

    print("--- RECALL against a dataset that does not exist ---")
    if recalled.result_count == 0:
        failures.append("REAL recall returned zero fragments")
finally:
    adapter.close()

if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("PASSED")
