"""Conversation service responsible for managing the conversational shopping flow.

Updated pipeline:
User Message → Intent Extraction → Intent Routing → (STORE / RECALL / NORMAL_RESPONSE / PRODUCT_DISCOVERY) → Response Generation → Memory Update (if appropriate) → Conversation History Update

Only PRODUCT_DISCOVERY invokes the Product API and shows product cards.
STORE, RECALL, and NORMAL_RESPONSE never call the Product API.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.api.product_api import app as product_api_app
from app.chatbot import extract_preferences
from app.llm import LLMStatus, get_llm_status
from app.services.constraint_resolver import ConstraintResolver
from app.services.intent_extraction import extract_intent
from app.services.memory_service import MemoryService
from app.services.response_generation import generate_response

logger = logging.getLogger(__name__)

# Product API base URL (relative to the running service)
PRODUCT_API_BASE = "http://127.0.0.1:8000"

#: Feature tags the Product API understands for the boolean columns.
_WIRELESS_FEATURE = "wireless"
_ANC_FEATURE = "noise-cancelling"


def normalize_constraints(constraints: Dict[str, Any]) -> Dict[str, Any]:
    """Convert resolver-style constraints into the API/prompt shape.

    The resolver emits flat lexical keys (``max_budget``, ``preferred_brand``)
    while the Product API takes ``max_price``/``brand`` and the response prompt
    expects a ``budget`` object. It also expresses wireless/ANC as booleans,
    which the API only understands as feature tags. Doing that translation in
    one place keeps "without ANC" enforceable end to end.
    """
    normalized: Dict[str, Any] = {
        "category": constraints.get("category") or "",
        "brand": constraints.get("brand") or constraints.get("preferred_brand") or "",
        "required_features": [
            str(feature) for feature in (constraints.get("required_features") or [])
        ],
        "excluded_features": [
            str(feature) for feature in (constraints.get("excluded_features") or [])
        ],
    }

    budget: Dict[str, Any] = {}
    nested_budget = constraints.get("budget")
    if isinstance(nested_budget, dict):
        for key in ("min", "max"):
            if nested_budget.get(key) is not None:
                budget[key] = nested_budget[key]
    if constraints.get("max_budget") is not None:
        budget.setdefault("max", constraints["max_budget"])
    if budget:
        budget.setdefault("currency", "INR")
        normalized["budget"] = budget

    sort_preference = constraints.get("sort_preference")
    if sort_preference:
        normalized["sort_preference"] = sort_preference

    # Booleans -> feature tags, reconciling any pre-existing tag.
    required = normalized["required_features"]
    excluded = normalized["excluded_features"]
    for boolean_key, feature in (
        ("wireless", _WIRELESS_FEATURE),
        ("noise_cancellation", _ANC_FEATURE),
    ):
        value = constraints.get(boolean_key)
        if value is None:
            continue
        normalized[boolean_key] = value
        if value:
            excluded = [tag for tag in excluded if tag.casefold() != feature]
            if feature not in required:
                required.append(feature)
        else:
            required = [tag for tag in required if tag.casefold() != feature]
            if feature not in excluded:
                excluded.append(feature)
    normalized["required_features"] = required
    normalized["excluded_features"] = excluded

    return normalized


class ConversationService:
    """Service that manages conversational flow for the retail shopping assistant.

    Pipeline:
    1. Intent Extraction: Structured intent from user message
    2. Intent Routing: Determine STORE / RECALL / NORMAL_RESPONSE / PRODUCT_DISCOVERY
    3. (STORE / RECALL / NORMAL_RESPONSE): No Product API call, no product cards
    4. (PRODUCT_DISCOVERY): Call Product API with resolved constraints, show product cards
    5. Memory Update: Store appropriate preferences (persistent only)
    6. Conversation History Update
    """

    def __init__(
        self,
        memory_service: Optional[MemoryService] = None,
        *,
        use_llm: bool = True,
    ) -> None:
        self.memory_service = memory_service or MemoryService()
        self.conversation_history: List[Dict[str, str]] = []
        self.use_llm = use_llm
        self._product_api_base = PRODUCT_API_BASE

        logger.info(
            "ConversationService initialized - LLM status: %s",
            self.get_llm_status().summary(),
        )

    def get_llm_status(self, *, check_provider: bool = True) -> LLMStatus:
        """Report the LLM state the UI should display.

        When ``use_llm`` is disabled the status is forced to deterministic so
        the UI cannot claim the model is active. Always secret-free.
        """
        if not self.use_llm:
            return LLMStatus(detail="LLM disabled for this conversation service.")
        return get_llm_status(check_provider=check_provider)

    def set_customer(self, customer_id: str) -> None:
        """Set the current customer for the conversation.

        Args:
            customer_id: The customer ID to set (e.g., 'customer_001')
        """
        self.memory_service.set_customer(customer_id)
        logger.info("Conversation service set for customer %s", customer_id)

    def process_query(self, query: str) -> Dict[str, Any]:
        """Process a user query through the full conversation pipeline.

        The workflow is:
        User Message → Intent Extraction → Intent Routing →
        (STORE / RECALL / NORMAL_RESPONSE / PRODUCT_DISCOVERY) →
        Response Generation → Memory Update (if appropriate) → Conversation History Update

        Only PRODUCT_DISCOVERY invokes the Product API and shows product cards.
        STORE, RECALL, and NORMAL_RESPONSE never call the Product API.

        Args:
            query: The user's query string

        Returns:
            Dictionary with response information including:
                - response: The assistant's response text
                - recommended_products: List of recommended product dicts (PRODUCT_DISCOVERY only)
                - memories: Retrieved memory fragments
                - preferences: Final resolved preferences
                - success: Whether the operation succeeded
                - error: Error message if failed
                - llm_used: Whether LLM was used for response generation
                - response_mode: STORE / RECALL / NORMAL_RESPONSE / PRODUCT_DISCOVERY
                - intent_source: Structured intent extraction source
        """
        customer_id = self.memory_service.get_customer()

        if not customer_id:
            return {
                "response": "I'm having trouble accessing your customer identity. Please try again.",
                "recommended_products": [],
                "memories": [],
                "preferences": {},
                "success": False,
                "error": "No customer selected",
                "llm_used": False,
                "response_mode": "deterministic",
            }

        try:
            # Step 1: Structured intent extraction from the user query
            # NOTE: Do NOT pass customer_preferences here - that triggers recall of ALL
            # stored preferences which then get applied to product search unintentionally.
            # Only pass conversation_history for context-dependent intent detection.
            intent = extract_intent(
                query,
                conversation_history=self.conversation_history,
                use_llm=self.use_llm,
            )

            # Step 2: Determine the intent mode and route accordingly
            intent_mode = self._determine_intent_mode(intent, query)

            # Step 3: Route based on intent mode
            if intent_mode == "STORE":
                # Store preference only - no Product API call, no product cards
                self._store_preference_only(query, intent)
                # Generate a deterministic acknowledgement response
                response_result = self._generate_store_response(query, intent)

            elif intent_mode == "RECALL":
                # Recall only what's relevant - no Product API call, no product cards
                response_result = self._generate_recall_response(query, intent)

            elif intent_mode == "NORMAL_RESPONSE":
                # Ordinary conversation - no Product API call, no product cards
                # Do NOT call _get_customer_preferences here (avoids triggering recall)
                # Do NOT call resolver.resolve_constraints (avoids applying remembered prefs to product search)
                response_result = self._generate_normal_response(query, intent)

            else:  # PRODUCT_DISCOVERY
                # Only invoke Product API for explicit product requests
                # Step 3: Resolve constraints with proper precedence
                resolver = ConstraintResolver(self.memory_service)
                resolved_constraints = resolver.resolve_constraints(
                    customer_id=customer_id,
                    current_query=query,
                    conversation_context=self.conversation_history,
                    intent_hints=intent,
                )

                # Step 4: Normalize, then call the Product API with those constraints
                constraints = normalize_constraints(resolved_constraints)
                api_products = self._call_product_api(constraints)

                # Step 5: Generate natural language response using LLM (or deterministic fallback)
                response_result = generate_response(
                    original_query=query,
                    resolved_constraints=constraints,
                    api_products=api_products,
                    conversation_context=self.conversation_history,
                )

                # Step 6: Update memory with persistent preferences (if appropriate)
                # For PRODUCT_DISCOVERY, we still store explicit preferences
                self._update_memory_if_appropriate(query, resolved_constraints)

            # Step 7: Update conversation history
            self.conversation_history.append({"role": "user", "content": query})

            # For all modes, add the assistant response
            self.conversation_history.append(
                {"role": "assistant", "content": response_result["message"]}
            )

            # Keep history manageable
            if len(self.conversation_history) > 20:
                self.conversation_history = self.conversation_history[-20:]

            # Build the return dict based on intent mode
            if intent_mode == "PRODUCT_DISCOVERY":
                # Use the product IDs from generate_response result
                recommended_products = response_result.get(
                    "recommended_product_ids", []
                )
            else:
                # For STORE/RECALL/NORMAL_RESPONSE, no products
                recommended_products = []

            logger.info("Query processed successfully for customer %s, mode: %s", customer_id, intent_mode)

            return {
                "response": response_result["message"],
                "recommended_products": recommended_products,
                "memories": self._get_relevant_memories(query),
                "preferences": {},
                "success": True,
                "error": None,
                "llm_used": bool(response_result.get("llm_used", False)),
                "response_mode": intent_mode,
                "intent_source": intent.get("source", "deterministic"),
                "follow_up_question": response_result.get(
                    "follow_up_question", "How can I help you further?"
                ),
            }

        except Exception as exc:
            logger.error(
                "Unexpected error during conversation: %s", exc, exc_info=True
            )
            return {
                "response": "I encountered an error while processing your request. Please try again.",
                "recommended_products": [],
                "memories": [],
                "preferences": {},
                "success": False,
                "error": str(exc),
                "llm_used": False,
                "response_mode": "deterministic",
            }

    # -----------------------------------------------------------------------
    # STORE intent handlers
    # -----------------------------------------------------------------------

    def _store_preference_only(self, query: str, intent: Dict[str, Any]) -> None:
        """Extract preferences from the query and store them as persistent memories.

        Rules:
        - Only extract what the user explicitly stated
        - Call self.memory_service.remember_with_customer() to persist
        - Do NOT call resolver.resolve_constraints (avoids applying to product search)
        - Do NOT call _call_product_api
        - Do NOT call generate_response with api_products
        """
        customer_id = self.memory_service.get_customer()
        if not customer_id:
            return

        # Extract preferences from the query using the deterministic extractor
        query_prefs = extract_preferences(query)

        facts_to_store: List[str] = []

        # Category preferences are persistent (if it's a real category, not default headphones)
        category = query_prefs.get("category")
        valid_categories = (
            "headphones",
            "earbuds",
            "audio",
            "professional",
            "footwear",
            "apparel",
            "bags",
            "smartphones",
            "laptops",
            "smartwatches",
        )
        if category and category != "headphones" and category in valid_categories:
            facts_to_store.append(f"Prefers {category} shopping")

        # Budget preferences are persistent (if meaningful > ₹500)
        budget = query_prefs.get("budget", {})
        max_budget = budget.get("max") if isinstance(budget, dict) else None
        if max_budget is not None and max_budget > 500:
            facts_to_store.append(f"Has a budget up to ₹{max_budget}")

        # Wireless preference
        if query_prefs.get("wireless") is not None:
            if query_prefs["wireless"]:
                facts_to_store.append("Prefers wireless headphones")

        # Noise cancellation preference
        if query_prefs.get("noise_cancellation"):
            facts_to_store.append("Prefers noise-cancelling headphones")

        # Brand preference
        brand = query_prefs.get("brand")
        if brand:
            facts_to_store.append(f"Prefers {brand} headphones")

        # Only store if we have facts
        if facts_to_store:
            try:
                self.memory_service.remember_with_customer(customer_id, facts_to_store)
                logger.info(
                    "Stored %d facts for customer %s (STORE mode)",
                    len(facts_to_store),
                    customer_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to store memories for customer %s: %s",
                    customer_id,
                    exc,
                )

    def _generate_store_response(self, query: str, intent: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a deterministic acknowledgement response for STORE mode.

        Rules:
        - Acknowledge the preference was stored
        - Do NOT mention products
        - Do NOT call Product API
        - Do NOT show product cards
        - Simple, natural acknowledgement
        """
        # Build a simple acknowledgement based on what was stored
        messages = {
            "category": "Got it! I'll remember that you're interested in {category}.",
            "budget": "Understood! I'll remember your preferred budget of ₹{budget}.",
            "wireless": "Got it! I'll remember that you prefer wireless headphones.",
            "noise_cancellation": "Got it! I'll remember that you prefer headphones with active noise cancellation.",
            "brand": "Understood! I'll remember that you prefer {brand} headphones.",
            "default": "Got it! I'll remember that.",
        }

        # Determine what was stored and build appropriate response
        category = intent.get("category")
        budget = intent.get("budget", {})
        max_budget = budget.get("max") if isinstance(budget, dict) else None
        brand = intent.get("brand")
        wireless = intent.get("wireless")
        noise_cancellation = intent.get("noise_cancellation")

        # Build the response text
        parts: List[str] = []

        if category and category != "headphones":
            parts.append(messages["category"].format(category=category))

        if max_budget is not None and max_budget > 500:
            parts.append(messages["budget"].format(budget=max_budget))

        if wireless is not None and wireless:
            parts.append(messages["wireless"])

        if noise_cancellation is not None and noise_cancellation:
            parts.append(messages["noise_cancellation"])

        if brand:
            parts.append(messages["brand"].format(brand=brand))

        if not parts:
            response_text = messages["default"]
        else:
            response_text = " ".join(parts) + "."

        # Default follow-up question
        follow_up = "Is there anything else you'd like to tell me about your preferences?"

        return {
            "message": response_text,
            "llm_used": False,
            "response_mode": "STORE",
            "follow_up_question": follow_up,
        }

    # -----------------------------------------------------------------------
    # RECALL intent handlers
    # -----------------------------------------------------------------------

    def _generate_recall_response(self, query: str, intent: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a response for RECALL mode.

        Rules:
        - Retrieve relevant memories using MCP recall
        - Answer ONLY what the user asked about
        - Do NOT show product cards
        - Do NOT call Product API
        - Do NOT return every saved preference - only what's relevant to the question
        - Do NOT fabricate missing preferences
        """
        customer_id = self.memory_service.get_customer()
        if not customer_id:
            return {
                "message": "I'm not sure about your preferences yet. Could you tell me what you're looking for?",
                "llm_used": False,
                "response_mode": "RECALL",
                "follow_up_question": "Is there a specific aspect of your preferences you'd like me to check?",
            }

        # Extract the specific question type from the query
        query_lower = query.casefold()

        # Check what the user is asking about
        if "headphone budget" in query_lower or "budget" in query_lower:
            # Ask about budget specifically
            budget_facts = self._recall_budget(customer_id)
            if budget_facts:
                response = budget_facts
            else:
                response = "You haven't mentioned your headphone budget yet, so I don't have that information."
        elif "brand" in query_lower or "prefer" in query_lower:
            # Ask about brand
            brand_facts = self._recall_brand(customer_id)
            if brand_facts:
                response = brand_facts
            else:
                response = "You haven't mentioned your preferred headphone brand yet, so I don't have that information."
        elif "colour" in query_lower or "color" in query_lower:
            response = "You haven't mentioned your preferred headphone colour yet, so I don't have that information."
        else:
            # General recall - ask what specifically they want to know
            recall = self.memory_service.recall(query)
            fragments = list(recall.fragments) if recall.fragments else []

            if not fragments:
                response = "You haven't mentioned any preferences yet, so I don't have that information."
            else:
                # Only mention the first relevant fragment as a simple acknowledgement
                response = f"Based on what you've told me: {fragments[0]}."

        return {
            "message": response,
            "llm_used": False,
            "response_mode": "RECALL",
            "follow_up_question": "Is there anything specific about your preferences you'd like to know more about?",
        }

    def _recall_budget(self, customer_id: str) -> Optional[str]:
        """Recall the user's headphone budget from memory."""
        try:
            recall = self.memory_service.recall("preferences")
            if recall.fragments:
                from app.chatbot import extract_preferences
                # Summary line already stripped by _fragments_from_result; fragments[0] is a real memory
                for frag in recall.fragments:
                    prefs = extract_preferences(frag)
                    max_budget = prefs.get("max_budget")
                    if max_budget:
                        return f"Your preferred headphone budget is ₹{max_budget}."
        except Exception as exc:
            logger.warning("Failed to recall budget: %s", exc)
        return None

    def _recall_brand(self, customer_id: str) -> Optional[str]:
        """Recall the user's preferred brand from memory."""
        try:
            recall = self.memory_service.recall("preferences")
            if recall.fragments:
                from app.chatbot import extract_preferences
                # Summary line already stripped by _fragments_from_result; fragments[0] is a real memory
                for frag in recall.fragments:
                    prefs = extract_preferences(frag)
                    brand = prefs.get("preferred_brand")
                    if brand:
                        return f"You previously mentioned that you prefer {brand} headphones."
        except Exception as exc:
            logger.warning("Failed to recall brand: %s", exc)
        return None

    # -----------------------------------------------------------------------
    # NORMAL_RESPONSE intent handlers
    # -----------------------------------------------------------------------

    def _generate_normal_response(self, query: str, intent: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a natural response for ordinary conversation.

        Rules:
        - NO Product API call
        - NO preference storage
        - NO memory recall (unless the query specifically asks about memories)
        - Pure conversational response
        - Zero MCP calls
        - Zero Product API calls
        """
        query_lower = query.casefold()

        # Check if this is actually a memory question
        memory_terms = ["remember", "preference", "what do you know", "my budget", "my brand"]
        is_memory_question = any(term in query_lower for term in memory_terms)

        if is_memory_question:
            # Defer to RECALL handling, but since we're in NORMAL_RESPONSE,
            # just give a simple response
            return {
                "message": "I'd be happy to tell you about your preferences! Could you ask me specifically what you'd like to know?",
                "llm_used": False,
                "response_mode": "NORMAL_RESPONSE",
                "follow_up_question": "What would you like to know about your preferences?",
            }

        # Ordinary conversation patterns
        greeting_terms = ["hi", "hello", "hey", "how are you"]
        gratitude_terms = ["thank", "thanks"]
        thinking_terms = ["thinking about", "considering", "might", "maybe"]

        if any(term in query_lower for term in greeting_terms):
            response = "Hello! I'm doing well, thank you for asking. How can I help you today?"
        elif any(term in query_lower for term in gratitude_terms):
            response = "You're welcome! I'm here whenever you need help with headphones or other shopping."
        elif any(term in query_lower for term in thinking_terms):
            # User is just thinking aloud - natural conversation
            if "headphones" in query_lower or "gaming" in query_lower:
                response = "That sounds interesting! Are you looking for recommendations, or just thinking aloud?"
            else:
                response = "I see you're thinking about that. I'm here to help whenever you're ready!"
        elif "ANC" in query_lower:
            response = "ANC stands for Active Noise Cancellation. It's a technology that uses microphones to detect and cancel out ambient noise, providing a quieter listening experience. Would you like to know more about headphones with ANC?"
        elif "wireless" in query_lower:
            response = "Wireless headphones connect via Bluetooth rather than a physical cable, giving you more freedom of movement. They're great for exercising, commuting, or just relaxing. Would you like to know more about wireless options?"
        else:
            # Default natural response
            response = "I understand. I'm here to help you find the right headphones or answer any questions you have. What's on your mind?"

        return {
            "message": response,
            "llm_used": False,
            "response_mode": "NORMAL_RESPONSE",
            "follow_up_question": "Is there anything else I can help you with?",
        }

    # -----------------------------------------------------------------------
    # PRODUCT_DISCOVERY handlers (existing logic, properly gated)
    # -----------------------------------------------------------------------

    def _call_product_api(self, resolved_constraints: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Call the Product API search endpoint with resolved constraints.

        Returns list of product dicts from the API response.
        """
        try:
            # Build query parameters from resolved constraints
            params: Dict[str, Any] = {}

            category = resolved_constraints.get("category")
            if category:
                params["category"] = category

            budget = resolved_constraints.get("budget", {})
            max_budget = budget.get("max")
            if max_budget is not None:
                params["max_price"] = max_budget

            brand = resolved_constraints.get("brand")
            if brand:
                params["brand"] = brand

            required_features = resolved_constraints.get("required_features")
            if required_features:
                # Convert to comma-separated string
                params["required_features"] = ", ".join(required_features)

            excluded_features = resolved_constraints.get("excluded_features")
            if excluded_features:
                params["excluded_features"] = ", ".join(excluded_features)

            sort_by = resolved_constraints.get("sort_preference")
            if sort_by:
                params["sort_by"] = sort_by

            # Make the API call
            resp = httpx.post(
                f"{self._product_api_base}/api/products/search",
                params=params,
                timeout=15.0,
            )

            if resp.status_code == 200:
                data = resp.json()
                products = data.get("products", [])
                logger.info(
                    "Product API returned %d products for constraints %s",
                    len(products),
                    resolved_constraints,
                )
                return products
            else:
                logger.warning(
                    "Product API returned status %d, returning empty results",
                    resp.status_code,
                )
                return []
        except httpx.RequestError as exc:
            logger.error("Product API request failed: %s", exc)
            # Graceful degradation - return empty results, not crash
            return []
        except Exception as exc:
            logger.error("Product API unexpected error: %s", exc)
            return []

    def _update_memory_if_appropriate(self, query: str, constraints: Dict[str, Any]) -> None:
        """Update customer memory with persistent preferences only.

        Rules:
        - Category preferences are persistent
        - Budget preferences are persistent (if meaningful > ₹500)
        - Wireless preference stored only if explicitly preferred
        - Noise cancellation stored only if explicitly preferred
        - Brand stored only if explicitly mentioned
        - Temporary constraints (budget for one purchase) are NOT persisted
        """
        customer_id = self.memory_service.get_customer()
        if not customer_id:
            return

        facts_to_store: List[str] = []

        # Category preferences are persistent
        category = constraints.get("category")
        if category:
            category_keywords = [
                "headphones",
                "footwear",
                "apparel",
                "bags",
                "running",
                "shoes",
            ]
            if any(kw in category for kw in category_keywords):
                facts_to_store.append(f"Prefers {category} shopping")

        # Budget preferences are persistent (but distinguish from temporary constraints)
        max_budget = constraints.get("budget", {}).get("max")
        if max_budget and max_budget > 0:
            # Only store substantial budgets, not one-time constraints
            if max_budget > 500:  # Only store if budget is meaningful
                facts_to_store.append(f"Has a budget up to ₹{max_budget}")

        # Wireless preference - only store if explicitly mentioned as a preference
        if constraints.get("wireless") is not None:
            wireless_str = "wireless" if constraints["wireless"] else "wired"
            # Only store if the user indicated this is their preference
            if constraints["wireless"]:  # Only store wireless as preference
                facts_to_store.append(f"Prefers {wireless_str} headphones")

        # Noise cancellation preference - only store if explicitly preferred
        if constraints.get("noise_cancellation"):
            facts_to_store.append("Prefers noise-cancelling headphones")

        # Preferred brand - only store if explicitly mentioned
        if constraints.get("brand"):
            facts_to_store.append(f"Prefers {constraints['brand']} headphones")

        # Only store if we have facts to store
        if facts_to_store:
            try:
                self.memory_service.remember_with_customer(customer_id, facts_to_store)
                logger.info(
                    "Stored %d facts for customer %s (PRODUCT_DISCOVERY)",
                    len(facts_to_store),
                    customer_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to store memories for customer %s: %s",
                    customer_id,
                    exc,
                )

    def clear_conversation(self) -> None:
        """Clear the conversation history without deleting long-term memories.

        Important: This only clears Streamlit session state, not Cognee memories.
        """
        self.conversation_history = []
        logger.info(
            "Conversation history cleared for customer %s",
            self.memory_service.get_customer(),
        )

    def _determine_intent_mode(
        self, intent: Dict[str, Any], query: str
    ) -> str:
        """Determine the intent mode for a user query.

        Returns one of:
        - "STORE": Store persistent preferences from this query
        - "RECALL": Retrieve relevant memories for this query
        - "NORMAL_RESPONSE": Just respond based on current query
        - "PRODUCT_DISCOVERY": Search, filter, compare, or recommend actual products

        Rules:
        - STORE: When the user explicitly states a preference (category, brand,
          wireless, noise_cancellation) or a meaningful budget (> ₹500)
        - RECALL: When the query references previous context, usage buckets,
          or asks for recommendations based on stored preferences
        - NORMAL_RESPONSE: All other queries - respond based on current constraints
        - PRODUCT_DISCOVERY: When the user explicitly requests product discovery

        IMPORTANT: A preference statement is NOT a shopping request.
        A budget statement is NOT a shopping request.
        A question about memory is NOT a shopping request.
        """
        query_lower = query.casefold()

        # --- STORE checks: examine query text directly ---
        # Preference: query STARTS WITH "I prefer X", "I like X", "I want X"
        # (Not just contains "i prefer" anywhere, e.g. questions ending with "do I prefer?")
        starts_with_preference = any(
            query_lower.startswith(p) for p in ["i prefer", "i like", "i love", "i want"]
        )

        if starts_with_preference:
            # Check for category
            for cat in [
                "headphones",
                "earbuds",
                "audio",
                "professional",
                "footwear",
                "apparel",
                "bags",
                "smartphones",
                "laptops",
                "smartwatches",
            ]:
                if f" {cat} " in query_lower or query_lower.startswith(cat + " ") or query_lower.endswith(" " + cat):
                    return "STORE"
            # Check for brand
            for brand in ["sony", "bose", "jabra", "beats", "sennheiser"]:
                if f" {brand} " in query_lower or query_lower.startswith(brand + " "):
                    return "STORE"
            # Check for wireless
            if " wireless" in query_lower or query_lower.startswith("wireless "):
                return "STORE"
            # Check for noise cancellation
            if "noise cancellation" in query_lower or "anc" in query_lower:
                return "STORE"

        # Check for budget (meaningful > ₹500) - ALWAYS check, not just with preference patterns
        # But skip if this is an explicit product request (the budget will be handled by PRODUCT_DISCOVERY path)
        product_request_terms = [
            "recommend",
            "suggest",
            "search",
            "compare",
            "show me",
            "find",
            "look for",
        ]
        is_product_request = any(term in query_lower for term in product_request_terms)
        budget_match = re.search(r"₹?\s*([\d,]+)", query_lower)
        if budget_match and not is_product_request:
            try:
                budget_val = int(budget_match.group(1).replace(",", ""))
                if budget_val > 500:
                    return "STORE"
            except ValueError:
                pass

        # --- RECALL checks: examine query text directly ---
        # Question about stored preferences
        recall_question_terms = [
            "what do you know",
            "what is my",
            "what colour",
            "what color",
            "my preferences",
            "my budget",
            "my brand",
        ]
        is_question = query.rstrip().endswith("?")
        if is_question and any(term in query_lower for term in recall_question_terms):
            return "RECALL"

        # Check for RECALL signals: usage buckets or context references
        usage = intent.get("usage")
        if usage:
            return "RECALL"

        # Check conversation history for context references
        if self.conversation_history:
            recent = self.conversation_history[-3:]
            combined = " ".join(t.get("content", "") for t in recent)
            lowered = combined.casefold()
            # If user refers to previous conversation context
            if any(term in lowered for term in ["before", "previous", "like last", "remember", "earlier"]):
                return "RECALL"

        # --- PRODUCT_DISCOVERY checks: explicit product requests ---
        # User explicitly asks for product discovery
        product_request_terms = [
            "recommend",
            "suggest",
            "search",
            "compare",
            "show me",
            "find",
            "look for",
        ]
        if any(term in query_lower for term in product_request_terms):
            # But not if it's a normal conversation about products
            # Exclude: "I'm thinking about buying", "I might explore", etc.
            exclusion_terms = [
                "thinking about",
                "might",
                "sometime",
                "explore",
                "considering",
            ]
            if not any(exc in query_lower for exc in exclusion_terms):
                return "PRODUCT_DISCOVERY"

        # Default to NORMAL_RESPONSE
        return "NORMAL_RESPONSE"

    def _get_relevant_memories(self, query: str) -> List[str]:
        """Get relevant memory fragments for display in the response.

        Only retrieves memories scoped to the current query category.
        """
        customer_id = self.memory_service.get_customer()
        if not customer_id:
            return []

        try:
            recall = self.memory_service.recall(query)
            if recall.fragments:
                return list(recall.fragments)
        except Exception as exc:
            logger.warning("Failed to retrieve memories: %s", exc)

        return []