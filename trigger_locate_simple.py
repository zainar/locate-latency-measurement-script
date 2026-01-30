#!/usr/bin/env python3
"""
Minimal locate trigger by system ID.

Usage:
  python3 trigger_locate_simple.py <env> <system_id> <node_type> [repeat_count] [interval_ms]

Examples:
  python3 trigger_locate_simple.py dev 0b409f9eeb834f9e8346ee4b9abe9cbb tag
  python3 trigger_locate_simple.py dev 0b409f9eeb834f9e8346ee4b9abe9cbb reader 3 2000
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
import time 

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
        prog="trigger_locate_simple.py",
        description="Minimal locate trigger by system ID.",
        epilog=f"Environments from env_config.json: {envs}",
    )
    parser.add_argument("env", help="Environment name from env_config.json")
    parser.add_argument("system_id", help="System ID of the node")
    parser.add_argument("node_type", help="tag/tracker or reader/anchor")
    parser.add_argument("repeat_count", nargs="?", type=int, default=0)
    parser.add_argument("interval_ms", nargs="?", type=int, default=2000)
    return parser


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    env = args.env
    system_id = args.system_id
    node_type = args.node_type
    repeat_count = args.repeat_count
    interval_ms = args.interval_ms

    api_node_type = normalize_node_type(node_type)
    env_config = load_env_config(env)
    api_config = Config(env_config["base_url"], env_config["user"], env_config["pw"])
    rest_client = Rest(api_config)

    if env_config.get("token_file"):
        rest_client.config.REST_TOKEN_FILE = env_config["token_file"]

    payload = {
        api_node_type: {
            "locate": {
                "repeat_count": repeat_count,
                "backoff": interval_ms,
            }
        },
        "tags": {
            "experimentID": "simple locate test",
            "exp_tag": "simple locate test",
            "ref_loc_id": "None",
            "ref_xy": "0,0,0",
        },
    }

    api_endpoint = f"nodes/{system_id}/action"
    print(f"POST {api_endpoint}")
    
    t_start = time.perf_counter()
    response = await rest_client.post(api_endpoint, payload, verbose=1)
    t_end = time.perf_counter()

    round_trip_ms = (t_end - t_start) * 1000
    print(f"API round-trip: {round_trip_ms:.1f} ms")
    
    print(json.dumps(response, indent=2) if isinstance(response, dict) else response)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
