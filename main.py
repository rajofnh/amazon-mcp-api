# ============================================
# main.py — Amazon MCP REST API
#
# This is the REST API that sits between your
# MCP server and SerpAPI. It contains the
# exact same filtering logic as your Streamlit
# app but exposed as a proper REST endpoint
# that Claude and headless agents can call.
#
# Endpoints:
#   GET  /health       — health check
#   POST /search       — search Amazon products
#   GET  /product/{asin} — get one product
#   GET  /search-link  — generate bulk Amazon URL
# ============================================

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import requests
import os
import logging
import time
from dotenv import load_dotenv
from auth import validate_token, AuthenticatedUser, generate_test_token

# Load environment variables from .env file
load_dotenv()

# ============================================
# CONCEPT: Structured logging
#
# We use Python's built-in logging module
# instead of print() statements. This gives us
# timestamps, log levels, and structured output
# that works correctly with cloud platforms
# like Render. Every log line will appear in
# Render's dashboard log viewer in real time.
# ============================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ"
)
logger = logging.getLogger(__name__)

# ============================================
# Configuration
# ============================================

SERPAPI_KEY = os.getenv("SERPAPI_KEY")
MIN_RATING = 4.5
MIN_REVIEWS = 1000

if not SERPAPI_KEY:
    logger.error("SERPAPI_KEY environment variable is not set")

# ============================================
# FastAPI app setup
# ============================================

app = FastAPI(
    title="Amazon MCP API",
    description="REST API for Amazon product search with quality filtering. "
                "Powers the Amazon MCP server for Claude integration.",
    version=os.getenv("API_VERSION", "1.0.0")
)

# Allow cross-origin requests
# This lets your MCP server call this API
# from a different port or domain
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ====== The following 8 lines to added so that localhost:8000 doesn't give error. from Rajeev
@app.get("/")
async def root():
    return {
        "service": "Amazon MCP API",
        "version": "1.0.0",
        "docs": "http://localhost:8000/docs",
        "health": "http://localhost:8000/health"
    }

# ============================================
# Request and Response Models
#
# CONCEPT: Pydantic models
# FastAPI uses Pydantic to define the shape
# of request and response data. This gives
# you automatic validation — if a required
# field is missing or the wrong type, FastAPI
# returns a clear error automatically.
# ============================================

class SearchRequest(BaseModel):
    query: str
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    limit: Optional[int] = 10

class Product(BaseModel):
    asin: Optional[str] = None
    title: Optional[str] = None
    rating: Optional[float] = None
    reviews: Optional[int] = None
    price: Optional[float] = None
    price_string: Optional[str] = None
    thumbnail: Optional[str] = None
    link: Optional[str] = None
    is_prime: Optional[bool] = False

class SearchResponse(BaseModel):
    query: str
    total_found: int
    products: List[Product]
    amazon_search_link: str
    criteria_applied: dict

# ============================================
# Core search function
# This is the exact same logic as your
# Streamlit app — just extracted into a
# reusable function that our endpoints call
# ============================================

def call_serpapi(query: str) -> list:
    """
    Call SerpAPI's Amazon engine.
    Returns raw results list or raises an exception.
    """
    if not SERPAPI_KEY:
        raise HTTPException(
            status_code=500,
            detail="API key not configured. Contact administrator."
        )

    params = {
        "engine": "amazon",
        "k": query,
        "amazon_domain": "amazon.com",
        "api_key": SERPAPI_KEY
    }

    try:
        response = requests.get(
            "https://serpapi.com/search",
            params=params,
            timeout=15
        )
        data = response.json()

        if "error" in data:
            raise HTTPException(
                status_code=502,
                detail=f"SerpAPI error: {data['error']}"
            )

        return data.get("organic_results", [])

    except requests.Timeout:
        raise HTTPException(
            status_code=504,
            detail="Search timed out. Please try again."
        )
    except requests.RequestException as e:
        raise HTTPException(
            status_code=502,
            detail=f"Search service unavailable: {str(e)}"
        )


def extract_price(price_data) -> float:
    """
    Safely extract a numeric price from
    whatever format SerpAPI returns it in.
    This is the same price extraction logic
    from your Streamlit app.
    """
    if price_data is None:
        return 0.0
    if isinstance(price_data, dict):
        return float(price_data.get("value", 0))
    if isinstance(price_data, (int, float)):
        return float(price_data)
    if isinstance(price_data, str):
        try:
            return float(
                price_data.replace("$", "").replace(",", "").strip()
            )
        except ValueError:
            return 0.0
    return 0.0


def filter_products(
    results: list,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None
) -> List[Product]:
    """
    Apply your quality criteria to raw SerpAPI results.
    Criteria: rating >= 4.5, reviews >= 1000, price in range.
    Returns a list of Product objects that passed all criteria.
    """
    matches = []

    for item in results:
        rating = item.get("rating", 0) or 0
        reviews = item.get("reviews", 0) or 0
        price_val = extract_price(item.get("price"))

        # Apply your hardcoded quality criteria
        if rating < MIN_RATING:
            continue
        if reviews < MIN_REVIEWS:
            continue

        # Apply optional price range
        if min_price and min_price > 0 and price_val < min_price:
            continue
        if max_price and max_price > 0 and price_val > max_price:
            continue

        # Build clean product object
        product = Product(
            asin=item.get("asin"),
            title=item.get("title"),
            rating=rating,
            reviews=reviews,
            price=price_val if price_val > 0 else None,
            price_string=f"${price_val:.2f}" if price_val > 0 else "Price unavailable",
            thumbnail=item.get("thumbnail"),
            link=item.get("link") or (
                f"https://www.amazon.com/dp/{item.get('asin')}"
                if item.get("asin") else None
            ),
            is_prime=item.get("is_prime", False)
        )
        matches.append(product)

    # Sort by rating descending, then reviews descending
    matches.sort(key=lambda x: (x.rating or 0, x.reviews or 0), reverse=True)
    return matches


