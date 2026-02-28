import main
from fastapi.testclient import TestClient

client = TestClient(main.app)


def reset_memory():
    main.EVENT_STORE.clear()
    main.SEEN_EVENT_IDS.clear()
    main.PAGERDUTY_STORE.clear()


def test_pull_status_stores_events(monkeypatch):
    reset_memory()

    # Avoid real HTTP routing during test
    async def fake_route(event):
        return True

    monkeypatch.setattr(main, "route_to_pagerduty", fake_route)

    resp = client.post("/ingest/pull-status")
    assert resp.status_code == 200

    body = resp.json()
    assert "fetched" in body
    assert body["stored"] >= 1

    events = client.get("/events").json()["events"]
    assert len(events) == body["stored"]


def test_pull_status_dedupes_on_second_run(monkeypatch):
    reset_memory()

    async def fake_route(event):
        return True

    monkeypatch.setattr(main, "route_to_pagerduty", fake_route)

    r1 = client.post("/ingest/pull-status").json()
    r2 = client.post("/ingest/pull-status").json()

    assert r1["stored"] >= 1
    assert r2["stored"] == 0  # same fixture IDs should be deduped


def test_pull_status_routes_critical(monkeypatch):
    reset_memory()

    # Ensure warnings don't route by default
    monkeypatch.setenv("ROUTE_WARNING", "false")

    async def fake_route(event):
        return True

    monkeypatch.setattr(main, "route_to_pagerduty", fake_route)

    resp = client.post("/ingest/pull-status").json()

    # At least the Spreedly incident impact=major should become critical
    assert resp["routed"] >= 1