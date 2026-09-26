"""Tests for the pure event mapping."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.hikvision_acs.events import (
    ACCESS_EVENT_TYPES,
    EVENT_ATTRIBUTE_KEYS,
    EVENT_CODES,
    AccessEvent,
    EventCategory,
    SerialDeduplicator,
    access_event_type,
    describe,
    event_attributes,
    parse_access_event,
)

from .helpers import access_payload, load_json


def _parse(payload: dict[str, Any]) -> AccessEvent:
    event = parse_access_event(payload)
    assert event is not None
    return event


def test_real_capture_is_a_replayed_card_grant() -> None:
    event = _parse(load_json("card_granted_replayed.json"))
    assert (event.major, event.minor) == (5, 38)
    assert event.category is EventCategory.ACCESS_GRANTED
    assert event.definition.confirmed
    assert event.live is False
    assert event.serial_no == 5606
    assert event.front_serial_no == 5605
    assert event.person_name == "Jane Doe"
    assert event.employee_no == "00000001"
    assert event.card_no == "0000000001"
    assert event.verify_mode == "cardOrFpOrPw"
    assert event.card_reader_no == 1
    assert event.device_time == "2026-07-12T12:57:33+01:00"
    assert event.door_no is None


def test_live_capture() -> None:
    event = _parse(load_json("derived_card_granted_live.json"))
    assert event.live is True
    assert event.key == "card_access_granted"


@pytest.mark.parametrize("value", [None, "true", 1, "yes"])
def test_only_explicit_true_is_live(value: object) -> None:
    payload = access_payload()
    body = dict(payload["AccessControllerEvent"])
    if value is None:
        body.pop("currentEvent")
    else:
        body["currentEvent"] = value
    assert _parse({**payload, "AccessControllerEvent": body}).live is False


@pytest.mark.parametrize(
    "payload",
    [
        {"eventType": "videoloss", "eventState": "inactive"},
        {"eventType": "AccessControllerEvent"},
        {"eventType": "AccessControllerEvent", "AccessControllerEvent": "nope"},
        {"eventType": "AccessControllerEvent", "AccessControllerEvent": {"subEventType": 1}},
        {"eventType": "AccessControllerEvent", "AccessControllerEvent": {"majorEventType": 5}},
        access_payload(majorEventType=True),
        access_payload(subEventType=[1]),
        access_payload(subEventType="abc"),
    ],
)
def test_non_access_or_unidentifiable_payloads(payload: dict[str, Any]) -> None:
    assert parse_access_event(payload) is None


def test_string_codes_are_accepted() -> None:
    event = _parse(access_payload(majorEventType=" 5 ", subEventType="38"))
    assert (event.major, event.minor) == (5, 38)


def test_unknown_code_degrades_gracefully() -> None:
    event = _parse(access_payload(5, 9999))
    assert event.category is EventCategory.UNKNOWN
    assert event.key == "unknown_5_9999"
    assert "major 5, minor 9999" in event.definition.description
    assert not event.definition.confirmed
    # Identity is still passed through for unknown codes.
    assert event.person_name == "Jane Doe"


@pytest.mark.parametrize(
    ("code", "key", "category"),
    [
        ((5, 1), "card_access_granted", EventCategory.ACCESS_GRANTED),
        ((5, 38), "card_access_granted", EventCategory.ACCESS_GRANTED),
        ((5, 181), "pin_access_granted", EventCategory.ACCESS_GRANTED),
        ((3, 1024), "remote_unlock", EventCategory.REMOTE_ACCESS),
        ((5, 21), "door_unlocked", EventCategory.DOOR),
        ((5, 22), "door_locked", EventCategory.DOOR),
        ((3, 121), "remote_arming", EventCategory.SYSTEM),
        ((3, 122), "remote_disarming", EventCategory.SYSTEM),
    ],
)
def test_codes_confirmed_against_device_log(
    code: tuple[int, int], key: str, category: EventCategory
) -> None:
    definition = describe(*code)
    assert (definition.key, definition.category, definition.confirmed) == (key, category, True)


def test_only_subscriber_bookkeeping_is_internal() -> None:
    internal = {code for code, definition in EVENT_CODES.items() if definition.internal}
    assert internal == {(3, 121), (3, 122)}
    assert not describe(3, 999).internal


def test_both_card_codes_are_confirmed_grants() -> None:
    # The reference device reports card reads as 5/1 and (in older records) 5/38.
    for code in ((5, 1), (5, 38)):
        definition = describe(*code)
        assert definition.category is EventCategory.ACCESS_GRANTED
        assert definition.confirmed


def test_only_captured_codes_are_confirmed() -> None:
    confirmed = {code for code, definition in EVENT_CODES.items() if definition.confirmed}
    unconfirmed = {code for code, definition in EVENT_CODES.items() if not definition.confirmed}
    assert unconfirmed == {(3, 112), (3, 1029)}
    assert confirmed.isdisjoint(unconfirmed)


def test_unknown_operation_codes_fall_back_to_system() -> None:
    # Major 3 is "operation" in Hikvision's scheme (remote logins, remote opens).
    definition = describe(3, 999)
    assert definition.category is EventCategory.SYSTEM
    assert definition.key == "unknown_3_999"
    assert not definition.confirmed


def test_field_fallbacks() -> None:
    event = _parse(
        access_payload(
            employeeNoString="",
            employeeNo=42,
            name="   ",
            cardNo=None,
            doorNo="1",
            cardReaderNo="x",
        )
    )
    assert event.employee_no == "42"
    assert event.person_name is None
    assert event.card_no is None
    assert event.door_no == 1
    assert event.card_reader_no is None


def test_event_attributes_match_declared_keys() -> None:
    attributes = event_attributes(_parse(access_payload()))
    assert tuple(attributes) == EVENT_ATTRIBUTE_KEYS
    assert attributes["event"] == "card_access_granted"
    assert attributes["category"] == "access_granted"


def test_access_event_types_cover_access_codes_plus_unknown() -> None:
    assert ACCESS_EVENT_TYPES == (
        "card_access_granted",
        "pin_access_granted",
        "remote_unlock",
        "unknown",
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    [((5, 181), "pin_access_granted"), ((5, 777), "unknown"), ((3, 1024), "remote_unlock")],
)
def test_access_event_type(code: tuple[int, int], expected: str) -> None:
    assert access_event_type(_parse(access_payload(*code))) == expected


class TestSerialDeduplicator:
    def test_drops_repeats(self) -> None:
        dedup = SerialDeduplicator()
        event = _parse(access_payload(serial=1))
        assert not dedup.is_duplicate(event)
        assert dedup.is_duplicate(event)

    def test_events_without_serial_always_pass(self) -> None:
        dedup = SerialDeduplicator()
        event = _parse(access_payload(serial=None))
        assert not dedup.is_duplicate(event)
        assert not dedup.is_duplicate(event)

    def test_window_is_bounded(self) -> None:
        dedup = SerialDeduplicator(window=2)
        first, second, third = (_parse(access_payload(serial=n)) for n in (1, 2, 3))
        for event in (first, second, third):
            assert not dedup.is_duplicate(event)
        # Serial 1 fell out of the window, e.g. after a device counter reset.
        assert not dedup.is_duplicate(first)
        assert dedup.is_duplicate(third)
