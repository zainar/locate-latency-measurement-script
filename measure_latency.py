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
import csv
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

import socketio
import websockets

from rest_client import Rest, Config


@dataclass
class Trigger:
    """A single locate API trigger."""
    index: int                      # 1-indexed trigger number
    timestamp_utc: datetime         # Wall-clock time when API call was initiated
    perf_time: float                # time.perf_counter() at API call initiation
    api_response_time_ms: float     # How long the POST took
    action_id: str = ""             # From API response (diagnostics)


@dataclass
class CollectedEvent:
    """A single location event collected from a WebSocket."""
    meas_id: str
    system_name: str
    arrival_time_utc: datetime      # Wall-clock time when event arrived
    arrival_perf_time: float        # time.perf_counter() when event arrived
    loc_info: dict
    raw_event: dict


class EventCollector:
    """Thread-safe collector for location events across multiple systems."""

    def __init__(self):
        self._events: dict[str, dict[str, CollectedEvent]] = {}  # meas_id -> {system_name -> event}
        self._all_events: list[CollectedEvent] = []  # flat list for trigger assignment
        self._lock = asyncio.Lock()
        self._counts: dict[str, int] = {}  # system_name -> total events seen

    async def add_event(
        self,
        meas_id: str,
        system_name: str,
        arrival_time_utc: datetime,
        arrival_perf_time: float,
        loc_info: dict,
        raw_event: dict,
        debug: bool = False
    ) -> bool:
        """
        Add an event to the collection.

        Returns True if added, False if duplicate meas_id for same system.
        """
        async with self._lock:
            # Track total events per system
            self._counts[system_name] = self._counts.get(system_name, 0) + 1

            if meas_id not in self._events:
                self._events[meas_id] = {}

            if system_name in self._events[meas_id]:
                if debug:
                    print(f"  [{system_name}] [skip] duplicate meas_id={meas_id}")
                return False

            event = CollectedEvent(
                meas_id=meas_id,
                system_name=system_name,
                arrival_time_utc=arrival_time_utc,
                arrival_perf_time=arrival_perf_time,
                loc_info=loc_info,
                raw_event=raw_event,
            )
            self._events[meas_id][system_name] = event
            self._all_events.append(event)
            return True

    def get_results(self) -> dict[str, dict[str, CollectedEvent]]:
        """Return all collected events grouped by meas_id."""
        return self._events

    def get_all_events(self) -> list[CollectedEvent]:
        """Return flat list of all collected events (for trigger assignment)."""
        return self._all_events

    def get_event_counts(self) -> dict[str, int]:
        """Return total event count per system."""
        return self._counts


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
    parser.add_argument("--interval", type=float, default=3.0, help="Seconds between Locate API triggers (default: 3.0)")
    parser.add_argument("--timeout", type=float, default=30.0, help="How long the test runs in seconds (default: 30.0)")
    parser.add_argument("--debug", action="store_true", help="Print all received messages")
    parser.add_argument("--output-format", choices=["console", "csv", "both"], default="both", help="Output format: console, csv, or both (default: both)")
    return parser


