# Billing cases, observed in the Stripe sandbox on 2026-09-17

Every case below was run against Stripe test mode with the app in `app.py` receiving webhooks
through `stripe listen`. The Stripe column is what the Dashboard or API showed. The DB column is the
matching row in our `entitlements` table (`email, product, status, source_event_id, subscription_id`).
Test customers are `*@example.test`. Amounts are USD cents. Card `4242 4242 4242 4242` unless stated.
Object ids are test-mode ids from a sandbox that has since been reset; they resolve to nothing.

Not every case went cleanly on the first try. Where the first attempt exposed a bug in the app,
the case says so, because that is the point of running them.

## 1. One-time purchase through Checkout (carol, AI Tools Cheat Sheet, $9)

| Side | Evidence |
|---|---|
| Stripe | Checkout `cs_test_a1Xk3YUc40ExTOfgExVuXHggpbEW4XHkISD0LVEBSKG415MiM6X2SQj0LK` `payment_status=paid`; charge `ch_3UGZCSJ0skORfSym15bJmoVp` succeeded 900; customer `cus_VH7R3QjnvgGdkW` |
| Event | `evt_1UGZCTJ0skORfSymVl096AJO` checkout.session.completed |
| DB | `carol@example.test, cheatsheet, active, evt_1UGZCTJ0skORfSymVl096AJO, NULL` |

Note: the first two purchases (carol, bob) were made while `stripe listen` was paired with the
wrong Stripe account (the CLI login was for the parent account, the app key for the sandbox inside
it). Stripe showed `pending_webhooks: 0` on every event because no endpoint was registered. Fixed
by running the listener with the same key the app uses (`listen.sh`), then replaying the events
with `stripe events resend`. Both rows above were written by the replay, seconds after the fix.

Duplicate delivery check: `evt_1UGZCTJ0skORfSymVl096AJO` was resent a second time. Response 200
`{"duplicate": true}`, the row's `updated_at` stayed `2026-09-17T06:56:29+00:00`, the events table
still had one row for that id.

## 2. Subscription start through Checkout (bob, Signal Weekly, $5/month)

| Side | Evidence |
|---|---|
| Stripe | Checkout `cs_test_a1Kjn9jsmjXIZToux9lbl87Qli9N33XsNZFBzY65GGVWzMJsanLvvsPI1k`; subscription `sub_1UGZCgJ0skORfSymQH1ZvYhS` active; invoice `in_1UGZCeJ0skORfSymU7b9YAuz` paid 500, `billing_reason=subscription_create`; customer `cus_VH7RBwAyfC5P8B` |
| Events | `evt_1UGZChJ0skORfSym1FnTaWe5` checkout.session.completed, `evt_1UGZChJ0skORfSymJQNS3UkU` invoice.paid |
| DB | `bob@example.test, weekly, active, evt_1UGZChJ0skORfSymJQNS3UkU, sub_1UGZCgJ0skORfSymQH1ZvYhS` |

Same replay note as case 1.

Live repeat with the listener fixed (dave, Signal Weekly): subscription `sub_1UGZFrJ0skORfSym66mxVViN`,
invoice `in_1UGZFpJ0skORfSymaWgIE6tn` paid 500. Events arrived in the order invoice.paid
`evt_1UGZFsJ0skORfSymwNxcoLXu`, then checkout.session.completed `evt_1UGZFtJ0skORfSymRSxxZnWP`.
That is the reverse of what a reader expects and it is normal; Stripe does not order deliveries.
Both handlers set `active`, so the order did not matter. DB: `dave@example.test, weekly, active`.

## 3. Plan change with proration (dave, Weekly $5/month to Annual $40/year)

`Subscription.modify(items=[{id, price: annual}], proration_behavior="create_prorations")`.

| Side | Evidence |
|---|---|
| Stripe | `sub_1UGZFrJ0skORfSym66mxVViN` active, item now `price_1UGZ0gJ0skORfSymyhxAZWZx` (annual). Pending invoice items: `ii_1UGZGgJ0skORfSymqqFtmSU8` -500 "Unused time on Signal Weekly", `ii_1UGZGgJ0skORfSymPr5t0PE2` +4000 "Remaining time on Signal Weekly Annual". Upcoming invoice preview: total 7500 (3500 proration + 4000 renewal), `next_payment_attempt` 1821167829 = 2027-09-17. |
| Event | `evt_1UGZGgJ0skORfSymyoExFDuV` customer.subscription.updated |
| DB | `dave, annual, active, evt_1UGZGgJ0skORfSymyoExFDuV, sub_...VViN` and `dave, weekly, canceled` (same event; the app marks the old product of the same subscription canceled) |

