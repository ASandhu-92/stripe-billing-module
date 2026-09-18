"""Create the three test-mode products, their prices and a customer portal configuration.

Idempotent: products are looked up by metadata key, prices by (product, amount, interval),
the portal configuration by its headline. Writes prices.json (test ids only).
"""
import json

import stripe

from stripe_client import PRICES_FILE, PRODUCTS, configure

PORTAL_HEADLINE = "billing (sandbox)"


def find_product(key: str):
    """Oldest active product carrying our metadata key. Product.search is eventually
    consistent (a rerun within a minute created duplicates), so use list and filter."""
    matches = [p for p in stripe.Product.list(active=True, limit=100).auto_paging_iter()
               if (p.metadata.to_dict() if p.metadata else {}).get("key") == key]
    matches.sort(key=lambda p: p.created)
    for dup in matches[1:]:  # a rerun raced the index once; archive the extras
        for price in stripe.Price.list(product=dup.id, active=True, limit=100).auto_paging_iter():
            stripe.Price.modify(price.id, active=False)
        stripe.Product.modify(dup.id, active=False)
        print(f"archived duplicate {dup.id} ({key})")
    return matches[0] if matches else None


def find_price(product_id: str, amount: int, interval: str | None):
    for p in stripe.Price.list(product=product_id, active=True, limit=100).auto_paging_iter():
        same_amount = p.unit_amount == amount and p.currency == "usd"
        same_kind = (p.recurring.interval if p.recurring else None) == interval
        if same_amount and same_kind:
            return p
    return None


def ensure_portal(price_ids: list[str]) -> str:
    for cfg in stripe.billing_portal.Configuration.list(limit=100).auto_paging_iter():
        if cfg.business_profile and cfg.business_profile.headline == PORTAL_HEADLINE:
            return cfg.id
    cfg = stripe.billing_portal.Configuration.create(
        business_profile={"headline": PORTAL_HEADLINE},
        default_return_url="http://localhost:8400/portal/return",
        features={
            "customer_update": {"enabled": True, "allowed_updates": ["email"]},
            "invoice_history": {"enabled": True},
            "payment_method_update": {"enabled": True},
            "subscription_cancel": {
                "enabled": True,
                "mode": "at_period_end",
                "proration_behavior": "none",
            },
            "subscription_update": {
                "enabled": True,
                "default_allowed_updates": ["price"],
                "proration_behavior": "create_prorations",
                "products": [
                    {"product": pid, "prices": [prid]} for pid, prid in price_ids
                ],
            },
        },
    )
    return cfg.id


def main() -> None:
    configure()
    out = {}
    for key, (name, amount, interval) in PRODUCTS.items():
        product = find_product(key) or stripe.Product.create(name=name, metadata={"key": key})
        price = find_price(product.id, amount, interval)
        if price is None:
            kwargs = {"product": product.id, "unit_amount": amount, "currency": "usd"}
            if interval:
                kwargs["recurring"] = {"interval": interval}
            price = stripe.Price.create(**kwargs)
        out[key] = {
            "name": name,
            "product_id": product.id,
            "price_id": price.id,
            "mode": "payment" if interval is None else "subscription",
            "amount_usd": amount / 100,
            "interval": interval,
        }
        print(f"{key:10} {product.id} {price.id} {'one-time' if interval is None else interval}")
    sub_pairs = [(out[k]["product_id"], out[k]["price_id"]) for k in ("weekly", "annual")]
    out["_portal_configuration"] = ensure_portal(sub_pairs)
    print("portal    ", out["_portal_configuration"])
    PRICES_FILE.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {PRICES_FILE.name}")


if __name__ == "__main__":
    main()
