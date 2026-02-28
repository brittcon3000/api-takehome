from __future__ import annotations

from fastapi import FastAPI
from typing import List

from models import StoredEvent

app = FastAPI(title="Vendor Signal Normalization & Routing")

# In-memory storage (newest first)
EVENT_STORE: List[StoredEvent] = []

# Idempotency tracking
SEEN_EVENT_IDS: set[str] = set()

MAX_EVENTS = 500


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/events")
async def get_events(limit: int = 50):
    # basic guardrails
    limit = max(1, min(500, int(limit)))
    return {"events": [e.model_dump(mode="json") for e in EVENT_STORE[:limit]]}