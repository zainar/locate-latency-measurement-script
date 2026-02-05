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
3. POST `/nodes/{system_id}/action` to trigger locate
4. Concurrent listeners await location events, match by system_id
5. Report latencies with deltas vs WiFi-Cloud baseline

## Protocol Differences

| System | Protocol | Auth | Coordinates | ID Format |
|--------|----------|------|-------------|-----------|
| WiFi-Cloud | WebSocket | URL token | Meters | Hex string |
| ILaaS | WebSocket | Pre-signed URL (expires ~5min) | Centimeters | `tag-{hex}` |
| ZLP | Socket.IO | JWT Bearer (expires ~60sec) | Centimeters | UUID with hyphens |

**ZLP ID conversion:** `d1638e05370c49a0bd5f5d9088e53b78` → `d1638e05-370c-49a0-bd5f-5d9088e53b78`

## Key Limitations

- **No event correlation**: Cannot definitively match WebSocket events to specific API triggers (no `action_id` propagation). Script matches by system_id and assumes first matching event is from our trigger.
- **ILaaS URL expiry**: Pre-signed URLs expire after ~5 minutes. Copy fresh from DevTools before running.
- **ZLP token expiry**: JWT tokens expire after ~60 seconds. Copy fresh `__session` cookie immediately before running.

## Debug Mode

Use `--debug` flag to print all received WebSocket messages for troubleshooting event matching and format issues.
