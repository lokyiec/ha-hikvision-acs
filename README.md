# Hikvision Access Control for Home Assistant

A Home Assistant custom integration for **Hikvision access control terminals**
(keypad / card / fingerprint door terminals). It talks to the device locally
over **ISAPI**, Hikvision's HTTP API. It needs no cloud, no MQTT and no
Hikvision SDK.

Events are **pushed** by the device over a long-lived HTTP stream
(`iot_class: local_push`), so a card read shows up in Home Assistant within a
moment, and nothing is polled.

## Why ISAPI and not the SDK

Popular alternatives use Hikvision's proprietary Network SDK on TCP port 8000.
Many current terminals don't expose that port at all. Their only access
protocols are HTTP/HTTPS and RTSP. This integration uses only what those
devices offer: plain ISAPI over HTTP(S) with Digest authentication.

## Supported devices

| Model | Firmware | Status |
| --- | --- | --- |
| DS-K1T805MBFWX (Value Series) | V1.9.1 build 240909 | Tested |
| Other DS-K1T3xx / DS-K1T8xx terminals | — | Expected to work; reports welcome |

The device needs these ISAPI endpoints:
`/ISAPI/System/deviceInfo`, `/ISAPI/Event/notification/alertStream` and
`/ISAPI/AccessControl/RemoteControl/door/1`.

## Installation

### HACS (custom repository)

1. HACS → ⋮ → *Custom repositories* → add `https://github.com/lokyiec/ha-hikvision-acs`, category *Integration*.
2. Install **Hikvision Access Control** and restart Home Assistant.

### Manual

Copy `custom_components/hikvision_acs` into your Home Assistant
`config/custom_components/` directory and restart.

## Setup

1. **Pick the account.** If the terminal supports extra login accounts, create
   a dedicated one with the lowest privilege level that still allows remote
   door control (needed for the *Open door* button) and event notifications.

   Some models only have `admin`. The Value Series DS-K1T805MBFWX (V1.9.1) is
   one: its web UI has no *Add* button under *User Management*, and
   `GET /ISAPI/Security/users/capabilities` returns `notSupport`. On those
   devices use `admin` with a strong password you don't use anywhere else, and
   connect over HTTPS.
2. In Home Assistant: *Settings → Devices & services → Add integration →
   Hikvision Access Control*.
3. Enter the host, port and credentials. HTTPS (port 443) is the default.
   *Verify SSL certificate* is off by default because these devices ship
   self-signed certificates. For plain HTTP, turn *SSL* off and use port 80.

The connection is tested once against `/ISAPI/System/deviceInfo`. If the
password is wrong, you'll see an error and nothing is retried. **Hikvision
devices lock an account after a handful of failed logins**, so check the
password before you submit again.

If the credentials stop working later (for example, the password was changed on
the device), the integration stops, doesn't retry, and asks you to
reauthenticate. Once a password is rejected, the integration won't send it
again, and the *Open door* button goes unavailable until you reauthenticate.

If the account can log in but isn't allowed to read the event stream (HTTP 403),
you'll see a **Repairs** issue instead of a reauth prompt. Grant the account
event/notification permission on the device, then reload the integration.

## Entities

Every entity's unique ID comes from the device serial number, not its IP
address, so changing the address doesn't break anything. If the same device is
added again under a new address, the existing entry is updated.

| Entity | Type | Description |
| --- | --- | --- |
| Access | `event` | One entry per access: `card_access_granted`, `pin_access_granted`, `remote_unlock` or `unknown`, with the person and details as attributes. **Its history is the access log.** Build lists and dashboards on it. |
| Last access event | `sensor` (timestamp) | When the last live access event arrived. Attributes: `event`, `description`, `category`, `confirmed`, `major`, `minor`, `serial_no`, `device_time`, `person_name`, `employee_no`, `card_no`, `verify_mode`, `card_reader_no`, `door_no`. |
| Access granted | `binary_sensor` | Turns on for 5 seconds on each live access-granted event. A new grant restarts the timer. |
| Open door | `button` | Pulses the door relay (door 1). Stays available while the event stream reconnects, and goes unavailable only if the credentials are rejected. |

Door relay and system events (door lock opened/closed, subscriber connect)
don't update *Access* or *Last access event*. They carry no person and would
push out who actually came in. They still go to the event bus and Activity.

Timestamps are **Home Assistant's receipt time**. The device clock is not
used, because it is easy to get wrong: if the device's DST setting is off,
every summer timestamp is an hour behind. The device's own timestamp is kept
as the `device_time` attribute.

### Activity

Every live event gets a readable Activity (logbook) entry, such as
"Jane authenticated via PIN" or "Terminal remote unlock". A *Logbook* card
filtered to the device gives a quick access list with no custom code.

The event entities go *unavailable* only after the stream has been down for
60 seconds, so a brief Wi-Fi drop doesn't flap them.

### Event bus

Every **live** access-controller event, known or not, is also fired as a
`hikvision_acs_event` on the Home Assistant event bus. It carries the same
fields as the sensor attributes, plus `device_id` and `serial_number`. Use this
for automations such as "when card X is used":