What this shows for support: with `create_prorations`, the $35 upgrade difference is not charged at
upgrade time. It sits as pending invoice items and lands on the next invoice, which after a
month-to-year change is a year away. The customer sees no charge today and a $75 invoice next
September. The product decision that avoids the confusion is `proration_behavior="always_invoice"`.

### 3b. Billing the proration today

`Invoice.create(customer, pending_invoice_items_behavior="include")` then finalize. Invoice
`in_1UGZHWJ0skORfSym6wbPUizO` total 3500 (-500, +4000). Finalize tried to charge and failed:
`evt_1UGZHbJ0skORfSymYKqK8AaD` invoice.payment_failed, because a subscription created by Checkout
keeps its card on the subscription, and the customer has no `invoice_settings.default_payment_method`.
Paid with `Invoice.pay(payment_method=<subscription's card>)`: status `paid`, `amount_paid=3500`,
`evt_1UGZJHJ0skORfSymkHn2xehp` invoice.paid.

Bug this exposed in the app: `invoice.payment_failed` on the proration invoice was attributed to
`weekly` (first line of the invoice, the credit line) and set weekly back to `past_due`. The handler
now picks the largest non-negative charge line, so both-product invoices resolve to the product
being paid for. `tests/test_webhook.py::test_proration_invoice_resolves_to_the_new_product` covers it.
The two events were forgotten from the events table and replayed against the fixed handler; final
DB rows: `annual active` (source `evt_1UGZJHJ0skORfSymkHn2xehp`), `weekly canceled`.

Lab residue: a second `invoice-now` run created `in_1UGZHmJ0skORfSymyuzAWzsJ` for $0 (no pending
items left). Paid, harmless, left in place.

## 4. Cancel at period end (dave, annual)

| Side | Evidence |
|---|---|
| Stripe | `Subscription.modify(cancel_at_period_end=True)`: status `active`, `cancel_at_period_end=true`, `canceled_at=1789628603`, access until `current_period_end=1821164229` (2027-09-17) |
| Event | `evt_1UGZLrJ0skORfSymntcVFtjk` customer.subscription.updated |
| DB | `dave, annual, active_cancelling, evt_1UGZLrJ0skORfSymntcVFtjk, sub_...VViN` |

Access stays on. Nothing is refunded. The support view shows both facts.

## 5. Cancel immediately (bob, weekly)

| Side | Evidence |
|---|---|
| Stripe | `Subscription.cancel()`: status `canceled`, `canceled_at=1789628608`. Invoice `in_1UGZCeJ0skORfSymU7b9YAuz` stays paid; no refund is created by cancelling. |
| Event | `evt_1UGZLxJ0skORfSymeAuD78Mu` customer.subscription.deleted |
| DB | `bob, weekly, canceled, evt_1UGZLxJ0skORfSymeAuD78Mu, sub_1UGZCgJ0skORfSymQH1ZvYhS` |

## 6. Renewal on a test clock, then a failed renewal (erin, weekly)

Customer `cus_VH7cIpzvI2A4om` on test clock `clock_1UGZN0J0skORfSymVe8LOOoZ` (frozen at
1789628674), card `pm_card_visa`, subscription `sub_1UGZN2J0skORfSymN7rxc4rW`.

First finding: `Customer.list(email=...)` does not return customers attached to a test clock. The
support view showed "no customer" and flagged a false mismatch. `Customer.search(query="email:...")`
does return them; `stripe_client.find_customers` falls back to search.

### 6a. Clock +32 days: renewal paid

| Side | Evidence |
|---|---|
| Stripe | invoice `in_1UGZOUJ0skORfSymy3XQ92vh` paid 500, `billing_reason=subscription_cycle`, `attempt_count=1`; subscription active |
| Event | `evt_1UGZOfJ0skORfSymltVd0Ydo` invoice.paid |
| DB | `erin, weekly, active` |

### 6b. Card swapped to `4000 0000 0000 0341` (`pm_card_chargeCustomerFail`, `pm_1UGZOmJ0skORfSym9RKBYXs0`), clock +31 days

| Side | Evidence |
|---|---|
| Stripe | invoice `in_1UGZOrJ0skORfSymxmozqolk` status `open`, `amount_paid=0`, `attempt_count=2`; last charge `failed`, `failure_code=card_declined`; subscription `past_due` |
| Events | `evt_1UGZP4J0skORfSymneVtY8ug` and `evt_1UGZPJJ0skORfSymhSQE2YAp` invoice.payment_failed (one per attempt), plus customer.subscription.updated `evt_1UGZOsJ0skORfSymyeIdvQwE`, `evt_1UGZP1J0skORfSymMOXt8iYv` |
| DB | `erin, weekly, past_due, evt_1UGZPJJ0skORfSymhSQE2YAp, sub_1UGZN2J0skORfSymN7rxc4rW` |

