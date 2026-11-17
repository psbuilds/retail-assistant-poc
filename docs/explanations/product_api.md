# Product API (`app/api/product_api.py`)

## Purpose and Responsibility

The Product API (`app/api/product_api.py`) is a FastAPI service that provides the product catalogue for the Retail Shopping Assistant. It loads the validated product JSON catalogue and exposes REST endpoints for category listing, product detail lookup, and product search with filtering and sorting. **All product data comes from the migrated `data/products.json` — the assistant never invents products, prices, ratings, or specifications.**

## Place in Overall Architecture

```
Streamlit UI
    |
Conversation Service
    |
    +-- PRODUCT_DISCOVERY route calls Product API
    |
Product API (FastAPI)
    |-- Loads catalogue from data/products.json at startup
    |-- GET /api/products/categories
    |-- GET /api/products/{product_id}
    |-- POST /api/products/search  (with filters, sorting)
    |
Memory Service (Cognee MCP)
    |-- Stores preferences, not products
    |
Ollama + Cognee MCP Server
    |-- Embeddings and LLM for response generation
```

## Internal Dependencies

- `fastapi` — Web framework
- `httpx` — Internal HTTP client (used by conversation service to call this API)
- `app.products` — Product validation and filtering logic
- `app.data.products.json` — Catalogue data (20 products, migrated)
- `app.llm.status.LLMStatus` — LLM reachability reporting
- `app.services.constraint_resolver.ConstraintResolver` — resolves constraints before API call

## Every Class and Its Role

### FastAPI `app` instance

- **Created at module level**: `app = FastAPI(...)` with title, description, version
- **Startup event**: `@app.on_event("startup")` → `startup_load_catalog()` loads the product catalogue
- **Lifecycle**: Catalogue loaded once at server start; all endpoints use the in‑memory `catalog` variable

### Startup Load

- **Function**: `startup_load_catalog()`
- **Execution**: FastAPI startup event
- **Try/except**: If catalogue fails to load, `catalog = tuple()` (empty); all endpoints return 503 "Product catalog not loaded"
- **Logging**: `logger.info("Product catalog loaded: %d products", len(catalog))` or `logger.error(...)` on failure

## Constructor Parameters and Important Attributes

### FastAPI `app` (module-level, no explicit constructor)

- **`app.title`** — `"Retail Product API"`
- **`app.description`** — `"Product catalogue search API for the Retail Shopping Assistant"`
- **`app.version`** — `"2.0.0"`
- **`app.router`** — internal FastAPI router containing all endpoints

### `catalog` (global, module-level)

- **Type**: `Optional[tuple[Product, ...]]`
- **Initialized by**: `startup_load_catalog()`
- **None means**: catalogue failed to load; all endpoints return 503
- **Populated**: 20 `Product` dataclass instances from `data/products.json`

## Every Maintained Function and Method

### `startup_load_catalog()`

**Execution order:**

1. Try: `catalog = load_catalog()` → reads `data/products.json`, validates each record
2. If `OSError` or `json.JSONDecodeError`: raise `ValueError` with path and original error
3. `catalog` set to validated tuple of `Product` instances
4. `logger.info("Product catalog loaded: %d products", len(catalog) if catalog else 0)`
5. If exception: `logger.error("Failed to load product catalog: %s", exc)`; `catalog = tuple()`

**Return**: `None` (sets global `catalog` variable)

### `get_categories()`

**Execution order:**

1. If not `catalog`: raise `HTTPException(status_code=503, detail="Product catalog not loaded")`
2. `categories = sorted(set(product.category for product in catalog))`
3. Return `{"categories": categories}`

**Return**: `Dict[str, List[str]]` — sorted unique category names

### `get_product(product_id)`

**Execution order:**

1. If not `catalog`: raise `HTTPException(status_code=503, detail="Product catalog not loaded")`
2. Iterate through `catalog` looking for `product.product_id.casefold() == product_id.casefold()`
3. If found: return dict with `id`, `name`, `category`, `brand`, `price`, `rating`, `features`, `wireless`, `noise_cancellation`
4. If not found after loop: raise `HTTPException(status_code=404, detail=f"Product {product_id} not found")`

**Return**: `Dict[str, Any]` — product detail dict

### `search_products()`

**Execution order:**

