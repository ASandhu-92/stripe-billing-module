#!/usr/bin/env bash
# Forward this account's test events to the local app. Same secret source as run.sh.
set -euo pipefail
cd "$(dirname "$0")"
: "${BILLING_SECRETS_FILE:?set BILLING_SECRETS_FILE to the SOPS-encrypted env file}"
eval "$(sops -d "$BILLING_SECRETS_FILE" | grep '^STRIPE_TEST_SECRET_KEY=')"
export STRIPE_API_KEY="$STRIPE_TEST_SECRET_KEY"
EVENTS=checkout.session.completed,invoice.paid,invoice.payment_failed,customer.subscription.updated,customer.subscription.deleted,charge.refunded
exec stripe listen --forward-to "localhost:${1:-8400}/webhook" --events "$EVENTS"
