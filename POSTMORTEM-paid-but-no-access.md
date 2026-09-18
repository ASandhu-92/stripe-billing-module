# Postmortem: paid in Stripe, no access in the app

Sandbox, 2026-09-17. Two instances of the same failure, one accidental and one staged. Both
customers had a successful payment in Stripe and no row in our `entitlements` table.

## Symptom

Customer completes Checkout, card is charged, Stripe shows the subscription active. The app treats
them as a non-customer. In the support view: `payment_state.paid = true`, `entitlements = []`,
`mismatch = true`.

## Instance A, accidental (alice, one-time purchase)

- Checkout `cs_test_a1E6MQ5vV3gqxgZ33IzWHnxsnEejdwcnextAA0exQxzPNvulc6zIxlaCN0`, paid 900,
  charge `ch_3UGZA5J0skORfSym0QEsWY5q`, customer `cus_VH7OQXONXmgp7h`, 06:52 UTC.
- Event `evt_1UGZA6J0skORfSymEtFKgwxN` checkout.session.completed, `pending_webhooks: 0`.
- Our events table: no row for that id. Our entitlements: nothing for alice.
- Root cause: `stripe listen` was logged in to the parent Stripe account, while the app's secret
  key belonged to a sandbox inside it. The listener was healthy, said "Ready", and was subscribed
  to the wrong account. `pending_webhooks: 0` is the tell: Stripe never had anywhere to send it.
  There was no delivery attempt to fail, so no retry would ever have fixed it.
- Fix: run the listener with the same key the app uses (`listen.sh` sets `STRIPE_API_KEY` from the
  same encrypted file). Then `stripe events resend evt_1UGZA6J0skORfSymEtFKgwxN`. Delivered 07:07:42
  UTC, response 200, row `alice, cheatsheet, active, evt_1UGZA6J0skORfSymEtFKgwxN` written.

## Instance B, staged (hank, Signal Weekly subscription)

Timeline, UTC:

| Time | What happened |
|---|---|
| 07:08:05 | `POST /checkout` creates `cs_test_a1QAihKqbGRxLZXt3OWnwy5tHg43mK3zQQkdMpr7A9WjxtNymjcCkZhJ8c` |
| 07:08:10 | Webhook receiver (the app on :8400) stopped on purpose |
| 07:08:25 | Customer completes Checkout with 4242. Stripe: customer `cus_VH7gfIOI9t04TE`, subscription `sub_1UGZQhJ0skORfSymBrOQrzrC` active, invoice `in_1UGZQgJ0skORfSymNqyMqH8X` paid 500, charge `ch_3UGZQgJ0skORfSym0Xv0JbmJ` succeeded |
| 07:08:25 | Stripe emits `evt_1UGZQjJ0skORfSymAfSZ7DAb` invoice.paid and `evt_1UGZQjJ0skORfSym7qfNo3zq` checkout.session.completed |
| 07:08:25 | Listener log, both events: `[ERROR] Failed to POST: Post "http://localhost:8400/webhook": dial tcp [::1]:8400: connect: connection refused` |
| 07:08:54 | Receiver restarted |
| 07:09:00 | Support view for hank: Stripe customer present, last charge paid, `entitlements: []`, mismatch `weekly stripe=active db=none sub_1UGZQhJ0skORfSymBrOQrzrC` |

Evidence the customer paid: `checkout.session.payment_status = paid`, `subscription.status = active`,
`invoice.status = paid`, `invoice.amount_paid = 500`.

Evidence we never processed it: `SELECT * FROM events WHERE id IN (...)` returned no rows;
`SELECT * FROM entitlements WHERE email = 'hank@example.test'` returned no rows.

Delivery attempt log: the two `[ERROR] Failed to POST ... connection refused` lines above. Note that
`stripe listen` does not retry. A Dashboard webhook endpoint would have retried with backoff for up
to three days, and the Dashboard would show each attempt and its response code. In production the
event page in Developers > Webhooks is where this evidence lives.

### Root cause