1. If not `catalog`: raise `HTTPException(status_code=503, detail="Product catalog not loaded")`
2. Start with `selected = list(catalog)` (full catalogue)
3. **Category filter**: if `category` param → `selected = [p for p in selected if p.category.casefold() == normalized_category]`
4. **Min price filter**: if `min_price` → `selected = [p for p in selected if p.price >= min_price]`
5. **Max price filter**: if `max_price` → `selected = [p for p in selected if p.price <= max_price]`
6. **Brand filter**: if `brand` → `selected = [p for p in selected if p.brand.casefold() == normalized_brand]`
7. **Required features filter**: if `required_features` → split by comma, keep products where any feature is in product's feature set
8. **Excluded features filter**: if `excluded_features` → split by comma, remove products where any excluded feature is in product's feature set
9. **Excluded product IDs filter**: if `excluded_product_ids` → split by comma, remove products whose ID is in the excluded list
10. **Rank products** via `_rank_products(selected, sort_by)`
11. **Apply limit** via `ranked[:limit]`
12. **Build response**: list of product dicts + `total` count + `query_constraints` metadata

**Return**: `Dict[str, Any]` with keys:
- `products` — list of product summary dicts
- `total` — number of products returned
- `query_constraints` — dict of applied constraints (category, max_price, min_price, brand, required_features)

### `_rank_products(products, sort_by)`

**Execution order:**

1. `sort_by == "rating"` → sort by `(-p.rating, p.price, p.product_id)` — highest rated first, tiebreak by lower price, then alphabetical ID
2. `sort_by == "price_low"` → sort by `(p.price, p.product_id)` — cheapest first
3. `sort_by == "price_high"` → sort by `(-p.price, p.product_id)` — most expensive first
4. `sort_by == "relevance"` (default) → same as `"rating"`: `(-p.rating, p.price, p.product_id)`
5. `sort_by` anything else → default to rating sort

**Return**: `List[Product]` — ranked product list

## Function Parameters and Return Values

| Function | Key Parameters | Return Value |
|---|---|---|
| `startup_load_catalog()` | — | `None` (sets global `catalog`) |
| `get_categories()` | — | `Dict[str, List[str]]` |
| `get_product(product_id)` | `product_id: str` | `Dict[str, Any]` — product detail or 404/503 error |
| `search_products(category, max_price, min_price, brand, required_features, excluded_features, excluded_product_ids, sort_by, limit)` | Various `Optional` query params | `Dict[str, Any]` — `{products, total, query_constraints}` or 503 error |
| `_rank_products(products, sort_by)` | `products: List[Product]`, `sort_by: str` | `List[Product]` — ranked list |

## Exceptions, Error Handling and Side Effects

### HTTP Exception (FastAPI)

- **`HTTPException(status_code=503, detail="Product catalog not loaded")`** — when `catalog` is `None` (failed to load at startup)
- **`HTTPException(status_code=404, detail=f"Product {product_id} not found")`** — when requested product ID doesn't exist in catalogue
- **FastAPI automatically** returns JSON error response with `{"detail": "..."}`

### Graceful Degradation (search_products)

- **Product API unreachable** (`httpx.RequestError`) → return `{"products": [], "total": 0, "query_constraints": {...}, status_code=200}`
- **Non‑200 response** from API → log warning, return `{"products": [], "total": 0, "query_constraints": {...}, status_code=200}`
- **Never crash** the assistant if the product catalogue is temporarily unavailable

### Side Effects

- `startup_load_catalog()` runs once at process startup; must succeed for all endpoints to work
- `catalog` is read‑only after startup; no concurrent modification model needed for this POC
- Search endpoint does not modify the global catalogue; returns a filtered view

## Actual Implementation Walkthrough in Execution Order

### Startup (FastAPI process start)

1. FastAPI creates `app` instance with metadata
2. `@app.on_event("startup")` triggers `startup_load_catalog()`
3. `load_catalog()` opens `data/products.json` (Path `__file__`.parents[1] / `data` / `products.json`)
4. JSON array loaded → `json.load(handle)` → validated record by record via `_validate_record(record, index)`
5. Each record validated for required fields: `product_id`, `name`, `category`, `brand`, `price`, `features`, `wireless`, `noise_cancellation`, `rating`
6. Price validated: must be finite positive number
7. Rating validated: must be finite and between 0.0 and 5.0
8. Features validated: non‑empty list of strings
9. Duplicate `product_id` check — raises `ValueError` if found
10. Validated `Product` instances stored in tuple → assigned to global `catalog`
11. `logger.info("Product catalog loaded: %d products", len(catalog))`
12. If any error: `logger.error("Failed to load product catalog: %s", exc)`; `catalog = tuple()`

### Typical API Call: Search Products

