"""Constants for the Hikvision Access Control integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "hikvision_acs"
MANUFACTURER: Final = "Hikvision"

DEFAULT_PORT_HTTPS: Final = 443
DEFAULT_PORT_HTTP: Final = 80
DEFAULT_SSL: Final = True
# These devices ship self-signed certificates, so verification is opt-in.
DEFAULT_VERIFY_SSL: Final = False

# The reference hardware has a single relay; door numbering starts at 1.
DEFAULT_DOOR_NO: Final = 1

# How long the "access granted" binary sensor stays on after an event.
ACCESS_GRANTED_ON_TIME: Final = timedelta(seconds=5)

# Stream reconnect policy: exponential backoff with jitter.
RECONNECT_BASE_DELAY: Final = 1.0
RECONNECT_MAX_DELAY: Final = 60.0
# Only mark entities unavailable after the stream has been down this long,
# so a Wi-Fi hiccup does not flap every entity.
UNAVAILABLE_GRACE: Final = timedelta(seconds=60)
# A connection that survived this long resets the backoff counter.
STABLE_CONNECTION: Final = timedelta(seconds=30)

# Hikvision bus event fired for every live access-controller event.
EVENT_ACCESS: Final = f"{DOMAIN}_event"


def signal_event(entry_id: str) -> str:
    """Dispatcher signal carrying live access events for one config entry."""
    return f"{DOMAIN}_{entry_id}_event"


def signal_availability(entry_id: str) -> str:
    """Dispatcher signal fired when stream availability changes."""
    return f"{DOMAIN}_{entry_id}_availability"


def issue_id_stream_permission(entry_id: str) -> str:
    """Repair issue raised when the account may not read the event stream."""
    return f"stream_permission_{entry_id}"
