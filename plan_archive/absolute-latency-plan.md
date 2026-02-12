# Per-Trigger Latency Measurement Spec

## Overview

Enhance the latency measurement script to record timestamps for each Locate API trigger and infer which websocket event corresponds to which trigger. This enables true round-trip latency measurement per trigger rather than just "time since test started."

## Current Behavior

1. Script calls Locate API repeatedly at a configured interval (e.g., every 3 seconds)
2. Script records a single `trigger_time` at the start of the test
3. All websocket events are timestamped relative to that initial trigger
4. Output shows `meas_id` and arrival times, but no way to know which trigger caused which event

## Desired Behavior

1. Script records a timestamp for **each** Locate API call
2. Script infers which trigger caused each event based on timing
3. Output shows per-trigger latency for each system
4. True round-trip latency = `event_arrival_time - trigger_timestamp`

## Implementation

### Data Structures

```python
@dataclass
class Trigger:
    index: int                    # 1-indexed trigger number
    timestamp: datetime           # When the Locate API was called
    api_response_time_ms: float   # How long the API call took (optional, for diagnostics)

@dataclass  
class Event:
    meas_id: str
    system: str                   # 'wifi_cloud', 'ilaas', or 'zlp'
    arrival_time: datetime        # When the websocket received the event
    assigned_trigger: Optional[int]  # Trigger index this event is assigned to (None if unassigned)
```

### Recording Triggers

Modify the trigger loop to store each trigger:

```python
triggers: List[Trigger] = []

for i in range(num_triggers):
    trigger_time = datetime.now(timezone.utc)
    response = await rest_client.post(locate_url, ...)
    api_duration = (datetime.now(timezone.utc) - trigger_time).total_seconds() * 1000
    
    triggers.append(Trigger(
        index=i + 1,
        timestamp=trigger_time,
        api_response_time_ms=api_duration
    ))
    
    await asyncio.sleep(trigger_interval)
```

### Event-to-Trigger Assignment

Assign each event to the **most recent preceding trigger**. Multiple events may be assigned to the same trigger—this is expected since:
- Tags may emit ambient locates from normal operation (e.g., continuous 3s update rate)
- The triggered locate is mixed in with ambient events

The user reviews the output and uses judgment to identify which event was caused by the trigger (typically the one with latency closest to expected system latency).

```python
def assign_events_to_triggers(
    events: List[Event], 
    triggers: List[Trigger]
) -> Dict[int, List[Event]]:
    """
    Assigns each event to the most recent trigger that preceded it.
    
    Returns: {trigger_index: [Event, Event, ...]}
    """
    
    # Sort triggers by timestamp
    sorted_triggers = sorted(triggers, key=lambda t: t.timestamp)
    
    result = {t.index: [] for t in triggers}
    
    for event in events:
        # Find the most recent trigger before this event arrived
        assigned_trigger = None
        for trigger in reversed(sorted_triggers):
            if trigger.timestamp < event.arrival_time:
                assigned_trigger = trigger
                break
        
        if assigned_trigger:
            event.assigned_trigger = assigned_trigger.index
            latency_ms = (event.arrival_time - assigned_trigger.timestamp).total_seconds() * 1000
            event.latency_ms = latency_ms
            result[assigned_trigger.index].append(event)
    
    # Sort events within each trigger by arrival time
    for trigger_index in result:
        result[trigger_index].sort(key=lambda e: e.arrival_time)
    
    return result
```

**Key points:**
- Every event gets assigned to exactly one trigger (the most recent one before it arrived)
- Multiple events per trigger is normal—user determines which is the "real" response
- Latency = `event_arrival_time - trigger_timestamp` for the assigned trigger

### Output Format

#### Console Output

