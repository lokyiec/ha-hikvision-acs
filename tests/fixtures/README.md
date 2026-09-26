# Test fixtures

File names say where each payload came from:

| Prefix | Origin |
| --- | --- |
| *(none)* | Real capture from the reference device (DS-K1T805MBFWX, V1.9.1), anonymised |
| `derived_` | A real capture with specific fields edited; the edit is described below |
| `synthetic_` | Hand-written from the ISAPI document structure; not a capture |

Anonymisation: person names, card numbers and employee numbers are replaced
with placeholders, IP addresses use the `192.0.2.0/24` documentation range and
MAC addresses the `00:00:5e:00:53:xx` documentation range.

- `card_granted_replayed.json`: card read, `5/38`. Note that this capture is a
  backlog record (`currentEvent: false`), flushed by the device on connect.
- `security_users_capabilities_not_supported.xml`: the reference device's
  answer to `GET /ISAPI/Security/users/capabilities`. It is a real ISAPI error
  document, and it is the evidence that this model only has the `admin`
  account.
- `derived_card_granted_live.json`: the same event with `currentEvent: true`
  and the next serial number, standing in for a live read.

The multipart framing around these bodies is built by `tests/helpers.py`.
Replace synthetic fixtures with real captures as they become available.
