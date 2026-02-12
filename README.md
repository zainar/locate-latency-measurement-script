# Latency Measurement Tool

Measure end-to-end latency from triggering a locate via REST API to receiving location events on one or more websockets. Supports comparing latency across multiple systems (WiFi-Cloud, ILaaS, ZLP) from a single locate trigger.

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

## Getting WebSocket URLs

### WiFi-Cloud and ILaaS

For WiFi-Cloud and ILaaS:

1. Open the corresponding web app in browser and log in
2. Open DevTools (F12) → Network tab
3. Filter by "WS" to show only WebSocket connections
4. Find the websocket connection to `/pipeline/ws/updates/...`
5. Copy the full URL including the `?token=...` parameter

Example WiFi-Cloud URL:
```
wss://api.wifi-prd-jpn.zainar.net/pipeline/ws/updates/v2/sites/<site_id>?token=<token>&event=tracker:location&...
```

**Note:** Tokens expire after some time. You'll need to copy fresh URLs if connections fail.

### ZLP (Socket.IO)

ZLP uses Socket.IO instead of plain WebSocket. You need:

1. **ZLP URL**: The base Socket.IO endpoint (e.g., `wss://zps-web-api.zlp-dev.zainar.net`)
2. **ZLP Token**: JWT from the `__session` cookie
3. **ZLP Account**: Your account resource name (UUID format)

To get the ZLP token:
1. Open the ZLP web app in browser and log in
2. Open DevTools (F12) → Application tab → Cookies
3. Find the `__session` cookie and copy its value (the JWT token)

Alternatively, from the Network tab:
1. Filter by "WS" and find the Socket.IO connection
2. Look for the auth message containing the token

## Usage

```bash
python measure_latency.py <env> <system_id> <node_type> [websocket options] [options]
```

**Arguments:**
- `env` - Environment name: `dev`, `int`, `prod-apac`, or `prod-us`
- `system_id` - System ID of the tag/tracker to locate
- `node_type` - `tag` (or `tracker`) / `reader` (or `anchor`)

**Websocket Options (at least one required):**
- `--ws-wifi-cloud` - WiFi-Cloud websocket URL (baseline for comparison)
- `--ws-ilaas` - ILaaS websocket URL (requires `--ilaas-account` and `--ilaas-site`)
- `--zlp-url` - ZLP Socket.IO URL (requires `--zlp-token` and `--zlp-account`)

**ILaaS Subscription Options (required when using --ws-ilaas):**
- `--ilaas-account` - ILaaS account resource name (e.g., `acct-958041d25b9a4b39b87b83f0eae834cb`)
- `--ilaas-site` - ILaaS site resource name (e.g., `site-f6aa5b0283114de6a3d8933bebc6dc4a`)

**ZLP Options (required when using --zlp-url):**
- `--zlp-token` - ZLP JWT token (from `__session` cookie)
- `--zlp-account` - ZLP account resource name (UUID format, e.g., `7eb56462-9d4f-4306-9a3d-4eca6fd927bd`)

**Other Options:**
- `--repeat-count` - Number of additional locates for server to perform (default: 0)
- `--interval` - Interval between locates in ms (default: 2000, range: 500-10000)
- `--timeout` - Event wait timeout in seconds (default: 30)
- `--debug` - Print all received websocket messages from all connections

## Examples

```bash
# Single websocket (WiFi-Cloud only)
python measure_latency.py prod-apac d1638e05370c49a0bd5f5d9088e53b78 tag \
  --ws-wifi-cloud "wss://api.wifi-prd-jpn.zainar.net/pipeline/ws/updates/v2/sites/...?token=..."

# Compare two systems (WiFi-Cloud vs ILaaS)
python measure_latency.py prod-apac d1638e05370c49a0bd5f5d9088e53b78 tag \
  --ws-wifi-cloud "wss://wifi-cloud-url...?token=..." \
  --ws-ilaas "wss://ilaas-url...?X-Amz-..." \
  --ilaas-account "acct-958041d25b9a4b39b87b83f0eae834cb" \
  --ilaas-site "site-f6aa5b0283114de6a3d8933bebc6dc4a"

# Compare all three systems
python measure_latency.py prod-apac d1638e05370c49a0bd5f5d9088e53b78 tag \
  --ws-wifi-cloud "wss://wifi-cloud-url...?token=..." \
  --ws-ilaas "wss://ilaas-url...?X-Amz-..." \
  --ilaas-account "acct-..." --ilaas-site "site-..." \
  --zlp-url "wss://zps-web-api.zlp-dev.zainar.net" \
  --zlp-token "eyJhbGci..." \
  --zlp-account "7eb56462-9d4f-4306-9a3d-4eca6fd927bd"

# ZLP only
python measure_latency.py prod-apac d1638e05370c49a0bd5f5d9088e53b78 tag \
  --zlp-url "wss://zps-web-api.zlp-dev.zainar.net" \
  --zlp-token "eyJhbGci..." \
  --zlp-account "7eb56462-9d4f-4306-9a3d-4eca6fd927bd"

# Multiple locates (1 initial + 3 repeats = 4 total, 1 second apart)
python measure_latency.py prod-apac d1638e05370c49a0bd5f5d9088e53b78 tag \
  --ws-wifi-cloud "..." --repeat-count 3 --interval 1000

# With debug output to see all events from all connections
python measure_latency.py prod-apac d1638e05370c49a0bd5f5d9088e53b78 tag \
  --ws-wifi-cloud "..." --ws-ilaas "..." --debug

# With custom timeout
python measure_latency.py prod-apac d1638e05370c49a0bd5f5d9088e53b78 tag \
  --ws-wifi-cloud "..." --timeout 60
```

## Output

### Single Websocket

```
Connecting to 1 websocket(s)...
  WiFi-Cloud: connected
Triggering locate for tag: d1638e05370c49a0bd5f5d9088e53b78 (1 locate(s), 2000ms interval)
  POST nodes/d1638e05370c49a0bd5f5d9088e53b78/action
  API response: OK (action_id: 1749bae966e14cacbb238ccc0f641754)
Waiting for location events (timeout: 30.0s)...
----------------------------------------
Trigger time: 2026-01-30T05:23:23.000Z

WiFi-Cloud:   1203.4 ms  @ (103.95, 48.56, 6.90)
----------------------------------------
```

### Multiple Websockets

```
Connecting to 3 websocket(s)...
  WiFi-Cloud: connected
  ILaaS: connected
  ZLP: connected
Triggering locate for tag: d1638e05370c49a0bd5f5d9088e53b78 (1 locate(s), 2000ms interval)
  POST nodes/d1638e05370c49a0bd5f5d9088e53b78/action
  API response: OK (action_id: 1749bae966e14cacbb238ccc0f641754)
Waiting for location events (timeout: 30.0s)...
----------------------------------------
Trigger time: 2026-01-30T05:23:23.000Z

WiFi-Cloud:   1203.4 ms  @ (103.95, 48.56, 6.90)
ILaaS:        2847.2 ms  @ (103.92, 48.61, 6.88)  (+1643.8 vs WiFi-Cloud)
ZLP:          timeout
----------------------------------------
```

- Each system shows its own location (coordinates may differ between systems)
- Delta shown vs WiFi-Cloud when available
- "timeout" or error message for failed listeners
- The per-trigger report header shows event counts broken down by system (e.g., `Events collected: WiFi-Cloud: 10, ILaaS: 10, ZLP: 10`) rather than a single total

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
