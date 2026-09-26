# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this project is

`ha-hikvision-acs` is a Home Assistant custom integration for **Hikvision access
control terminals** (keypad / card / fingerprint door terminals), talking to the
device locally over **ISAPI** — Hikvision's HTTP API.

- Integration domain: `hikvision_acs`
- Component path: `custom_components/hikvision_acs/`
- Language: Python, async (`aiohttp`), no blocking I/O in the event loop
- No cloud, no MQTT, no Hikvision SDK — HA talks to the device directly

This is a public portfolio project. Code quality, tests, documentation and
commit hygiene matter as much as the feature set. Prefer a small, correct,
well-tested surface over broad half-working coverage.

## Why ISAPI and not the SDK

The widely used `pergolafabio/Hikvision-Addons` add-on works over Hikvision's
proprietary Network SDK on TCP port 8000. The target hardware for this project
does not expose that port at all:

```
$ nc -zv 192.168.0.45 8000
Connection refused
$ nc -zv 192.168.0.45 80
succeeded!
```

The device lists its own access protocols, and SDK is not among them:

```
GET /ISAPI/Security/adminAccesses
→ HTTP/80 (enabled), RTSP/554, HTTPS/443 (enabled)
```

So the SDK route is closed on this class of device, and ISAPI is the only
transport. Do not add SDK support or SDK-based dependencies.

## Reference hardware

Primary development device:

| Field | Value |
| --- | --- |
| Model | DS-K1T805MBFWX (Value Series) |
| Firmware | V1.9.1, build 240909 |
| Device type | ACS / accessControlTerminal |
| Relays (`alarmOutNum`) | 1 → door number is always `1` |
| Alarm inputs | 2 |
| Auth | HTTP Digest (Basic also accepted) |
| Ports | HTTP 80, HTTPS 443 |

Treat this model as *tested*, not as *the only supported model*. Other
DS-K1T3xx / DS-K1T8xx terminals are expected to work; anything model-specific
must be data-driven (lookup tables), never hardcoded branching.

## The two ISAPI endpoints that matter

**Event stream** — a long-lived multipart HTTP response, one part per event:

```
GET /ISAPI/Event/notification/alertStream
```

**Door open** — fires the relay:

```
PUT /ISAPI/AccessControl/RemoteControl/door/1
Content-Type: application/xml

<RemoteControlDoor><cmd>open</cmd></RemoteControlDoor>
```

**Device identity** — used for the config-flow connection test and device info:

```
GET /ISAPI/System/deviceInfo
```

## Event payload shape

Parts are delimited by `--MIME_boundary`, each with its own headers, and the
body for access events is JSON (other event types may send XML or JPEG — the
parser must not assume JSON).

A real card-read event from the reference device (anonymised):

```json
{
  "ipAddress": "192.168.0.45",
  "portNo": 80,
  "protocol": "HTTP",
  "macAddress": "00:00:5e:00:53:01",
  "dateTime": "2026-07-12T12:57:33+01:00",
  "activePostCount": 1,
  "eventType": "AccessControllerEvent",
  "eventState": "active",
  "eventDescription": "Access Controller Event",
  "AccessControllerEvent": {
    "deviceName": "Access Control Device",
    "majorEventType": 5,
    "subEventType": 38,
    "cardNo": "0000000001",
    "cardType": 1,
    "reportChannel": 3,
    "cardReaderKind": 1,
    "cardReaderNo": 1,
    "verifyNo": 166,
    "name": "Jane Doe",
    "employeeNoString": "00000001",
    "serialNo": 5606,
    "currentVerifyMode": "cardOrFpOrPw",
    "currentEvent": false,
    "frontSerialNo": 5605,
    "hasRecord": false
  }
}
```

### Fields the integration relies on

- `majorEventType` / `subEventType` — event identity. **Codes differ between
  firmware versions**; the same card read is reported as `5/1` on some devices
  and `5/38` on others. Keep the mapping in one table and make unknown codes
  degrade gracefully (report raw major/minor, do not crash, do not drop).
- `currentEvent` — `false` means a replayed/backlog record, `true` means live.
  On connect the device flushes buffered history, so **automations must only
  fire on live events**. This distinction is the single most important piece of
  logic in the event pipeline.
- `serialNo` / `frontSerialNo` — monotonic counters, the basis for
  deduplication and (later) backfill.
- `name`, `employeeNoString`, `cardNo` — person identity. Personal data: never
  log at INFO, never put a card number in an entity ID.