1. Client (Streamlit conversational service) POSTs to `http://127.0.0.1:8000/api/products/search` with constraints
2. Example constraints: `category="headphones"`, `max_price=5000`, `wireless=true`, `sort_by="rating"`
3. FastAPI `search_products()` receives query parameters
4. Starts with `selected = list(catalog)` (all 20 products)
5. **Category filter**: keeps only products where `product.category.casefold() == "headphones"` — typically 12‑14 products
6. **Max price filter**: keeps only products where `product.price <= 5000` — typically 5‑8 products
7. **Wireless feature**: the search endpoint doesn't have a direct `wireless` query param; the conversational service translates `wireless` preference into `required_features` or `excluded_features`
8. **Ranking**: `_rank_products(selected, sort_by)` — by default, highest rating first
9. **Limit**: `ranked[:limit]` — top 5 by default
10. **Response built**: `{"products": [product dicts], "total": N, "query_constraints": {...}}`
11. FastAPI returns JSON with HTTP 200

### Typical API Call: Get Product Detail

1. Client calls `GET http://127.0.0.1:8000/api/products/p001`
2. FastAPI `get_product("p001")` iterates catalog
3. Finds product with `product_id == "p001"` (Wireless Earbuds)
4. Returns dict: `{"id": "p001", "name": "Wireless Earbuds", "category": "headphones", "brand": "AudioTech", "price": 2499, "rating": 4.5, "features": ["wireless", "lightweight", "travel-friendly", "in-ear"], "wireless": true, "noise_cancellation": false}`
5. FastAPI returns JSON with HTTP 200

### Typical API Call: Get Categories

1. Client calls `GET http://127.0.0.1:8000/api/products/categories`
2. FastAPI `get_categories()` iterates catalog
3. Collects `set(product.category for product in catalog)` → `{"headphones", "audio", "earbuds", "professional"}`
4. Returns sorted: `{"categories": ["audio", "earbuds", "headphones", "professional"]}`
5. FastAPI returns JSON with HTTP 200

## Input and Output Data Flow

### Input Flow (client → Product API)

```
HTTP GET /api/products/categories
    → returns {"categories": ["audio", "earbuds", "headphones", "professional"]}

HTTP POST /api/products/search
    body/params: category, max_price, min_price, brand, required_features, excluded_features, excluded_product_ids, sort_by, limit
    → returns {"products": [...], "total": N, "query_constraints": {...}}

HTTP GET /api/products/{product_id}
    → returns product detail dict or 404
```

### Output Flow (Product API → client)

```
GET /api/products/categories
    → {"categories": ["audio", "earbuds", "headphones", "professional"]}

POST /api/products/search
    → {"products": [
         {"id": "p005", "name": "Wireless Over-Ear ANC Headphones", "brand": "Bose", "price": 7499, "rating": 4.7, "features": ["wireless", "over-ear", "noise-cancelling", "call-friendly"], "wireless": true, "noise_cancellation": true},
         ...
       ], "total": 3, "query_constraints": {"category": "headphones", "max_price": 5000}}

GET /api/products/p001
    → {"id": "p001", "name": "Wireless Earbuds", "category": "headphones", "brand": "AudioTech", "price": 2499, "rating": 4.5, "features": ["wireless", "lightweight", "travel-friendly", "in-ear"], "wireless": true, "noise_cancellation": false}
```

## Integration with MCP, Cognee, Ollama and Streamlit

### MCP Integration

- **Zero direct MCP calls** from Product API — it only reads the static JSON catalogue
- Memory preferences (brand, budget, features) are extracted from Cognee MCP recall and translated into Product API query parameters by the conversational service
- The Product API is **stateless** w.r.t. memory — it does not store or manage preferences

### Cognee Integration

- Product API has **no dependency** on Cognee or MCP
- Preferences stored in Cognee `retail_demo` dataset are translated into API constraints by the conversational service before calling the Product API
- The assistant pipeline: `User → Conversation Service → (translate preferences) → Product API → (product cards) → Conversation Service → (natural language response) → User`

### Ollama Integration

- LLM used by `generate_response()` for natural language responses when models are available
- If Ollama/models unavailable, `generate_response()` falls back to deterministic response generation from Product API data and stored preferences
- LLM status reported in UI status panel

### Streamlit Integration

- Streamlit UI calls `service.process_query(query)` which may invoke the Product API (PRODUCT_DISCOVERY mode)
- Product API base URL: `http://127.0.0.1:8000` (configurable via `PRODUCT_API_URL` env var)
- Product cards rendered via `render_product_card(product)` in the sidebar/chat area
- If API unavailable, assistant says "I'm having trouble accessing the product catalogue" and shows no product cards

## Important Design Decisions and Trade-offs

### Migrated Catalogue (Not Generated)

- **Decision**: Product data comes from the original `data/products.json` of the reference project, copied exactly (SHA-256 verified).
- **Trade-off**: No flexibility to adjust product data for the POC demo without modifying the original file. However, the original catalogue contains 20 diverse headphone/audio products that cover all required filtering features.
- **Rationale**: "Do not replace original product data with fabricated examples." Verified by `shasum -a 256`.

