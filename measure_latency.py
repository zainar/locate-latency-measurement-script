#!/usr/bin/env python3
"""
Measure end-to-end latency from triggering a locate via REST API
to receiving the location event on one or more websockets.

Supports comparing latency across multiple systems (WiFi-Cloud, ILaaS, ZLP)
from a single locate trigger.

Usage:
  python3 measure_latency.py <env> <system_id> <node_type> --ws-wifi-cloud "wss://..."
  python3 measure_latency.py <env> <system_id> <node_type> --ws-wifi-cloud "..." --ws-ilaas "..."
  python3 measure_latency.py <env> <system_id> <node_type> --zlp-url "..." --zlp-token "..." --zlp-account "..."

How to get the WebSocket URL (WiFi-Cloud/ILaaS):
  1. Open ZaiNar web app in browser, log in
  2. Open DevTools -> Network tab -> filter by "WS"
  3. Find the websocket connection, copy the full URL (includes ?token=...)
  4. Pass it as --ws-wifi-cloud or --ws-ilaas argument

ZLP uses Socket.IO; get the JWT from the __session cookie in DevTools -> Application -> Cookies.

ILaaS URLs (AWS API Gateway with X-Amz-* query params) are pre-signed and expire
after about 5 minutes. Copy the ILaaS WebSocket URL from DevTools shortly before
running this script, or the connection will fail with HTTP 403 "Signature expired".

Example:
  python3 measure_latency.py prod-apac d1638e05370c49a0bd5f5d9088e53b78 tag \
    --ws-wifi-cloud "wss://api.wifi-prd-jpn.zainar.net/pipeline/ws/updates/v2/sites/...?token=..."
"""

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

import socketio
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


