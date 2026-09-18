import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Placeholder values: the tests never call the network, and the catalogue is the example file.
os.environ["STRIPE_TEST_SECRET_KEY"] = "sk_test_unit_tests_never_call_the_network"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_unit_test_secret"
os.environ["BILLING_PRICES_FILE"] = str(ROOT / "prices.example.json")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLING_DB", str(tmp_path / "test.db"))
    import models
    monkeypatch.setattr(models, "DB_PATH", str(tmp_path / "test.db"))
    from fastapi.testclient import TestClient
    import app as app_module
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture()
def db(client):
    import models
    return models.connect()


def sign(payload: bytes, secret: str = "whsec_unit_test_secret", ts: int | None = None) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def post_event(client, event: dict, secret: str = "whsec_unit_test_secret"):
    body = json.dumps(event).encode()
    return client.post("/webhook", content=body,
                       headers={"stripe-signature": sign(body, secret),
                                "content-type": "application/json"})
