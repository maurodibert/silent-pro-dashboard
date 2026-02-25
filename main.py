"""
Silent Pro Dashboard - FastAPI Backend
"""
import os
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sp_api.api import Orders
from sp_api.base import Marketplaces
from dotenv import load_dotenv
from collections import defaultdict

logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = FastAPI(title="Silent Pro Dashboard")

# Configuration
CONFIG = {
    "LWA_CLIENT_ID": os.getenv("LWA_CLIENT_ID"),
    "LWA_CLIENT_SECRET": os.getenv("LWA_CLIENT_SECRET"),
    "REFRESH_TOKEN": os.getenv("REFRESH_TOKEN"),
    "AWS_ACCESS_KEY": os.getenv("SP_AWS_ACCESS_KEY"),
    "AWS_SECRET_KEY": os.getenv("SP_AWS_SECRET_KEY"),
    "SP_API_ROLE_ARN": os.getenv("SP_API_ROLE_ARN"),
    "MARKETPLACE_ID": os.getenv("MARKETPLACE_ID", "ATVPDKIKX0DER"),
    "DAY_START_HOUR_UTC": 8,  # 5am ART = 8am UTC
}

PRODUCTS = {
    "VM-7EA4-DVAO": "Black Mamba Premium",
    "5Y-T9K7-1HM1": "Black Mamba Lite",
    "J9-H173-J5AF": "Old School Mini",
}


def get_credentials():
    return {
        "refresh_token": CONFIG["REFRESH_TOKEN"],
        "lwa_app_id": CONFIG["LWA_CLIENT_ID"],
        "lwa_client_secret": CONFIG["LWA_CLIENT_SECRET"],
        "aws_access_key": CONFIG["AWS_ACCESS_KEY"],
        "aws_secret_key": CONFIG["AWS_SECRET_KEY"],
        "role_arn": CONFIG["SP_API_ROLE_ARN"],
    }


def get_business_day_start_utc(days_back: int = 0) -> datetime:
    """Get business day start in UTC. Business day: 5am ART = 8am UTC"""
    now = datetime.now(timezone.utc)

    # If before 8am UTC, we're in yesterday's business day
    if now.hour < CONFIG["DAY_START_HOUR_UTC"]:
        days_back += 1

    start = datetime(
        now.year, now.month, now.day,
        CONFIG["DAY_START_HOUR_UTC"], 0, 0,
        tzinfo=timezone.utc
    ) - timedelta(days=days_back)

    return start


def date_string_to_utc(date_str: str, is_end: bool = False) -> datetime:
    """Convert YYYY-MM-DD (Argentina) to UTC"""
    parts = date_str.split("-")
    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])

    base = datetime(year, month, day, CONFIG["DAY_START_HOUR_UTC"], 0, 0, tzinfo=timezone.utc)
    if is_end:
        # End of business day = next day at 8am UTC
        return base + timedelta(days=1)
    return base


def get_argentina_date(utc_dt: datetime) -> str:
    """Convert UTC to Argentina date string"""
    art_time = utc_dt - timedelta(hours=3)
    return art_time.strftime("%Y-%m-%d")


def get_current_argentina_date() -> str:
    """Get current date in Argentina (considering 5am boundary)"""
    now = datetime.now(timezone.utc)
    art_time = now - timedelta(hours=3)

    # If before 5am ART, still "yesterday"
    if art_time.hour < 5:
        art_time -= timedelta(days=1)

    return art_time.strftime("%Y-%m-%d")


class OrdersRequest(BaseModel):
    days_back: int = 0
    product_sku: str = "ALL"
    start_date: Optional[str] = None
    end_date: Optional[str] = None


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/api/products")
async def get_products():
    return [
        {"sku": "ALL", "name": "All Products"},
        {"sku": "5Y-T9K7-1HM1", "name": "Black Mamba Lite"},
        {"sku": "VM-7EA4-DVAO", "name": "Black Mamba Premium"},
        {"sku": "J9-H173-J5AF", "name": "Old School Mini"},
    ]


