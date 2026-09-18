#!/usr/bin/env bash
# Start the billing app with keys decrypted from a SOPS-encrypted env file.
# Usage: BILLING_SECRETS_FILE=secrets/stripe.enc.env ./run.sh [port]
# Local dev with `stripe listen`: WEBHOOK_SECRET_FROM_CLI=1 makes the app use the CLI's
# signing secret for this account instead of the stored one. Nothing is written to disk.
set -euo pipefail
cd "$(dirname "$0")"
: "${BILLING_SECRETS_FILE:?set BILLING_SECRETS_FILE to the SOPS-encrypted env file}"
set -a
eval "$(sops -d "$BILLING_SECRETS_FILE" | grep '^STRIPE_')"
if [ -n "${WEBHOOK_SECRET_FROM_CLI:-}" ]; then
  STRIPE_WEBHOOK_SECRET=$(STRIPE_API_KEY="$STRIPE_TEST_SECRET_KEY" stripe listen --print-secret)
fi
set +a
exec .venv/bin/uvicorn app:app --port "${1:-8400}" --host 127.0.0.1
