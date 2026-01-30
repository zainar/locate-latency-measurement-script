#!/usr/bin/env python3
"""
Measure end-to-end latency from triggering a locate via REST API
to receiving the location event on the websocket.

Usage:
  python3 measure_latency.py <env> <system_id> <node_type> --ws-url "wss://..."

How to get the WebSocket URL:
  1. Open ZaiNar web app in browser, log in
  2. Open DevTools -> Network tab -> filter by "WS"
  3. Find the websocket connection, copy the full URL (includes ?token=...)
  4. Pass it as --ws-url argument

Example:
  python3 measure_latency.py prod-apac d1638e05370c49a0bd5f5d9088e53b78 tag \
    --ws-url "wss://api.wifi-prd-jpn.zainar.net/pipeline/ws/updates/v2/sites/...?token=..."
"""

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import websockets

from rest_client import Rest, Config


def load_env_configs() -> dict:
    config_path = Path(__file__).with_name("env_config.json")
    with config_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_env_config(env: str) -> dict:
    configs = load_env_configs()
    if env not in configs:
        available = ", ".join(sorted(configs.keys()))
        raise ValueError(f"Unknown environment '{env}'. Available: {available}")

    cfg = configs[env]
    base_url = cfg["base_url"].rstrip("/") + "/"
    return {
        "base_url": base_url,
        "user": cfg["username"],
        "pw": cfg["password"],
        "token_file": cfg.get("token_file"),
    }


def normalize_node_type(node_type: str) -> str:
    node_type_lower = node_type.lower()
    if node_type_lower in {"tag", "tracker"}:
        return "tracker"
    if node_type_lower in {"reader", "anchor"}:
        return "anchor"
    raise ValueError("node_type must be 'tag', 'tracker', 'reader', or 'anchor'.")


def build_parser() -> argparse.ArgumentParser:
    envs = ", ".join(sorted(load_env_configs().keys()))
    parser = argparse.ArgumentParser(
        prog="measure_latency.py",
        description="Measure end-to-end latency from REST API trigger to websocket event.",
        epilog=f"Environments from env_config.json: {envs}",
    )
    parser.add_argument("env", help="Environment name from env_config.json")
    parser.add_argument("system_id", help="System ID of the node (for REST API trigger)")
    parser.add_argument("node_type", help="tag/tracker or reader/anchor")
    parser.add_argument("--ws-url", required=True, help="WebSocket URL from browser DevTools")
    parser.add_argument("--repeat-count", type=int, default=0, help="Number of additional locates for server to perform (default: 0)")
    parser.add_argument("--interval", type=int, default=2000, help="Interval between locates in ms (default: 2000, range: 500-10000)")
    parser.add_argument("--timeout", type=float, default=30.0, help="Timeout waiting for event (seconds)")
    parser.add_argument("--debug", action="store_true", help="Print all received messages")
    return parser


async def trigger_locate(rest_client: Rest, system_id: str, api_node_type: str,
                         repeat_count: int = 0, interval_ms: int = 2000) -> dict:
    """Trigger a locate via REST API and return the response."""
    payload = {
        api_node_type: {
            "locate": {
                "repeat_count": repeat_count,
                "backoff": interval_ms,
            }
        },
        "tags": {
            "experimentID": "latency measurement",
            "exp_tag": "latency measurement",
            "ref_loc_id": "None",
            "ref_xy": "0,0,0",
        },
    }
    api_endpoint = f"nodes/{system_id}/action"
    print(f"  POST {api_endpoint}")
    response = await rest_client.post(api_endpoint, payload, verbose=0)
    return response


def format_timestamp(epoch_ms: int) -> str:
    """Format epoch milliseconds to ISO timestamp."""
    dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return dt.isoformat(timespec='milliseconds')


def extract_location_from_event(event: dict) -> Optional[dict]:
    """Extract location information from websocket event. Returns None if no location data."""
    loc = event.get("update", {}).get("events", {}).get("tracker", {}).get("location", {})
    if not loc or "x" not in loc:
        return None
    return {
        "x": loc.get("x", 0),
        "y": loc.get("y", 0),
        "z": loc.get("z", 0),
        "timestamp": loc.get("timestamp", 0),
        "published_timestamp": loc.get("published_timestamp", 0),
        "node_id": event.get("id", "unknown"),
    }


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    env = args.env
    system_id = args.system_id
    node_type = args.node_type
    ws_url = args.ws_url
    repeat_count = args.repeat_count
    interval_ms = args.interval
    timeout = args.timeout
    debug = args.debug

    api_node_type = normalize_node_type(node_type)
    env_config = load_env_config(env)
    api_config = Config(env_config["base_url"], env_config["user"], env_config["pw"])
    rest_client = Rest(api_config)

    if env_config.get("token_file"):
        rest_client.config.REST_TOKEN_FILE = env_config["token_file"]

    print("Connecting to websocket...")

    async with websockets.connect(ws_url) as websocket:
        print("Connected!")
        total_locates = 1 + repeat_count
        print(f"Triggering locate for {node_type}: {system_id} ({total_locates} locate(s), {interval_ms}ms interval)")

        # Record trigger time and send request
        t_trigger = time.perf_counter()
        response = await trigger_locate(rest_client, system_id, api_node_type, repeat_count, interval_ms)

        if isinstance(response, dict) and response.get("error"):
            print(f"  API error: {response}")
            return 1

        action_id = response.get("action_id", "")
        print(f"  API response: OK (action_id: {action_id})")

        print(f"Waiting for location event (timeout: {timeout}s)...")

        # Wait for matching event
        try:
            while True:
                raw_message = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                t_event = time.perf_counter()

                try:
                    event = json.loads(raw_message)
                except json.JSONDecodeError:
                    if debug:
                        print(f"  [non-JSON] {raw_message[:100]}")
                    continue

                event_node_id = event.get("id", "")
                loc_info = extract_location_from_event(event)

                # Skip non-location events
                if loc_info is None:
                    if debug:
                        event_type = list(event.get("update", {}).get("events", {}).keys())
                        print(f"  [skip] node={event_node_id} type={event_type} (not a location event)")
                    continue

                if debug:
                    print(f"  [event] node={event_node_id} loc=({loc_info['x']:.1f}, {loc_info['y']:.1f})")

                # Match by system_id (the 'id' field in events matches system_id)
                if event_node_id != system_id:
                    continue

                latency_ms = (t_event - t_trigger) * 1000

                print("Event received!")
                print("-" * 40)
                print(f"End-to-end latency: {latency_ms:.1f} ms")
                if loc_info["timestamp"]:
                    print(f"Server timestamp:   {format_timestamp(loc_info['timestamp'])}")
                print(f"Node ID:            {event_node_id}")
                print(f"Location:           ({loc_info['x']:.2f}, {loc_info['y']:.2f}, {loc_info['z']:.2f})")
                print("-" * 40)
                return 0

        except asyncio.TimeoutError:
            print(f"Timeout: No matching event received within {timeout}s")
            return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