The customer keeps access while Stripe retries (Smart Retries in the sandbox default). What happens
after the last retry depends on the Billing setting "if all retries fail": cancel, mark unpaid, or
leave as is. That setting, not the code, decides when access ends.

## 7. Full refund (carol, $9 cheat sheet)

| Side | Evidence |
|---|---|
| Stripe | refund `re_3UGZCSJ0skORfSym1LTyBkku` succeeded 900; charge `ch_3UGZCSJ0skORfSym15bJmoVp` `refunded=true`, `amount_refunded=900` |
| Event | `evt_3UGZCSJ0skORfSym1XUXFvB7` charge.refunded |
| DB | `carol, cheatsheet, refunded, evt_3UGZCSJ0skORfSym1XUXFvB7, NULL` |

## 8. Partial refund (grace, $4 of $9)

Grace bought the cheat sheet through Checkout live (`evt_1UGZMYJ0skORfSymw0sAG7BQ`), then
`Refund.create(charge, amount=400)`.

| Side | Evidence |
|---|---|
| Stripe | refund `re_3UGZMWJ0skORfSym1Z7RD5RG` succeeded 400; charge `ch_3UGZMWJ0skORfSym1mLeyQoN` `refunded=false`, `amount_refunded=400` |
| Event | `evt_3UGZMWJ0skORfSym1XQl7wXu` charge.refunded, handler response `applied: false, reason: partial refund, entitlement unchanged` |
| DB | `grace, cheatsheet, active, evt_1UGZMYJ0skORfSymw0sAG7BQ, NULL` (unchanged) |

Stripe fires the same `charge.refunded` event for partial and full refunds. `charge.refunded`
(the boolean) is only true when the whole amount is back. A partial refund does not revoke access here.

## 9. Cancel through the hosted customer portal (hank, weekly)

`GET /portal?email=hank@example.test` returned a `billing.stripe.com` session. In the portal:
"Cancel subscription", confirm. Portal then showed "Cancels Oct 17".

| Side | Evidence |
|---|---|
| Stripe | `sub_1UGZQhJ0skORfSymBrOQrzrC` status `active`, `cancel_at_period_end=false`, `cancel_at=1792220901`, `canceled_at=1789629169`, `cancellation_details.reason=cancellation_requested` |
| Event | `evt_1UGZV0J0skORfSymL0NWSlpL` customer.subscription.updated |
| DB | `hank, weekly, active_cancelling, evt_1UGZV0J0skORfSymL0NWSlpL, sub_...rzrC` |

Bug this exposed: the portal does not set `cancel_at_period_end`. It sets `cancel_at` to the period
end and leaves the flag false. The first pass of the handler only looked at the flag and left hank
`active`. Now either field means "scheduled to end".
`tests/test_webhook.py::test_portal_cancel_sets_cancel_at_not_the_flag` covers it. The event was
forgotten and replayed; row above is the replay.

## 10. Paid but no access (alice, then hank)

Two instances, one accidental and one deliberate. Written up in `POSTMORTEM-paid-but-no-access.md`.

## Final state of the entitlements table

| email | product | status | source_event_id | subscription_id |
|---|---|---|---|---|
| alice@example.test | cheatsheet | active | evt_1UGZA6J0skORfSymEtFKgwxN | |
| bob@example.test | weekly | canceled | evt_1UGZLxJ0skORfSymeAuD78Mu | sub_1UGZCgJ0skORfSymQH1ZvYhS |
| carol@example.test | cheatsheet | refunded | evt_3UGZCSJ0skORfSym1XUXFvB7 | |
| dave@example.test | annual | active_cancelling | evt_1UGZLrJ0skORfSymntcVFtjk | sub_1UGZFrJ0skORfSym66mxVViN |
| dave@example.test | weekly | canceled | evt_1UGZJHJ0skORfSymkHn2xehp | sub_1UGZFrJ0skORfSym66mxVViN |
| erin@example.test | weekly | past_due | evt_1UGZPJJ0skORfSymhSQE2YAp | sub_1UGZN2J0skORfSymN7rxc4rW |
| grace@example.test | cheatsheet | active | evt_1UGZMYJ0skORfSymw0sAG7BQ | |
| hank@example.test | weekly | active_cancelling | evt_1UGZV0J0skORfSymL0NWSlpL | sub_1UGZQhJ0skORfSymBrOQrzrC |

`python reconcile.py` at the end: `0 customer(s) with mismatches out of 7`. 25 event ids stored.
