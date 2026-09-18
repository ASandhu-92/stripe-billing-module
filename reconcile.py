"""List every place Stripe and the entitlements table disagree.

    python reconcile.py            # all emails known to either side
    python reconcile.py a@b.test   # one email

Exit code is the number of mismatching customers (0 = clean), so it can run from cron.
"""
import sys

import stripe

import models
from stripe_client import configure
from support import support_view


def main(argv) -> int:
    configure()
    conn = models.connect()
    emails = {e.lower() for e in argv[1:]}
    if not emails:
        emails = {r["email"] for r in models.get_entitlements(conn)}
        for c in stripe.Customer.list(limit=100).auto_paging_iter():
            if c.email:
                emails.add(c.email.lower())
    bad = 0
    for email in sorted(emails):
        view = support_view(conn, email)
        if view["mismatch"]:
            bad += 1
            for m in view["mismatches"]:
                print(f"MISMATCH {email:32} {m['product']:10} stripe={m['stripe']:<18} "
                      f"db={m['db']:<18} {m.get('stripe_object', '')}")
        else:
            print(f"ok       {email:32} {len(view['entitlements'])} entitlement(s)")
    print(f"\n{bad} customer(s) with mismatches out of {len(emails)}")
    return bad


if __name__ == "__main__":
    sys.exit(main(sys.argv))
