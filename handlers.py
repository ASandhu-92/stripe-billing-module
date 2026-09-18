"""Turn a verified Stripe event into entitlement rows.

Every handler is pure with respect to the database handle it is given, so the tests can run
them against fixture payloads with no network. The only network call is the customer email
lookup, and only when no earlier event told us the email for that customer id.
"""
import stripe

from models import (current_product_for_subscription, email_for_customer, remember_customer,
                    set_entitlement, supersede_subscription_products)
from stripe_client import product_key_for_id

HANDLED = {
    "checkout.session.completed",
    "invoice.paid",
    "invoice.payment_failed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "charge.refunded",
}


def _get(obj, *path, default=None):
    """Walk dicts or Stripe objects by attribute/key without caring which they are."""
    cur = obj
    for p in path:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            cur = getattr(cur, p, None)
    return default if cur is None else cur


def _email(conn, obj, customer_field="customer", *email_paths):
    for path in email_paths:
        email = _get(obj, *path)
        if email:
            remember_customer(conn, _get(obj, customer_field), email)
            return email.lower()
    customer_id = _get(obj, customer_field)
    if not customer_id:
        return None
    known = email_for_customer(conn, customer_id)
    if known:
        return known
    cust = stripe.Customer.retrieve(customer_id)  # network, last resort
    remember_customer(conn, customer_id, cust.email)
    return cust.email.lower() if cust.email else None


def _product_from_invoice(conn, inv, prices):
    """Which of our products an invoice is about.

    A proration invoice after a plan change carries lines for BOTH products (a credit for the
    old one, a charge for the new one), so the first line is not the answer. Order of trust:
    the largest non-proration charge line, then the largest proration charge line, then the
    subscription we already know about, then the metadata Checkout stamped on the subscription.
    Lines beat our own memory because the subscription.updated event may not have arrived yet.
    """
    best = None
    for line in _get(inv, "lines", "data", default=[]):
        pid = _get(line, "pricing", "price_details", "product") or _get(line, "price", "product")
        key = product_key_for_id(pid, prices) if pid else None
        if not key:
            continue
        proration = bool(_get(line, "parent", "subscription_item_details", "proration")
                         or _get(line, "proration"))
        amount = _get(line, "amount", default=0) or 0
        rank = (0 if proration else 1, amount)
        if best is None or rank > best[0]:
            best = (rank, key)
    if best:
        return best[1]
    sub_id = _sub_id_from_invoice(inv)
    if sub_id:
        known = current_product_for_subscription(conn, sub_id)
        if known:
            return known
    return _get(inv, "parent", "subscription_details", "metadata", "product") or _get(
        inv, "subscription_details", "metadata", "product"
    )


def _product_from_subscription(sub, prices):
    """The live price wins; subscription metadata is only what Checkout stamped at the start
    and goes stale after a plan change."""
    for item in _get(sub, "items", "data", default=[]):
        pid = _get(item, "price", "product")
        if pid:
            key = product_key_for_id(pid, prices)
            if key:
                return key
    return _get(sub, "metadata", "product")


def _sub_id_from_invoice(inv):
    sub_id = _get(inv, "parent", "subscription_details", "subscription") or _get(inv, "subscription")
    if sub_id:
        return sub_id
    # a manually created invoice that bills proration items still names the subscription per line
    for line in _get(inv, "lines", "data", default=[]):
        sub_id = _get(line, "parent", "subscription_item_details", "subscription")
        if sub_id:
            return sub_id
    return None


def _subscription_status(sub) -> str:
    status = _get(sub, "status")
    if status in ("active", "trialing"):
        # the API flag is cancel_at_period_end; the hosted portal sets cancel_at (a timestamp)
        # instead and leaves the flag False. Either means "ends, do not renew".
        scheduled = _get(sub, "cancel_at_period_end") or _get(sub, "cancel_at")
        return "active_cancelling" if scheduled else "active"
    if status in ("past_due", "unpaid", "incomplete"):
        return "past_due"
    return "canceled"


def handle(conn, event, prices) -> dict:
    """Apply one event. Returns a small dict describing what changed, for logs and tests."""
    etype = event["type"] if isinstance(event, dict) else event.type
    obj = event["data"]["object"] if isinstance(event, dict) else event.data.object
    eid = event["id"] if isinstance(event, dict) else event.id

    if etype == "checkout.session.completed":
        email = _email(conn, obj, "customer", ("customer_details", "email"), ("customer_email",))
        product = _get(obj, "metadata", "product")
        if _get(obj, "payment_status") != "paid" or not email or not product:
            return {"applied": False, "reason": "unpaid, or missing email/product"}
        sub_id = _get(obj, "subscription")
        set_entitlement(conn, email, product, "active", eid, sub_id)
        return {"applied": True, "email": email, "product": product, "status": "active",
                "subscription": sub_id}

    if etype in ("invoice.paid", "invoice.payment_failed"):
        email = _email(conn, obj, "customer", ("customer_email",))
        product = _product_from_invoice(conn, obj, prices)
        if not email or not product:
            return {"applied": False, "reason": "no email or product on invoice"}
        status = "active" if etype == "invoice.paid" else "past_due"
        sub_id = _sub_id_from_invoice(obj)
        set_entitlement(conn, email, product, status, eid, sub_id)
        superseded = supersede_subscription_products(conn, sub_id, product, eid) if sub_id else []
        return {"applied": True, "email": email, "product": product, "status": status,
                "subscription": sub_id, "superseded": superseded}

    if etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        email = _email(conn, obj, "customer")
        product = _product_from_subscription(obj, prices)
        if not email or not product:
            return {"applied": False, "reason": "no email or product on subscription"}
        status = "canceled" if etype.endswith("deleted") else _subscription_status(obj)
        sub_id = _get(obj, "id")
        set_entitlement(conn, email, product, status, eid, sub_id)
        superseded = supersede_subscription_products(conn, sub_id, product, eid)
        return {"applied": True, "email": email, "product": product, "status": status,
                "subscription": sub_id, "superseded": superseded}

    if etype == "charge.refunded":
        email = _email(conn, obj, "customer", ("billing_details", "email"), ("receipt_email",))
        product = _get(obj, "metadata", "product")
        if not product and _get(obj, "invoice"):
            inv = stripe.Invoice.retrieve(_get(obj, "invoice"))  # network
            product = _product_from_invoice(conn, inv, prices)
        if not email or not product:
            return {"applied": False, "reason": "no email or product on charge"}
        if not _get(obj, "refunded"):
            # partial refund: access stays, the money is the support team's to explain
            return {"applied": False, "reason": "partial refund, entitlement unchanged",
                    "email": email, "product": product}
        set_entitlement(conn, email, product, "refunded", eid)
        return {"applied": True, "email": email, "product": product, "status": "refunded"}

    return {"applied": False, "reason": f"unhandled type {etype}"}