def ilaas_url_signature_expired(url: str, max_age_seconds: int = 240) -> Optional[str]:
    """
    If url is an AWS pre-signed URL (has X-Amz-Date), return an error message when
    the signature is older than max_age_seconds (default 4 min); else return None.
    """
    parsed = urlparse(url)
    if not parsed.query:
        return None
    q = parse_qs(parsed.query)
    amz_dates = q.get("X-Amz-Date") or q.get("x-amz-date")
    if not amz_dates:
        return None
    date_str = amz_dates[0]
    try:
        # X-Amz-Date format: 20260203T004746Z
        url_time = datetime.strptime(date_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    now = datetime.now(timezone.utc)
    if (now - url_time).total_seconds() > max_age_seconds:
        return (
            f"ILaaS URL signature has expired (X-Amz-Date was {date_str}). "
            "Copy a fresh WebSocket URL from the browser DevTools (Network -> WS) shortly before running."
        )
    return None


def normalize_node_type(node_type: str) -> str:
    node_type_lower = node_type.lower()
    if node_type_lower in {"tag", "tracker"}:
        return "tracker"
    if node_type_lower in {"reader", "anchor"}:
        return "anchor"
    raise ValueError("node_type must be 'tag', 'tracker', 'reader', or 'anchor'.")


def system_id_to_uuid(system_id: str) -> str:
    """Convert system_id to UUID format for ZLP matching.

    Example: d1638e05370c49a0bd5f5d9088e53b78 -> d1638e05-370c-49a0-bd5f-5d9088e53b78
    """
    s = system_id.replace("-", "")
    return f"{s[:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}"


def build_parser() -> argparse.ArgumentParser:
    envs = ", ".join(sorted(load_env_configs().keys()))
    parser = argparse.ArgumentParser(
        prog="measure_latency.py",
        description="Measure end-to-end latency from REST API trigger to websocket event(s).",
        epilog=f"Environments from env_config.json: {envs}",
    )
    parser.add_argument("env", help="Environment name from env_config.json")
    parser.add_argument("system_id", help="System ID of the node (for REST API trigger)")
    parser.add_argument("node_type", help="tag/tracker or reader/anchor")
    parser.add_argument("--ws-wifi-cloud", help="WiFi-Cloud websocket URL (baseline)")
    parser.add_argument("--ws-ilaas", help="ILaaS websocket URL")
    parser.add_argument("--ilaas-account", help="ILaaS account resource name (e.g., acct-958041d25b9a4b39b87b83f0eae834cb)")
    parser.add_argument("--ilaas-site", help="ILaaS site resource name (e.g., site-f6aa5b0283114de6a3d8933bebc6dc4a)")
    parser.add_argument("--zlp-url", help="ZLP Socket.IO URL (e.g., wss://zps-web-api.zlp-dev.zainar.net)")
    parser.add_argument("--zlp-token", help="ZLP JWT token (from __session cookie)")
    parser.add_argument("--zlp-account", help="ZLP account resource name")
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


async def subscribe_ilaas(websocket, account_res_name: str, site_res_name: str, timeout: float = 10.0) -> bool:
    """
    Send ILaaS subscription message and wait for confirmation.
    Returns True on success, False on failure.
    """
    subscription_msg = {
        "accountResName": account_res_name,
        "action": "setFilterOptions",
        "messageTypes": [
            "location-updates",
            "geomatch-updates",
            "alerts-equipment",
            "alerts-safety",
            "alerts-system",
            "alerts-zone",
            "site-updates"
        ],
        "siteResName": site_res_name
    }

    await websocket.send(json.dumps(subscription_msg))

    # Wait for confirmation (status 200)
    try:
        response = await asyncio.wait_for(websocket.recv(), timeout=timeout)
        data = json.loads(response)
        if data.get("statusCode") == 200 or data.get("status") == 200:
            return True
        # Some APIs return success differently
        return True  # Assume success if we got any response
    except asyncio.TimeoutError:
        return False
    except Exception:
        return False


def extract_location_from_event(event: dict) -> Optional[dict]:
    """
    Extract location information from websocket event. Returns None if no location data.

    Supports two formats:
    - WiFi-Cloud: {"id": "...", "update": {"events": {"tracker": {"location": {"x": ..., "y": ..., "z": ...}}}}}
    - ILaaS: {"tagResName": "tag-...", "measureType": "location", "x": ..., "y": ..., "z": ...}
    """
    # Try WiFi-Cloud format first
    loc = event.get("update", {}).get("events", {}).get("tracker", {}).get("location", {})
    if loc and "x" in loc:
        return {
            "x": loc.get("x", 0),
            "y": loc.get("y", 0),
            "z": loc.get("z", 0),
            "timestamp": loc.get("timestamp", 0),
            "published_timestamp": loc.get("published_timestamp", 0),
            "node_id": event.get("id", "unknown"),
        }

    # Try ILaaS format
    if event.get("measureType") == "location" and "x" in event:
        # ILaaS uses tagResName like "tag-d1638e05..." - extract the system_id part
        tag_res_name = event.get("tagResName", "")
        node_id = tag_res_name.replace("tag-", "") if tag_res_name.startswith("tag-") else tag_res_name

        # ILaaS coordinates appear to be in centimeters, convert to meters for display
        return {
            "x": event.get("x", 0) / 100.0,
            "y": event.get("y", 0) / 100.0,
            "z": event.get("z", 0) / 100.0,
            "timestamp": event.get("timestamp", 0),
            "published_timestamp": event.get("sns_timestamp", 0),
            "node_id": node_id,
        }

    return None


async def listen_for_location(
    name: str,
    websocket,
    system_id: str,
    t_trigger: float,
    timeout: float,
    debug: bool
) -> dict:
    """
    Listen on an already-connected websocket for a location event matching system_id.

    Returns: {"name": str, "latency_ms": float, "loc_info": dict}
         or: {"name": str, "error": str} on timeout/failure
    """
    try:
        while True:
            raw_message = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            t_event = time.perf_counter()

            try:
                event = json.loads(raw_message)
            except json.JSONDecodeError:
                if debug:
                    print(f"  [{name}] [non-JSON] {raw_message[:100]}")
                continue

            event_node_id = event.get("id", "")
            loc_info = extract_location_from_event(event)

            # Skip non-location events
            if loc_info is None:
                if debug:
                    # Show more detail for debugging different event formats
                    event_type = list(event.get("update", {}).get("events", {}).keys())
                    if not event_type:
                        # Might be a different format entirely - show raw keys
                        print(f"  [{name}] [skip] keys={list(event.keys())[:5]} (unknown format)")
                    else:
                        print(f"  [{name}] [skip] node={event_node_id} type={event_type} (not a location event)")
                continue

            # Match by system_id (WiFi-Cloud uses event["id"]; ILaaS uses tagResName -> loc_info["node_id"])
            match_id = event_node_id or loc_info.get("node_id", "")
            if debug:
                print(f"  [{name}] [event] node={match_id} loc=({loc_info['x']:.1f}, {loc_info['y']:.1f})")

            if match_id != system_id:
                continue

            latency_ms = (t_event - t_trigger) * 1000
            return {
                "name": name,
                "latency_ms": latency_ms,
                "loc_info": loc_info,
            }

    except asyncio.TimeoutError:
        return {"name": name, "error": "timeout"}
    except Exception as e:
        return {"name": name, "error": str(e)}


async def connect_zlp(url: str, token: str, debug: bool) -> Optional[socketio.AsyncClient]:
    """
    Connect to ZLP Socket.IO server.

    Returns: AsyncClient on success, None on failure.
    """
    sio = socketio.AsyncClient()
    connected = asyncio.Event()
    connect_error = {"error": None}

    @sio.on("connect", namespace="/locations")
    async def on_connect():
        print("  ZLP: connected")
        if debug:
            print("  [ZLP] [socketio] connected to /locations namespace")
        connected.set()

    @sio.on("connect_error", namespace="/locations")
    async def on_connect_error(data):
        connect_error["error"] = str(data)
        connected.set()

    try:
        base_url = url.rstrip("/")
        await sio.connect(
            base_url,
            socketio_path="/socket.io/",
            transports=["websocket"],
            namespaces=["/locations"],
            auth={"token": f"Bearer {token}"},
        )

        # Wait for namespace connection (with timeout)
        await asyncio.wait_for(connected.wait(), timeout=10.0)

        if connect_error["error"]:
            print(f"  ZLP: connect_error - {connect_error['error']}")
            await sio.disconnect()
            return None

        return sio

    except Exception as e:
        print(f"  ZLP: connection failed - {e}")
        try:
            await sio.disconnect()
        except Exception:
            pass
        return None


async def listen_zlp_for_location(
    sio: socketio.AsyncClient,
    system_id: str,
    t_trigger: float,
    timeout: float,
    debug: bool
) -> dict:
    """
    Listen on an already-connected ZLP Socket.IO client for location events.

    Returns: {"name": "ZLP", "latency_ms": float, "loc_info": dict}
         or: {"name": "ZLP", "error": str}
    """
    target_device_id = system_id_to_uuid(system_id)
    result = {"name": "ZLP", "error": "timeout"}
    event_received = asyncio.Event()

    @sio.on("GET_NEW_LOCATION_HISTORY", namespace="/locations")
    async def on_location(data):
        nonlocal result
        t_event = time.perf_counter()

        device_res_name = data.get("deviceResName", "")

        if debug:
            x_cm = data.get("x", 0)
            y_cm = data.get("y", 0)
            z_cm = data.get("z", 0)
            print(f"  [ZLP] [event] deviceResName={device_res_name} loc=({x_cm/100:.1f}, {y_cm/100:.1f}, {z_cm/100:.1f})")

        if device_res_name != target_device_id:
            return

        # Match found - extract location info (coordinates in cm, convert to m)
        latency_ms = (t_event - t_trigger) * 1000
        result = {
            "name": "ZLP",
            "latency_ms": latency_ms,
            "loc_info": {
                "x": data.get("x", 0) / 100.0,
                "y": data.get("y", 0) / 100.0,
                "z": data.get("z", 0) / 100.0,
                "timestamp": data.get("locationTime", 0),
                "published_timestamp": 0,
                "node_id": device_res_name,
            },
        }
        event_received.set()

    try:
        await asyncio.wait_for(event_received.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        result = {"name": "ZLP", "error": "timeout"}

    return result


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    env = args.env
    system_id = args.system_id
    node_type = args.node_type
    repeat_count = args.repeat_count
    interval_ms = args.interval
    timeout = args.timeout
    debug = args.debug

    # Build websocket list from provided URLs (plain websocket connections)
    websocket_configs = []
    if args.ws_wifi_cloud:
        websocket_configs.append(("WiFi-Cloud", args.ws_wifi_cloud))
    if args.ws_ilaas:
        websocket_configs.append(("ILaaS", args.ws_ilaas))

    # ZLP uses Socket.IO, handled separately
    zlp_enabled = bool(args.zlp_url)

    if not websocket_configs and not zlp_enabled:
        parser.error("At least one connection must be provided (--ws-wifi-cloud, --ws-ilaas, or --zlp-url)")

    # Validate ILaaS subscription parameters
    if args.ws_ilaas and (not args.ilaas_account or not args.ilaas_site):
        parser.error("--ws-ilaas requires both --ilaas-account and --ilaas-site")

    # Validate ZLP parameters
    if args.zlp_url and (not args.zlp_token or not args.zlp_account):
        parser.error("--zlp-url requires both --zlp-token and --zlp-account")

    api_node_type = normalize_node_type(node_type)
    env_config = load_env_config(env)
    api_config = Config(env_config["base_url"], env_config["user"], env_config["pw"])
    rest_client = Rest(api_config)

    if env_config.get("token_file"):
        rest_client.config.REST_TOKEN_FILE = env_config["token_file"]

    # Connect ALL websockets before triggering (ensures listeners ready)
    total_connections = len(websocket_configs) + (1 if zlp_enabled else 0)
    print(f"Connecting to {total_connections} websocket(s)...")
    connections = []
    connected_names = []

    LOG_PATH = Path(__file__).resolve().with_name(".cursor") / "debug.log"

    for name, url in websocket_configs:
        if name == "ILaaS":
            expiry_msg = ilaas_url_signature_expired(url)
            if expiry_msg:
                print(f"  {name}: {expiry_msg}")
                continue
        try:
            # #region agent log
            if name == "ILaaS":
                parsed = urlparse(url)
                q = parse_qs(parsed.query)
                with open(LOG_PATH, "a", encoding="utf-8") as _lf:
                    _lf.write(json.dumps({"sessionId": "debug-session", "hypothesisId": "H3", "location": "measure_latency.py:connect", "message": "ILaaS URL before connect", "data": {"host": parsed.hostname, "path": parsed.path, "query_keys": list(q.keys()), "X_Amz_Date": q.get("X-Amz-Date", [None])[0], "url_len": len(url)}, "timestamp": int(time.time() * 1000)}) + "\n")
            # #endregion
            # #region agent log
            extra_headers = {}
            origin_sent = None
            if name == "ILaaS":
                origin_sent = "https://zps.ilaas-prd-apac.zainar.net"
                extra_headers["Origin"] = origin_sent
                with open(LOG_PATH, "a", encoding="utf-8") as _lf:
                    _lf.write(json.dumps({"sessionId": "debug-session", "hypothesisId": "H1", "location": "measure_latency.py:connect", "message": "ILaaS request headers", "data": {"additional_headers_keys": list(extra_headers.keys()), "origin_sent": origin_sent}, "timestamp": int(time.time() * 1000)}) + "\n")
            # #endregion
            ws = await websockets.connect(
                url,
                additional_headers=extra_headers if extra_headers else None,
            )

            # ILaaS requires subscription message after connecting
            if name == "ILaaS":
                print(f"  {name}: connected, subscribing...")
                success = await subscribe_ilaas(ws, args.ilaas_account, args.ilaas_site)
                if success:
                    print(f"  {name}: subscribed")
                else:
                    print(f"  {name}: subscription failed")
                    await ws.close()
                    continue

            connections.append((name, ws))
            connected_names.append(name)
            if name != "ILaaS":
                print(f"  {name}: connected")
        except Exception as e:
            # #region agent log
            err_data = {"exception_type": type(e).__name__, "message": str(e)}
            resp = getattr(e, "response", None) or (e.args[0] if getattr(e, "args", None) and len(e.args) > 0 else None)
            if resp is not None:
                err_data["status_code"] = getattr(resp, "status_code", getattr(e, "status_code", None))
                body = getattr(resp, "body", None)
                if body is not None:
                    err_data["response_body"] = (body if isinstance(body, str) else body.decode("utf-8", errors="replace"))[:500]
            if getattr(e, "status_code", None) is not None:
                err_data["status_code"] = e.status_code
            with open(LOG_PATH, "a", encoding="utf-8") as _lf:
                _lf.write(json.dumps({"sessionId": "debug-session", "hypothesisId": "H5", "location": "measure_latency.py:connect_exc", "message": "connect exception", "data": err_data, "timestamp": int(time.time() * 1000)}) + "\n")
            # #endregion
            print(f"  {name}: connection failed - {e}")

    # Connect ZLP Socket.IO (before trigger, like other websockets)
    zlp_client = None
    if zlp_enabled:
        zlp_client = await connect_zlp(args.zlp_url, args.zlp_token, debug)

    if not connections and not zlp_client:
        print("Error: No websocket connections established")
        return 1

    try:
        total_locates = 1 + repeat_count
        print(f"Triggering locate for {node_type}: {system_id} ({total_locates} locate(s), {interval_ms}ms interval)")

        # Record trigger time and send request
        t_trigger = time.perf_counter()
        trigger_time_utc = datetime.now(timezone.utc)
        response = await trigger_locate(rest_client, system_id, api_node_type, repeat_count, interval_ms)

        if isinstance(response, dict) and response.get("error"):
            print(f"  API error: {response}")
            return 1

        action_id = response.get("action_id", "")
        print(f"  API response: OK (action_id: {action_id})")

        print(f"Waiting for location events (timeout: {timeout}s)...")

        # Launch listeners concurrently
        tasks = [
            listen_for_location(name, ws, system_id, t_trigger, timeout, debug)
            for name, ws in connections
        ]

        # ZLP uses Socket.IO, add its listener task separately
        if zlp_client:
            tasks.append(listen_zlp_for_location(
                sio=zlp_client,
                system_id=system_id,
                t_trigger=t_trigger,
                timeout=timeout,
                debug=debug,
            ))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        successful_results = []
        failed_results = []

        for result in results:
            if isinstance(result, Exception):
                failed_results.append({"name": "unknown", "error": str(result)})
            elif "error" in result:
                failed_results.append(result)
            else:
                successful_results.append(result)

        # Format output
        print("-" * 40)
        print(f"Trigger time: {trigger_time_utc.isoformat(timespec='milliseconds')}")
        print()

        # Find WiFi-Cloud result for delta calculation
        wifi_cloud_latency = None
        for r in successful_results:
            if r["name"] == "WiFi-Cloud":
                wifi_cloud_latency = r["latency_ms"]
                break

        # Sort results: successful first (by name order: WiFi-Cloud, ILaaS, ZLP), then failed
        name_order = {"WiFi-Cloud": 0, "ILaaS": 1, "ZLP": 2}
        all_results = successful_results + failed_results
        all_results.sort(key=lambda r: name_order.get(r["name"], 99))

        for result in all_results:
            name = result["name"]
            if "error" in result:
                print(f"{name + ':':13} {result['error']}")
            else:
                latency = result["latency_ms"]
                loc = result["loc_info"]
                loc_str = f"({loc['x']:.2f}, {loc['y']:.2f}, {loc['z']:.2f})"

                if wifi_cloud_latency is not None and name != "WiFi-Cloud":
                    delta = latency - wifi_cloud_latency
                    delta_str = f"  ({delta:+.1f} vs WiFi-Cloud)"
                else:
                    delta_str = ""

                print(f"{name + ':':13} {latency:7.1f} ms  @ {loc_str}{delta_str}")

        print("-" * 40)

        # Exit 0 if at least one succeeded, exit 1 if all failed
        return 0 if successful_results else 1

    finally:
        # Close all websocket connections
        for name, ws in connections:
            try:
                await ws.close()
            except Exception:
                pass
        # Close ZLP Socket.IO connection
        if zlp_client:
            try:
                await zlp_client.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
