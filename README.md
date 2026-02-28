# Vendor Signal Normalization & Routing

A FastAPI-based platform service that ingests vendor signals from multiple sources (Stripe webhooks and Statuspage summaries), normalizes them into a unified internal schema, stores recent events with idempotency guarantees, and routes critical events to a mock PagerDuty destination.

This project demonstrates core platform engineering concerns: reliability, observability, validation, routing logic, idempotency, and production-minded tradeoffs.

---

# Setup Instructions

## Prerequisites

- Python 3.10+
- Docker Desktop (optional but recommended)

---

## Environment Configuration

Copy the example environment file and create your local environment file:

```bash
cp .env.example .env
```

Required:

```
STRIPE_WEBHOOK_SECRET=
```

Optional:

```
ROUTE_WARNING=false
SPREEDLY_STATUS_SUMMARY_URL=
BRAZE_STATUS_SUMMARY_URL=
SELF_BASE_URL=http://localhost:3000
```

Notes:
- `.env` is intentionally not committed.
- `.env.example` documents required configuration.
- The application fails fast if `STRIPE_WEBHOOK_SECRET` is missing.

---

## Run Locally (Python) - Create Virutal Environement to avoid project package overlap

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 3000
```

---

## Run With Docker

```bash
docker compose up --build
```

Then test:

```bash
curl http://localhost:3000/healthz
```

Expected:

```json
{"ok": true}
```

---

# Usage Examples

## Health Check

```bash
curl http://localhost:3000/healthz
```

---

## Query Recent Events

```bash
curl "http://localhost:3000/events?limit=50"
```

Returns normalized events stored in memory.

---

## Ingest Stripe Webhook

This endpoint verifies Stripe signatures using the raw request body.

```bash
curl -X POST http://localhost:3000/ingest/stripe \
  -H "Content-Type: application/json" \
  -H "Stripe-Signature: t=..." \
  -d @fixtures/stripe/payment_failed.json
```

Behavior:
- Returns 400 if signature verification fails
- Returns `{ received: true }` on success
- Dedupes duplicate `event_id` values
- Routes based on severity rules

---

## Pull Statuspage Signals

By default, this loads local fixtures.  
If `SPREEDLY_STATUS_SUMMARY_URL` or `BRAZE_STATUS_SUMMARY_URL` are configured, it fetches real data.

```bash
curl -X POST http://localhost:3000/ingest/pull-status
```

Example response:

```json
{
  "fetched": {
    "spreedly": 1,
    "braze": 1
  },
  "stored": 2,
  "routed": 1
}
```

---

## Mock PagerDuty Destination

```bash
curl -X POST http://localhost:3000/destinations/pagerduty \
  -H "Content-Type: application/json" \
  -d '{
    "event_id":"evt_manual_1",
    "source":"stripe",
    "kind":"payment",
    "severity":"critical",
    "service":"stripe",
    "summary":"manual test",
    "description":null,
    "started_at":"2024-01-15T10:30:00Z",
    "resolved_at":null,
    "raw": {"hello":"world"},
    "routed": false,
    "delivered_to": []
  }'
```

Returns `202 Accepted`.

---

## AI Summarization (Bonus)

A deterministic AI stub endpoint demonstrates how LLM-assisted classification could be integrated without introducing nondeterminism into the critical routing path.

```bash
curl -X POST http://localhost:3000/ai/summarize \
  -H "Content-Type: application/json" \
  -d '{"text":"Multiple payment failures detected across APAC region"}'
```

Example response:

```json
{
  "summary": "Payment failure detected",
  "suggested_severity": "critical"
}
```

This endpoint intentionally uses deterministic keyword logic rather than a real LLM to ensure reproducibility and test stability.

---

# Architecture Overview

## High-Level Data Flow

1. Stripe webhook or Statuspage poll request is received.
2. Vendor payload is normalized into a unified internal schema.
3. Event is stored in memory.
4. Idempotency ensures duplicate `event_id` values are ignored.
5. Based on severity rules, event may be routed to the mock PagerDuty endpoint.
6. Stored events can be queried via `/events`.

---

## Normalized Event Schema

All vendor signals are mapped into a single schema:

- `event_id`
- `source`
- `kind`
- `severity`
- `service`
- `summary`
- `description`
- `started_at`
- `resolved_at`
- `raw`

Strict validation is enforced using Pydantic models.

---

## Key Design Decisions

### Unified Schema
All vendor inputs (Stripe + Statuspage) normalize into a single internal representation to simplify routing and querying.

### Idempotency
An in-memory `SEEN_EVENT_IDS` set ensures duplicate events are not stored or re-routed.

### Best-Effort Routing
Routing failures:
- Are logged
- Do not block ingestion
- Do not fail the request

### Structured Logging
- JSON-formatted logs
- Correlation IDs added via middleware
- Request-level logging for observability

### AI Isolation

The AI summarization endpoint is intentionally separated from the ingestion and routing pipeline. Routing decisions remain deterministic and rule-based to prevent nondeterministic escalation behavior. AI outputs are advisory and do not directly trigger paging.

---

# Testing

Run tests:

```bash
pytest -q
```

## What Is Tested

Stripe ingestion:
- Missing signature returns 400
- Duplicate events are deduped
- Critical events trigger routing

Statuspage ingestion:
- Events are normalized and stored
- Second poll dedupes existing events
- Critical incidents trigger routing

External dependencies (Stripe verification and routing calls) are mocked to keep tests deterministic.

---

# Security Considerations

## Stripe Signature Verification
Stripe requires validation using the raw request body.  
The implementation uses `stripe.Webhook.construct_event()` to ensure cryptographic integrity.

## Secret Management
- No secrets are committed.
- Environment variables are loaded from `.env`.
- `.env.example` documents required variables.

## Input Validation
- All request bodies are validated using Pydantic models.
- Invalid inputs return appropriate HTTP status codes.

---

# Production Readiness Discussion

If productionizing this service, I would add:

## Persistence
Replace in-memory storage with:
- Redis
- Postgres
- DynamoDB

This ensures idempotency across restarts and multi-instance deployments.

## Delivery Reliability
Introduce:
- Queue-based routing (SQS/Kafka)
- Retry with exponential backoff
- Dead Letter Queue (DLQ)

## Scalability
- Horizontal scaling behind a load balancer
- Shared idempotency store
- Stateless service instances

## Observability
- Metrics (Prometheus/OpenTelemetry)
- Distributed tracing
- Alerting for routing failures
- Structured log aggregation

## Security Enhancements
- Authentication for internal endpoints
- Rate limiting
- Replay attack protection
- Secret rotation via a secure secret manager

## AI Integration Strategy

In this implementation, AI summarization is deterministic and isolated from the routing path.

In production, AI outputs would be integrated as advisory metadata (e.g., `ai_summary`, `ai_suggested_severity`) attached to normalized events at ingestion time.

Routing decisions would remain rule-based and deterministic. AI would assist with:

- Event summarization for human triage
- Suggested severity classification
- Incident clustering
- Runbook recommendations

Additional production considerations would include:

- Structured prompt design with enforced JSON schema
- PII and secret redaction before model submission
- Audit logging of model inputs/outputs
- Latency timeouts and deterministic fallback
- Cost controls and rate limiting
- Guardrails to prevent AI-only escalation

This separation ensures operational reliability while still enabling AI-driven enhancements.

---

# Endpoints

- `GET /healthz`
- `GET /events`
- `POST /ingest/stripe`
- `POST /ingest/pull-status`
- `POST /destinations/pagerduty`