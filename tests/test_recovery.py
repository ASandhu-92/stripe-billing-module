"""What happens to an event when processing stops part way. See models.process_event."""
import json
import subprocess
import sys

import fixtures as fx
from conftest import ROOT, post_event

# Run the real handler on one event in a child process, then kill the process before the commit.
KILL_MID_EVENT = """
import json, os, sys
sys.path.insert(0, {root!r})
import models
from handlers import handle
from stripe_client import load_prices
event = json.loads({event!r})
conn = models.connect({db!r})
def work(c):
    handle(c, event, load_prices())
    os._exit(9)  # killed after the handler wrote, before the commit
models.process_event(conn, event["id"], event["type"], work)
"""


def counts(db):
    return (db.execute("SELECT count(*) FROM events").fetchone()[0],
            db.execute("SELECT count(*) FROM entitlements").fetchone()[0])


def test_process_killed_after_claim_leaves_nothing_and_the_retry_is_processed(client, db, tmp_path):
    event = fx.checkout_completed()
    script = KILL_MID_EVENT.format(root=str(ROOT), event=json.dumps(event), db=str(tmp_path / "test.db"))
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 9, proc.stderr
    assert counts(db) == (0, 0)  # neither the event id nor the half-done write survived

    r = post_event(client, event)  # Stripe's retry
    assert r.status_code == 200 and r.json().get("duplicate") is None and r.json()["applied"] is True
    assert counts(db) == (1, 1)


def test_repeat_after_success_is_a_duplicate_with_no_write(client, db):
    assert post_event(client, fx.checkout_completed()).json()["applied"] is True
    r = post_event(client, fx.checkout_completed())
    assert r.status_code == 200 and r.json()["duplicate"] is True
    assert counts(db) == (1, 1)


def test_failure_after_a_partial_write_rolls_the_write_back(client, db, monkeypatch):
    import app as app_module
    from handlers import handle as real_handle

    def write_then_fail(conn, event, prices):
        real_handle(conn, event, prices)
        raise RuntimeError("failed after the entitlement write")
    monkeypatch.setattr(app_module, "handle", write_then_fail)
    r = post_event(client, fx.checkout_completed())
    assert r.status_code == 500
    assert counts(db) == (0, 0)


def test_replay_after_a_failure_is_processed(client, db, monkeypatch):
    import app as app_module
    from handlers import handle as real_handle

    def boom(*a, **k):
        raise RuntimeError("temporary failure")
    monkeypatch.setattr(app_module, "handle", boom)
    assert post_event(client, fx.checkout_completed()).status_code == 500
    monkeypatch.setattr(app_module, "handle", real_handle)
    r = post_event(client, fx.checkout_completed())
    assert r.status_code == 200 and r.json()["applied"] is True
    assert counts(db) == (1, 1)


def test_stale_processing_row_from_the_old_version_is_cleared(tmp_path):
    import models
    path = str(tmp_path / "old.db")
    conn = models.connect(path)
    conn.execute("INSERT INTO events (id, type, status, received_at) "
                 "VALUES ('evt_old', 'checkout.session.completed', 'processing', '2026-09-17T00:00:00+00:00')")
    conn.commit()
    conn.close()
    conn = models.connect(path)
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
