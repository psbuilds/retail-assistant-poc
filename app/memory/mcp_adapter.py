"""Persistent MCP client adapter for the Cognee memory server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import threading
from concurrent.futures import Future
from typing import Any

from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from app.memory.base import (
    MemoryAdapter,
    MemoryAdapterError,
    MemoryConfigurationError,
    MemoryOperationError,
)

logger = logging.getLogger(__name__)

DEFAULT_MCP_URL = "http://127.0.0.1:8101/mcp"
DEFAULT_QUERY_TYPE = "CHUNKS"
DEFAULT_TOP_K = 10
DEFAULT_TIMEOUT_SECONDS = 180.0

#: The Cognee MCP server reports tool failures as a normal text result
#: (``"Error: Recall failed: ..."``) instead of an MCP protocol error, so the
#: payload has to be inspected or a failure looks like an empty memory.
TOOL_ERROR_PREFIX = "Error:"

#: First line of the server's recall payload. It is a protocol summary, not a
#: stored memory, so it must not be handed to the assistant as one.
_RECALL_SUMMARY_PATTERN = re.compile(
    r"^(?:\d+ memor(?:y|ies) found\b.*"
    r"|no matching memories"
    r"|still indexing\b.*"
    r"|memory indexing failed\b.*)$"
)

#: ``format_recall_body`` tags each hit with the source it came from.
_RECALL_SOURCE_PREFIX_PATTERN = re.compile(r"^\[[a-z_]+\]\s*")


class _PersistentMcpSession:
    """Own an async MCP transport/session on a private event-loop thread."""

    def __init__(self, url: str, timeout_seconds: float) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="mcp-session", daemon=True)
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._commands: asyncio.Queue[Any] | None = None
        self._stopped: Future[None] | None = None
        self._thread.start()
        self._ready.wait()
        if self._startup_error is not None:
            self.close()
            raise self._startup_error

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._serve())
        self._loop.run_forever()
        self._loop.close()

    async def _serve(self) -> None:
        try:
            stack = contextlib.AsyncExitStack()
            await stack.__aenter__()
            read_stream, write_stream, *_ = await stack.enter_async_context(
                streamable_http_client(self.url)
            )
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            missing = {"remember", "recall"} - names
            if missing:
                raise RuntimeError(f"MCP server is missing required tools: {sorted(missing)}")
            self._stack = stack
            self._session = session
            self._commands = asyncio.Queue()
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            return

        self._ready.set()
        try:
            while True:
                command = await self._commands.get()
                if command is None:
                    break
                name, arguments, result = command
                try:
                    value = await self._session.call_tool(name, arguments)
                except BaseException as exc:
                    result.set_exception(exc)
                else:
                    result.set_result(value)
        finally:
            await stack.aclose()
            self._loop.call_soon(self._loop.stop)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._session is None or self._commands is None:
            raise RuntimeError("MCP session is not initialized")
        result: Future[Any] = Future()
        self._loop.call_soon_threadsafe(self._commands.put_nowait, (name, arguments, result))
        return result.result(timeout=self.timeout_seconds)

    def _submit(self, coroutine: Any) -> Any:
        future: Future[Any] = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=self.timeout_seconds)

    def close(self) -> None:
        if not self._thread.is_alive():
            return
        if self._commands is not None:
            self._loop.call_soon_threadsafe(self._commands.put_nowait, None)
        else:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=self.timeout_seconds)


class McpMemoryAdapter(MemoryAdapter):
    """Use one MCP protocol session for all memory operations on this adapter."""

    integration = "mcp"

    def __init__(
        self,
        *,
        mcp_url: str | None = None,
        recall_query_type: str | None = None,
        top_k: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.mcp_url = (mcp_url or os.getenv("COGNEE_MCP_URL") or DEFAULT_MCP_URL).rstrip("/")
        self.recall_query_type = recall_query_type or os.getenv(
            "COGNEE_RECALL_QUERY_TYPE", DEFAULT_QUERY_TYPE
        )
        self.top_k = top_k or DEFAULT_TOP_K
        self.timeout_seconds = timeout_seconds or DEFAULT_TIMEOUT_SECONDS
        self._connection: _PersistentMcpSession | None = None
        self._closed = False

    def _get_connection(self) -> _PersistentMcpSession:
        if self._closed:
            raise MemoryConfigurationError(
                "MCP adapter is closed", integration=self.integration, operation="configure"
            )
        # If we have a cached connection, validate it is still alive.
        # Streamlit reruns can cancel daemon threads, so we need to check.
        if self._connection is not None:
            # Check if the thread is still alive; if not, reset and recreate.
            # Use getattr for compatibility with test mocks that don't have _thread
            thread = getattr(self._connection, "_thread", None)
            if thread is not None and not thread.is_alive():
                logger.warning(
                    "Cached MCP session thread is dead. Resetting connection."
                )
                self._connection = None
            # else: thread is alive (or mock doesn't have _thread) - reuse the session

        # If _connection was reset to None (or never set), create a new session
        if self._connection is None:
            try:
                self._connection = _PersistentMcpSession(self.mcp_url, self.timeout_seconds)
            except asyncio.CancelledError as exc:
                # Initialization was cancelled (e.g., during Streamlit rerun).
                # Reset _connection so the next attempt can retry cleanly.
                self._connection = None
                raise MemoryOperationError(
                    f"MCP session initialization was cancelled: {exc}",
                    integration=self.integration,
                    operation="initialize",
                ) from exc
            except Exception as exc:
                # Initialization failed; reset _connection so the next attempt can retry.
                # Transient failures (e.g., during Streamlit startup) should not permanently
                # disable the adapter.
                self._connection = None
                raise MemoryOperationError(
                    f"MCP session initialization failed: {exc}",
                    integration=self.integration,
                    operation="initialize",
                ) from exc
        return self._connection

    def _remember(self, customer_id: str, facts: tuple[str, ...], dataset: str) -> int:
        result = self._call(
            "remember",
            {
                "data": "\n".join(facts),
                "dataset_name": dataset,
                "background": False,
                "self_improvement": True,
            },
            customer_id,
        )
        return len(_text_from_result(result))

    def _recall(self, customer_id: str, query: str, dataset: str) -> tuple[str, ...]:
        result = self._call(
            "recall",
            {
                "query": query,
                # The MCP tool takes a comma-separated string, not the list the
                # REST API accepts. One customer maps to exactly one dataset.
                "datasets": dataset,
                "search_type": self.recall_query_type,
                "top_k": self.top_k,
            },
            customer_id,
        )
        return _fragments_from_result(result)

    def _call(self, name: str, arguments: dict[str, Any], customer_id: str) -> Any:
        try:
            result = self._get_connection().call_tool(name, arguments)
        except MemoryAdapterError:
            raise
        except Exception as exc:
            raise MemoryOperationError(
                f"MCP {name} tool call failed: {exc}",
                integration=self.integration,
                operation=name,
                customer_id=customer_id,
            ) from exc
        _raise_on_tool_error(
            result, integration=self.integration, operation=name, customer_id=customer_id
        )
        return result

    def forget(self, customer_id: str, dataset: str) -> int:
        """Delete all memories for a customer dataset using the MCP forget tool.

        Args:
            customer_id: The customer ID whose memories to delete
            dataset: The dataset name to delete memories from

        Returns:
            Result count from the forget operation
        """
        try:
            result = self._call(
                "forget",
                {
                    "customer_id": customer_id,
                    "dataset_name": dataset,
                },
                customer_id,
            )
            # _call returns the raw result; extract count from text if possible
            text = _text_from_result(result).strip()
            # Try to extract a number from the result text
            import re
            match = re.search(r"(\d+)", text)
            if match:
                return int(match.group(1))
            # If no number, return 1 if we got a success signal
            if "success" in text.lower() or "deleted" in text.lower():
                return 1
            return 0
        except Exception as exc:
            raise MemoryAdapterError(
                f"MCP forget failed: {exc}",
                integration=self.integration,
                operation="forget",
                customer_id=customer_id,
            ) from exc

    def close(self) -> None:
        connection, self._connection = self._connection, None
        self._closed = True
        if connection is not None:
            connection.close()


def _text_from_result(result: Any) -> str:
    return "\n".join(
        content.text
        for content in getattr(result, "content", ())
        if isinstance(content, types.TextContent) and content.text
    )


def _raise_on_tool_error(
    result: Any, *, integration: str, operation: str, customer_id: str
) -> None:
    """Turn a tool-level failure into a :class:`MemoryOperationError`.

    Cognee's MCP tools answer failures with a text result saying "Error: ..." or
    with ``isError`` rather than a protocol error. Without this check a failed
    recall would be reported to the user as "you have no preferences", which is
    the one distinction the assistant must not lose.
    """
    if getattr(result, "isError", False):
        detail = _text_from_result(result).strip() or "tool returned an error result"
        raise MemoryOperationError(
            f"MCP {operation} tool reported an error: {detail}",
            integration=integration,
            operation=operation,
            customer_id=customer_id,
        )

    text = _text_from_result(result).strip()
    first_line = text.splitlines()[0].strip() if text else ""
    if first_line.startswith(TOOL_ERROR_PREFIX):
        raise MemoryOperationError(
            f"MCP {operation} tool reported an error: {text}",
            integration=integration,
            operation=operation,
            customer_id=customer_id,
        )


def _fragments_from_result(result: Any) -> tuple[str, ...]:
    """Extract the stored memories from a Cognee MCP recall payload.

    The server renders one text block: a summary line (``"3 memories found ..."``,
    ``"no matching memories"``), then one block per memory separated by blank
    lines, each optionally prefixed with its ``[source]`` tag.
    """
    lines = _text_from_result(result).strip().splitlines()
    if lines and _RECALL_SUMMARY_PATTERN.match(lines[0].strip()):
        lines = lines[1:]

    fragments: list[str] = []
    for block in "\n".join(lines).split("\n\n"):
        text = _RECALL_SOURCE_PREFIX_PATTERN.sub("", block.strip()).strip()
        if text and not _is_system_marker(text):
            fragments.append(text)
    return tuple(dict.fromkeys(fragments))


def _is_system_marker(text: str) -> bool:
    lowered = text.lower()
    return lowered.startswith(("memory is still", "cognee", "knowledge graph"))