def build_amazon_search_link(query: str, asins: List[str]) -> str:
    """
    Generate the bulk Amazon search URL that shows
    only the products that passed the criteria.
    This is the same link your Streamlit app generates.
    """
    if asins:
        asin_query = "|".join(asins)
        return (
            f"https://www.amazon.com/s?"
            f"k={query.replace(' ', '+')}"
            f"&rh=p_78%3A{asin_query}"
        )
    return f"https://www.amazon.com/s?k={query.replace(' ', '+')}"

# ---- Token generator endpoint ----
# CONCEPT: This endpoint generates a valid
# test JWT for our MCP server and headless
# agent to use. In production this would be
# replaced by Auth0's token endpoint.
# Protected by a simple admin secret so
# only authorised clients can get tokens.

@app.get("/token")
async def get_test_token(secret: str = Query(...)):
    admin_secret = os.getenv("ADMIN_SECRET", "admin-secret-2024")

    if secret != admin_secret:
        raise HTTPException(
            status_code=401,
            detail="Invalid admin secret"
        )

    token = generate_test_token()
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": 86400,
        "scope": "amazon:search amazon:read",
        "note": "Test token — replace with Auth0 in Stage 6"
    }

# ============================================
# ENDPOINTS
# ============================================

# ---- Endpoint 1: Health check ----
# CONCEPT: Every production API has a health
# check endpoint. Monitoring systems ping this
# every 60 seconds to confirm the service is
# alive. Render uses this to know if your
# service is healthy. Returns 200 OK when good.

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "version": os.getenv("API_VERSION", "1.0.0"),
        "service": "amazon-mcp-api",
        "serpapi_configured": bool(SERPAPI_KEY)
    }


# ---- Endpoint 2: Search Amazon products ----
# This is the main endpoint. It takes a query
# and optional price range, calls SerpAPI,
# filters results by your quality criteria,
# and returns structured product data.

# ---- @app.post("/search", response_model=SearchResponse)
# ---- async def search_products(request: SearchRequest):
@app.post("/search", response_model=SearchResponse)
async def search_products(
    request: SearchRequest,
    user: AuthenticatedUser = Depends(validate_token)
):

    start_time = time.time()

    logger.info(
    f"[SEARCH] query='{request.query}' "
    f"user={user.email} "
    f"tenant={user.tenant_id} "
    f"min_price={request.min_price} "
    f"max_price={request.max_price}"
)


    # Call SerpAPI
    raw_results = call_serpapi(request.query)
    logger.info(f"[SEARCH] SerpAPI returned {len(raw_results)} raw results")

    # Filter by your quality criteria
    filtered = filter_products(
        raw_results,
        request.min_price,
        request.max_price
    )

    # Apply limit
    limited = filtered[:request.limit] if request.limit else filtered

    # Build the bulk Amazon search link
    asins = [p.asin for p in limited if p.asin]
    amazon_link = build_amazon_search_link(request.query, asins)

    duration = round((time.time() - start_time) * 1000)

    logger.info(
        f"[SEARCH] query='{request.query}' "
        f"found={len(limited)} "
        f"duration={duration}ms"
    )

    return SearchResponse(
        query=request.query,
        total_found=len(limited),
        products=limited,
        amazon_search_link=amazon_link,
        criteria_applied={
            "min_rating": MIN_RATING,
            "min_reviews": MIN_REVIEWS,
            "min_price": request.min_price,
            "max_price": request.max_price
        }
    )


# ---- Endpoint 3: Get one product by ASIN ----
# Takes an Amazon ASIN and returns full
# details for that specific product.
# Called by the get_product_details MCP tool.

# --- @app.get("/product/{asin}")
# --- async def get_product(asin: str):
@app.get("/product/{asin}")
async def get_product(
    asin: str,
    user: AuthenticatedUser = Depends(validate_token)
):


    logger.info(f"[PRODUCT] Looking up ASIN: {asin}")

    # Search Amazon for this specific ASIN
    raw_results = call_serpapi(asin)

    # Find the matching product
    for item in raw_results:
        if item.get("asin") == asin:
            price_val = extract_price(item.get("price"))
            return {
                "asin": asin,
                "title": item.get("title"),
                "rating": item.get("rating"),
                "reviews": item.get("reviews"),
                "price": price_val,
                "price_string": f"${price_val:.2f}" if price_val > 0 else "Price unavailable",
                "link": f"https://www.amazon.com/dp/{asin}",
                "thumbnail": item.get("thumbnail"),
                "is_prime": item.get("is_prime", False)
            }

    raise HTTPException(
        status_code=404,
        detail=f"Product with ASIN {asin} not found"
    )


# ---- Endpoint 4: Generate search link ----
# Generates the bulk Amazon search URL
# for a list of ASINs.

@app.get("/search-link")
async def get_search_link(
    query: str = Query(..., description="Product search query"),
    asins: str = Query("", description="Comma-separated list of ASINs")
):
    asin_list = [a.strip() for a in asins.split(",") if a.strip()]
    link = build_amazon_search_link(query, asin_list)
    return {
        "query": query,
        "asin_count": len(asin_list),
        "amazon_search_link": link
    }