```
System ID: 1c67b05a134e48eab1fc557a2f850b81
Trigger interval: 5.0s
Triggers: 60 | Events collected: WC=72, ILaaS=70, ZLP=71

trigger | trigger_time              | meas_id          | WC latency | ILaaS latency | ZLP latency
--------+---------------------------+------------------+------------+---------------+------------
      1 | 2026-02-05T17:48:03.632Z  | 0000019c2ee20447 |    2134ms  |       5831ms  |     3253ms
        |                           | 0000019c2ee20512 |    3842ms  |       7102ms  |     4891ms
      2 | 2026-02-05T17:48:08.645Z  | 0000019c2ee231b2 |    1980ms  |       4849ms  |     2797ms
      3 | 2026-02-05T17:48:13.658Z  |                - |          - |             - |           -
      4 | 2026-02-05T17:48:18.671Z  | 0000019c2ee24e33 |    2201ms  |       5632ms  |     3054ms
        |                           | 0000019c2ee24f01 |    4012ms  |       6891ms  |     4523ms
        |                           | 0000019c2ee24f88 |    4899ms  |       7788ms  |     5401ms
...
-------------------------------------------------------------------------------------------------
```

**Notes on output:**
- Multiple rows per trigger when multiple events arrive between triggers
- First row shows trigger index and timestamp; subsequent rows for same trigger leave those columns blank
- `-` indicates no events arrived for that trigger
- User reviews and determines which event per trigger is the "real" triggered response vs ambient

#### CSV Output

**Filename:** `latency_per_trigger_{system_id}_{timestamp}.csv`

```csv
trigger_index,trigger_time,meas_id,wifi_cloud_latency_ms,ilaas_latency_ms,zlp_latency_ms
1,2026-02-05T17:48:03.632Z,0000019c2ee20447,2134,5831,3253
1,2026-02-05T17:48:03.632Z,0000019c2ee20512,3842,7102,4891
2,2026-02-05T17:48:08.645Z,0000019c2ee231b2,1980,4849,2797
3,2026-02-05T17:48:13.658Z,,,,
4,2026-02-05T17:48:18.671Z,0000019c2ee24e33,2201,5632,3054
4,2026-02-05T17:48:18.671Z,0000019c2ee24f01,4012,6891,4523
4,2026-02-05T17:48:18.671Z,0000019c2ee24f88,4899,7788,5401
```

Each event gets its own row. Multiple rows with the same `trigger_index` indicate multiple events arrived between that trigger and the next.

### Edge Cases

1. **More events than triggers:** Normal—multiple events per trigger window due to ambient tag activity.

2. **Zero events for a trigger:** Possible if tag was inactive or packets dropped. Show empty row.

3. **Event arrives before first trigger:** Discard (no trigger to assign it to).

4. **WiFi-Cloud returns 0 events:** Assignment proceeds with ILaaS and ZLP only. WiFi-Cloud column shows `-` for all rows.

### Configuration

Add/modify these CLI arguments:

```
--trigger-interval SECONDS    Interval between Locate API calls (default: 5.0)
--output-format FORMAT        'console', 'csv', or 'both' (default: 'both')
```

### Backward Compatibility

- Keep the existing `meas_id`-based output as an option (`--legacy-output`)
- Default to the new per-trigger output
- Both outputs can be generated in the same run if needed

## Testing

1. Run with 5-second intervals, verify events are assigned to correct triggers
2. Run with tag in continuous mode (e.g., 3s update rate), observe multiple events per trigger
3. Test with one system disabled (e.g., WiFi-Cloud websocket disconnected)
4. Verify CSV output loads correctly in Excel/pandas

## Notes

- **Trigger interval choice:** Shorter intervals = more data points but harder to distinguish triggered events from ambient. Longer intervals = cleaner separation but fewer samples. 5-10 seconds is a reasonable starting point. Document in README.
- **Identifying the "real" response:** The triggered event typically has the shortest latency within its trigger window. Ambient events will have higher latency (since they happened closer to the previous trigger).
- The assignment algorithm is simple: each event goes to its most recent preceding trigger. No filtering or heuristics.