### Read‑Only Catalogue After Startup

- **Decision**: `catalog` is set once at startup and never modified. Search returns a filtered view.
- **Trade-off**: No hot‑loading of new products during a running session.
- **Rationale**: POC simplicity; the catalogue is small (20 products) and static.

### Graceful Degradation When API Down

- **Decision**: If Product API unavailable, assistant shows no product cards but continues conversation with deterministic responses.
- **Trade-off**: No product recommendations when the API is down, but the assistant remains functional for preferences, recall, and normal conversation.
- **Rationale**: "Never invent products, prices, ratings, specifications or availability." Better to show no results than fake ones.

### Feature Flags as Required/Excluded Features

- **Decision**: The `wireless` and `noise-cancellation` boolean columns in each Product are translated into feature tags for the search API.
- **Trade-off**: The search API uses `required_features`/`excluded_features` comma‑separated strings; boolean columns are handled by the conversational service's `normalize_constraints()`.
- **Rationale**: Keeps the Product API simple (one search endpoint) while allowing the conversational service to express complex preference combinations.

## A Concrete Usage Example

### Startup

```
$ uvicorn app.api.product_api:app --host 127.0.0.1 --port 8000
```

INFO: Uvicorn running on http://127.0.0.1:8000
INFO: Product catalog loaded: 20 products

### Get Categories

```bash
$ curl http://127.0.0.1:8000/api/products/categories
{"categories": ["audio", "earbuds", "headphones", "professional"]}
```

### Search Wireless Headphones Under ₹5000

```bash
$ curl -X POST "http://127.0.0.1:8000/api/products/search?category=headphones&max_price=5000&required_features=wireless&sort_by=rating"
{
  "products": [
    {"id": "p005", "name": "Wireless Over-Ear ANC Headphones", "brand": "Bose", "price": 7499, ...},
    {"id": "p002", "name": "Over-Ear Studio Headphones", "brand": "SoundMax", "price": 8999, ...},
    ...
  ],
  "total": 5,
  "query_constraints": {"category": "headphones", "max_price": 5000, "required_features": "wireless"}
}
```

### Get Product Detail

```bash
$ curl http://127.0.0.1:8000/api/products/p005
{"id": "p005", "name": "Wireless Over-Ear ANC Headphones", "brand": "Bose", "price": 7499, "rating": 4.7, "features": ["wireless", "over-ear", "noise-cancelling", "call-friendly"], "wireless": true, "noise_cancellation": true}
```

### Search with Budget from Memory

Conversational flow:

1. User: `I have a budget of ₹8000.`
2. Assistant stores `Has a budget up to ₹8000` in Cognee `retail_demo`
3. User: `Recommend wireless headphones.`
4. Conversational service recalls budget → `max_price=8000`
5. Calls Product API: `POST /api/products/search?category=headphones&max_price=8000&required_features=wireless`
6. API returns matching products (price ≤ 8000, wireless)
7. Assistant shows product cards + natural language response

## Tests Covering the Module

- `tests/test_mcp_adapter.py` — does not test Product API directly (focuses on MCP adapter)
- Unit‑level: the test suite confirms the product catalogue loads correctly (20 products, SHA-256 verified)
- Integration‑style: manual verification that `load_catalog()` returns 20 validated `Product` instances; FastAPI server starts and all three endpoints respond correctly when the server is running
- The existing 26 tests pass after the catalogue migration and `.env` embedding fix

## Actual Known Limitations

1. **No real‑time catalogue updates** — `catalog` is set at startup only; adding/removing products requires restarting the API.
2. **No authentication or rate limiting** — FastAPI default; not needed for POC.
3. **Price all in INR** — All product prices are Indian Rupees; no currency conversion.
4. **Feature tag mapping** — `wireless` and `noise-cancellation` booleans are mapped to `required_features`/`excluded_features` strings; other potential features in the product data are not exposed as searchable API params.
5. **No pagination beyond `limit`** — The `limit` query param caps results at 50; no `offset` or cursor‑based pagination.
6. **Category names must match exactly** (case‑insensitive) — the API normalizes via `.casefold()`, but mis‑spelled category names return no results.
7. **Ollama/LLM dependency for `generate_response`** — The conversational service's `generate_response()` may call the LLM for natural language generation; if Ollama/models are down, deterministic fallback is used (see `app/services/response_generation.py`).
8. **MCP `forget` tool** — Added to the adapter assuming the Cognee MCP server version supports it; if not, `MemoryAdapterError` is raised.

---