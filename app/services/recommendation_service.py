"""Recommendation service responsible for understanding shopping requirements and filtering the product catalogue.

Responsible for:
- Understanding shopping requirements from natural language queries
- Filtering the product catalogue based on preferences and constraints
- Applying product constraints (budget, features, exclusions)
- Considering relevant remembered preferences
- Ranking and explaining recommendations
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.products import filter_by_category, filter_by_max_budget, filter_by_wireless, \
    filter_by_noise_cancellation, filter_by_brand, filter_by_required_features, \
    filter_by_excluded_features, filter_by_excluded_product_ids, \
    load_catalog, Product

logger = logging.getLogger(__name__)


class RecommendationService:
    """Service for generating product recommendations based on preferences and requirements."""

    def __init__(self) -> None:
        self.catalog = load_catalog()
        # Store recommendations explanations separately (Product is a frozen dataclass)
        self._product_explanations: Dict[str, str] = {}

    def get_recommendations(
        self,
        preferences: Dict[str, Any],
        limit: int = 3,
        current_query: str = "",
    ) -> List[Product]:
        """Get product recommendations based on preferences.

        The recommendation logic considers:
        1. Current user message requirements
        2. Relevant conversation history
        3. Memories retrieved through MCP
        4. Available products in the catalogue

        Args:
            preferences: Merged preferences from memory and current query
            limit: Maximum number of recommendations to return
            current_query: The current user query for context

        Returns:
            List of recommended products
        """
        selected = self._apply_constraints(preferences)
        ranked = self._rank_by_score(selected, preferences)

        # Apply limit
        limited = ranked[:limit]

        # Add explanation context using separate dict
        self._add_explanations(limited, preferences, current_query)

        return limited

    def _extract_intent_from_query(self, query: str) -> Dict[str, Any]:
        """Extract shopping intent from a natural language query.

        Args:
            query: The user's query string

        Returns:
            Dictionary of extracted preferences
        """
        from app.chatbot import extract_preferences
        return extract_preferences(query)

    def _apply_constraints(self, preferences: Dict[str, Any]) -> List[Product]:
        """Apply all preference constraints to filter the catalogue.

        Args:
            preferences: Dictionary of preference key-value pairs

        Returns:
            Filtered product list
        """
        selected = self.catalog  # Start with full catalog

        # Category filter (most important - hard constraint)
        category = preferences.get("category")
        if category:
            selected = filter_by_category(selected, category)

        # Budget filter (hard constraint)
        max_budget = preferences.get("max_budget")
        if max_budget is not None:
            selected = filter_by_max_budget(selected, max_budget)

        # Wireless filter
        wireless = preferences.get("wireless")
        if wireless is not None:
            selected = filter_by_wireless(selected, wireless)

        # Noise cancellation filter
        noise_cancellation = preferences.get("noise_cancellation")
        if noise_cancellation is not None:
            selected = filter_by_noise_cancellation(selected, noise_cancellation)

        # Brand filter
        preferred_brand = preferences.get("preferred_brand")
        if preferred_brand:
            selected = filter_by_brand(selected, preferred_brand)

        # Required features filter
        required_features = preferences.get("required_features")
        if required_features:
            selected = filter_by_required_features(selected, required_features)

        # Excluded features filter
        excluded_features = preferences.get("excluded_features")
        if excluded_features:
            selected = filter_by_excluded_features(selected, excluded_features)

        # Excluded product IDs
        excluded_product_ids = preferences.get("excluded_product_ids")
        if excluded_product_ids:
            selected = filter_by_excluded_product_ids(selected, excluded_product_ids)

        return selected

    def _rank_by_score(self, products: List[Product], preferences: Dict[str, Any]) -> List[Product]:
        """Rank products by rating, price, and ID.

        Products are sorted by:
        1. Rating (descending)
        2. Price (ascending - lower price ranks higher for same rating)
        3. Product ID (alphabetical)

        Args:
            products: List of filtered products
            preferences: Current preferences (for limit context)

        Returns:
            Ranked product list
        """
        return sorted(products, key=lambda p: (-p.rating, p.price, p.product_id))

    def _add_explanations(
        self,
        products: List[Product],
        preferences: Dict[str, Any],
        current_query: str,
    ) -> None:
        """Add explanation text to each product recommendation.

        Explanations are stored in a separate dict rather than on the frozen
        Product dataclass.

        Args:
            products: List of recommended products to explain
            preferences: The preferences used for filtering
            current_query: The current user query for context
        """
        self._product_explanations = {}
        for product in products:
            explanation_parts: List[str] = []

            # Explain features that match
            if preferences.get("wireless") is not None:
                if product.wireless == preferences["wireless"]:
                    explanation_parts.append(
                        "wireless" if preferences["wireless"] else "wired"
                    )

            if preferences.get("noise_cancellation"):
                if product.noise_cancellation:
                    explanation_parts.append("noise cancellation")

            if preferences.get("required_features"):
                matching_features = [
                    f for f in product.features
                    if f.casefold() in [feat.casefold() for feat in preferences["required_features"]]
                ]
                if matching_features:
                    explanation_parts.append(
                        f"with {', '.join(matching_features[:2])}"
                    )

            if preferences.get("excluded_features"):
                # Only mention if the product survived the exclusion filter
                excluded_matching = [
                    f for f in product.features
                    if f.casefold() in [feat.casefold() for feat in preferences["excluded_features"]]
                ]
                # Only add explanation if product doesn't match excluded features
                if not any(f.casefold() in [feat.casefold() for feat in preferences["excluded_features"]]
                           for f in product.features):
                    explanation_parts.append("avoids excluded features")

            if preferences.get("preferred_brand") and product.brand.casefold() == preferences["preferred_brand"].casefold():
                explanation_parts.append(f"from {preferences['preferred_brand']}")

            # Store the explanation in the separate dict
            self._product_explanations[product.product_id] = " ".join(explanation_parts) if explanation_parts else "Good match for your needs"

    def get_recommendation_summary(self, products: List[Product]) -> str:
        """Generate a summary string for a list of product recommendations.

        Args:
            products: List of recommended products

        Returns:
            Summary string describing the recommendations
        """
        if not products:
            return "No products match your current criteria."

        summary_parts: List[str] = []
        for product in products[:3]:
            price_str = f"₹{product.price}"
            explanation = self._product_explanations.get(product.product_id, "")
            part = f"{product.name} - {price_str}"
            if explanation and explanation != "Good match for your needs":
                part += f" ({explanation})"
            summary_parts.append(part)

        return ". ".join(summary_parts) + "."