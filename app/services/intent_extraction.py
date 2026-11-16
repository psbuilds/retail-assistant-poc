"""Intent extraction service.

Turns a user message into the structured intent schema below:

- category, budget {min,max,currency}, brand
- required_features, excluded_features
- usage, sort_preference

Two layers, in this order:

1. **Deterministic extraction** (``app.chatbot.extract_preferences`` plus lexical
   rules). It is cheap, repeatable, and always runs. It is the authority: any
   signal it finds in the current message wins.
2. **LLM extraction**, used only to fill fields the deterministic layer left
   blank (for example "something suitable for travelling" implies usage=travel
   but no explicit category term). Model output is parsed as JSON, validated
   against a closed vocabulary, and discarded wholesale if unusable.

The LLM never filters products itself and never bypasses
``ConstraintResolver``: its output is an *intent*, merged into the resolver's
lowest-priority slot for the current-request layer.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.chatbot import _FEATURES, extract_preferences

logger = logging.getLogger(__name__)

#: Categories the catalogue actually uses.
VALID_CATEGORIES = (
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

VALID_SORT_PREFERENCES = ("rating", "price_low", "price_high", "relevance")

#: Usage buckets the extraction understands.
VALID_USAGES = ("commute", "sports", "travel", "gaming", "calls", "work", "study")

MAX_REASONABLE_BUDGET = 1_000_000

#: Tokens the model may draw on for features; extra tags are dropped.
FEATURE_VOCABULARY = tuple(sorted({feature.casefold() for feature in _FEATURES}))

INTENT_MAX_TOKENS = 200

INTENT_SYSTEM_PROMPT = """You extract shopping intent from a customer message. Reply with JSON only.

Allowed categories: headphones, earbuds, audio, professional, footwear, apparel, bags, smartphones, laptops, smartwatches
Allowed features: """ + ", ".join(FEATURE_VOCABULARY) + """

Rules:
- Use "" when the message does not state a value. Never guess.
- budget.max is a plain integer in rupees. Omit it when no ceiling is stated.
- "without ANC" means excluded_features contains "noise-cancelling".
- Keep features to the allowed list, lowercase.

