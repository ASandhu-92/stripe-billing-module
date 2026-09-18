"""Drive the billing cases that do not need a browser, then print Stripe state next to ours.

    python lab_cases.py state <email>                 what Stripe says vs what the DB says
    python lab_cases.py change <email> <product>      move the customer's subscription to a new price
    python lab_cases.py invoice-now <email>           bill the pending proration items today
    python lab_cases.py pay-open <email>              retry every open invoice with the subscription's card
    python lab_cases.py cancel-period-end <email>     cancel at period end
    python lab_cases.py cancel-now <email>            cancel immediately
    python lab_cases.py refund <email> [amount_cents] refund the last charge, fully or partially
    python lab_cases.py clock-subscribe <email> <product> [card_pm]
                                                      customer on a test clock with a subscription
    python lab_cases.py set-card <email> <card_pm>    swap the customer's default card
    python lab_cases.py clock-advance <email> <days>  advance that customer's clock, wait for it

Every command ends by printing the support view, so each case in CASES.md is a paste of this.
"""
import json
import sys
import time

import stripe

import models
from stripe_client import configure, find_customers, load_prices
from support import support_view

PRICES = None


def customer(email):
    res = find_customers(email, limit=1)
    if not res:
        sys.exit(f"no Stripe customer {email}")
    return res[0]


def live_subscription(cust):
    subs = [s for s in stripe.Subscription.list(customer=cust.id, status="all", limit=10).data
            if s.status not in ("canceled", "incomplete_expired")]
    if not subs:
        sys.exit(f"{cust.email} has no live subscription")
    return subs[0]


def state(email):
    time.sleep(3)  # let the webhook land
    conn = models.connect()
    view = support_view(conn, email)
    conn.close()
    print("\nSUPPORT VIEW")
    print(json.dumps(view, indent=1, default=str))


def cmd_change(email, product):
    cust = customer(email)
    sub = live_subscription(cust)
    item = sub["items"].data[0]
    new = stripe.Subscription.modify(
        sub.id,
        items=[{"id": item.id, "price": PRICES[product]["price_id"]}],
        proration_behavior="create_prorations",
        payment_behavior="error_if_incomplete",
        metadata={"product": product},
    )
    print(f"subscription {new.id} status={new.status} now on {product}")
    print_upcoming(cust, new)
    state(email)


def _proration(line):
    d = line.parent.subscription_item_details if line.parent else None
    return d.proration if d else "?"


def print_upcoming(cust, sub):
    """Prorations land on the NEXT invoice by default. Show what that invoice will contain."""
    prev = stripe.Invoice.create_preview(customer=cust.id, subscription=sub.id)
    print(f"upcoming invoice preview: total={prev.total} next_payment_attempt={prev.next_payment_attempt}")
    for line in prev.lines.data:
        print(f"  {line.amount:>7}  proration={_proration(line)}  {line.description}")
    pending = stripe.InvoiceItem.list(customer=cust.id, pending=True).data
    print(f"pending invoice items now: {[(i.amount, i.description) for i in pending]}")


def cmd_invoice_now(email):
    """Bill the pending proration items today instead of at the next renewal."""
    cust = customer(email)
    inv = stripe.Invoice.create(customer=cust.id, auto_advance=True, pending_invoice_items_behavior="include")
    inv = stripe.Invoice.finalize_invoice(inv.id)
    if inv.status != "paid":
        # a Checkout-created subscription keeps its card on the subscription, not on the
        # customer, so a manual invoice has nothing to charge unless we point at it
        sub = live_subscription(cust)
        inv = stripe.Invoice.pay(inv.id, payment_method=sub.default_payment_method)
    print(f"invoice {inv.id} status={inv.status} total={inv.total} paid={inv.amount_paid}")
    for line in inv.lines.data:
        print(f"  {line.amount:>7}  proration={_proration(line)}  {line.description}")
    state(email)


