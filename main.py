from __future__ import annotations

import os
import httpx
from dotenv import load_dotenv
import stripe
import uuid
import structlog
from fastapi import Request, HTTPException, FastAPI, Response
from datetime import datetime, timezone
from models import NormalizedEvent, StoredEvent
import json
from pathlib import Path

from typing import List

load_dotenv()

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()

STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
if not STRIPE_WEBHOOK_SECRET:
    raise RuntimeError("Missing required env var: STRIPE_WEBHOOK_SECRET")

app = FastAPI(title="Vendor Signal Normalization & Routing")

# Middleware
@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    response: Response = await call_next(request)
    response.headers["x-request-id"] = request_id
    log.info("request_complete", request_id=request_id, path=request.url.path, method=request.method, status_code=response.status_code)
    return response

# In-memory storage (newest first)
EVENT_STORE: List[StoredEvent] = []

PAGERDUTY_STORE: List[StoredEvent] = []

# Idempotency tracking
SEEN_EVENT_IDS: set[str] = set()

MAX_EVENTS = 500

def store_event(event: StoredEvent) -> tuple[bool, bool]:
    """
    Stores event if not seen before.

    Returns:
        (stored, deduped)
    """
    if event.event_id in SEEN_EVENT_IDS:
        return False, True  # not stored, deduped

    SEEN_EVENT_IDS.add(event.event_id)

    # newest first
    EVENT_STORE.insert(0, event)

    # trim store to max size
    if len(EVENT_STORE) > MAX_EVENTS:
        EVENT_STORE.pop()

    return True, False

def should_route(severity: str) -> bool:
    if severity == "critical":
        return True
    if severity == "warning":
        return os.environ.get("ROUTE_WARNING", "false").lower() == "true"
    return False

async def route_to_pagerduty(event: StoredEvent) -> bool:
    """
    Best-effort routing. Returns True if destination accepted the event.
    """
    base_url = os.environ.get("SELF_BASE_URL", "http://localhost:3000")
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
            resp = await client.post("/destinations/pagerduty", json=event.model_dump(mode="json"))
        return resp.status_code == 202
    except Exception as e:
        log.warning("pagerduty_route_failed", error=str(e), event_id=event.event_id)
        return False

def normalize_stripe_event(event: dict) -> NormalizedEvent | None:
    event_type = event.get("type")
    event_id = event.get("id")
    created = event.get("created")
    obj_id = (((event.get("data") or {}).get("object")) or {}).get("id")

    if not (event_type and event_id and created and obj_id):
        return None

    if event_type == "payout.failed":
        severity = "critical"
    elif event_type == "payment_intent.payment_failed":
        severity = "warning"
    else:
        return None  # ignore other event types for this exercise

    started_at = datetime.fromtimestamp(int(created), tz=timezone.utc)

    return NormalizedEvent(
        event_id=event_id,
        source="stripe",
        kind="payment",
        severity=severity,
        service="stripe",
        summary=f"{event_type}: {obj_id}",
        description=None,
        started_at=started_at,
        resolved_at=None,
        raw=event,
    )

def map_incident_severity(impact: str) -> str:
    if impact in ("critical", "major"):
        return "critical"
    if impact == "minor":
        return "warning"
    return "info"


def map_component_severity(status: str) -> str | None:
    if status == "degraded_performance":
        return "warning"
    if status in ("partial_outage", "major_outage"):
        return "critical"
    return None  # operational or anything else - ignore


def normalize_statuspage_summary(data: dict, source: str) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []

    # Incidents
    for inc in data.get("incidents", []):
        severity = map_incident_severity(inc.get("impact", ""))
        event = NormalizedEvent(
            event_id=inc["id"],
            source=source,
            kind="incident",
            severity=severity,
            service=data.get("page", {}).get("name", source),
            summary=inc.get("name", ""),
            description=None,
            started_at=datetime.fromisoformat(inc["created_at"].replace("Z", "+00:00")),
            resolved_at=(
                datetime.fromisoformat(inc["resolved_at"].replace("Z", "+00:00"))
                if inc.get("resolved_at")
                else None
            ),
            raw=inc,
        )
        events.append(event)

    # Components
    for comp in data.get("components", []):
        severity = map_component_severity(comp.get("status", ""))
        if not severity:
            continue

        event = NormalizedEvent(
            event_id=comp["id"],
            source=source,
            kind="status",
            severity=severity,
            service=data.get("page", {}).get("name", source),
            summary=f"{comp.get('name')} is {comp.get('status')}",
            description=None,
            started_at=datetime.fromisoformat(comp["updated_at"].replace("Z", "+00:00")),
            resolved_at=None,
            raw=comp,
        )
        events.append(event)

    return events

