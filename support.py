"""The support view: what Stripe says, what our database says, and whether they agree."""
import stripe

from models import get_entitlements
from stripe_client import find_customers, load_prices, product_key_for_id

ACTIVE_IN_DB = {"active", "active_cancelling", "past_due"}


def _sub_product(sub, prices):
    for item in sub["items"].data:
        key = product_key_for_id(item.price.product, prices)
        if key:
            return key
    return (sub.metadata.to_dict() if sub.metadata else {}).get("product")


def _stripe_expectation(customer_id: str, prices: dict) -> dict:
    """Which products Stripe says this customer should have, and why."""
    expected = {}
    subs = stripe.Subscription.list(customer=customer_id, status="all", limit=20)
    for sub in subs.auto_paging_iter():
        product = _sub_product(sub, prices)
        if not product:
            continue
        if sub.status in ("active", "trialing"):
            scheduled = sub.cancel_at_period_end or sub.cancel_at
            expected[product] = {"expect": "active_cancelling" if scheduled else "active",
                                 "because": sub.id}
        elif sub.status in ("past_due", "unpaid"):
            expected[product] = {"expect": "past_due", "because": sub.id}
        else:
            expected.setdefault(product, {"expect": "canceled", "because": sub.id})
    # one-time purchases: paid checkout sessions in payment mode
    sessions = stripe.checkout.Session.list(customer=customer_id, limit=20)
    for s in sessions.auto_paging_iter():
        if s.mode != "payment" or s.payment_status != "paid":
            continue
        product = (s.metadata.to_dict() if s.metadata else {}).get("product")
        if not product:
            continue
        refunded = False
        if s.payment_intent:
            pi = stripe.PaymentIntent.retrieve(s.payment_intent, expand=["latest_charge"])
            ch = pi.latest_charge
            refunded = bool(ch and ch.refunded)
        expected[product] = {"expect": "refunded" if refunded else "active", "because": s.id}
    return expected


def support_view(conn, email: str) -> dict:
    prices = load_prices()
    email = email.lower()
    customers = find_customers(email)
    ours = {e["product"]: e for e in get_entitlements(conn, email)}
    view = {"email": email, "stripe_customer": None, "subscriptions": [], "last_invoices": [],
            "payment_state": None, "entitlements": list(ours.values()), "mismatches": [],
            "mismatch": False}
    if not customers:
        # no Stripe customer, so every entitlement we hold is unexplained
        for product, row in ours.items():
            if row["status"] in ACTIVE_IN_DB:
                view["mismatches"].append({"product": product, "stripe": "no customer",
                                           "db": row["status"]})
        view["mismatch"] = bool(view["mismatches"])
        return view

    cust = customers[0]
    view["stripe_customer"] = {"id": cust.id, "created": cust.created,
                               "delinquent": cust.delinquent}
    for sub in stripe.Subscription.list(customer=cust.id, status="all", limit=20).auto_paging_iter():
        item = sub["items"].data[0] if sub["items"].data else None
        view["subscriptions"].append({
            "id": sub.id, "status": sub.status, "product": _sub_product(sub, prices),
            "cancel_at_period_end": sub.cancel_at_period_end,
            "cancel_at": sub.cancel_at,
            "canceled_at": sub.canceled_at,
            "current_period_end": item.current_period_end if item else None,
        })
    invoices = stripe.Invoice.list(customer=cust.id, limit=5).data
    for inv in invoices:
        view["last_invoices"].append({
            "id": inv.id, "status": inv.status, "amount_due": inv.amount_due,
            "amount_paid": inv.amount_paid, "created": inv.created,
            "attempt_count": inv.attempt_count, "billing_reason": inv.billing_reason,
        })
    charges = stripe.Charge.list(customer=cust.id, limit=1).data
    if charges:
        ch = charges[0]
        view["payment_state"] = {"last_charge": ch.id, "status": ch.status, "paid": ch.paid,
                                 "refunded": ch.refunded, "amount_refunded": ch.amount_refunded,
                                 "failure_code": ch.failure_code, "failure_message": ch.failure_message}
    expected = _stripe_expectation(cust.id, prices)
    for product, exp in expected.items():
        db_status = ours.get(product, {}).get("status")
        if exp["expect"] in ("active", "active_cancelling", "past_due"):
            ok = db_status in ACTIVE_IN_DB
        else:
            ok = db_status not in ACTIVE_IN_DB
        if not ok:
            view["mismatches"].append({"product": product, "stripe": exp["expect"],
                                       "stripe_object": exp["because"], "db": db_status or "none"})
    for product, row in ours.items():
        if product not in expected and row["status"] in ACTIVE_IN_DB:
            view["mismatches"].append({"product": product, "stripe": "nothing", "db": row["status"]})
    view["mismatch"] = bool(view["mismatches"])
    return view