def cmd_cancel_period_end(email):
    sub = live_subscription(customer(email))
    new = stripe.Subscription.modify(sub.id, cancel_at_period_end=True)
    item = new["items"].data[0]
    print(f"subscription {new.id} status={new.status} cancel_at_period_end={new.cancel_at_period_end} "
          f"access until {item.current_period_end}")
    state(email)


def cmd_cancel_now(email):
    sub = live_subscription(customer(email))
    new = stripe.Subscription.cancel(sub.id)
    print(f"subscription {new.id} status={new.status} canceled_at={new.canceled_at}")
    state(email)


def cmd_refund(email, amount=None):
    cust = customer(email)
    ch = stripe.Charge.list(customer=cust.id, limit=1).data[0]
    kwargs = {"charge": ch.id}
    if amount:
        kwargs["amount"] = int(amount)
    rf = stripe.Refund.create(**kwargs)
    ch = stripe.Charge.retrieve(ch.id)
    print(f"refund {rf.id} status={rf.status} amount={rf.amount}  charge {ch.id} refunded={ch.refunded} "
          f"amount_refunded={ch.amount_refunded}")
    state(email)


def cmd_pay_open(email):
    """Retry every open invoice with the subscription's own payment method."""
    cust = customer(email)
    sub = live_subscription(cust)
    for inv in stripe.Invoice.list(customer=cust.id, status="open", limit=10).data:
        inv = stripe.Invoice.pay(inv.id, payment_method=sub.default_payment_method)
        print(f"invoice {inv.id} status={inv.status} total={inv.total} paid={inv.amount_paid}")
    state(email)


def cmd_clock_subscribe(email, product, card_pm="pm_card_visa"):
    clock = stripe.test_helpers.TestClock.create(frozen_time=int(time.time()), name=email)
    cust = stripe.Customer.create(email=email, test_clock=clock.id)
    pm = stripe.PaymentMethod.attach(card_pm, customer=cust.id)
    stripe.Customer.modify(cust.id, invoice_settings={"default_payment_method": pm.id})
    sub = stripe.Subscription.create(
        customer=cust.id,
        items=[{"price": PRICES[product]["price_id"]}],
        metadata={"product": product},
        payment_behavior="error_if_incomplete",
    )
    print(f"clock {clock.id} frozen_time={clock.frozen_time}  customer {cust.id}  pm {pm.id} "
          f"subscription {sub.id} status={sub.status}")
    state(email)


def cmd_set_card(email, card_pm):
    cust = customer(email)
    pm = stripe.PaymentMethod.attach(card_pm, customer=cust.id)
    stripe.Customer.modify(cust.id, invoice_settings={"default_payment_method": pm.id})
    print(f"customer {cust.id} default payment method now {pm.id} ({card_pm})")


def cmd_clock_advance(email, days):
    cust = customer(email)
    clock = stripe.test_helpers.TestClock.retrieve(cust.test_clock)
    target = clock.frozen_time + int(days) * 86400
    stripe.test_helpers.TestClock.advance(clock.id, frozen_time=target)
    for _ in range(60):
        clock = stripe.test_helpers.TestClock.retrieve(clock.id)
        if clock.status == "ready":
            break
        time.sleep(3)
    print(f"clock {clock.id} status={clock.status} frozen_time={clock.frozen_time} (+{days}d)")
    state(email)


COMMANDS = {
    "state": state,
    "change": cmd_change,
    "invoice-now": cmd_invoice_now,
    "pay-open": cmd_pay_open,
    "cancel-period-end": cmd_cancel_period_end,
    "cancel-now": cmd_cancel_now,
    "refund": cmd_refund,
    "clock-subscribe": cmd_clock_subscribe,
    "set-card": cmd_set_card,
    "clock-advance": cmd_clock_advance,
}

if __name__ == "__main__":
    configure()
    PRICES = load_prices()
    if len(sys.argv) < 3 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    COMMANDS[sys.argv[1]](*sys.argv[2:])