ROOT_DIR = Path(__file__).resolve().parent

async def fetch_json(url: str) -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


def load_fixture(rel_path: str) -> dict:
    path = ROOT_DIR / rel_path
    with open(path, "r") as f:
        return json.load(f)

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/events")
async def get_events(limit: int = 50):
    # basic guardrails
    limit = max(1, min(500, int(limit)))
    return {"events": [e.model_dump(mode="json") for e in EVENT_STORE[:limit]]}

@app.post("/ingest/stripe")
async def ingest_stripe(request: Request):
    sig = request.headers.get("stripe-signature")
    if not sig:
        raise HTTPException(status_code=400, detail="Missing Stripe-Signature header")

    raw_body = await request.body()  # IMPORTANT: raw body required for Stripe signature verification

    try:
        verified_event = stripe.Webhook.construct_event(
            payload=raw_body,
            sig_header=sig,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception:
        log.warning("stripe_signature_verification_failed")
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")

    normalized = normalize_stripe_event(verified_event)
    if not normalized:
        return {"received": True, "ignored": True}

    stored_event = StoredEvent(**normalized.model_dump(), routed=False, delivered_to=[])
    stored, deduped = store_event(stored_event)

    if deduped:
        return {"received": True, "deduped": True}
    
    if should_route(stored_event.severity):
        ok = await route_to_pagerduty(stored_event)
        if ok:
            stored_event.routed = True
            stored_event.delivered_to.append("pagerduty")

    return {"received": True}

@app.post("/destinations/pagerduty", status_code=202)
async def pagerduty_destination(event: StoredEvent):
    # Log receipt (structured)
    log.info(
        "pagerduty_received",
        event_id=event.event_id,
        source=event.source,
        severity=event.severity,
        summary=event.summary,
    )

    # Store in memory (newest first)
    PAGERDUTY_STORE.insert(0, event)
    if len(PAGERDUTY_STORE) > MAX_EVENTS:
        PAGERDUTY_STORE.pop()

    return {"accepted": True}

@app.post("/ingest/pull-status")
async def ingest_pull_status():
    spreedly_url = os.environ.get("SPREEDLY_STATUS_SUMMARY_URL")
    braze_url = os.environ.get("BRAZE_STATUS_SUMMARY_URL")

    fetched_counts = {"spreedly": 0, "braze": 0}
    stored_count = 0
    routed_count = 0

    sources = [
        ("spreedly_status", "spreedly", spreedly_url, "fixtures/statuspage/spreedly_summary.json"),
        ("braze_status", "braze", braze_url, "fixtures/statuspage/braze_summary.json"),
    ]

    for source, key, url, fixture_path in sources:
        if url:
            try:
                data = await fetch_json(url)
            except Exception as e:
                log.warning(
                    "statuspage_fetch_failed_falling_back_to_fixture",
                    vendor=key,
                    url=url,
                    error=str(e),
                )
                data = load_fixture(fixture_path)
        else:
            data = load_fixture(fixture_path)

        normalized_events = normalize_statuspage_summary(data, source)
        fetched_counts[key] = len(normalized_events)

        for ne in normalized_events:
            stored_event = StoredEvent(**ne.model_dump(), routed=False, delivered_to=[])
            _, deduped = store_event(stored_event)
            if deduped:
                continue

            stored_count += 1

            if should_route(stored_event.severity):
                ok = await route_to_pagerduty(stored_event)
                if ok:
                    stored_event.routed = True
                    stored_event.delivered_to.append("pagerduty")
                    routed_count += 1

    return {"fetched": fetched_counts, "stored": stored_count, "routed": routed_count}