Output shape:
{"category": "", "budget": {"max": null}, "brand": "", "required_features": [], "excluded_features": [], "usage": ""}"""


def extract_intent(
    query: str,
    conversation_history: Optional[List[Dict[str, str]]] = None,
    customer_preferences: Optional[Dict[str, Any]] = None,
    *,
    use_llm: bool = True,
    llm_client: Any = None,
) -> Dict[str, Any]:
    """Extract a validated structured intent for one user message.

    Args:
        query: The user's current message.
        conversation_history: Prior turns, for refusals to re-state context.
        customer_preferences: Long-term preferences, used only as context.
        use_llm: Set False to force the deterministic layer (tests, offline).
        llm_client: Optional pre-built provider; resolved from config otherwise.

    Returns:
        Dict with the intent keys plus ``source`` (``"deterministic"`` or
        ``"llm"``) describing which layer filled the gaps.
    """
    deterministic, category_from_default = _deterministic_intent(
        query, conversation_history, customer_preferences
    )

    if not use_llm:
        return deterministic

    client = llm_client
    if client is None:
        from app.llm import get_llm_client

        client = get_llm_client()
    if client is None:
        return deterministic

    raw = _call_intent_model(client, query, conversation_history)
    if not raw:
        return deterministic

    llm_intent = _parse_and_validate_llm_intent(raw)
    if llm_intent is None:
        return deterministic

    merged, used_llm = _merge_intents(deterministic, llm_intent, category_from_default)
    if used_llm:
        merged["source"] = "llm"
    return merged


# --------------------------------------------------------------------------- #
# Deterministic layer
# --------------------------------------------------------------------------- #


def _deterministic_intent(
    query: str,
    conversation_history: Optional[List[Dict[str, str]]],
    customer_preferences: Optional[Dict[str, Any]],
) -> tuple[Dict[str, Any], bool]:
    """Run the lexical extraction.

    Returns:
        ``(intent, category_from_default)`` where the flag says whether the
        category was assumed rather than stated, so the LLM may override it.
    """
    query_prefs = extract_preferences(query)

    explicit_category = query_prefs.get("category")
    category_from_history = None
    if not explicit_category and conversation_history:
        category_from_history = _category_from_history(conversation_history)
    category_from_memory = (
        customer_preferences.get("category") if customer_preferences else None
    )

    if explicit_category:
        category = explicit_category
        category_from_default = False
    elif category_from_history:
        category = category_from_history
        category_from_default = False
    elif category_from_memory:
        category = category_from_memory
        category_from_default = False
    else:
        category = "headphones"
        category_from_default = True

    intent: Dict[str, Any] = {
        "category": category,
        "budget": _extract_budget(query_prefs),
        "brand": _extract_brand(query_prefs),
        "required_features": _extract_features(query_prefs, include=True),
        "excluded_features": _extract_features(query_prefs, include=False),
        "usage": _extract_usage(query, conversation_history),
        "sort_preference": _extract_sort_preference(query_prefs),
        "source": "deterministic",
    }

    # Keep the explicit boolean signals: they are what lets a later stage
    # reconcile "without ANC" against feature tags the model suggests.
    for key in ("wireless", "noise_cancellation"):
        if isinstance(query_prefs.get(key), bool):
            intent[key] = query_prefs[key]

    return _validate_intent(intent), category_from_default


def _category_from_history(conversation_history: List[Dict[str, str]]) -> Optional[str]:
    category_terms = {
        "headphones": ("headphone", "earbuds", "headset", "earphone"),
        "footwear": ("shoe", "shoes", "sneaker", "boots", "running"),
        "apparel": ("shirt", "hoodie", "jacket", "clothing"),
        "bags": ("bag", "backpack", "tote"),
        "smartphones": ("smartphone", "phone"),
        "laptops": ("laptop", "notebook"),
        "smartwatches": ("smartwatch", "watch"),
    }
    for turn in reversed(conversation_history[-3:]):
        text = (turn.get("content") or "").casefold()
        for category, terms in category_terms.items():
            if any(term in text for term in terms):
                return category
    return None


def _extract_budget(query_prefs: Dict[str, Any]) -> Dict[str, Any]:
    budget_data: Dict[str, Any] = {}

    max_budget = query_prefs.get("max_budget")
    if max_budget is not None and max_budget > 0:
        budget_data["max"] = max_budget
        budget_data["currency"] = "INR"

    min_budget = query_prefs.get("min_budget")
    if min_budget is not None and min_budget >= 0:
        budget_data["min"] = min_budget

    return budget_data


def _extract_brand(query_prefs: Dict[str, Any]) -> str:
    brand = query_prefs.get("preferred_brand", "")
    return brand if isinstance(brand, str) and brand.strip() else ""


def _extract_features(query_prefs: Dict[str, Any], include: bool = True) -> List[str]:
    key = "required_features" if include else "excluded_features"
    features = query_prefs.get(key, ())
    if isinstance(features, tuple):
        return list(features)
    return list(features) if features else []


def _extract_usage(
    query: str, conversation_history: Optional[List[Dict[str, str]]]
) -> str:
    lowered = query.casefold()

    usage_indicators = {
        "commute": ["commute", "working", "office", "work from home", "wfh"],
        "sports": ["sports", "gym", "workout", "running", "jogging"],
        "travel": ["travel", "travelling", "trip", "flight"],
        "gaming": ["gaming", "game", "pc"],
        "calls": ["call", "calls", "conference", "zoom"],
    }

    for usage, terms in usage_indicators.items():
        if any(term in lowered for term in terms):
            return usage

    if conversation_history:
        combined = " ".join(
            (turn.get("content") or "").casefold()
            for turn in conversation_history[-3:]
        )
        for usage, terms in usage_indicators.items():
            if any(term in combined for term in terms):
                return usage

    return ""


def _extract_sort_preference(query_prefs: Dict[str, Any]) -> str:
    explicit = query_prefs.get("sort_preference")
    if explicit:
        return str(explicit).lower()
    return "rating"


# --------------------------------------------------------------------------- #
# LLM layer
# --------------------------------------------------------------------------- #


def _call_intent_model(
    client: Any, query: str, conversation_history: Optional[List[Dict[str, str]]]
) -> str:
    """Ask the model for an intent. Returns "" on any provider failure."""
    from app.llm import LLMChatError

    history_block = ""
    if conversation_history:
        recent = [
            f"{turn.get('role', 'user')}: {(turn.get('content') or '')[:200]}"
            for turn in conversation_history[-4:]
            if turn.get("content")
        ]
        if recent:
            history_block = "\nRECENT TURNS:\n" + "\n".join(recent) + "\n"

    messages = [
        {"role": "system", "content": INTENT_SYSTEM_PROMPT},
        {"role": "user", "content": f'{history_block}CUSTOMER MESSAGE: "{query}"'},
    ]

    try:
        return client.chat(messages, max_tokens=INTENT_MAX_TOKENS, temperature=0.0)
    except LLMChatError as exc:
        logger.warning("LLM intent extraction failed, using deterministic intent: %s", exc)
        return ""


def _parse_and_validate_llm_intent(raw: str) -> Optional[Dict[str, Any]]:
    """Parse the model's JSON and coerce it into the intent vocabulary.

    Any missing or malformed field is simply dropped; a totally unparseable
    reply returns ``None`` so the deterministic intent stands.
    """
    payload = _extract_json_object(raw)
    if payload is None:
        logger.warning("LLM intent output was not JSON.")
        return None

    intent: Dict[str, Any] = {}

    category = str(payload.get("category") or "").strip().casefold()
    if category in VALID_CATEGORIES:
        intent["category"] = category

    budget = payload.get("budget")
    if isinstance(budget, dict):
        max_value = _coerce_budget(budget.get("max"))
        if max_value is not None:
            intent["budget"] = {"max": max_value, "currency": "INR"}

    brand = str(payload.get("brand") or "").strip()
    if brand:
        intent["brand"] = brand

    required = _coerce_features(payload.get("required_features"))
    if required:
        intent["required_features"] = required

    excluded = _coerce_features(payload.get("excluded_features"))
    if excluded:
        intent["excluded_features"] = excluded

    usage = str(payload.get("usage") or "").strip().casefold()
    if usage in VALID_USAGES:
        intent["usage"] = usage

    sort_pref = str(payload.get("sort_preference") or "").strip().casefold()
    if sort_pref in VALID_SORT_PREFERENCES:
        intent["sort_preference"] = sort_pref

    if not intent:
        logger.warning("LLM intent contained no usable fields.")
        return None
    return intent


def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None

    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)

    start = text.find("{")
    if start == -1:
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_budget(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        if not digits:
            return None
        value = int(digits)
    if not isinstance(value, (int, float)):
        return None
    amount = int(value)
    if amount <= 0 or amount > MAX_REASONABLE_BUDGET:
        return None
    return amount


def _coerce_features(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    cleaned: List[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        feature = item.strip().casefold().replace("_", "-")
        if feature in FEATURE_VOCABULARY and feature not in cleaned:
            cleaned.append(feature)
    return cleaned


def _merge_intents(
    deterministic: Dict[str, Any],
    llm_intent: Dict[str, Any],
    category_from_default: bool,
) -> tuple[Dict[str, Any], bool]:
    """Fill deterministic gaps from the model, keeping stated values intact.

    Returns:
        ``(merged_intent, llm_contributed)``.
    """
    merged = dict(deterministic)
    contributed = False

    llm_category = llm_intent.get("category")
    if llm_category and (
        category_from_default or not deterministic.get("category")
    ):
        merged["category"] = llm_category
        contributed = True

    if llm_intent.get("budget") and not merged.get("budget"):
        merged["budget"] = llm_intent["budget"]
        contributed = True

    if llm_intent.get("brand") and not merged.get("brand"):
        merged["brand"] = llm_intent["brand"]
        contributed = True

    if llm_intent.get("required_features") and not merged.get("required_features"):
        merged["required_features"] = llm_intent["required_features"]
        contributed = True

    if llm_intent.get("excluded_features") and not merged.get("excluded_features"):
        merged["excluded_features"] = llm_intent["excluded_features"]
        contributed = True

    if llm_intent.get("usage") and not merged.get("usage"):
        merged["usage"] = llm_intent["usage"]
        contributed = True

    if (
        llm_intent.get("sort_preference")
        and merged.get("sort_preference") == "rating"
        and llm_intent["sort_preference"] != "rating"
    ):
        merged["sort_preference"] = llm_intent["sort_preference"]
        contributed = True

    merged = _reconcile_boolean_features(merged)

    return _validate_intent(merged), contributed


def _reconcile_boolean_features(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Make feature tags agree with the explicit boolean signals.

    "Show me headphones without ANC" sets ``noise_cancellation=False``. If the
    model also proposed ``noise-cancelling`` as a required feature, honouring
    both is impossible, and the explicit user signal must win.
    """
    required = list(intent.get("required_features") or [])
    excluded = list(intent.get("excluded_features") or [])

    for key, feature in (
        ("wireless", "wireless"),
        ("noise_cancellation", "noise-cancelling"),
    ):
        value = intent.get(key)
        if not isinstance(value, bool):
            continue
        if value:
            excluded = [tag for tag in excluded if tag.casefold() != feature]
            if feature not in required:
                required.append(feature)
        else:
            required = [tag for tag in required if tag.casefold() != feature]
            if feature not in excluded:
                excluded.append(feature)

    intent["required_features"] = required
    intent["excluded_features"] = excluded
    return intent


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _validate_intent(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize and validate the intent against the schema."""
    validated: Dict[str, Any] = {
        "category": intent.get("category", "headphones"),
        "budget": intent.get("budget", {}),
        "brand": intent.get("brand", ""),
        "required_features": intent.get("required_features", []),
        "excluded_features": intent.get("excluded_features", []),
        "usage": intent.get("usage", ""),
        "sort_preference": intent.get("sort_preference", "rating"),
        "source": intent.get("source", "deterministic"),
    }

    if not isinstance(validated["required_features"], list):
        validated["required_features"] = []
    if not isinstance(validated["excluded_features"], list):
        validated["excluded_features"] = []

    budget = validated["budget"]
    if not isinstance(budget, dict):
        budget = {}
    budget.setdefault("currency", "INR")
    validated["budget"] = budget

    for boolean_key in ("wireless", "noise_cancellation"):
        if isinstance(intent.get(boolean_key), bool):
            validated[boolean_key] = intent[boolean_key]

    if validated["category"] not in VALID_CATEGORIES:
        validated["category"] = "headphones"

    if validated["sort_preference"] not in VALID_SORT_PREFERENCES:
        validated["sort_preference"] = "rating"

    if not isinstance(validated["brand"], str):
        validated["brand"] = ""

    return validated
