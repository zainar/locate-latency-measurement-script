# Spec: Multi-System Latency Comparison Using meas_id Correlation

## Background

We now have a `meas_id` field that propagates through all three systems (WiFi-Cloud, ILaaS, ZLP) for each locate event. This enables definitive correlation of events across systems—we can now compare latency for the exact same physical locate rather than relying on timing heuristics.

## Objective

Update `measure_latency.py` to:
1. Collect all location events (with `meas_id`) across all connected websockets until timeout
2. Correlate events by `meas_id` across systems
3. Output a table showing latency per system for each `meas_id`
4. Provide summary statistics

## Current Behavior

The script triggers a locate, captures the first matching event per websocket, and exits. Output:

```
----------------------------------------
Trigger time: 2026-02-04T20:07:08.333+00:00

WiFi-Cloud:    1306.7 ms  @ (150.31, 65.42, 1.10)
ILaaS:         6526.2 ms  @ (45.38, 20.53, 1.00)  (+5219.5 vs WiFi-Cloud)
ZLP:           1991.4 ms  @ (45.43, 19.92, 1.00)  (+684.7 vs WiFi-Cloud)
----------------------------------------
```

## Proposed Behavior

### Data Collection

1. Connect all websockets (unchanged)
2. Trigger locate, record `t0 = time.perf_counter()` (unchanged)
3. **New:** Continue listening on all websockets until `--timeout` (default 30s)
4. **New:** For each location event received, extract `meas_id` and record:
   - `meas_id`
   - System name (WiFi-Cloud, ILaaS, ZLP)
   - Latency: `t_event - t0` in milliseconds
5. After timeout, correlate events by `meas_id` and render output

### meas_id Extraction

The `meas_id` field location varies by system. Based on DevTools inspection:

| System | Path to meas_id | Notes |
|--------|-----------------|-------|
| WiFi-Cloud | `events.tracker.location.meas_id` | Verify exact path |
| ILaaS | `meas_id` (root level) or `location.meas_id` | Verify exact path |
| ZLP | `events.tracker.location.meas_id` | Verify exact path |

**Implementation note:** These paths need verification. Add debug logging during development to confirm exact structure.

### Output Format

```
----------------------------------------
Trigger time: 2026-02-04T20:07:08.333+00:00
Timeout: 30.0s | Events captured: WiFi-Cloud: 12, ILaaS: 10, ZLP: 11

meas_id            | WiFi-Cloud |       ILaaS |         ZLP
-------------------|------------|-------------|------------
0000019c2e99d78a   |   1306.7ms |  6526.2ms (+5219) |  1991.4ms (+685)
0000019c2e9b87c2   |   1450.2ms |  5890.1ms (+4440) |  2100.3ms (+650)
0000019c2e9c1234   |   1280.5ms |           - |  1850.2ms (+570)
0000019c2e9c5678   |   1320.0ms |  6100.0ms (+4780) |           -
...

Summary:
  Matched (all 3):  8/12 events
  WiFi-Cloud avg:   1340.2 ms
  ILaaS avg:        6012.4 ms  (+4672.2 vs WC)
  ZLP avg:          1985.7 ms  (+645.5 vs WC)
----------------------------------------
```

**Format details:**

- **meas_id column:** 16-char hex string, left-aligned
- **Latency columns:** Right-aligned, format `1234.5ms`
- **Delta:** Show in parentheses after ILaaS/ZLP values, e.g., `(+5219)`. Delta = that system's latency minus WiFi-Cloud latency for same `meas_id`. Omit delta if WiFi-Cloud is missing for that `meas_id`.
- **Missing events:** Display `-` (centered or right-aligned)
- **Row ordering:** Sort by WiFi-Cloud latency ascending (WiFi-Cloud is expected to arrive first). If WiFi-Cloud is missing, sort by earliest arrival.

### Summary Statistics

After the table:

- **Matched (all 3):** Count of `meas_id`s that have events in all three systems / total unique `meas_id`s seen
- **Per-system average:** Average latency across all events for that system
- **Per-system delta:** Average delta vs WiFi-Cloud (only for `meas_id`s where both systems have data)

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| No events received | Print "No events received within timeout" |
| Only one system connected | Still works; table has fewer columns |
| Event missing `meas_id` | Skip event, optionally log in debug mode |
| Duplicate `meas_id` for same system | Keep first occurrence (log warning in debug) |

### CLI Changes

No new arguments required. Existing args work as before:

- `--timeout` now controls how long to collect events (was: how long to wait for first event)
- `--debug` shows all received messages including those without `meas_id`

**Optional future enhancement:** Add `--output csv` flag to export raw data for analysis.

## Data Structures

Suggested internal structure for collecting results:

```python
# Key: meas_id, Value: dict of system -> (latency_ms, raw_event)
results: dict[str, dict[str, tuple[float, dict]]] = {}

# Example:
# {
#     "0000019c2e99d78a": {
#         "WiFi-Cloud": (1306.7, {...}),
#         "ILaaS": (6526.2, {...}),
#         "ZLP": (1991.4, {...}),
#     },
#     "0000019c2e9b87c2": {
#         "WiFi-Cloud": (1450.2, {...}),
#         "ZLP": (2100.3, {...}),
#         # ILaaS missing for this meas_id
#     },
# }
```

## Testing

1. **Single websocket:** Verify table renders correctly with one column
2. **All three websockets:** Verify correlation works, deltas calculate correctly
3. **Missing events:** Verify `-` renders for gaps
4. **No meas_id:** Verify events without `meas_id` are skipped gracefully
5. **Timeout behavior:** Verify script waits full timeout before rendering results

## Out of Scope

- Coordinates in output (may add `--coords` flag later)
- CSV export (future enhancement)
- Automated auth/token refresh