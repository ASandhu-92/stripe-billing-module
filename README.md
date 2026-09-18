# stripe-billing-module

Stripe Checkout, subscriptions, the hosted customer portal, webhooks, and a support view, in about
900 lines of Python. Built in a Stripe sandbox as the billing piece of a small content product, and
used to learn how billing tickets actually look from the inside. Test mode only. No live payments
were ever taken, and nothing in this tree has touched a live key.

The interesting part is not the Checkout call. It is what happens after the customer pays:
webhooks arriving out of order, a plan change that puts two products on one invoice, the portal
cancelling in a different way than the API, a partial refund firing the same event as a full one,
and a receiver that was down when the payment happened. Each of those was run against the sandbox
and written up.

Read `CASES.md` for what was run and what Stripe and the database showed for each case,
`POSTMORTEM-paid-but-no-access.md` for the failure that was staged on purpose, and
`RUNBOOK-billing-support.md` for how to answer the five common billing tickets against this data.

## What it does

| Route | Purpose |
|---|---|
| `POST /checkout` `{email, product}` | Creates a Checkout Session in `payment` or `subscription` mode and returns its URL |
| `POST /webhook` | Verifies the Stripe signature, records `event.id` before doing anything, returns 200 on a repeat without re-applying, handles the six events below |
| `GET /portal?email=` | Creates a customer portal session (cancel, change plan, update card) |
| `GET /support/{email}` | Customer, subscriptions, last 5 invoices, last charge, our entitlement rows, and `mismatches` when Stripe and the database disagree |
| `GET /events` | The event ids we have accepted |

Events handled: `checkout.session.completed`, `invoice.paid`, `invoice.payment_failed`,
`customer.subscription.updated`, `customer.subscription.deleted`, `charge.refunded`.

Products (test mode): a $9 one-time purchase, a $5 a month subscription, a $40 a year
subscription. `setup_products.py` creates them and writes their ids to `prices.json`.

## Files

```
app.py               routes
handlers.py          event -> entitlement rules (no network except an email lookup for unknown customers)
models.py            SQLite: events, entitlements, customers
support.py           the support view and the mismatch check
stripe_client.py     key loading (env only), price catalogue, test-clock-aware customer lookup
setup_products.py    creates products, prices and the portal configuration; idempotent
reconcile.py         lists every Stripe vs database mismatch; exit code = number of bad customers
lab_cases.py         drives the API side of the cases (plan change, cancel, refund, test clocks)
run.sh, listen.sh    start the app and `stripe listen` with keys from an encrypted env file
prices.example.json  the shape of prices.json, with placeholder ids; the tests use it
tests/               pytest, 16 tests, no network
```

## Run it

Needs Python 3.12, the Stripe CLI, `sops`, and a Stripe sandbox with its secret key and the CLI's
webhook signing secret in a SOPS-encrypted env file (`.env.example` lists the names). Any other
way of getting the three `STRIPE_*` variables into the environment works too; the scripts just
never read a plaintext file.

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export BILLING_SECRETS_FILE=secrets/stripe.enc.env
.venv/bin/python setup_products.py           # once; writes prices.json
WEBHOOK_SECRET_FROM_CLI=1 ./run.sh 8400      # app, using the CLI's signing secret
./listen.sh 8400                             # in another terminal
.venv/bin/python -m pytest tests -q
```

The app refuses a key that does not start with `sk_test_`.

## What it does not do

- No live mode. There is no code path for it and the key check blocks it.
- No UI. Checkout and the portal are Stripe's hosted pages; everything else is JSON.
- No retries of its own. A failed handler returns 500 and forgets the event id, so Stripe's
  retry gets a clean attempt. That is deliberate; see the postmortem for why a CLI listener is
  not the same as a Dashboard endpoint here.
- No authentication on `/portal`, `/support` and `/events`. They are meant to be called by the
  product's own logged-in backend on localhost, never exposed to the internet as they are.
- No success-page fallback yet. Access comes from the webhook only. The postmortem lists this
  as the first thing to add.

## Things learned the hard way

- The Stripe CLI login and the app's secret key must point at the same account. A CLI paired with
  the parent account while the app uses a sandbox key gives a healthy-looking listener that never
  receives anything, and `pending_webhooks: 0` on every event.
- Stripe does not order deliveries. `checkout.session.completed` arrived after `invoice.paid` for a
  live subscription. Handlers must be safe in any order.
- A proration invoice carries lines for both the old and the new product. The first line is the
  credit for the old plan. Attribute the invoice to the product being charged for, not line one.
- The hosted portal cancels by setting `cancel_at`, not `cancel_at_period_end`. Read both.
- `Customer.list(email=)` skips customers on a test clock. `Customer.search` finds them.
- With `proration_behavior=create_prorations`, a month-to-year upgrade charges nothing today and
  puts the difference on the next invoice, a year out. Use `always_invoice` if that is not the
  intended experience.
- Cancelling never refunds. Cancel and refund are two decisions and two API calls.
- `charge.refunded` fires for partial refunds too; check the boolean `refunded` on the charge.

## About

Written in September 2026 over two days, drafted with Claude Code, reviewed, run, debugged and
maintained by me. The three bugs described in `CASES.md` were found by running the cases, not by
reading the code. MIT licence.
