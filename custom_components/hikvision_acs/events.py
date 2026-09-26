"""Semantic mapping of Hikvision access-control events.

Pure module: no aiohttp, no Home Assistant imports.

Hikvision identifies events by a ``(majorEventType, subEventType)`` pair, and
the numbers differ between firmware versions. All knowledge about what a pair
means lives in :data:`EVENT_CODES`. Unknown pairs are never dropped: they come
back as :attr:`EventCategory.UNKNOWN` carrying the raw codes, so users can
report them and the table can grow from real captures.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

ACCESS_CONTROLLER_EVENT: Final = "AccessControllerEvent"


class EventCategory(StrEnum):
    """Coarse meaning of an event, used to route it to entities."""

    ACCESS_GRANTED = "access_granted"
    ACCESS_DENIED = "access_denied"
    REMOTE_ACCESS = "remote_access"
    DOOR = "door"
    SYSTEM = "system"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EventDefinition:
    """What a major/minor pair means.

    ``confirmed`` is ``True`` only when the meaning has been verified against
    a real capture. Unconfirmed entries are best guesses and say so.

    ``internal`` marks bookkeeping caused by event subscribers themselves,
    such as this integration connecting to the stream. Such events are
    logged at DEBUG only and never reach entities, the bus or Activity,
    where they would appear on every restart or Wi-Fi drop.
    """

    key: str
    description: str
    category: EventCategory
    confirmed: bool
    internal: bool = False


# Grow this table from captured samples only. Never promote an entry to
# confirmed=True without a real payload that proves the meaning.
#
# "Confirmed" entries on the reference device (DS-K1T805MBFWX, V1.9.1) were
# matched one-to-one, by timestamp and serial order, against the labels in the
# device's own web UI event log (Access Control → Event Search).
EVENT_CODES: Final[Mapping[tuple[int, int], EventDefinition]] = {
    (3, 112): EventDefinition(
        key="system_event_112",
        description="System/operation event (3/112, meaning unconfirmed)",
        category=EventCategory.SYSTEM,
        confirmed=False,
    ),
    (3, 121): EventDefinition(
        key="remote_arming",
        description="Remote alarm arming (event subscriber connected)",
        category=EventCategory.SYSTEM,
        confirmed=True,
        internal=True,
    ),
    (3, 122): EventDefinition(
        key="remote_disarming",
        description="Remote alarm disarming (event subscriber disconnected)",
        category=EventCategory.SYSTEM,
        confirmed=True,
        internal=True,
    ),
    (3, 1024): EventDefinition(
        key="remote_unlock",
        description="Remote unlock",
        category=EventCategory.REMOTE_ACCESS,
        confirmed=True,
    ),
    (3, 1029): EventDefinition(
        key="system_event_1029",
        description="System/operation event (3/1029, meaning unconfirmed)",
        category=EventCategory.SYSTEM,
        confirmed=False,
    ),
    (5, 1): EventDefinition(
        key="card_access_granted",
        description="Authenticated via card",
        category=EventCategory.ACCESS_GRANTED,
        confirmed=True,
    ),
    (5, 21): EventDefinition(
        key="door_unlocked",
        description="Door lock opened",
        category=EventCategory.DOOR,
        confirmed=True,
    ),
    (5, 22): EventDefinition(
        key="door_locked",
        description="Door lock closed",
        category=EventCategory.DOOR,
        confirmed=True,
    ),
    (5, 38): EventDefinition(
        key="card_access_granted",
        description="Authenticated via card",
        category=EventCategory.ACCESS_GRANTED,
        # Seen in older records on the same device, with card identity.
        confirmed=True,
    ),
    (5, 181): EventDefinition(
        key="pin_access_granted",
        description="Authenticated via PIN",
        category=EventCategory.ACCESS_GRANTED,
        confirmed=True,
    ),
}


# Hikvision's major types are stable across firmware (SDK: 1 alarm,
# 2 exception, 3 operation, 5 event), so an unknown minor can still be given
# a coarse category from its major alone.
MAJOR_FALLBACK_CATEGORIES: Final[Mapping[int, EventCategory]] = {
    3: EventCategory.SYSTEM,
}


def describe(major: int, minor: int) -> EventDefinition:
    """Look up a code pair, degrading to a fallback definition.

    Unknown pairs keep their raw codes in the key and description; the
    category comes from :data:`MAJOR_FALLBACK_CATEGORIES` or is ``UNKNOWN``.
    """
    known = EVENT_CODES.get((major, minor))
    if known is not None:
        return known
    return EventDefinition(
        key=f"unknown_{major}_{minor}",
        description=f"Unknown event (major {major}, minor {minor})",
        category=MAJOR_FALLBACK_CATEGORIES.get(major, EventCategory.UNKNOWN),
        confirmed=False,
    )


# Categories that mean "someone was let in, or tried to be": they feed the
# access history. Door relay and system events are bookkeeping, not entries.
ACCESS_CATEGORIES: Final = frozenset(
    {
        EventCategory.ACCESS_GRANTED,
        EventCategory.ACCESS_DENIED,
        EventCategory.REMOTE_ACCESS,
        EventCategory.UNKNOWN,
    }
)

UNKNOWN_ACCESS_TYPE: Final = "unknown"

# Every event type the access history can report: one per known access code,
# plus a catch-all for codes not in the table yet.
ACCESS_EVENT_TYPES: Final[tuple[str, ...]] = (
    *sorted(
        {
            definition.key
            for definition in EVENT_CODES.values()
            if definition.category in ACCESS_CATEGORIES
        }
    ),
    UNKNOWN_ACCESS_TYPE,
)


@dataclass(frozen=True, slots=True)
class AccessEvent:
    """A decoded ``AccessControllerEvent``.

    ``live`` is the single most important field: the device replays buffered
    history on every connect with ``currentEvent: false``, and only live
    events may drive automations.

    ``device_time`` is the device's own timestamp, passed through verbatim.
    Device clocks are frequently wrong, so it is informational only.
    """

    major: int
    minor: int
    definition: EventDefinition
    live: bool
    serial_no: int | None = None
    front_serial_no: int | None = None
    device_time: str | None = None
    person_name: str | None = None
    employee_no: str | None = None
    card_no: str | None = None
    verify_mode: str | None = None
    card_reader_no: int | None = None
    door_no: int | None = None

    @property
    def category(self) -> EventCategory:
        """Shortcut for ``definition.category``."""
        return self.definition.category

    @property
    def key(self) -> str:
        """Shortcut for ``definition.key``."""
        return self.definition.key


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def parse_access_event(payload: Mapping[str, Any]) -> AccessEvent | None:
    """Turn a decoded alertStream JSON payload into an :class:`AccessEvent`.

    Returns ``None`` for anything that is not an access-controller event
    (heartbeats, video-loss notifications, other event types) or that lacks
    the major/minor codes needed to identify it.
    """
    if payload.get("eventType") != ACCESS_CONTROLLER_EVENT:
        return None
    body = payload.get(ACCESS_CONTROLLER_EVENT)
    if not isinstance(body, Mapping):
        return None

    major = _as_int(body.get("majorEventType"))
    minor = _as_int(body.get("subEventType"))
    if major is None or minor is None:
        return None

    return AccessEvent(
        major=major,
        minor=minor,
        definition=describe(major, minor),
        # Strict on purpose: only an explicit ``true`` counts as live. A
        # replayed record wrongly treated as live could open a door.
        live=body.get("currentEvent") is True,
        serial_no=_as_int(body.get("serialNo")),
        front_serial_no=_as_int(body.get("frontSerialNo")),
        device_time=_as_str(payload.get("dateTime")),
        person_name=_as_str(body.get("name")),
        employee_no=_as_str(body.get("employeeNoString")) or _as_str(body.get("employeeNo")),
        card_no=_as_str(body.get("cardNo")),
        verify_mode=_as_str(body.get("currentVerifyMode")),
        card_reader_no=_as_int(body.get("cardReaderNo")),
        door_no=_as_int(body.get("doorNo")),
    )


EVENT_ATTRIBUTE_KEYS: Final = (
    "event",
    "description",
    "category",
    "confirmed",
    "major",
    "minor",
    "serial_no",
    "device_time",
    "person_name",
    "employee_no",
    "card_no",
    "verify_mode",
    "card_reader_no",
    "door_no",
)


def access_event_type(event: AccessEvent) -> str:
    """Return the access-history type: the known key, or ``"unknown"``."""
    return event.key if event.key in ACCESS_EVENT_TYPES else UNKNOWN_ACCESS_TYPE


def event_attributes(event: AccessEvent) -> dict[str, Any]:
    """Flat, JSON-serialisable view of an event for attributes and bus data."""
    return {
        "event": event.key,
        "description": event.definition.description,
        "category": event.category.value,
        "confirmed": event.definition.confirmed,
        "major": event.major,
        "minor": event.minor,
        "serial_no": event.serial_no,
        "device_time": event.device_time,
        "person_name": event.person_name,
        "employee_no": event.employee_no,
        "card_no": event.card_no,
        "verify_mode": event.verify_mode,
        "card_reader_no": event.card_reader_no,
        "door_no": event.door_no,
    }


@dataclass(slots=True)
class SerialDeduplicator:
    """Drops events whose ``serialNo`` was seen recently.

    Serial numbers are monotonic per device but reset on factory reset, so
    this remembers a bounded window of recent serials instead of rejecting
    everything below a high-water mark. Events without a serial always pass.
    """

    window: int = 256
    _seen: set[int] = field(default_factory=set, init=False, repr=False)
    _order: deque[int] = field(default_factory=deque, init=False, repr=False)

    def is_duplicate(self, event: AccessEvent) -> bool:
        """Return ``True`` if seen before; otherwise remember it."""
        serial = event.serial_no
        if serial is None:
            return False
        if serial in self._seen:
            return True
        self._seen.add(serial)
        self._order.append(serial)
        if len(self._order) > self.window:
            self._seen.discard(self._order.popleft())
        return False
