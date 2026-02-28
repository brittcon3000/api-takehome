import json
import os
from fastapi.testclient import TestClient

import main


client = TestClient(main.app)


def test_stripe_missing_signature_returns_400():
    resp = client.post("/ingest/stripe", json={})
    assert resp.status_code == 400


def test_stripe_dedupes_duplicate_event(monkeypatch):
    # Arrange: mock Stripe signature verification to return a known event
    with open("fixtures/stripe/payment_failed.json", "r") as f:
        event = json.load(f)

    def fake_construct_event(payload, sig_header, secret):
        return event

    monkeypatch.setattr(main.stripe.Webhook, "construct_event", fake_construct_event)

    # Need a signature header present to pass initial check
    headers = {"Stripe-Signature": "t=fake,v1=fake"}

    # Act 1: first call stores
    r1 = client.post("/ingest/stripe", content=json.dumps(event), headers=headers)
    assert r1.status_code == 200
    assert r1.json().get("received") is True

    # Act 2: second call dedupes
    r2 = client.post("/ingest/stripe", content=json.dumps(event), headers=headers)
    assert r2.status_code == 200
    assert r2.json().get("deduped") is True


def test_stripe_critical_routes_to_pagerduty(monkeypatch):
    # Arrange fixture: payout.failed is critical
    with open("fixtures/stripe/payout_failed.json", "r") as f:
        event = json.load(f)

    def fake_construct_event(payload, sig_header, secret):
        return event

    monkeypatch.setattr(main.stripe.Webhook, "construct_event", fake_construct_event)

    # Mock route_to_pagerduty to avoid real HTTP call
    async def fake_route(event):
        return True

    monkeypatch.setattr(main, "route_to_pagerduty", fake_route)

    headers = {"Stripe-Signature": "t=fake,v1=fake"}

    resp = client.post("/ingest/stripe", content=json.dumps(event), headers=headers)
    assert resp.status_code == 200

    # Confirm stored event metadata was updated
    events = client.get("/events").json()["events"]
    assert len(events) >= 1
    assert events[0]["event_id"] == event["id"]
    assert events[0]["severity"] == "critical"
    assert events[0]["routed"] is True
    assert "pagerduty" in events[0]["delivered_to"]