"""Constraint Resolver service responsible for combining memory, conversation context, and current requirements.

The resolver applies the following precedence:
Current explicit user instructions
    > Current conversation context
    > Long-term customer preferences
    > System defaults

This ensures that explicit current requests always override stored preferences, while
still benefiting from persistent preferences when the user doesn't contradict them.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.memory.base import MemoryAdapter, MemoryAdapterError
from app.services.memory_service import MemoryService

logger = logging.getLogger(__name__)


class ConstraintResolutionError(MemoryAdapterError):
    """Raised when constraint resolution fails."""

    pass


class ConstraintResolver:
    """Resolves shopping constraints by combining memory, conversation context, and current requirements.

    Precedence (highest to lowest):
    1. Current explicit user instructions (from current message)
    2. Current conversation context (previous turn requirements)
    3. Long-term customer preferences (from Cognee MCP memory)
    4. System defaults (category-appropriate feature sets)
    """

    def __init__(self, memory_service: MemoryService) -> None:
        self.memory_service = memory_service

    def resolve_constraints(
        self,
        customer_id: str,
        current_query: str,
        conversation_context: Optional[List[Dict[str, str]]] = None,
        intent_hints: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Resolve the full set of shopping constraints for a customer query.

        Args:
            customer_id: The customer identity for memory retrieval
            current_query: The user's current message
            conversation_context: Previous conversation turns for reference
            intent_hints: Structured intent from ``extract_intent``. Used only to
                fill gaps the current message did not state explicitly, so it
                sits at the bottom of the current-request layer and can never
                override an explicit user signal.

        Returns:
            Dictionary of resolved constraints suitable for product search
        """
        # Step 1: Extract preferences from the current query (highest precedence)
        current_preferences = self._extract_query_preferences(current_query)

        # Step 1b: Let the structured intent fill in what the query left unsaid
        if intent_hints:
            current_preferences = self._fill_from_intent(
                current_preferences, intent_hints
            )

        # Step 2: Retrieve relevant long-term memories from Cognee MCP
        memory_preferences: Dict[str, Any] = {}
        try:
            recall_result = self.memory_service.recall(current_query)
            if recall_result.fragments:
                # Extract preferences only from relevant memory fragments
                memory_preferences = self._extract_memory_preferences(
                    recall_result.fragments, current_query
                )
        except MemoryAdapterError as exc:
            logger.warning("Memory recall failed, using current query only: %s", exc)
            # Continue with current query preferences only - graceful degradation

        # Step 3: Extract preferences from conversation context
        context_preferences: Dict[str, Any] = {}
        if conversation_context:
            context_preferences = self._extract_context_preferences(conversation_context)

        # Step 4: Merge with proper precedence
        # Memory first, then context enhances, but current query always wins
        # Precedence: current > context > memory
        resolved = self._apply_precedence(
            current_preferences, context_preferences, memory_preferences
        )

        # Step 5: Apply category-specific feature filtering
        resolved = self._apply_category_filtering(resolved)

        logger.info(
            "Constraints resolved for customer %s: %s",
            customer_id,
            {k: v for k, v in resolved.items() if v is not None},
        )

        return resolved

    @staticmethod
    def _fill_from_intent(
        query_preferences: Dict[str, Any], intent: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Add intent fields the lexical pass did not find. Never overwrites."""
        merged = dict(query_preferences)

        category = intent.get("category")
        if category and not merged.get("category"):
            merged["category"] = category

        budget = intent.get("budget") or {}
        if isinstance(budget, dict):
            max_budget = budget.get("max")
            if max_budget is not None and not merged.get("max_budget"):
                merged["max_budget"] = max_budget

        brand = intent.get("brand")
        if brand and not merged.get("preferred_brand"):
            merged["preferred_brand"] = brand

        required = intent.get("required_features") or []
        if required and not merged.get("required_features"):
            merged["required_features"] = tuple(required)

        excluded = intent.get("excluded_features") or []
        if excluded and not merged.get("excluded_features"):
            merged["excluded_features"] = tuple(excluded)

        for boolean_key in ("wireless", "noise_cancellation"):
            if isinstance(intent.get(boolean_key), bool) and merged.get(boolean_key) is None:
                merged[boolean_key] = intent[boolean_key]

        return merged

    def _extract_query_preferences(self, query: str) -> Dict[str, Any]:
        """Extract preferences from the current user query using deterministic extraction.

        This is the primary extraction method - the "explicit user instructions" layer.
        """
        from app.chatbot import extract_preferences
        return extract_preferences(query)

    def _extract_memory_preferences(
        self, fragments: Tuple[str, ...], query: str
    ) -> Dict[str, Any]:
        """Extract preferences from retrieved Cognee memories, scoped to the current query category.

        Only memories relevant to the current shopping category are considered.
        """
        from app.chatbot import extract_preferences, relevant_memory_fragments

        # Determine the category from the query
        query_prefs = self._extract_query_preferences(query)
        category = query_prefs.get("category")

        # If query has a category, filter memories to that category
        if category:
            relevant = relevant_memory_fragments(fragments, query_prefs)
            memory_text = "\n".join(relevant) if relevant else ""
        else:
            memory_text = "\n".join(fragments) if fragments else ""

        # Extract preferences from relevant memories
        return extract_preferences(memory_text) if memory_text else {}

    def _extract_context_preferences(
        self, conversation_context: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        """Extract preferences from conversation context (previous turns).

        This handles conversational refinements like "Actually, make it 3000"
        or "I don't need ANC this time."
        """
        prefs: Dict[str, Any] = {}

        # Look at the most recent turns for refinements
        # Process in reverse order (most recent first)
        for turn in reversed(conversation_context[-4:]):  # Last 4 turns
            msg = turn.get("content", "")
            if not msg:
                continue

            # Check for budget updates
            budget_match = (
                r"(?:increase|change|set|update)\s+(?:budget|max)\s+(?:to|as|up to)?\s*[₹$]?\s*([\d,]+)"
            )
            match = __import__("re").search(budget_match, msg.casefold())
            if match:
                prefs["max_budget"] = int(match.group(1).replace(",", ""))

            # Check for feature removals
            if any(
                term in msg.casefold()
                for term in ["without", "no need", "don't need", "not this time"]
            ):
                # Mark features to be removed in the merge step
                prefs["_remove_features"] = prefs.get("_remove_features", [])
                if "anc" in msg.casefold() or "noise cancellation" in msg.casefold():
                    prefs["_remove_features"].append("noise_cancellation")
                if "wireless" in msg.casefold():
                    prefs["_remove_features"].append("wireless")

        return prefs

    def _apply_precedence(
        self,
        current: Dict[str, Any],
        context: Dict[str, Any],
        memory: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Apply the precedence hierarchy: current > context > memory.

        Rules:
        - Current explicit preferences always win
        - Context preferences enhance but don't override current
        - Memory preferences are the baseline, unless contradicted
        - Explicit "remove" signals from context override memory
        """
        merged: Dict[str, Any] = {}

        # Start with memory preferences (base layer)
        merged.update(memory)

        # Apply context preferences (enhance memory, but don't contradict current)
        merged = self._merge_context_into(merged, context)

        # Current query always overrides everything
        merged.update(current)

        # Handle explicit removal requests from context
        self._apply_removals(merged, context)

        # Clean up None values and normalize
        cleaned = self._normalize_constraints(merged)

        return cleaned

    def _merge_context_into(
        self, base: Dict[str, Any], context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Merge context preferences into the base, respecting existing values.

        Context enhances memory but won't override explicit current preferences.
        """
        result = dict(base)

        for key, value in context.items():
            if key.startswith("_"):  # Skip internal flags
                continue
            if key not in result:
                # Only add if not already set by memory or current
                result[key] = value
            elif result[key] is None:
                result[key] = value

        return result

    def _apply_removals(self, merged: Dict[str, Any], context: Dict[str, Any]) -> None:
        """Apply explicit feature removal requests from context.

        Example: User says "I don't need ANC this time" even if memory says "Prefers ANC"
        """
        remove_keys = context.get("_remove_features", [])
        for key in remove_keys:
            if key not in merged:
                continue
            # "I don't need ANC this time" is an explicit negative requirement:
            # keep the key with a False value so the Product API excludes those
            # products. Dropping the key would let the stored preference win,
            # which is exactly what the user asked to override.
            if key in ("noise_cancellation", "wireless"):
                merged[key] = False
            else:
                merged.pop(key, None)

    def _apply_category_filtering(self, constraints: Dict[str, Any]) -> Dict[str, Any]:
        """Apply category-specific feature filtering.

        Ensures that feature filters are appropriate for the product category.
        E.g., noise_cancellation only makes sense for headphones, not footwear.
        """
        category = constraints.get("category")

        # Category-specific feature rules
        if category and category != "headphones":
            # For non-headphone categories, remove headphone-specific preferences
            constraints.pop("wireless", None)
            constraints.pop("noise_cancellation", None)

        return constraints

    def _normalize_constraints(self, constraints: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize the resolved constraints, removing None values and normalizing types.

        Ensures the output is clean and ready for product API consumption.
        """
        cleaned: Dict[str, Any] = {}

        for key, value in constraints.items():
            if value is None:
                continue
            if isinstance(value, bool):
                cleaned[key] = value
            elif isinstance(value, (int, float)):
                cleaned[key] = value
            elif isinstance(value, str) and value.strip():
                cleaned[key] = value.strip()
            elif isinstance(value, tuple) and all(
                v is not None for v in value
            ):
                cleaned[key] = value
            elif isinstance(value, list) and value:
                cleaned[key] = value

        return cleaned