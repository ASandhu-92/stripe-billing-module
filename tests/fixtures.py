"""Minimal event payloads shaped like the ones observed from the sandbox (API 2026-08-26)."""
import json
from pathlib import Path

PRICES = json.loads((Path(__file__).resolve().parents[1] / "prices.example.json").read_text())


def event(eid, etype, obj):
    return {"id": eid, "object": "event", "type": etype, "api_version": "2026-08-26.dahlia",
            "data": {"object": obj}, "livemode": False, "pending_webhooks": 0}


def checkout_completed(eid="evt_co_1", email="alice@example.test", product="cheatsheet",
                       customer="cus_alice", subscription=None, payment_status="paid"):
    return event(eid, "checkout.session.completed", {
        "id": "cs_test_1", "object": "checkout.session", "customer": customer,
        "customer_details": {"email": email}, "customer_email": email,
        "mode": "subscription" if subscription else "payment",
        "payment_status": payment_status, "status": "complete",
        "subscription": subscription, "metadata": {"product": product}})


def _line(product, amount, proration=False, subscription="sub_1"):
    p = PRICES[product]
    return {"object": "line_item", "amount": amount, "description": p["name"],
            "pricing": {"type": "price_details",
                        "price_details": {"price": p["price_id"], "product": p["product_id"]}},
            "parent": {"type": "subscription_item_details", "invoice_item_details": None,
                       "subscription_item_details": {"proration": proration,
                                                     "subscription": subscription}}}


def invoice(eid, etype, email="bob@example.test", customer="cus_bob", product="weekly",
            subscription="sub_1", lines=None, status="paid"):
    lines = lines if lines is not None else [_line(product, PRICES[product]["amount_usd"] * 100,
                                                  subscription=subscription)]
    return event(eid, etype, {
        "id": "in_1", "object": "invoice", "customer": customer, "customer_email": email,
        "status": status, "amount_paid": 500 if status == "paid" else 0,
        "parent": {"type": "subscription_details",
                   "subscription_details": {"subscription": subscription,
                                            "metadata": {"product": product}}} if subscription else None,
        "lines": {"object": "list", "data": lines}})


def subscription(eid, etype, customer="cus_bob", product="weekly", sub_id="sub_1",
                 status="active", cancel_at_period_end=False, cancel_at=None):
    p = PRICES[product]
    return event(eid, etype, {
        "id": sub_id, "object": "subscription", "customer": customer, "status": status,
        "cancel_at_period_end": cancel_at_period_end, "cancel_at": cancel_at,
        "metadata": {"product": "weekly"},
        "items": {"object": "list", "data": [
            {"id": "si_1", "price": {"id": p["price_id"], "product": p["product_id"]}}]}})


def charge_refunded(eid, email="carol@example.test", customer="cus_carol", product="cheatsheet",
                    refunded=True, amount_refunded=900):
    return event(eid, "charge.refunded", {
        "id": "ch_1", "object": "charge", "customer": customer, "refunded": refunded,
        "amount": 900, "amount_refunded": amount_refunded, "invoice": None,
        "billing_details": {"email": email}, "receipt_email": None,
        "metadata": {"product": product}})
