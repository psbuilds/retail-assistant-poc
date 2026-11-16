"""Product API service - FastAPI-based service exposing product catalogue search endpoints.

Provides REST endpoints for product search, filtering, and detail retrieval.
The catalogue is loaded from the validated product JSON file.

Endpoints:
- GET /api/products/categories - List available product categories
- GET /api/products/{product_id} - Get product detail by ID
- POST /api/products/search - Search products with filters and sorting
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from app.products import (
    filter_by_category,
    filter_by_max_budget,
    filter_by_wireless,
    filter_by_noise_cancellation,
    filter_by_brand,
    filter_by_required_features,
    filter_by_excluded_features,
    filter_by_excluded_product_ids,
    load_catalog,
    Product,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Retail Product API",
    description="Product catalogue search API for the Retail Shopping Assistant",
    version="2.0.0",
)

# Load catalog at startup
catalog: Optional[tuple[Product, ...]] = None


@app.on_event("startup")
async def startup_load_catalog():
    global catalog
    try:
        catalog = load_catalog()
        logger.info("Product catalog loaded: %d products", len(catalog) if catalog else 0)
    except Exception as exc:
        logger.error("Failed to load product catalog: %s", exc)
        catalog = tuple()


# --- Category endpoint ---

@app.get("/api/products/categories", include_in_schema=True)
async def get_categories() -> Dict[str, List[str]]:
    """Get list of available product categories.

    Returns:
        Dict with category list keyed by category type
    """
    if not catalog:
        raise HTTPException(status_code=503, detail="Product catalog not loaded")

    categories = sorted(set(product.category for product in catalog))
    return {"categories": categories}


# --- Product detail endpoint ---

@app.get("/api/products/{product_id}", include_in_schema=True)
async def get_product(product_id: str) -> Dict[str, Any]:
    """Get product detail by product ID.

    Args:
        product_id: The product identifier (e.g., "p001")

    Returns:
        Product details dict

    Raises:
        HTTPException: 404 if product not found
    """
    if not catalog:
        raise HTTPException(status_code=503, detail="Product catalog not loaded")

    for product in catalog:
        if product.product_id.casefold() == product_id.casefold():
            return {
                "id": product.product_id,
                "name": product.name,
                "category": product.category,
                "brand": product.brand,
                "price": product.price,
                "rating": product.rating,
                "features": list(product.features),
                "wireless": product.wireless,
                "noise_cancellation": product.noise_cancellation,
            }

    raise HTTPException(status_code=404, detail=f"Product {product_id} not found")


# --- Search endpoint ---

@app.post("/api/products/search", include_in_schema=True)
async def search_products(
    category: Optional[str] = Query(
        None,
        description="Product category filter (headphones, earbuds, audio, professional, footwear, apparel, bags)",
    ),
    max_price: Optional[int] = Query(
        None, description="Maximum price filter (inclusive, in INR)", ge=0
    ),
    min_price: Optional[int] = Query(
        None, description="Minimum price filter (inclusive, in INR)", ge=0
    ),
    brand: Optional[str] = Query(
        None, description="Brand filter exact match (case-insensitive)",
    ),
    required_features: Optional[str] = Query(
        None,
        description="Comma-separated list of features that must be present",
    ),
    excluded_features: Optional[str] = Query(
        None,
        description="Comma-separated list of features that must NOT be present",
    ),
    excluded_product_ids: Optional[str] = Query(
        None,
        description="Comma-separated list of product IDs to exclude",
    ),
    sort_by: Optional[str] = Query(
        "rating",
        description="Sort by: rating, price_low, price_high, relevance",
    ),
    limit: int = Query(
        5, ge=1, le=50, description="Maximum number of results to return",
    ),
) -> Dict[str, Any]:
    """Search products with filtered constraints.

    Returns matching products ranked by the specified sort preference.
    Returns empty results if no products match the criteria rather than unrelated products.

    Args:
        category: Product category filter
        max_price: Maximum price (inclusive)
        min_price: Minimum price (inclusive)
        brand: Brand name filter
        required_features: Comma-separated features that must be present
        excluded_features: Comma-separated features that must NOT be present
        excluded_product_ids: Comma-separated product IDs to exclude
        sort_by: Sort preference (rating, price_low, price_high, relevance)
        limit: Maximum number of results

    Returns:
        Dict with products list and metadata
    """
    if not catalog:
        raise HTTPException(status_code=503, detail="Product catalog not loaded")

    # Start with full catalog
    selected = list(catalog)

    # Apply category filter (most important constraint)
    if category:
        normalized_category = category.casefold()
        selected = [p for p in selected if p.category.casefold() == normalized_category]

    # Apply budget filters
    if min_price is not None:
        selected = [p for p in selected if p.price >= min_price]
    if max_price is not None:
        selected = [p for p in selected if p.price <= max_price]

    # Apply brand filter
    if brand:
        normalized_brand = brand.casefold()
        selected = [p for p in selected if p.brand.casefold() == normalized_brand]

    # Apply required features filter
    if required_features:
        feat_list = [f.strip().casefold() for f in required_features.split(",") if f.strip()]
        selected = [
            p for p in selected
            if any(f.casefold() in _feature_set(p) for f in feat_list)
        ]

    # Apply excluded features filter
    if excluded_features:
        excl_list = [f.strip().casefold() for f in excluded_features.split(",") if f.strip()]
        selected = [
            p for p in selected
            if not any(f.casefold() in _feature_set(p) for f in excl_list)
        ]

    # Apply excluded product IDs
    if excluded_product_ids:
        excl_ids = [id.strip().casefold() for id in excluded_product_ids.split(",") if id.strip()]
        selected = [p for p in selected if p.product_id.casefold() not in excl_ids]

    # Rank products
    ranked = _rank_products(selected, sort_by)

    # Apply limit
    limited = ranked[:limit]

    # Build response
    products = [
        {
            "id": product.product_id,
            "name": product.name,
            "category": product.category,
            "brand": product.brand,
            "price": product.price,
            "rating": product.rating,
            "features": [f for f in product.features],
            "wireless": product.wireless,
            "noise_cancellation": product.noise_cancellation,
        }
        for product in limited
    ]

    return {
        "products": products,
        "total": len(limited),
        "query_constraints": {
            "category": category,
            "max_price": max_price,
            "min_price": min_price,
            "brand": brand,
            "required_features": (
                [f.strip() for f in required_features.split(",") if f.strip()]
                if required_features
                else []
            ),
            "excluded_features": (
                [f.strip() for f in excluded_features.split(",") if f.strip()]
                if excluded_features
                else []
            ),
        },
    }


def _feature_set(product: Product) -> set[str]:
    """Get the case-folded feature set of a product."""
    return {f.casefold() for f in product.features}


def _rank_products(
    products: List[Product], sort_by: str
) -> List[Product]:
    """Rank products by the specified sort preference.

    Ranking signals (in order):
    1. Rating (descending) - highest rated first
    2. Price (ascending for price_low, descending for price_high)
    3. Product ID (alphabetical, tiebreaker)

    Mandatory filters are never violated - only products that pass all filters are ranked.
    """
    if sort_by == "price_low":
        return sorted(products, key=lambda p: (p.price, p.product_id))
    elif sort_by == "price_high":
        return sorted(products, key=lambda p: (-p.price, p.product_id))
    elif sort_by == "relevance":
        # Default: rating desc, price asc, ID alpha
        return sorted(products, key=lambda p: (-p.rating, p.price, p.product_id))
    else:
        # Default to rating sort
        return sorted(products, key=lambda p: (-p.rating, p.price, p.product_id))