"""Billing module: Stripe Checkout, webhook, customer portal and a support view.

Run (keys exported from the encrypted env file first, see run.sh):
    uvicorn app:app --port 8400
"""
import os
from contextlib import asynccontextmanager

import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

import models
from handlers import HANDLED, handle
from stripe_client import configure, find_customers, load_prices, mode_for, webhook_secret
from support import support_view

BASE_URL = os.environ.get("BILLING_BASE_URL", "http://localhost:8400")

@asynccontextmanager
async def lifespan(_app):
    configure()
    models.connect().close()  # creates the schema
    yield


app = FastAPI(title="billing (sandbox)", lifespan=lifespan)


def db():
    return models.connect()


class CheckoutIn(BaseModel):
    email: str
    product: str  # cheatsheet | weekly | annual


@app.post("/checkout")
def create_checkout(body: CheckoutIn):
    prices = load_prices()
    if body.product not in prices or body.product.startswith("_"):
        raise HTTPException(400, f"unknown product {body.product}")
    mode = mode_for(body.product)
    params = {
        "mode": mode,
        "customer_email": body.email,
        "line_items": [{"price": prices[body.product]["price_id"], "quantity": 1}],
        "success_url": f"{BASE_URL}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{BASE_URL}/checkout/cancel",
        "metadata": {"product": body.product},
    }
    if mode == "subscription":
        params["subscription_data"] = {"metadata": {"product": body.product}}
    else:
        params["customer_creation"] = "always"
        params["payment_intent_data"] = {"metadata": {"product": body.product}}
    session = stripe.checkout.Session.create(**params)
    return {"id": session.id, "url": session.url, "mode": mode}


@app.get("/checkout/success")
def checkout_success(session_id: str):
    return {"ok": True, "session_id": session_id,
            "note": "access is granted by the webhook, not by reaching this page"}


@app.get("/checkout/cancel")
def checkout_cancel():
    return {"ok": False, "note": "checkout abandoned"}


@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, webhook_secret())
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        raise HTTPException(400, f"invalid payload or signature: {exc.__class__.__name__}")

    if event["type"] not in HANDLED:
        return {"received": True, "ignored": event["type"]}

    conn = db()
    try:
        prices = load_prices()
        try:
            result = models.process_event(conn, event["id"], event["type"],
                                          lambda c: handle(c, event, prices))
        except models.Duplicate:
            return {"received": True, "duplicate": True, "event": event["id"]}
        except Exception as exc:  # rolled back, event id included: Stripe's retry is processed
            return JSONResponse(status_code=500, content={"error": str(exc), "event": event["id"]})
        return {"received": True, "event": event["id"], "type": event["type"], **result}
    finally:
        conn.close()


@app.get("/portal")
def portal(email: str, redirect: bool = False):
    customers = find_customers(email, limit=1)
    if not customers:
        raise HTTPException(404, "no Stripe customer with that email")
    prices = load_prices()
    session = stripe.billing_portal.Session.create(
        customer=customers[0].id,
        configuration=prices.get("_portal_configuration"),
        return_url=f"{BASE_URL}/portal/return",
    )
    if redirect:
        return RedirectResponse(session.url, status_code=303)
    return {"url": session.url, "customer": customers[0].id}


@app.get("/portal/return")
def portal_return():
    return {"ok": True, "note": "changes made in the portal arrive through the webhook"}


@app.get("/support/{email}")
def support(email: str):
    conn = db()
    try:
        return support_view(conn, email)
    finally:
        conn.close()


@app.get("/events")
def events(limit: int = 20):
    conn = db()
    try:
        return models.list_events(conn, limit)
    finally:
        conn.close()


@app.get("/health")
def health():
    return {"ok": True}
