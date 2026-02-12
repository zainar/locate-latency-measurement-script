# Claude Code Handover: Multi-Websocket Latency Measurement

## Context

I have a script (`measure_latency.py`) that measures end-to-end latency from triggering a locate via REST API to receiving the location event on a websocket. Currently it monitors a single websocket.

## Background

When we trigger a locate, the data fans out to three different systems simultaneously:

- **WiFi-Cloud:** Legacy internal config/test UI (lean architecture, expected to be fastest)
- **ILaaS:** Current production customer-facing system
- **ZLP:** New architecture we're migrating to (currently dev environment only)

I want to compare latency across these systems for the same trigger event.

## Requirements

### 1. Multiple websocket support

Accept up to three websocket URLs via separate flags:

- `--ws-wifi-cloud` (optional)
- `--ws-ilaas` (optional)
- `--ws-zlp` (optional)

At least one must be provided.

### 2. Single trigger, parallel listeners

The script still makes one API call to trigger the locate, but listens on all provided websockets concurrently. Each listener captures its own `t_event` timestamp independently.

### 3. Unified output

Report latency for each system that was monitored, e.g.:

```
----------------------------------------
Trigger time: 2026-01-30T05:23:23.000Z

WiFi-Cloud:  1203.4 ms
ILaaS:       2847.2 ms  (+1643.8 vs WiFi-Cloud)
ZLP:         1892.1 ms  (+688.7 vs WiFi-Cloud)
----------------------------------------
```

Show delta vs WiFi-Cloud if WiFi-Cloud is present (since it's our baseline). Handle cases where some websockets timeout while others succeed.

### 4. Backward compatible args

All other arguments (`env`, `system_id`, `node_type`, `--repeat-count`, `--interval`, `--timeout`, `--debug`) work the same and apply to all websockets.

### 5. Graceful partial results

If one websocket times out but others succeed, report what we got rather than failing entirely.

## Existing code

See `measure_latency.py` for current implementation. It uses `asyncio` and the `websockets` library.

## Instructions

Start in plan mode—walk me through how you'd structure the concurrent websocket listeners before writing code.