The receiver was down when the events were sent. The payment does not depend on our receiver, so
Stripe finished the payment and the customer saw a success page. Access depends on our receiver,
so it never happened. There is no code bug in this instance; the design has a single path from
payment to access and that path was closed.

### Fix (this incident)

Replay the two events into the restarted receiver:

```
stripe events resend evt_1UGZQjJ0skORfSym7qfNo3zq
stripe events resend evt_1UGZQjJ0skORfSymAfSZ7DAb
```

Listener log at 07:09:29: both `<-- [200] POST http://localhost:8400/webhook`. Row written:
`hank@example.test, weekly, active, evt_1UGZQjJ0skORfSymAfSZ7DAb, sub_1UGZQhJ0skORfSymBrOQrzrC`.
In the Dashboard the same action is Developers > Webhooks > endpoint > event > Resend.

### reconcile.py before and after

Before the replay:

```
ok       alice@example.test               1 entitlement(s)
ok       bob@example.test                 1 entitlement(s)
ok       carol@example.test               1 entitlement(s)
ok       dave@example.test                2 entitlement(s)
ok       erin@example.test                1 entitlement(s)
ok       grace@example.test               1 entitlement(s)
MISMATCH hank@example.test                weekly     stripe=active             db=none               sub_1UGZQhJ0skORfSymBrOQrzrC

1 customer(s) with mismatches out of 7
```

After the replay:

```
ok       alice@example.test               1 entitlement(s)
ok       bob@example.test                 1 entitlement(s)
ok       carol@example.test               1 entitlement(s)
ok       dave@example.test                2 entitlement(s)
ok       erin@example.test                1 entitlement(s)
ok       grace@example.test               1 entitlement(s)
ok       hank@example.test                1 entitlement(s)

0 customer(s) with mismatches out of 7
```

### Prevention

1. `reconcile.py` on a schedule. It asks Stripe for every customer's subscriptions and paid
   Checkout sessions, compares with `entitlements`, and exits non-zero with the list of mismatches.
   Every ten minutes is enough for a product this size. A mismatch that persists for two runs pages
   someone; one run can be a webhook still in flight.
2. A second path on the success page. `GET /checkout/success` currently just says "access is
   granted by the webhook". It should retrieve the session, and if `payment_status = paid` and we
   have no row, apply the same handler with the session as input. That covers the receiver-down
   window for the customer sitting in front of the screen. The webhook stays the source of truth
   for everything that happens later (renewals, cancellations, refunds).
3. Monitor the receiver, not just the app. A `/health` that answers 200 while the webhook route is
   broken tells you nothing. Stripe's Dashboard shows failing endpoints; wire that to the alerting
   in production, or at minimum check "Webhooks: failing" once a day.
4. Keep the listener and the app on the same key source. Instance A was two healthy components
   pointed at two different accounts.

### Customer reply (three sentences)

Thank you for flagging this, and sorry for the trouble. Your payment went through on our side and
I have just corrected your account, so Signal Weekly is active for you now; please sign out and back
in if you still see the old state. If anything else looks wrong on your account or your receipt,
reply here and I will sort it out directly.

### Engineering escalation (five lines)

1. Customer hank@example.test paid for Signal Weekly at 07:08:25 UTC (`sub_1UGZQhJ0skORfSymBrOQrzrC`, `in_1UGZQgJ0skORfSymNqyMqH8X` paid 500) and had no entitlement row until 07:09:29.
2. Cause: webhook receiver was down 07:08:10 to 07:08:54; `evt_1UGZQjJ0skORfSym7qfNo3zq` and `evt_1UGZQjJ0skORfSymAfSZ7DAb` failed with connection refused; no retry because the listener is the CLI.
3. Fixed for this customer by resending both events; `reconcile.py` now reports 0 mismatches.
4. Ask: schedule `reconcile.py`, add the success-page fallback, and alert on webhook endpoint failures rather than on `/health`.
5. Blast radius check: any other customer who paid in that 44 second window would be in the same state; reconcile found none.
