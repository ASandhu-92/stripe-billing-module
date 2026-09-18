import json

import fixtures as fx
from conftest import post_event, sign


def rows(db, email):
    return {r["product"]: dict(r) for r in db.execute(
        "SELECT * FROM entitlements WHERE email = ?", (email,)).fetchall()}


def test_bad_signature_is_400(client, db):
    body = json.dumps(fx.checkout_completed()).encode()
    r = client.post("/webhook", content=body,
                    headers={"stripe-signature": sign(body, "whsec_wrong_secret")})
    assert r.status_code == 400
    assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_missing_signature_is_400(client):
    r = client.post("/webhook", content=b"{}")
    assert r.status_code == 400


def test_unhandled_type_is_acknowledged(client, db):
    r = post_event(client, fx.event("evt_x", "customer.created", {"id": "cus_x", "object": "customer"}))
    assert r.status_code == 200 and r.json()["ignored"] == "customer.created"
    assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_one_time_purchase_grants_access(client, db):
    r = post_event(client, fx.checkout_completed())
    assert r.status_code == 200 and r.json()["applied"] is True
    row = rows(db, "alice@example.test")["cheatsheet"]
    assert row["status"] == "active" and row["source_event_id"] == "evt_co_1"


def test_unpaid_checkout_grants_nothing(client, db):
    post_event(client, fx.checkout_completed(payment_status="unpaid"))
    assert rows(db, "alice@example.test") == {}


def test_duplicate_event_is_200_without_second_write(client, db):
    post_event(client, fx.checkout_completed())
    before = rows(db, "alice@example.test")["cheatsheet"]
    r = post_event(client, fx.checkout_completed())
    assert r.status_code == 200 and r.json()["duplicate"] is True
    after = rows(db, "alice@example.test")["cheatsheet"]
    assert after == before
    assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_invoice_paid_activates_subscription_product(client, db):
    r = post_event(client, fx.invoice("evt_ip_1", "invoice.paid"))
    assert r.json()["applied"] is True
    row = rows(db, "bob@example.test")["weekly"]
    assert row["status"] == "active" and row["subscription_id"] == "sub_1"


def test_invoice_payment_failed_marks_past_due(client, db):
    post_event(client, fx.invoice("evt_ip_1", "invoice.paid"))
    post_event(client, fx.invoice("evt_ipf_1", "invoice.payment_failed", status="open"))
    assert rows(db, "bob@example.test")["weekly"]["status"] == "past_due"


def test_subscription_updated_cancel_at_period_end(client, db):
    post_event(client, fx.invoice("evt_ip_1", "invoice.paid"))  # teaches us cus_bob's email
    post_event(client, fx.subscription("evt_su_1", "customer.subscription.updated",
                                       cancel_at_period_end=True))
    assert rows(db, "bob@example.test")["weekly"]["status"] == "active_cancelling"


def test_portal_cancel_sets_cancel_at_not_the_flag(client, db):
    """Observed live: the hosted portal leaves cancel_at_period_end False and sets cancel_at."""
    post_event(client, fx.invoice("evt_ip_1", "invoice.paid"))
    post_event(client, fx.subscription("evt_su_p", "customer.subscription.updated",
                                       cancel_at=1792220901))
    assert rows(db, "bob@example.test")["weekly"]["status"] == "active_cancelling"


def test_plan_change_supersedes_old_product(client, db):
    post_event(client, fx.invoice("evt_ip_1", "invoice.paid"))
    r = post_event(client, fx.subscription("evt_su_2", "customer.subscription.updated", product="annual"))
    assert r.json()["superseded"] == ["weekly"]
    got = rows(db, "bob@example.test")
    assert got["annual"]["status"] == "active" and got["weekly"]["status"] == "canceled"


def test_proration_invoice_resolves_to_the_new_product(client, db):
    """Two lines, one per product; the credit line comes first. Old code picked 'weekly'."""
    post_event(client, fx.invoice("evt_ip_1", "invoice.paid"))
    lines = [fx._line("weekly", -500, proration=True), fx._line("annual", 4000, proration=True)]
    ev = fx.invoice("evt_ip_2", "invoice.paid", subscription=None, lines=lines)
    r = post_event(client, ev)
    assert r.json()["product"] == "annual"
    got = rows(db, "bob@example.test")
    assert got["annual"]["status"] == "active" and got["weekly"]["status"] == "canceled"


def test_subscription_deleted_cancels(client, db):
    post_event(client, fx.invoice("evt_ip_1", "invoice.paid"))
    post_event(client, fx.subscription("evt_sd_1", "customer.subscription.deleted", status="canceled"))
    assert rows(db, "bob@example.test")["weekly"]["status"] == "canceled"


def test_full_refund_revokes_partial_does_not(client, db):
    post_event(client, fx.checkout_completed("evt_co_c", "carol@example.test", customer="cus_carol"))
    r = post_event(client, fx.charge_refunded("evt_cr_1", refunded=False, amount_refunded=400))
    assert r.json()["applied"] is False
    assert rows(db, "carol@example.test")["cheatsheet"]["status"] == "active"
    post_event(client, fx.charge_refunded("evt_cr_2"))
    assert rows(db, "carol@example.test")["cheatsheet"]["status"] == "refunded"


def test_out_of_order_delivery_still_ends_active(client, db):
    """checkout.session.completed after invoice.paid, as observed live for dave."""
    post_event(client, fx.invoice("evt_ip_1", "invoice.paid", email="dave@example.test",
                                  customer="cus_dave"))
    post_event(client, fx.checkout_completed("evt_co_d", "dave@example.test", product="weekly",
                                             customer="cus_dave", subscription="sub_1"))
    row = rows(db, "dave@example.test")["weekly"]
    assert row["status"] == "active" and row["subscription_id"] == "sub_1"


def test_handler_failure_returns_500_and_forgets_event(client, db, monkeypatch):
    import app as app_module

    def boom(*a, **k):
        raise RuntimeError("db on fire")
    monkeypatch.setattr(app_module, "handle", boom)
    r = post_event(client, fx.checkout_completed())
    assert r.status_code == 500
    assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
