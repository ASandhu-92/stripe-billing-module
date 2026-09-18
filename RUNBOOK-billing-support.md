# Billing support runbook

For anyone answering billing tickets on a product built on this module. Every check below is a
field in `GET /support/{email}` unless it says Dashboard. Verify the person first: the email they
write from must match `email` on the customer record, or they must answer from inside the logged-in
app. Never read card numbers, keys or webhook secrets into a ticket.

Fields in the support view: `stripe_customer`, `subscriptions[]` (id, status, product,
cancel_at_period_end, cancel_at, canceled_at, current_period_end), `last_invoices[]` (id, status,
amount_due, amount_paid, attempt_count, billing_reason), `payment_state` (last charge: status, paid,
refunded, amount_refunded, failure_code), `entitlements[]` (product, status, source_event_id),
`mismatches[]`, `mismatch`.

Product statuses on our side: `active`, `active_cancelling` (ends at period end, still has access),
`past_due` (renewal failed, still has access while Stripe retries), `canceled`, `refunded`.

## 1. "I cancelled but was charged"

Check, in this order:

1. `subscriptions[].canceled_at` and `cancel_at` or `cancel_at_period_end`. A cancel at period end
   leaves the subscription active until `current_period_end`; no new charge should appear after
   that date.
2. `last_invoices[]`: find the charged invoice. Compare its `created` with `canceled_at`. If the
   invoice is before the cancel, it is the last period they were entitled to.
3. If the invoice is after `cancel_at`, or the subscription has no cancel at all: the cancel did
   not take. Check whether there is a second subscription (`subscriptions[]` shows all statuses).
   Customers often cancel one and keep another.
4. `payment_state.refunded` to see whether someone already refunded it.

Answer separately for the cancel and for the money. The cancel is a fact about the subscription.
The refund is a policy decision: refund the last invoice if the cancel was before the charge, or
if the policy allows a goodwill refund. Refund through the Dashboard (Payments > charge > Refund);
the `charge.refunded` webhook updates our side.

Escalate when: the subscription shows canceled in Stripe and our `entitlements` still says
`active` (that is a `mismatch` row), or when there is a charge with no invoice behind it.

## 2. "My upgrade price looks wrong"

1. `subscriptions[].product` confirms the plan they are on now.
2. `last_invoices[]`: look for `billing_reason=subscription_update` or a manual invoice with two
   lines. A plan change with prorations produces a credit line for unused time on the old plan and
   a charge line for the remaining time on the new plan. The net is what they pay, not the sticker
   price. The Dashboard invoice page shows the lines with dates.
3. If no such invoice exists, the proration is sitting as pending invoice items and will be added
   to the next renewal. Tell the customer exactly that and when the next renewal is
   (`current_period_end`). Case 3 in `CASES.md` is the worked example: monthly to annual, nothing
   charged today, $75 at the next renewal a year out.
4. Check the previous invoice was paid. Unpaid time on the old plan is not credited, which makes
   the number look smaller than the customer computed.

Escalate when: the credit line is missing on an invoice that should have one, or the amounts do
not add up to price difference times fraction of period. That is a proration setting problem, not
a support problem.

## 3. "You refunded me but I cannot see it"

1. `payment_state.refunded` and `amount_refunded`. If both are zero, no refund exists; find out who
   promised one and create it.
2. Dashboard > the charge > Refunds: the refund's `status` (`pending`, `succeeded`, `failed`,
   `canceled`) and its date. A refund can fail if the card is closed; the money then needs a
   different route.
3. Give the customer the date and the fact that a refund shows as a reversal of the original
   charge, not a new deposit, and takes 5 to 10 business days to appear depending on their bank.
   Offer the acquirer reference number from the refund page if their bank asks.

Escalate when: refund `status=failed`, or a refund is `succeeded` for more than 10 business days and
the customer's bank has nothing.

## 4. "I paid but still cannot use the app"

1. `payment_state.paid` and `last_invoices[0].status` confirm the money side.
2. `entitlements[]`: is there a row for the product, and what is its `status`?
3. `mismatch`: if true, the two sides disagree and the `mismatches[]` entry names the product and
   the Stripe object behind the expectation.
4. If there is no row: find the event in Dashboard > Developers > Events (filter by the customer),
   check its webhook delivery attempts and response codes, and resend it to the endpoint. Then
   re-run the support view; `mismatch` should turn false.
5. If there is a row but `status=past_due`: their renewal failed. That is case 5 below, not this one.

Do not tell the customer "the payment failed" because they cannot log in. Payment and access are
two systems. Say that the payment is confirmed and the account is being corrected.

Escalate when: the event was delivered with a 200 and there is still no row (handler bug), or
`reconcile.py` shows more than one customer in this state (systemic, not one ticket).
Postmortem template: `POSTMORTEM-paid-but-no-access.md`.

## 5. "My AI credits disappeared"

This product does not sell credits, so this is the shape of the investigation, not a policy.

1. Confirm the plan and its state: `subscriptions[].status`, `entitlements[].status`. A
   `past_due` or `canceled` plan usually explains credits going away if the product ties credits to
   an active plan.
2. Find the credit policy in writing: do credits reset each period, roll over, or get revoked on a
   failed renewal? The answer is in the product, not in Stripe. Stripe knows about invoices and
   periods; the application knows about credits.
3. Line up the invoice period (`last_invoices[0]` and `current_period_end`) with the day the
   credits changed. A reset on the renewal date is expected behaviour, and the customer needs the
   policy explained. A change on any other day is a bug.

Escalate when: credits changed and no invoice, renewal, plan change or failure happened that day.
Include the customer id, the subscription id and the timestamp of the change.

## When to touch Stripe yourself and when not

Fine to do from the Dashboard: refunds inside policy, resending a webhook event, cancelling at
period end on the customer's request, updating a customer's email.

Ask first: refunds outside policy, cancelling immediately with a refund, anything on a disputed
charge (a dispute is its own process with a deadline; do not refund a disputed charge without
checking), and any edit to products, prices or the webhook endpoint.

Never: type card numbers into anything, share `sk_` or `whsec_` values, delete Stripe objects.