async def trigger_locate(rest_client: Rest, system_id: str, api_node_type: str) -> dict:
    """Trigger a single locate via REST API and return the response."""
    payload = {
        api_node_type: {
            "locate": {
                "repeat_count": 0,
                "backoff": 2000,
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


def extract_meas_id(event: dict, system_name: str, zlp_data: Optional[dict] = None) -> Optional[str]:
    """
    Extract meas_id from an event based on system type.

    Args:
        event: The raw event dict (for WiFi-Cloud and ILaaS)
        system_name: "WiFi-Cloud", "ILaaS", or "ZLP"
        zlp_data: The Socket.IO payload (only for ZLP)

    Returns:
        meas_id string or None if not found
    """
    if system_name == "WiFi-Cloud":
        # Path: event.update.events.tracker.location.meas_id
        loc = event.get("update", {}).get("events", {}).get("tracker", {}).get("location", {})
        return loc.get("meas_id")

    elif system_name == "ILaaS":
        # Try root level first, then location sub-object
        meas_id = event.get("meas_id")
        if meas_id:
            return str(meas_id)
        loc = event.get("location", {})
        if loc:
            return loc.get("meas_id")
        return None

    elif system_name == "ZLP":
        # Socket.IO payload: data.location.meas_id
        if zlp_data:
            loc = zlp_data.get("location", {})
            meas_id = loc.get("meas_id")
            if meas_id:
                return str(meas_id)
        return None

    return None


async def listen_for_location(
    name: str,
    websocket,
    system_id: str,
    t_trigger: float,
    timeout: float,
    debug: bool,
    collector: Optional[EventCollector] = None
) -> dict:
    """
    Listen on an already-connected websocket for location events matching system_id.

    If collector is provided, collects ALL matching events until timeout.
    Otherwise, returns on first match (legacy behavior).

    Returns: {"name": str, "count": int} when collecting
         or: {"name": str, "latency_ms": float, "loc_info": dict} (legacy single-event)
         or: {"name": str, "error": str} on failure
    """
    event_count = 0
    deadline = time.perf_counter() + timeout
    first_event_logged = False

    try:
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break

            try:
                raw_message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break

            t_event = time.perf_counter()
            t_event_utc = datetime.now(timezone.utc)

            try:
                event = json.loads(raw_message)
            except json.JSONDecodeError:
                if debug:
                    print(f"  [{name}] [non-JSON] {raw_message[:100]}")
                continue

            # Debug: log full structure of first event
            if debug and not first_event_logged:
                print(f"  [{name}] [first-event-structure] {json.dumps(event, indent=2)[:500]}")
                first_event_logged = True

            event_node_id = event.get("id", "")
            loc_info = extract_location_from_event(event)

            # Skip non-location events
            if loc_info is None:
                if debug:
                    event_type = list(event.get("update", {}).get("events", {}).keys())
                    if not event_type:
                        print(f"  [{name}] [skip] keys={list(event.keys())[:5]} (unknown format)")
                    else:
                        print(f"  [{name}] [skip] node={event_node_id} type={event_type} (not a location event)")
                continue

            # Match by system_id
            match_id = event_node_id or loc_info.get("node_id", "")
            if debug:
                meas_id = extract_meas_id(event, name)
                print(f"  [{name}] [event] node={match_id} meas_id={meas_id} loc=({loc_info['x']:.1f}, {loc_info['y']:.1f})")

            if match_id != system_id:
                continue

            # Collection mode: add to collector and continue
            if collector is not None:
                meas_id = extract_meas_id(event, name)
                if meas_id is None:
                    if debug:
                        print(f"  [{name}] [skip] no meas_id in event")
                    continue

                await collector.add_event(
                    meas_id=meas_id,
                    system_name=name,
                    arrival_time_utc=t_event_utc,
                    arrival_perf_time=t_event,
                    loc_info=loc_info,
                    raw_event=event,
                    debug=debug,
                )
                event_count += 1
                continue

            # Legacy mode: return on first match
            latency_ms = (t_event - t_trigger) * 1000
            return {
                "name": name,
                "latency_ms": latency_ms,
                "loc_info": loc_info,
            }

        # Timeout reached
        if collector is not None:
            return {"name": name, "count": event_count}
        return {"name": name, "error": "timeout"}

    except Exception as e:
        if collector is not None:
            return {"name": name, "count": event_count, "error": str(e)}
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
    debug: bool,
    collector: Optional[EventCollector] = None
) -> dict:
    """
    Listen on an already-connected ZLP Socket.IO client for location events.

    If collector is provided, collects ALL matching events until timeout.
    Otherwise, returns on first match (legacy behavior).

    Returns: {"name": "ZLP", "count": int} when collecting
         or: {"name": "ZLP", "latency_ms": float, "loc_info": dict} (legacy single-event)
         or: {"name": "ZLP", "error": str}
    """
    target_device_id = system_id_to_uuid(system_id)
    result = {"name": "ZLP", "error": "timeout"}
    event_received = asyncio.Event()
    event_count = [0]  # Use list for mutability in nested function
    first_event_logged = [False]

    @sio.on("GET_NEW_LOCATION_HISTORY", namespace="/locations")
    async def on_location(data):
        nonlocal result
        t_event = time.perf_counter()
        t_event_utc = datetime.now(timezone.utc)

        # Debug: log full structure of first event
        if debug and not first_event_logged[0]:
            print(f"  [ZLP] [first-event-structure] {json.dumps(data, indent=2, default=str)[:500]}")
            first_event_logged[0] = True

        device_res_name = data.get("deviceResName", "")

        if debug:
            x_cm = data.get("x", 0)
            y_cm = data.get("y", 0)
            z_cm = data.get("z", 0)
            meas_id = extract_meas_id({}, "ZLP", zlp_data=data)
            print(f"  [ZLP] [event] deviceResName={device_res_name} meas_id={meas_id} loc=({x_cm/100:.1f}, {y_cm/100:.1f}, {z_cm/100:.1f})")

        if device_res_name != target_device_id:
            return

        # Match found - extract location info (coordinates in cm, convert to m)
        loc_info = {
            "x": data.get("x", 0) / 100.0,
            "y": data.get("y", 0) / 100.0,
            "z": data.get("z", 0) / 100.0,
            "timestamp": data.get("locationTime", 0),
            "published_timestamp": 0,
            "node_id": device_res_name,
        }

        # Collection mode: add to collector
        if collector is not None:
            meas_id = extract_meas_id({}, "ZLP", zlp_data=data)
            if meas_id is None:
                if debug:
                    print(f"  [ZLP] [skip] no meas_id in event")
                return

            await collector.add_event(
                meas_id=meas_id,
                system_name="ZLP",
                arrival_time_utc=t_event_utc,
                arrival_perf_time=t_event,
                loc_info=loc_info,
                raw_event=data,
                debug=debug,
            )
            event_count[0] += 1
            return

        # Legacy mode: return on first match
        latency_ms = (t_event - t_trigger) * 1000
        result = {
            "name": "ZLP",
            "latency_ms": latency_ms,
            "loc_info": loc_info,
        }
        event_received.set()

    # Collection mode: wait full timeout duration
    if collector is not None:
        await asyncio.sleep(timeout)
        return {"name": "ZLP", "count": event_count[0]}

    # Legacy mode: wait for first event or timeout
    try:
        await asyncio.wait_for(event_received.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        result = {"name": "ZLP", "error": "timeout"}

    return result


async def trigger_loop(
    rest_client: Rest,
    system_id: str,
    api_node_type: str,
    num_triggers: int,
    trigger_interval: float,
    debug: bool = False,
) -> list[Trigger]:
    """Fire N locate triggers at a configurable interval, returning Trigger metadata."""
    triggers: list[Trigger] = []
    for i in range(1, num_triggers + 1):
        t0 = time.perf_counter()
        t0_utc = datetime.now(timezone.utc)
        response = await trigger_locate(rest_client, system_id, api_node_type)
        api_rt_ms = (time.perf_counter() - t0) * 1000

        action_id = ""
        if isinstance(response, dict):
            action_id = response.get("action_id", "")

        trigger = Trigger(
            index=i,
            timestamp_utc=t0_utc,
            perf_time=t0,
            api_response_time_ms=api_rt_ms,
            action_id=action_id,
        )
        triggers.append(trigger)
        print(f"  Trigger {i}/{num_triggers} sent (api_rt={api_rt_ms:.0f}ms, action_id={action_id})")

        # Sleep between triggers (except after last)
        if i < num_triggers:
            sleep_time = trigger_interval - (api_rt_ms / 1000)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    return triggers


def assign_events_to_triggers(
    events: list[CollectedEvent],
    triggers: list[Trigger],
) -> dict[int, list[tuple[CollectedEvent, float]]]:
    """
    Assign each event to its most recent preceding trigger.

    Returns: {trigger_index: [(event, latency_ms), ...]} sorted by arrival time within each group.
    Events arriving before the first trigger are discarded.
    """
    if not triggers:
        return {}

    # Sort triggers by perf_time (should already be sorted, but be safe)
    sorted_triggers = sorted(triggers, key=lambda t: t.perf_time)

    result: dict[int, list[tuple[CollectedEvent, float]]] = {t.index: [] for t in sorted_triggers}

    for event in events:
        # Find the most recent trigger before this event
        assigned_trigger = None
        for trigger in reversed(sorted_triggers):
            if trigger.perf_time <= event.arrival_perf_time:
                assigned_trigger = trigger
                break

        if assigned_trigger is None:
            # Event arrived before first trigger - discard
            continue

        latency_ms = (event.arrival_perf_time - assigned_trigger.perf_time) * 1000
        result[assigned_trigger.index].append((event, latency_ms))

    # Sort each group by arrival time
    for idx in result:
        result[idx].sort(key=lambda pair: pair[0].arrival_perf_time)

    return result


def format_trigger_results_table(
    triggers: list[Trigger],
    assigned: dict[int, list[tuple[CollectedEvent, float]]],
    system_id: str,
    trigger_interval: float,
    connected_systems: list[str],
) -> str:
    """Format per-trigger results as a console table with inter-event gap tracking."""
    lines = []
    system_order = ["WiFi-Cloud", "ILaaS", "ZLP"]
    columns = [s for s in system_order if s in connected_systems]

    events_per_system: dict[str, int] = {col: 0 for col in columns}
    for evts in assigned.values():
        for event, _ in evts:
            if event.system_name in events_per_system:
                events_per_system[event.system_name] += 1
    events_summary = ", ".join(f"{name}: {count}" for name, count in events_per_system.items())
    lines.append("=" * 80)
    lines.append(f"Per-Trigger Latency Report")
    lines.append(f"  System ID: {system_id}")
    lines.append(f"  Triggers: {len(triggers)}, interval: {trigger_interval}s")
    lines.append(f"  Events collected: {events_summary}")
    lines.append("=" * 80)

    # Column widths — wider to accommodate gap info
    trig_w = 9   # "Trigger N"
    time_w = 15  # trigger time
    meas_w = 18  # meas_id
    lat_w = 22   # latency column (e.g. "1234.5ms (gap 3.1s)")

    # Header
    header = f"{'trigger':<{trig_w}} | {'trigger_time':<{time_w}} | {'meas_id':<{meas_w}}"
    for col in columns:
        header += f" | {col:>{lat_w}}"
    lines.append(header)

    sep = "-" * trig_w + "-+-" + "-" * time_w + "-+-" + "-" * meas_w
    for _ in columns:
        sep += "-+-" + "-" * lat_w
    lines.append(sep)

    # Track last arrival perf_time per system for gap calculation
    last_arrival_perf: dict[str, float] = {}
    # Collect all gaps per system for summary
    all_gaps: dict[str, list[float]] = {col: [] for col in columns}

    for trigger in triggers:
        trigger_events = assigned.get(trigger.index, [])
        trigger_label = f"#{trigger.index}"
        trigger_time_str = trigger.timestamp_utc.strftime("%H:%M:%S.%f")[:-3]

        if not trigger_events:
            row = f"{trigger_label:<{trig_w}} | {trigger_time_str:<{time_w}} | {'(no events)':<{meas_w}}"
            for _ in columns:
                row += f" | {'-':>{lat_w}}"
            lines.append(row)
            continue

        # Group events by meas_id, preserving per-event data for gap tracking
        meas_groups: dict[str, dict[str, tuple[float, CollectedEvent]]] = {}
        for event, latency_ms in trigger_events:
            if event.meas_id not in meas_groups:
                meas_groups[event.meas_id] = {}
            meas_groups[event.meas_id][event.system_name] = (latency_ms, event)

        first_row = True
        for meas_id, sys_data in meas_groups.items():
            if first_row:
                trig_cell = f"{trigger_label:<{trig_w}}"
                time_cell = f"{trigger_time_str:<{time_w}}"
                first_row = False
            else:
                trig_cell = f"{'':<{trig_w}}"
                time_cell = f"{'':<{time_w}}"

            row = f"{trig_cell} | {time_cell} | {meas_id:<{meas_w}}"
            for col in columns:
                if col in sys_data:
                    latency_ms, event = sys_data[col]
                    if col in last_arrival_perf:
                        gap_s = event.arrival_perf_time - last_arrival_perf[col]
                        all_gaps[col].append(gap_s)
                        cell = f"{latency_ms:.0f}ms (gap {gap_s:.1f}s)"
                    else:
                        cell = f"{latency_ms:.0f}ms"
                    last_arrival_perf[col] = event.arrival_perf_time
                else:
                    cell = "-"
                row += f" | {cell:>{lat_w}}"
            lines.append(row)

    lines.append(sep)

    # Summary: events per trigger
    total_events = sum(events_per_system.values())
    avg_events = total_events / len(triggers) if triggers else 0
    lines.append(f"Average events/trigger: {avg_events:.1f}")

    # Per-system average latency across all triggers
    for col in columns:
        all_latencies = []
        for evts in assigned.values():
            for event, latency_ms in evts:
                if event.system_name == col:
                    all_latencies.append(latency_ms)
        if all_latencies:
            avg = sum(all_latencies) / len(all_latencies)
            lines.append(f"  {col} avg latency: {avg:.1f}ms (n={len(all_latencies)})")

    # Per-system average inter-event gap
    for col in columns:
        if all_gaps[col]:
            avg_gap = sum(all_gaps[col]) / len(all_gaps[col])
            lines.append(f"  {col} avg inter-event gap: {avg_gap:.1f}s (n={len(all_gaps[col])})")

    lines.append("=" * 80)
    return "\n".join(lines)


def write_trigger_results_csv(
    triggers: list[Trigger],
    assigned: dict[int, list[tuple[CollectedEvent, float]]],
    system_id: str,
) -> str:
    """
    Write per-trigger results to a CSV file with inter-event gap columns.

    Returns the filename written.
    """
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"latency_per_trigger_{system_id}_{timestamp_str}.csv"
    filepath = Path(__file__).resolve().parent / filename

    # Track last arrival perf_time per system for gap calculation
    last_arrival_perf: dict[str, float] = {}

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "trigger_index", "trigger_time", "api_response_time_ms",
            "meas_id", "wifi_cloud_latency_ms", "ilaas_latency_ms", "zlp_latency_ms",
            "wifi_cloud_gap_s", "ilaas_gap_s", "zlp_gap_s",
        ])

        for trigger in triggers:
            trigger_events = assigned.get(trigger.index, [])
            trigger_time_str = trigger.timestamp_utc.isoformat(timespec="milliseconds")

            if not trigger_events:
                writer.writerow([
                    trigger.index, trigger_time_str, f"{trigger.api_response_time_ms:.1f}",
                    "", "", "", "", "", "", "",
                ])
                continue

            # Group events by meas_id, preserving event objects for gap tracking
            meas_groups: dict[str, dict[str, tuple[float, CollectedEvent]]] = {}
            for event, latency_ms in trigger_events:
                if event.meas_id not in meas_groups:
                    meas_groups[event.meas_id] = {}
                meas_groups[event.meas_id][event.system_name] = (latency_ms, event)

            for meas_id, sys_data in meas_groups.items():
                gaps: dict[str, str] = {}
                for sys_name in ("WiFi-Cloud", "ILaaS", "ZLP"):
                    if sys_name in sys_data:
                        _, event = sys_data[sys_name]
                        if sys_name in last_arrival_perf:
                            gap_s = event.arrival_perf_time - last_arrival_perf[sys_name]
                            gaps[sys_name] = f"{gap_s:.2f}"
                        else:
                            gaps[sys_name] = ""
                        last_arrival_perf[sys_name] = event.arrival_perf_time
                    else:
                        gaps[sys_name] = ""

                writer.writerow([
                    trigger.index,
                    trigger_time_str,
                    f"{trigger.api_response_time_ms:.1f}",
                    meas_id,
                    f"{sys_data['WiFi-Cloud'][0]:.1f}" if "WiFi-Cloud" in sys_data else "",
                    f"{sys_data['ILaaS'][0]:.1f}" if "ILaaS" in sys_data else "",
                    f"{sys_data['ZLP'][0]:.1f}" if "ZLP" in sys_data else "",
                    gaps["WiFi-Cloud"],
                    gaps["ILaaS"],
                    gaps["ZLP"],
                ])

    return str(filepath)


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    env = args.env
    system_id = args.system_id
    node_type = args.node_type
    interval = args.interval
    timeout = args.timeout
    debug = args.debug
    output_format = args.output_format

    # Auto-compute trigger count and interval from --interval and --timeout
    num_triggers = max(1, int(timeout // interval))
    trigger_interval = interval

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

    connected_systems = connected_names + (["ZLP"] if zlp_client else [])

    try:
        print(f"Triggering locate every {interval}s for {timeout}s ({num_triggers} triggers)")

        collector = EventCollector()

        # Listeners run for the full test duration
        listener_tasks = [
            listen_for_location(name, ws, system_id, 0.0, timeout, debug, collector=collector)
            for name, ws in connections
        ]
        if zlp_client:
            listener_tasks.append(listen_zlp_for_location(
                sio=zlp_client, system_id=system_id, t_trigger=0.0,
                timeout=timeout, debug=debug, collector=collector,
            ))

        # Build trigger loop task
        trigger_task = trigger_loop(
            rest_client=rest_client,
            system_id=system_id,
            api_node_type=api_node_type,
            num_triggers=num_triggers,
            trigger_interval=trigger_interval,
            debug=debug,
        )

        # Run listeners and trigger loop concurrently
        all_results = await asyncio.gather(*listener_tasks, trigger_task, return_exceptions=True)

        # Last result is from trigger_loop
        trigger_result = all_results[-1]
        listener_results = all_results[:-1]

        # Report listener exceptions
        for result in listener_results:
            if isinstance(result, Exception):
                print(f"  Listener error: {result}")
            elif isinstance(result, dict) and "error" in result and result.get("count", 0) == 0:
                print(f"  {result['name']}: {result['error']}")

        if isinstance(trigger_result, Exception):
            print(f"  Trigger loop error: {trigger_result}")
            return 1

        triggers = trigger_result
        events = collector.get_all_events()
        assigned = assign_events_to_triggers(events, triggers)

        # Output results
        if output_format in ("console", "both"):
            table = format_trigger_results_table(
                triggers=triggers,
                assigned=assigned,
                system_id=system_id,
                trigger_interval=trigger_interval,
                connected_systems=connected_systems,
            )
            print(table)

        if output_format in ("csv", "both"):
            csv_path = write_trigger_results_csv(
                triggers=triggers,
                assigned=assigned,
                system_id=system_id,
            )
            print(f"CSV written to: {csv_path}")

        return 0 if events else 1

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
