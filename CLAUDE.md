# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Latency measurement tool for comparing location event delivery across three ZaiNar backend systems:
- **WiFi-Cloud**: Plain WebSocket (baseline/legacy)
- **ILaaS**: AWS API Gateway WebSocket with pre-signed URLs
- **ZLP**: Socket.IO-based system with JWT auth

## Commands

```bash
# Setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp env_config.example.json env_config.json  # Then edit with credentials

# Run latency measurement
python measure_latency.py <env> <system_id> <node_type> [options]

# Simple locate trigger (no WebSocket monitoring)
python trigger_locate_simple.py <env> <system_id> <node_type> [repeat_count] [interval_ms]

# Syntax check
python3 -m py_compile measure_latency.py
```

**Environments:** `dev`, `int`, `prod-apac`, `prod-us`

**Node types:** `tag`/`tracker` or `reader`/`anchor`

## Architecture

```
measure_latency.py      # Main tool - triggers locate, monitors WebSockets, reports latency
trigger_locate_simple.py # Simpler trigger-only script
rest_client.py          # Async REST client with token caching (20-min refresh)
env_config.json         # Per-environment API credentials (gitignored)
```

**Flow:**
1. Connect all WebSockets concurrently (WiFi-Cloud, ILaaS, ZLP)
2. Record trigger timestamp
3. POST `/nodes/{system_id}/action` to trigger locate (supports `--repeat-count` for multiple locates)
4. Concurrent listeners collect ALL location events until timeout, matching by system_id
5. Correlate events across systems by `meas_id`
6. Output correlation table with per-event latencies and summary statistics

## Protocol Differences

| System | Protocol | Auth | Coordinates | ID Format |
|--------|----------|------|-------------|-----------|
| WiFi-Cloud | WebSocket | URL token | Meters | Hex string |
| ILaaS | WebSocket | Pre-signed URL (expires ~5min) | Centimeters | `tag-{hex}` |
| ZLP | Socket.IO | JWT Bearer (expires ~60sec) | Centimeters | UUID with hyphens |

**ZLP ID conversion:** `d1638e05370c49a0bd5f5d9088e53b78` → `d1638e05-370c-49a0-bd5f-5d9088e53b78`

## Event Correlation

Events are correlated across systems using `meas_id`, which is consistent for the same locate event across all three backends:

| System | meas_id Path |
|--------|--------------|
| WiFi-Cloud | `event.update.events.tracker.location.meas_id` |
| ILaaS | `event.meas_id` or `event.location.meas_id` |
| ZLP | `data.location.meas_id` |

## Output Format

The script outputs a correlation table showing latencies for each `meas_id` across systems:

```
------------------------------------------------------------
Trigger time: 2026-02-05T10:30:00.000+00:00
Timeout: 30.0s

meas_id            |   WiFi-Cloud |        ILaaS |          ZLP
-------------------|--------------|--------------|-------------
0000019c2e99d78a   |    1306.7ms |  6526.2ms (+5219) |  1991.4ms (+685)
0000019c2e9b87c2   |    1450.2ms |            - |  2100.3ms (+650)
-------------------|--------------|--------------|-------------
Matched            |        7/10 |         3/10 |         8/10
Avg delta vs WC    |            - |    +4672.2ms |     +645.5ms
------------------------------------------------------------
```

- **Matched**: Events with this system present / total unique meas_ids
- **Avg delta vs WC**: Average latency difference vs WiFi-Cloud (baseline)
- Rows sorted by WiFi-Cloud latency (or earliest arrival if WiFi-Cloud missing)

## Key Limitations

- **No trigger-to-event correlation**: Cannot match WebSocket events to specific API triggers (no `action_id` propagation). Events are matched by system_id only.
- **ILaaS URL expiry**: Pre-signed URLs expire after ~5 minutes. Copy fresh from DevTools before running.
- **ZLP token expiry**: JWT tokens expire after ~60 seconds. Copy fresh `__session` cookie immediately before running.

## Debug Mode

Use `--debug` flag to:
- Print full JSON structure of the first event from each system (for verifying `meas_id` paths)
- Log all received location events with `meas_id` and coordinates
- Show skipped events (non-location, missing `meas_id`, duplicates)