def _fetch_orders_sync(request: OrdersRequest):
    """Run SP-API calls in a thread to avoid blocking the async event loop."""
    credentials = get_credentials()
    orders_api = Orders(credentials=credentials, marketplace=Marketplaces.US)

    is_custom_range = request.start_date and request.end_date

    if is_custom_range:
        created_after = date_string_to_utc(request.start_date, False)
        created_before = date_string_to_utc(request.end_date, True)
    else:
        created_after = get_business_day_start_utc(request.days_back)
        # Always set an upper boundary so past periods don't bleed into today
        if request.days_back == 0:
            created_before = None  # Today: open-ended up to now
        else:
            created_before = get_business_day_start_utc(0)  # Today's start

    # Fetch orders with pagination
    all_orders = []
    next_token = None

    while True:
        if next_token:
            response = orders_api.get_orders(NextToken=next_token)
        else:
            created_after_str = created_after.strftime("%Y-%m-%dT%H:%M:%SZ")
            params = {
                "CreatedAfter": created_after_str,
                "MaxResultsPerPage": 100,
            }
            if created_before:
                params["CreatedBefore"] = created_before.strftime("%Y-%m-%dT%H:%M:%SZ")
            response = orders_api.get_orders(**params)

        orders = response.payload.get("Orders", [])
        all_orders.extend(orders)

        next_token = response.payload.get("NextToken")
        if not next_token or len(all_orders) >= 500:
            break

        time.sleep(0.3)

    # Process orders
    by_product = defaultdict(lambda: {"orders": [], "totalUnits": 0, "totalRevenue": 0})
    by_date = defaultdict(lambda: {"units": 0, "revenue": 0})
    shipped, pending, canceled = 0, 0, 0

    for order in all_orders:
        # Get order items with exponential backoff retry
        items = []
        for attempt in range(5):
            try:
                items_response = orders_api.get_order_items(order["AmazonOrderId"])
                items = items_response.payload.get("OrderItems", [])
                break
            except Exception as e:
                backoff = 2 ** attempt  # 1, 2, 4, 8, 16 seconds
                if attempt < 4:
                    time.sleep(backoff)
                else:
                    logger.warning("Failed to fetch items for %s: %s", order["AmazonOrderId"], e)

        # Convert order date to Argentina business day
        order_date_utc = datetime.fromisoformat(order["PurchaseDate"].replace("Z", "+00:00"))
        order_art = order_date_utc - timedelta(hours=3)

        if order_art.hour < 5:
            order_art -= timedelta(days=1)

        date_key = order_art.strftime("%Y-%m-%d")

        # Process items
        for item in items:
            sku = item.get("SellerSKU", "")

            if request.product_sku != "ALL" and sku != request.product_sku:
                continue

            product_name = PRODUCTS.get(sku, sku)
            qty = item.get("QuantityOrdered", 1)
            price = float(item.get("ItemPrice", {}).get("Amount", 0))

            by_product[product_name]["orders"].append({
                "orderId": order["AmazonOrderId"],
                "status": order["OrderStatus"],
                "quantity": qty,
                "price": price,
                "date": date_key,
            })
            by_product[product_name]["totalUnits"] += qty
            by_product[product_name]["totalRevenue"] += price * qty

            by_date[date_key]["units"] += qty
            by_date[date_key]["revenue"] += price * qty

        # Count statuses
        status = order["OrderStatus"]
        if status == "Shipped":
            shipped += 1
        elif status == "Pending":
            pending += 1
        elif status == "Canceled":
            canceled += 1

        time.sleep(0.5)  # Respect SP-API getOrderItems rate limit

    # Date range
    if is_custom_range:
        date_range = {"start": request.start_date, "end": request.end_date}
    else:
        date_range = {
            "start": get_argentina_date(created_after),
            "end": get_current_argentina_date() if request.days_back == 0
                   else get_argentina_date(created_before - timedelta(seconds=1)),
        }

    return {
        "success": True,
        "dateRange": date_range,
        "daysBack": request.days_back,
        "isCustomRange": is_custom_range,
        "productFilter": request.product_sku,
        "totalOrders": len(all_orders),
        "byProduct": {k: v for k, v in by_product.items()},
        "byDate": {k: v for k, v in by_date.items()},
        "summary": {"shipped": shipped, "pending": pending, "canceled": canceled},
    }


@app.post("/api/orders")
async def get_orders(request: OrdersRequest):
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _fetch_orders_sync, request)
    except Exception as e:
        logger.exception("Error fetching orders")
        return {"success": False, "error": str(e)}


# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")
