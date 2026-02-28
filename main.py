from __future__ import annotations

import os
from dotenv import load_dotenv
import stripe
from fastapi import Request, HTTPException
from datetime import datetime, timezone
from models import NormalizedEvent

from fastapi import FastAPI
from typing import List

from models import StoredEvent

load_dotenv()

STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
if not STRIPE_WEBHOOK_SECRET:
    raise RuntimeError("Missing required env var: STRIPE_WEBHOOK_SECRET")

app = FastAPI(title="Vendor Signal Normalization & Routing")

# In-memory storage (newest first)
EVENT_STORE: List[StoredEvent] = []

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

    return {"received": True}