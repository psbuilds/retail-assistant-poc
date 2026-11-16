"""Response generation service.

Given the user's query, the resolved constraints, and the products the Product
API actually returned, produce a natural-language answer plus the product IDs
the UI should render.

Contract:

- The Product API is the only source of truth. The model is given the exact
  result set and may only talk about those products.
- Anything the model returns is validated against the result set. Unknown
  product IDs are dropped, invented prices cause the whole model answer to be
  rejected, and a model failure of any kind falls back to a deterministic
  message built from the same result set.
- ``llm_used`` reports which path produced the message, so the caller never
  has to guess.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from app.llm import ChatMessage, LLMChatError, get_llm_client

logger = logging.getLogger(__name__)

#: Model must answer concisely; shopping replies do not need essays.
MAX_RESPONSE_TOKENS = 400

#: Instructions for the generation model. A worked example teaches small
#: models far more reliably than a list of prohibitions, so the example comes
#: first and the rules follow it.
_SYSTEM_PROMPT = """You are a retail shopping assistant. You may only talk about the product rows given to you. Reply with JSON and nothing else.

Match this style exactly.

Example input:
- id=p900 | name="Studio Headphones" | brand=Auralis | price_inr=5499 | rating=4.6 | wireless=False | noise_cancellation=True
CUSTOMER MESSAGE: "headphones under 6000"

Example output:
{"message": "The Studio Headphones at ₹5499 are the only match under ₹6000. They bring noise cancellation and a 4.6★ rating, so they suit focused listening.", "recommended_product_ids": ["p900"], "follow_up_question": "Would you like to compare them with a wireless pair?"}

Rules for every reply:
- Name every product you recommend, exactly as written in its row, and quote its price_inr. Never recommend a product you did not name.
- Only list products that satisfy the customer's stated constraints, and describe each one positively. Do not list a product just to say it is unsuitable.
- Never invent a product, price, rating, brand, feature, or URL.
- Only say a product has noise cancellation or is wireless when its row says true.
- Never call a product affordable or in-budget when its price_inr is above max_price_inr.
- If there are no rows, say plainly that nothing matched, name one constraint to relax, and return an empty recommended_product_ids array.
- 40 to 90 words in the message field.

Output shape: {"message": "...", "recommended_product_ids": ["id", ...], "follow_up_question": "..."}"""


class ResponseGenerationError(Exception):
    """Raised when response generation cannot produce any answer at all."""


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def generate_response(
    original_query: str,
    resolved_constraints: Dict[str, Any],
    api_products: Sequence[Dict[str, Any]],
    conversation_context: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Generate the assistant reply for one turn.

    Args:
        original_query: The user's message.
        resolved_constraints: Output of ``ConstraintResolver.resolve_constraints``.
        api_products: Products returned by the Product API. Source of truth.
        conversation_context: Prior turns, for continuity only.

    Returns:
        ``{"message", "recommended_product_ids", "follow_up_question",
        "llm_used", "response_mode"}``.
    """
    products = [dict(product) for product in api_products]

    client = get_llm_client()
    if client is None:
        logger.info("No LLM client available; using deterministic response.")
        return _deterministic_result(original_query, resolved_constraints, products)

    messages = _build_messages(
        original_query, resolved_constraints, products, conversation_context
    )

    try:
        raw = client.chat(messages, max_tokens=MAX_RESPONSE_TOKENS)
    except LLMChatError as exc:
        logger.warning("LLM response generation failed: %s", exc)
        return _deterministic_result(original_query, resolved_constraints, products)

    parsed = _parse_model_output(raw)
    if parsed is None:
        logger.warning("LLM output was not usable JSON; using deterministic response.")
        return _deterministic_result(original_query, resolved_constraints, products)

    validated = _validate_model_output(
        parsed, products, resolved_constraints.get("budget")
    )
    if validated is None:
        logger.warning(
            "LLM output failed product-grounding checks; using deterministic response."
        )
        return _deterministic_result(original_query, resolved_constraints, products)

    message, product_ids, follow_up = validated
    return {
        "message": message,
        "recommended_product_ids": product_ids,
        "follow_up_question": follow_up,
        "llm_used": True,
        "response_mode": "llm",
    }


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #


def _format_product(product: Dict[str, Any]) -> str:
    """Render one API product as a line the model can quote from."""
    parts = [
        f'id={product.get("id")}',
        f'name="{product.get("name")}"',
        f"brand={product.get('brand', 'unknown')}",
        f"price_inr={product.get('price')}",
        f"rating={product.get('rating')}",
        f"wireless={bool(product.get('wireless'))}",
        f"noise_cancellation={bool(product.get('noise_cancellation'))}",
    ]
    features = product.get("features") or []
    parts.append(f"features=[{', '.join(str(f) for f in features)}]")
    return "- " + " | ".join(parts)


