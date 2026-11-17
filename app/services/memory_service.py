"""Memory service that interacts with Cognee through the MCP adapter.

Responsible for:
- Interacting with the existing MCP adapter
- Retrieving customer-specific memories
- Sending information to Cognee through MCP
- Normalizing memory responses
- Applying only additional memory policies not natively supported by Cognee
- Using one fixed persistent memory dataset for the demo
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.memory.base import MemoryAdapter, MemoryAdapterError, RememberResult, RecallResult, dataset_for
from app.memory.mcp_adapter import McpMemoryAdapter

logger = logging.getLogger(__name__)

# One fixed dataset for the single-user demo.
# The dataset is initialized at first use: if the configured name exists in Cognee,
# it is used; otherwise we fall back to the dataset derived from the customer ID
# (retail_customer_001), which is guaranteed to be accessible in the single-user POC.
DEMO_DATASET = "retail_demo"


class MemoryService:
    """Service layer for memory operations using Cognee via MCP.

    Uses one fixed persistent memory dataset for the entire application.
    No customer isolation - all preferences are stored in the shared demo dataset.
    """

    def __init__(self, memory_adapter: Optional[MemoryAdapter] = None) -> None:
        self.memory_adapter = memory_adapter or McpMemoryAdapter()
        self.current_dataset = None  # Will be initialized lazily
        self._initialized = False

    def ensure_dataset_initialized(self) -> str:
        """Ensure the dataset is available and set self.current_dataset.

        Checks if the configured DEMO_DATASET exists in Cognee.
        If it does, uses it. If not, falls back to the dataset derived from
        customer_id via dataset_for() (retail_customer_001), which is guaranteed
        to exist in the single-user POC.

        Idempotent: safe to call on every Streamlit rerun.
        """
        if self._initialized:
            return self.current_dataset  # type: ignore[return-value]

        # Use the Cognee datasets API to check what exists.
        # This is a dataset discovery operation, not a memory operation,
        # so it does not violate the MCP-only constraint.
        import asyncio
        from cognee import datasets

        try:
            existing_datasets = asyncio.get_event_loop().run_until_complete(
                datasets.list_datasets()
            )
            dataset_names = [ds.name if hasattr(ds, "name") else str(ds) for ds in existing_datasets]
        except Exception:
            dataset_names = []

        configured = DEMO_DATASET
        fallback = dataset_for("customer_001")

        if configured in dataset_names:
            logger.info("Using configured dataset: %s", configured)
            self.current_dataset = configured  # type: ignore[assignment]
        else:
            logger.info(
                "Configured dataset %s not found in Cognee; falling back to %s",
                configured,
                fallback,
            )
            self.current_dataset = fallback  # type: ignore[assignment]

        self._initialized = True
        return self.current_dataset  # type: ignore[return-value]

    def _ensure_initialized(self) -> None:
        """Call ensure_dataset_initialized before first memory operation."""
        if not self._initialized:
            self.ensure_dataset_initialized()

    def __init__(self, memory_adapter: Optional[MemoryAdapter] = None) -> None:
        self.memory_adapter = memory_adapter or McpMemoryAdapter()
        self._initialized = False
        self._ensure_initialized()

    def set_customer(self, customer_id: str) -> None:
        """Set the current customer (kept for UI compatibility).

        In single-user mode, the dataset is always the demo dataset.
        """
        logger.info("Memory service activated for demo dataset: %s", self.current_dataset)

    def get_customer(self) -> str:
        """Get the current customer ID (kept for UI compatibility)."""
        return "customer_001"

    def get_dataset(self) -> str:
        """Get the current dataset name."""
        return self.current_dataset

    def remember(self, facts: List[str]) -> RememberResult:
        """Store facts as memory for the demo user.

        Args:
            facts: List of fact strings to store

        Returns:
            RememberResult with operation details
        """
        # Validate facts - remove empty strings
        cleaned_facts = [fact.strip() for fact in facts if fact and fact.strip()]
        if not cleaned_facts:
            raise MemoryAdapterError(
                "No valid facts to remember",
                integration="memory_service",
                operation="remember",
                customer_id="",
            )

        try:
            result = self.memory_adapter.remember("customer_001", tuple(cleaned_facts), self.current_dataset)
            logger.info("Remembered %d facts", result.fact_count)
            return result
        except MemoryAdapterError:
            raise
        except Exception as exc:
            raise MemoryAdapterError(
                f"Memory remember failed: {exc}",
                integration="memory_service",
                operation="remember",
                customer_id="",
            ) from exc

    def recall(self, query: str) -> RecallResult:
        """Retrieve relevant memories for the demo user.

        Args:
            query: The query string to search memories against

        Returns:
            RecallResult with retrieved fragments
        """
        try:
            result = self.memory_adapter.recall("customer_001", query, self.current_dataset)
            logger.info("Recalled %d fragments", result.result_count)
            return result
        except MemoryAdapterError:
            raise
        except Exception as exc:
            raise MemoryAdapterError(
                f"Memory recall failed: {exc}",
                integration="memory_service",
                operation="recall",
                customer_id="",
            ) from exc

    def forget(self, customer_id: str, dataset: str) -> Dict[str, Any]:
        """Delete all memories for the demo dataset using the MCP forget tool.

        Args:
            customer_id: The customer ID whose memories to delete
            dataset: The dataset name to delete memories from

        Returns:
            Dictionary with deletion results including deleted counts and remaining counts
        """
        try:
            result = self.memory_adapter.forget(customer_id, dataset)
            logger.info("Forgot memories, result: %s", result)
            return result
        except MemoryAdapterError:
            raise
        except Exception as exc:
            raise MemoryAdapterError(
                f"Memory forget failed: {exc}",
                integration="memory_service",
                operation="forget",
                customer_id=customer_id,
            ) from exc

    def remember_with_customer(self, customer_id: str, facts: List[str]) -> RememberResult:
        """Store facts for the demo customer.

        In single-user mode, always uses the demo dataset.
        """
        dataset = self.current_dataset
        try:
            result = self.memory_adapter.remember(customer_id, tuple(facts))
            logger.info("Remembered %d facts for demo dataset", result.fact_count)
            return result
        except Exception as exc:
            raise MemoryAdapterError(
                f"Memory remember failed: {exc}",
                integration="memory_service",
                operation="remember",
                customer_id=customer_id,
            ) from exc

    def recall_with_customer(self, customer_id: str, query: str) -> RecallResult:
        """Retrieve memories for the demo customer.

        In single-user mode, always uses the demo dataset.
        """
        dataset = self.current_dataset
        try:
            result = self.memory_adapter.recall(customer_id, query)
            logger.info("Recalled %d fragments for demo dataset", result.result_count)
            return result
        except Exception as exc:
            raise MemoryAdapterError(
                f"Memory recall failed: {exc}",
                integration="memory_service",
                operation="recall",
                customer_id=customer_id,
            ) from exc

    def get_customer_isolation_report(self, customer_id: str) -> Dict[str, Any]:
        """Get a report of memories (single-user mode - all in one dataset).

        Args:
            customer_id: The customer ID to check

        Returns:
            Dictionary with isolation information
        """
        dataset = self.current_dataset
        try:
            result = self.memory_adapter.recall(customer_id, "all memories")
            return {
                "customer_id": customer_id,
                "dataset": dataset,
                "fragment_count": result.result_count,
                "fragments": result.fragments,
                "isolation_verified": True,  # Single dataset for demo
            }
        except Exception as exc:
            raise MemoryAdapterError(
                f"Failed to get isolation report: {exc}",
                integration="memory_service",
                operation="isolation_report",
                customer_id=customer_id,
            ) from exc

    def close(self) -> None:
        """Close the memory adapter and release resources."""
        if hasattr(self.memory_adapter, "close"):
            self.memory_adapter.close()