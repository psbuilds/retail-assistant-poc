"""Unit tests for the genuine, persistent MCP adapter."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from mcp import types

from app.memory.base import MemoryOperationError
from app.memory.mcp_adapter import McpMemoryAdapter


def _result(*texts, is_error=False, meta=None):
    return SimpleNamespace(
        content=[types.TextContent(type="text", text=text) for text in texts],
        isError=is_error,
        meta=meta,
    )


def _recall_payload(*memories, summary="2 memories found (retail_customer_001)"):
    """Render a recall payload the way the Cognee MCP server does."""
    if not memories:
        return _result("no matching memories")
    body = "\n\n".join(f"[graph] {memory}" for memory in memories)
    return _result(f"{summary}\n{body}")


class FakeConnection:
    def __init__(self, url, timeout):
        self.url = url
        self.timeout = timeout
        self.calls = []
        self.closed = False

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "remember":
            return _result(
                "Stored permanently in knowledge graph (dataset=retail_customer_001, status=completed)."
            )
        return _recall_payload("Prefers affordable wireless headphones.")

    def close(self):
        self.closed = True


def test_endpoint_is_explicit_and_session_is_reused():
    with patch("app.memory.mcp_adapter._PersistentMcpSession", FakeConnection):
        adapter = McpMemoryAdapter(mcp_url="http://localhost:8001/mcp")
        adapter.remember("customer_001", ("Fact A", "Fact B"))
        first = adapter._connection
        result = adapter.recall("customer_001", "headphones")
        adapter.recall("customer_001", "travel")

        assert adapter.mcp_url == "http://localhost:8001/mcp"
        assert adapter._connection is first
        assert [call[0] for call in first.calls] == ["remember", "recall", "recall"]
        assert first.calls[0][1] == {
            "data": "Fact A\nFact B",
            "dataset_name": "retail_customer_001",
            "background": False,
            "self_improvement": True,
        }
        # The MCP recall tool declares ``datasets: str | None`` and splits it on
        # commas, so a dataset name must be sent as a string, not a list.
        assert first.calls[1][1] == {
            "query": "headphones",
            "datasets": "retail_customer_001",
            "search_type": "CHUNKS",
            "top_k": 10,
        }
        assert result.fragments == ("Prefers affordable wireless headphones.",)
        adapter.close()
        assert first.closed


def test_environment_endpoint_is_used(monkeypatch):
    monkeypatch.setenv("COGNEE_MCP_URL", "http://example.test/mcp")
    assert McpMemoryAdapter().mcp_url == "http://example.test/mcp"


def test_context_manager_closes_persistent_session():
    with patch("app.memory.mcp_adapter._PersistentMcpSession", FakeConnection):
        with McpMemoryAdapter() as adapter:
            adapter.recall("customer_001", "query")
            connection = adapter._connection
        assert connection.closed


def test_recall_strips_protocol_summary_from_fragments():
    result = _recall_payload(
        "Prefers Sony headphones.",
        "Usually likes over-ear headphones.",
    )
    connection = MagicMock()
    connection.call_tool.return_value = result
    with patch("app.memory.mcp_adapter._PersistentMcpSession", return_value=connection):
        recall = McpMemoryAdapter().recall("customer_001", "brand")

    # The summary line is protocol chatter; handing it to the assistant would
    # make it answer questions with "2 memories found".
    assert recall.fragments == (
        "Prefers Sony headphones.",
        "Usually likes over-ear headphones.",
    )


def test_recall_without_matches_is_an_empty_success():
    connection = MagicMock()
    connection.call_tool.return_value = _recall_payload()
    with patch("app.memory.mcp_adapter._PersistentMcpSession", return_value=connection):
        recall = McpMemoryAdapter().recall("customer_001", "brand")

    assert recall.fragments == ()
    assert recall.result_count == 0


def test_recall_tool_error_is_not_reported_as_empty_memory():
    connection = MagicMock()
    connection.call_tool.return_value = _result(
        "Error: Recall failed: [Errno 61] Connection refused"
    )
    with patch("app.memory.mcp_adapter._PersistentMcpSession", return_value=connection):
        with pytest.raises(MemoryOperationError) as excinfo:
            McpMemoryAdapter().recall("customer_001", "brand")

    assert excinfo.value.operation == "recall"
    assert "Connection refused" in str(excinfo.value)


def test_recall_error_flag_is_not_reported_as_empty_memory():
    connection = MagicMock()
    connection.call_tool.return_value = _result("index unavailable", is_error=True)
    with patch("app.memory.mcp_adapter._PersistentMcpSession", return_value=connection):
        with pytest.raises(MemoryOperationError):
            McpMemoryAdapter().recall("customer_001", "brand")


def test_remember_tool_error_is_not_reported_as_success():
    connection = MagicMock()
    connection.call_tool.return_value = _result(
        "Error: Remember failed: embeddings provider misconfigured"
    )
    with patch("app.memory.mcp_adapter._PersistentMcpSession", return_value=connection):
        with pytest.raises(MemoryOperationError) as excinfo:
            McpMemoryAdapter().remember("customer_001", ("Prefers Sony.",))

    assert excinfo.value.operation == "remember"


def test_tool_errors_are_wrapped_and_chained():
    connection = MagicMock()
    cause = RuntimeError("server rejected tool call")
    connection.call_tool.side_effect = cause
    with patch("app.memory.mcp_adapter._PersistentMcpSession", return_value=connection):
        with pytest.raises(MemoryOperationError) as excinfo:
            McpMemoryAdapter().recall("customer_001", "query")

    assert excinfo.value.integration == "mcp"
    assert excinfo.value.operation == "recall"
    assert excinfo.value.customer_id == "customer_001"
    assert excinfo.value.__cause__ is cause


def test_session_initialization_failure_is_wrapped():
    cause = RuntimeError("handshake failed")
    with patch(
        "app.memory.mcp_adapter._PersistentMcpSession", side_effect=cause
    ), pytest.raises(MemoryOperationError) as excinfo:
        McpMemoryAdapter().recall("customer_001", "query")

    assert excinfo.value.operation == "initialize"
    assert excinfo.value.__cause__ is cause


def test_cancelled_session_initialization_is_wrapped():
    with patch(
        "app.memory.mcp_adapter._PersistentMcpSession",
        side_effect=asyncio.CancelledError(),
    ), pytest.raises(MemoryOperationError) as excinfo:
        McpMemoryAdapter().recall("customer_001", "query")

    assert excinfo.value.operation == "initialize"
    assert isinstance(excinfo.value.__cause__, asyncio.CancelledError)