def _describe_constraints(constraints: Dict[str, Any]) -> str:
    lines: List[str] = []

    category = constraints.get("category")
    if category:
        lines.append(f"category={category}")

    budget = constraints.get("budget") or {}
    if isinstance(budget, dict) and budget.get("max") is not None:
        lines.append(f"max_price_inr={budget['max']}")
    if isinstance(budget, dict) and budget.get("min") is not None:
        lines.append(f"min_price_inr={budget['min']}")

    brand = constraints.get("brand")
    if brand:
        lines.append(f"brand={brand}")

    if constraints.get("wireless") is not None:
        lines.append(f"wireless={constraints['wireless']}")
    if constraints.get("noise_cancellation") is not None:
        lines.append(f"noise_cancellation={constraints['noise_cancellation']}")

    required = constraints.get("required_features") or []
    if required:
        lines.append(f"required_features={', '.join(map(str, required))}")
    excluded = constraints.get("excluded_features") or []
    if excluded:
        lines.append(f"excluded_features={', '.join(map(str, excluded))}")

    return "\n".join(f"- {line}" for line in lines) or "- none"


def _build_messages(
    original_query: str,
    resolved_constraints: Dict[str, Any],
    products: Sequence[Dict[str, Any]],
    conversation_context: Optional[List[Dict[str, str]]],
) -> List[ChatMessage]:
    system = _SYSTEM_PROMPT

    context_lines: List[str] = []
    if conversation_context:
        for turn in conversation_context[-4:]:
            role = turn.get("role", "user")
            content = (turn.get("content") or "").strip()
            if content:
                context_lines.append(f"{role}: {content[:300]}")

    product_block = (
        "\n".join(_format_product(product) for product in products)
        if products
        else "(none - the Product API returned no matches)"
    )

    user = (
        f'CUSTOMER MESSAGE: "{original_query}"\n\n'
        f"RESOLVED CONSTRAINTS:\n{_describe_constraints(resolved_constraints)}\n\n"
        f"PRODUCT ROWS FROM PRODUCT API (the only allowed products):\n{product_block}\n\n"
        + (
            "RECENT CONVERSATION:\n" + "\n".join(context_lines) + "\n\n"
            if context_lines
            else ""
        )
        + "Write the JSON reply now."
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# --------------------------------------------------------------------------- #
# Model output handling
# --------------------------------------------------------------------------- #


def _parse_model_output(raw: str) -> Optional[Dict[str, Any]]:
    """Extract the first JSON object from model output, tolerating fences."""
    if not raw:
        return None

    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)

    start = text.find("{")
    if start == -1:
        return None

    decoder = json.JSONDecoder()
    while start != -1:
        try:
            parsed, _ = decoder.raw_decode(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(parsed, dict):
            return parsed
        start = text.find("{", start + 1)

    return None


def _validate_model_output(
    parsed: Dict[str, Any],
    products: Sequence[Dict[str, Any]],
    resolved_budget: Optional[Dict[str, Any]] = None,
) -> Optional[tuple[str, List[str], str]]:
    """Ground the model answer in the actual API result set.

    Returns:
        ``(message, product_ids, follow_up)`` when the answer is trustworthy,
        otherwise ``None`` so the caller falls back.
    """
    message = str(parsed.get("message") or "").strip()
    if not message:
        logger.warning("LLM returned an empty message.")
        return None

    actual_ids = [str(product.get("id")) for product in products if product.get("id")]

    raw_ids = parsed.get("recommended_product_ids")
    claimed_ids: List[str] = []
    if isinstance(raw_ids, list):
        claimed_ids = [str(item) for item in raw_ids if str(item)]

    if not actual_ids:
        # No products: the message must not smuggle recommendations in.
        if _mentions_any_product_name(message, products) or claimed_ids:
            logger.warning("LLM proposed products despite an empty result set.")
            return None
    else:
        allowed = set(actual_ids)
        unknown = [pid for pid in claimed_ids if pid not in allowed]
        if unknown:
            logger.warning("LLM referenced product IDs outside the API result: %s", unknown)
            return None
        if not claimed_ids:
            claimed_ids = actual_ids
        # Preserve API ordering (API ranking is authoritative).
        claimed_ids = [pid for pid in actual_ids if pid in set(claimed_ids)]

        # A recommendation blurb that names no product is not grounded in the
        # result set; the deterministic template does better.
        if not _mentions_any_product_name(message, products):
            logger.warning("LLM message named none of the returned products.")
            return None

    if not _is_grounded(message, products, resolved_budget):
        return None

    follow_up = str(parsed.get("follow_up_question") or "").strip()
    if not follow_up:
        follow_up = _default_follow_up(bool(products))

    return message, claimed_ids, follow_up


def _mentions_any_product_name(message: str, products: Sequence[Dict[str, Any]]) -> bool:
    lowered = message.casefold()
    return any(
        str(product.get("name", "")).casefold() in lowered
        for product in products
        if product.get("name")
    )


def _allowed_amounts(products: Sequence[Dict[str, Any]]) -> set[int]:
    """Prices that may legitimately appear in a message."""
    amounts: set[int] = set()
    for product in products:
        try:
            amounts.add(int(round(float(product.get("price")))))
        except (TypeError, ValueError):
            continue
    return amounts


def _is_grounded(
    message: str,
    products: Sequence[Dict[str, Any]],
    resolved_budget: Optional[Dict[str, Any]] = None,
) -> bool:
    """Reject messages that quote a price neither the API nor the budget set.

    A model that invents ``₹4,999`` for a catalogue with no such price is
    fabricating, however plausible the sentence looks. Prices in the message
    must therefore come from the result set or from the resolved budget the
    customer actually stated.
    """
    mentions = re.findall(r"(?:₹|rs\.?|inr)\s*([\d,]+)", message.casefold())
    if not mentions:
        return True

    allowed = _allowed_amounts(products)
    if isinstance(resolved_budget, dict):
        for key in ("max", "min"):
            value = resolved_budget.get(key)
            if isinstance(value, (int, float)):
                allowed.add(int(round(float(value))))

    for raw in mentions:
        try:
            value = int(raw.replace(",", ""))
        except ValueError:
            continue
        if value in allowed:
            continue
        # Budget figures quoted from the constraints are allowed too.
        logger.warning(
            "LLM quoted price %s which is not in the API result set.", value
        )
        return False
    return True


# --------------------------------------------------------------------------- #
# Deterministic fallback
# --------------------------------------------------------------------------- #


def _deterministic_result(
    original_query: str,
    resolved_constraints: Dict[str, Any],
    products: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the fallback answer from API data only."""
    return {
        "message": _deterministic_message(resolved_constraints, products),
        "recommended_product_ids": [
            str(product.get("id")) for product in products if product.get("id")
        ],
        "follow_up_question": _default_follow_up(bool(products)),
        "llm_used": False,
        "response_mode": "deterministic",
    }


def _constraint_phrases(constraints: Dict[str, Any]) -> List[str]:
    """Human-readable constraint phrases, derived from resolved values only."""
    phrases: List[str] = []

    budget = constraints.get("budget") or {}
    if isinstance(budget, dict) and budget.get("max") is not None:
        phrases.append(f"under ₹{budget['max']}")

    brand = constraints.get("brand")
    if brand:
        phrases.append(f"from {brand}")

    anc = constraints.get("noise_cancellation")
    if anc is True:
        phrases.append("with noise cancellation")
    elif anc is False:
        phrases.append("without noise cancellation")

    wireless = constraints.get("wireless")
    if wireless is True:
        phrases.append("wireless")

    # Skip feature tags already conveyed by the boolean phrasing above.
    implied = set()
    if anc is not None:
        implied.add("noise-cancelling")
    if wireless is not None:
        implied.add("wireless")

    required = [
        str(feature)
        for feature in (constraints.get("required_features") or [])
        if str(feature).casefold() not in implied
    ]
    if required:
        phrases.append(f"with {', '.join(required[:2])}")

    return phrases


def _deterministic_message(
    constraints: Dict[str, Any], products: Sequence[Dict[str, Any]]
) -> str:
    """Compose a readable answer using only the API result set."""
    category = constraints.get("category") or "products"
    phrases = _constraint_phrases(constraints)
    qualifier = f" {' '.join(phrases)}" if phrases else ""

    if not products:
        return (
            f"I couldn't find any {category}{qualifier} in the catalogue right now. "
            "Try relaxing the budget or dropping one of the feature requirements."
        )

    lines = [f"Here {'is' if len(products) == 1 else 'are'} the {category}{qualifier} I found:"]
    for product in products:
        name = product.get("name", "Unknown product")
        price = product.get("price")
        rating = product.get("rating")
        detail = f"• {name} — ₹{price}"
        if rating is not None:
            detail += f" ({rating}★)"
        extras: List[str] = []
        if product.get("noise_cancellation"):
            extras.append("noise cancellation")
        if product.get("wireless"):
            extras.append("wireless")
        if extras:
            detail += f", {', '.join(extras)}"
        lines.append(detail)

    return "\n".join(lines)


def _default_follow_up(has_products: bool) -> str:
    if has_products:
        return "Would you like more detail on any of these, or should I adjust the budget or features?"
    return "Would you like me to widen the budget or relax a feature requirement?"
