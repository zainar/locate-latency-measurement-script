# Request: Correlation ID for End-to-End Latency Measurement

**Author:** Jake S  
**Date:** January 2025  
**Status:** Request for engineering input

---

## Objective

Establish a reliable method to measure end-to-end latency from locate trigger to client-side event delivery. This is foundational work for benchmarking system performance and comparing ILaaS vs. ZLP pipelines.

## Why this matters

As we scale to multiple customer deployments, we need to understand and control latency characteristics across the system. Latency is critical for real-time use cases like safety alerts and red zone detection. Without reliable measurement, we can't:

- Establish baselines or set SLOs
- Compare ILaaS vs. ZLP performance
- Detect regressions
- Quantify the impact of architectural changes

## What I built

A test script that:
1. Connects to the websocket and listens for location events
2. Triggers a locate via `POST /nodes/{system_id}/action`
3. Captures the first matching location event for that `system_id`
4. Reports elapsed time from trigger to event receipt

**Result:** Latency measurements are unreliable due to event correlation issues (see below).

## The problem: No event correlation

The locate API returns an `action_id`, but this identifier does not propagate through the pipeline to the websocket event. There is no way to definitively match a websocket event to the API call that triggered it.

**Consequences:**

| Scenario | Impact on measurement |
|----------|----------------------|
| Tag in continuous mode | Measured event may be from scheduled emit, not our trigger |
| Multiple systems triggering locates | Cannot distinguish which trigger caused which event |
| Packet loss / retries | Cannot identify which of multiple trigger attempts succeeded |

**Current workaround:** Configure tag to very slow update rate (e.g., 1 hour) so triggered locate is the only event in the measurement window. This is fragile and doesn't scale to automated testing or production monitoring.

## Request

Add support for a correlation identifier that flows from the locate trigger through to the websocket event.

**Proposed behavior:**

1. Client includes an optional `correlation_id` (or uses the returned `action_id`) when calling the locate API
2. This ID propagates through the pipeline (edge → cloud → websocket)
3. The websocket event includes the ID in its payload, enabling client-side correlation

**Open questions for engineering:**

- Is this feasible with the current pipeline architecture?
- What's the lift? Is this a small change or does it touch multiple services?
- Are there alternative approaches that achieve the same goal?
- Any concerns about adding fields to the event payload?

## Scope

This request is specifically for the correlation mechanism. Separate from:

- Building a full latency monitoring system
- Automated test infrastructure
- Alerting or dashboards

Happy to discuss tradeoffs. If full correlation is expensive, I'm open to lighter-weight alternatives that still enable reliable measurement.
