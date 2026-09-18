"""Stripe key loading and the price catalogue.

Keys come from the environment only. The operator exports them from an encrypted env file before
starting the app; nothing in this tree ever holds a real key.
"""
import json
import os
from pathlib import Path

import stripe

HERE = Path(__file__).resolve().parent
PRICES_FILE = Path(os.environ.get("BILLING_PRICES_FILE") or HERE / "prices.json")

PRODUCTS = {
    # key: (display name, unit amount in cents, recurring interval or None)
    "cheatsheet": ("AI Tools Cheat Sheet", 900, None),
    "weekly": ("Signal Weekly", 500, "month"),
    "annual": ("Signal Weekly Annual", 4000, "year"),
}


def configure() -> None:
    key = os.environ.get("STRIPE_TEST_SECRET_KEY")
    if not key:
        raise RuntimeError("STRIPE_TEST_SECRET_KEY is not set; export it from the encrypted env file first")
    if not key.startswith("sk_test_"):
        raise RuntimeError("refusing to run with a non-test Stripe key")
    stripe.api_key = key


def webhook_secret() -> str:
    value = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not value:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is not set")
    return value


def load_prices() -> dict:
    """{key: {product_id, price_id, name, mode}} written by setup_products.py."""
    if not PRICES_FILE.exists():
        raise RuntimeError(f"{PRICES_FILE.name} missing; run setup_products.py")
    return json.loads(PRICES_FILE.read_text())


def product_key_for_id(stripe_product_id: str, prices: dict | None = None) -> str | None:
    prices = prices or load_prices()
    for key, row in prices.items():
        if row["product_id"] == stripe_product_id:
            return key
    return None


def mode_for(key: str) -> str:
    return "payment" if PRODUCTS[key][2] is None else "subscription"


def find_customers(email: str, limit: int = 5) -> list:
    """Customers by email. Customer.list silently skips customers attached to a test clock,
    so fall back to search, which includes them (search is eventually consistent)."""
    found = stripe.Customer.list(email=email.lower(), limit=limit).data
    if found:
        return found
    safe = email.lower().replace("'", "\\'")
    return stripe.Customer.search(query=f"email:'{safe}'", limit=limit).data