```yaml
triggers:
  - trigger: event
    event_type: hikvision_acs_event
    event_data:
      category: access_granted
      employee_no: "00000001"
actions:
  - action: light.turn_on
    target:
      entity_id: light.hallway
```

### Live vs replayed events

Each time it connects, the terminal first replays buffered history. Those
records are marked `currentEvent: false`. **Only events explicitly marked
`currentEvent: true` reach entities or the event bus.** Replayed records are
logged at DEBUG and dropped, so a Home Assistant restart can't re-run
yesterday's "door opened" automations. Events are also deduplicated by the
device's `serialNo`.

## Event codes

Hikvision identifies events by a `majorEventType` / `subEventType` pair, and
**the numbers differ between firmware versions**. The table grows only from
real captures. Codes whose meaning hasn't been confirmed by a capture are
marked as such, and their events have `confirmed: false`.

Codes marked ✅ were confirmed on the DS-K1T805MBFWX (V1.9.1) by matching them
against the device's own web UI event log (*Access Control → Event Search*).

| major/minor | Meaning | Category | Confirmed |
| --- | --- | --- | --- |
| 5/1 | Authenticated via card | `access_granted` | ✅ |
| 5/38 | Authenticated via card (older records) | `access_granted` | ✅ |
| 5/181 | Authenticated via PIN | `access_granted` | ✅ |
| 3/1024 | Remote unlock (HA button, Hik-Connect) | `remote_access` | ✅ |
| 5/21 | Door lock opened | `door` | ✅ |
| 5/22 | Door lock closed, about 3 s after 5/21 | `door` | ✅ |
| 3/121 | Remote alarm arming: an event subscriber (such as this integration) connected. Debug log only. | `system` | ✅ |
| 3/122 | Remote alarm disarming: an event subscriber disconnected. Debug log only. | `system` | ✅ |
| 3/112 | Unknown | `system` | ❌ |
| 3/1029 | System/operation event, possibly a configuration change | `system` | ❌ |

The two subscriber codes are bookkeeping that this integration causes itself
on every connect, so they are logged at DEBUG and not sent to entities, the
event bus or Activity. Every other code, known or unknown, is.

Unknown codes are never dropped. They arrive with
`event: unknown_<major>_<minor>`. Their category is `system` for major 3
(Hikvision's "operation" type) and `unknown` otherwise.

The quickest way to decode a new code is to trigger it, then find it in the
device's *Event Search* page at the same time. The page labels every entry.

### Capturing your own codes

Reports of new codes are the most useful contribution. Two ways to capture
them:

**From Home Assistant:** go to *Developer tools → Events*, listen to
`hikvision_acs_event`, then trigger the action on the device (card read, wrong
PIN, door left open…). Note `major`, `minor` and what you did.

**Straight from the device:**

```bash
curl --digest -u USER -k -N https://DEVICE_IP/ISAPI/Event/notification/alertStream
```

Then open an issue with the major/minor pair, what you did on the device, your
model and firmware, and ideally the JSON body. **Remove names, card numbers,
employee numbers and MAC addresses first.**

## Privacy and security

- The integration controls physical access. Use a dedicated low-privilege
  account where the device supports one. Otherwise use `admin` with a strong,
  unique password. Home Assistant stores it in its config storage.
- Credentials, `Authorization` headers and card numbers are never logged.
  Personal data is never logged at INFO or above.
- Person name, employee number and card number appear in the sensor
  attributes and bus events, so Home Assistant's recorder stores them. Exclude
  `sensor.*_last_access_event` from the recorder if you don't want that
  history.

## Troubleshooting

Enable debug logging:

```yaml
logger:
  logs:
    custom_components.hikvision_acs: debug
```

Debug logs show stream connects and reconnects (exponential backoff with
jitter, capped at 60 s), replayed and duplicate events being ignored, and
events with unconfirmed codes.

## Development

Requirements: Python 3.14 and, for a local Home Assistant, Docker.

```bash
make setup   # create .venv and install test tooling
make test    # pytest with coverage
make lint    # ruff + mypy --strict
make ha      # local Home Assistant on http://localhost:8123
```

`make ha` mounts `custom_components/hikvision_acs` into the container.
`make ha-restart` picks up code changes, and `make ha-logs` follows the
integration's log lines.

Layout:

| Module | Role |
| --- | --- |
| `parser.py` | Pure: incremental multipart parser and JSON/XML payload decoding |
| `events.py` | Pure: major/minor → semantic event table, live/replay detection, dedup |
| `api.py` | ISAPI client (aiohttp, Digest auth): device info, alertStream, door control |
| `coordinator.py` | Stream lifecycle: reconnect with backoff, availability grace period, dispatch |
| `sensor.py`, `binary_sensor.py`, `button.py` | Entities |
| `config_flow.py` | UI setup and reauthentication |

`parser.py` and `events.py` don't import aiohttp or Home Assistant. They pass
`mypy --strict` and are tested against anonymised fixtures in `tests/fixtures/`
(see the README there for where each fixture came from).

## License

MIT