- `dateTime` — **do not trust it.** Device clocks are easy to misconfigure: the
  reference device once ran two months off, and its "one hour off" turned out
  to be DST switched off in the device's time settings (the timestamp carried
  `+01:00` in summer). Prefer HA's own receipt time for state, and expose the
  device timestamp as an attribute only.

### Observed codes (reference device, V1.9.1)

Codes marked confirmed were matched one-to-one, by timestamp and order, against
the labels in the device's own web UI event log (Access Control → Event
Search). The device log labels are quoted.

| major/minor | Meaning | Confirmed |
| --- | --- | --- |
| 5/1 | "Authenticated via Valid Card" (live card reads, person identity) | yes |
| 5/38 | Card authenticated, access granted (older records, person identity) | yes |
| 5/181 | "Authenticated via PIN" (person identity) | yes |
| 5/21 | "Door Open (Door Lock)", the lock releasing | yes |
| 5/22 | "Door Closed (Door Lock)", about 3 s after 5/21 | yes |
| 3/1024 | "Remote Unlock" (HA button, Hik-Connect) | yes |
| 3/121 | "Remote: Alarm Arming", an event-stream subscriber connecting | yes |
| 3/122 | "Remote: Alarm Disarming", a subscriber disconnecting | yes |
| 3/112 | Unknown | no |
| 3/1029 | System/operation event; possibly a configuration change | no |

This table is incomplete and must grow from captured samples, not guesses. If a
code's meaning is not confirmed by an actual capture, mark it unconfirmed.

## Architecture

```
custom_components/hikvision_acs/
├── __init__.py          # setup/unload, wiring
├── manifest.json        # domain, requirements, version, iot_class: local_push
├── config_flow.py       # UI setup: host, port, credentials, SSL verify
├── const.py             # DOMAIN, defaults, config keys
├── api.py               # ISAPI client: auth, alertStream, door control
├── parser.py            # multipart splitting + per-part decoding (pure, testable)
├── events.py            # major/minor → semantic event mapping
├── coordinator.py       # stream lifecycle, reconnect, dispatch to entities
├── sensor.py
├── binary_sensor.py
└── button.py
```

Design rules:

- `parser.py` and `events.py` are **pure and dependency-free** — no aiohttp, no
  HA imports. They take bytes/dicts and return dicts/dataclasses. This is what
  makes the test suite possible, and it is the part worth showing off.
- Networking lives in `api.py`. Entity classes never make HTTP calls directly.
- The stream is push, not poll. `iot_class` is `local_push`; do not introduce a
  polling `DataUpdateCoordinator` update interval for events.
- The stream **will** drop — the reference device is on Wi-Fi. Reconnect with
  exponential backoff and jitter, log reconnects at DEBUG, surface prolonged
  failure as entity availability rather than by raising.

## Scope

**v1**

- Config flow with a real connection test against `/ISAPI/System/deviceInfo`
- alertStream client with reconnect
- `sensor` — last access event, with person name / card / verify mode as attributes
- `binary_sensor` — access granted (momentary, auto-off)
- `button` — open door

**Later, not now**

- Backfill from `/ISAPI/AccessControl/AcsEvent` after downtime, deduplicated by `serialNo`
- Door state, tamper, alarm inputs
- Daily/hourly statistics
- Person/user management

Do not start v2 items unless asked. A shipped, tested v1 beats a sprawling draft.

## Conventions

- Target the Home Assistant version current at time of writing; follow HA's own
  component conventions (config entries, `async_setup_entry`, unique IDs,
  `DeviceInfo`) rather than inventing structure.
- Every entity needs a stable `unique_id` derived from the device serial number,
  not from the IP address — IPs change.
- Type hints everywhere. The pure modules should pass a strict type checker.
- Tests with `pytest` + `pytest-asyncio`. Parser tests run against captured
  fixtures in `tests/fixtures/` — real payloads, anonymised (replace real names,
  card numbers and MACs with placeholders before committing).
- No secrets, real card numbers, real person names, or real MAC addresses in
  the repo, in fixtures, or in commit messages.
- README in English: supported models, setup steps, entity list, the event-code
  table, and how a user can capture their own codes to report new ones.

## Security notes

This integration controls physical access to a building. Accordingly:

- Recommend in the README that users create a **dedicated low-privilege ISAPI
  account** rather than reusing `admin`.
- The device locks out logins after a handful of failed attempts — validate
  credentials once in the config flow and do not retry a rejected login in a
  loop.
- Prefer HTTPS (443) by default in the config flow, with SSL verification
  optional, since these devices ship self-signed certificates.
- Never log credentials, full card numbers, or Authorization headers.
