# Latency Measurement Tool

Measure end-to-end latency from triggering a locate via REST API to receiving the location event on the websocket.

## Setup

```bash
# Create virtual environment (if not already done)
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration (required before first run)

The tool reads API credentials from `env_config.json`, which is not committed to the repo. Create it from the example:

```bash
cp env_config.example.json env_config.json
```

Edit `env_config.json` and replace `your_username` and `your_password` with your real API credentials for each environment you use. The REST client will cache an API token in `token.txt` after the first authenticate; both `env_config.json` and `token.txt` are gitignored and must not be committed.

## Getting the WebSocket URL

1. Open ZaiNar web app in browser and log in
2. Open DevTools (F12) → Network tab
3. Filter by "WS" to show only WebSocket connections
4. Find the websocket connection to `/pipeline/ws/updates/...`
5. Copy the full URL including the `?token=...` parameter

The URL looks like:
```
wss://api.wifi-prd-jpn.zainar.net/pipeline/ws/updates/v2/sites/<site_id>?token=<token>&event=tracker:location&...
```

**Note:** The token expires after some time. You'll need to copy a fresh URL if the connection fails.

## Usage

```bash
python measure_latency.py <env> <system_id> <node_type> --ws-url "<websocket_url>" [options]
```

**Arguments:**
- `env` - Environment name: `dev`, `int`, `prod-apac`, or `prod-us`
- `system_id` - System ID of the tag/tracker to locate
- `node_type` - `tag` (or `tracker`) / `reader` (or `anchor`)
- `--ws-url` - WebSocket URL copied from browser DevTools
- `--repeat-count` - Number of additional locates for server to perform (default: 0)
- `--interval` - Interval between locates in ms (default: 2000, range: 500-10000)
- `--timeout` - Event wait timeout in seconds (default: 30)
- `--debug` - Print all received websocket messages

## Examples

```bash
# Basic latency measurement (single locate)
python measure_latency.py prod-apac d1638e05370c49a0bd5f5d9088e53b78 tag \
  --ws-url "wss://api.wifi-prd-jpn.zainar.net/pipeline/ws/updates/v2/sites/...?token=..."

# Multiple locates (1 initial + 3 repeats = 4 total, 1 second apart)
python measure_latency.py prod-apac d1638e05370c49a0bd5f5d9088e53b78 tag \
  --ws-url "..." --repeat-count 3 --interval 1000

# With debug output to see all events
python measure_latency.py prod-apac d1638e05370c49a0bd5f5d9088e53b78 tag \
  --ws-url "..." --debug

# With custom timeout
python measure_latency.py prod-apac d1638e05370c49a0bd5f5d9088e53b78 tag \
  --ws-url "..." --timeout 60
```

## Output

```
Connecting to websocket...
Connected!
Triggering locate for tag: d1638e05370c49a0bd5f5d9088e53b78
  POST nodes/d1638e05370c49a0bd5f5d9088e53b78/action
  API response: OK (action_id: 1749bae966e14cacbb238ccc0f641754)
Waiting for location event (timeout: 15.0s)...
Event received!
----------------------------------------
End-to-end latency: 3403.0 ms
Server timestamp:   2026-01-30T05:23:26.274+00:00
Node ID:            d1638e05370c49a0bd5f5d9088e53b78
Location:           (105.26, 48.57, 6.74)
----------------------------------------
```

## Other Scripts

### trigger_locate_simple.py

Trigger a locate without measuring latency:

```bash
python trigger_locate_simple.py <env> <system_id> <node_type> [repeat_count] [interval_ms]

# Examples
python trigger_locate_simple.py dev 0b409f9eeb834f9e8346ee4b9abe9cbb tag
python trigger_locate_simple.py dev 0b409f9eeb834f9e8346ee4b9abe9cbb reader 3 2000
```

## Limitations

**Event correlation:** The script cannot definitively correlate websocket events with the specific locate request that triggered them. The `action_id` returned by the API does not appear in websocket events, and custom tags are not propagated.

The latency measurement reports the time from triggering the API call to receiving the first matching location event for the specified `system_id`. This event may have been triggered by:
- Our API call (expected)
- A continuous locate already running on the tracker
- Another system triggering a locate

For most use cases this is acceptable, but be aware of this limitation when interpreting results.

## Environment Configuration

Environments are configured in `env_config.json` (see [Configuration](#configuration-required-before-first-run) above). The file defines:

| Environment | API Endpoint |
|-------------|--------------|
| `dev` | api.wifi-dev.zainar.net |
| `int` | api.wifi-int.zainar.net |
| `prod-apac` | api.wifi-prd-jpn.zainar.net |
| `prod-us` | api.wifi-prd-us.zainar.net |
