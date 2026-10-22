"""The messages that cross the link between node C and the laptop (never the radio).

These are the *wire-level* messages: what C's firmware sends and receives on its USB serial
line (docs/SERIAL_PROTOCOL.md gives their exact text). They are lower-level than the typed
gateway events the controller sees: C hands radio frames up as raw bytes with the link-layer
facts only C knows, and the laptop decodes them with the protocol codec.

Uplink (C to laptop)
* `FrameUp` - one radio frame C received and the controller should see. Numbered: `n` counts
  C's numbered messages in this boot of C (`boot` is C's own session id), so the laptop can
  notice a missing one and ask for it again.
* `StatusUp` - C's answer to a heartbeat (buffer, outage and numbering state). Not numbered:
  a lost one is replaced by the next.
* `ReplayStartUp`, `ReplayEndUp` - markers around a replay after an outage. Not numbered.
* `GoneUp` - "the numbers you asked for are no longer held": counted as lost by the laptop.
* `HelloUp` - C has just booted (also not numbered).

Downlink (laptop to C)
* `HeartbeatDown` - the controller is alive, plus the cumulative acknowledgement.
* `AckDown` - the cumulative acknowledgement alone (does not count as a heartbeat).
* `ResendDown` - please resend your numbered messages from `from_n`.
* `TransmitDown` - originate a management frame: C builds the header, the laptop gives the
  type, destination and payload.

Every count and number is an unsigned 32-bit value. `boot` 0 means "not known".
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FrameUp:
    boot: int  # C's session id: numbering restarts at 1 when this changes
    n: int  # this message's number in that boot (1, 2, 3, ...)
    age_ms: int  # how long C held the frame before this line, on C's own clock
    replayed: bool  # true if this is not the frame's first, immediate hand-over
    via: int  # the link-layer sender: the neighbor whose radio handed C the frame
    frame: bytes  # the frame exactly as received (header and payload)
    duplicate: bool = False  # DATA only: C's duplicate filter had already seen it


@dataclass(frozen=True)
class StatusUp:
    boot: int
    last: int  # highest number C has assigned in this boot (0 = none yet)
    low: int  # lowest number C still holds; last + 1 if it holds none
    controller_reachable: bool
    held: int  # entries waiting to be sent for the first time
    unacked: int  # numbered entries sent but not yet acknowledged
    capacity: int
    window: int  # most numbered entries C keeps unacknowledged
    dropped_overflow: int  # cumulative since C booted: waiting entries given up, any kind
    dropped_readings: int  # of those, readings (cumulative): data lost at C
    coalesced: int
    replayed: int  # entries sent late (held first)
    retransmitted: int  # entries sent again because the laptop asked
    outages: int


@dataclass(frozen=True)
class ReplayStartUp:
    boot: int
    entries: int  # entries waiting to be sent when the controller came back
    held: int  # of those, stored while the controller was unreachable
    dropped_overflow: int  # this outage, any kind
    dropped_readings: int  # this outage, of those the readings
    coalesced: int  # this outage
    outage_ms: int  # C's own measure of how long the controller was away


@dataclass(frozen=True)
class ReplayEndUp:
    boot: int
    replayed: int
    dropped_overflow: int
    dropped_readings: int
    coalesced: int


@dataclass(frozen=True)
class GoneUp:
    boot: int
    first: int  # numbers first..last were asked for and are no longer held
    last: int
    # One letter per number from first to last: "R" a reading (DATA), "M" another management
    # frame, "S" a report, acknowledgement or telemetry superseded by a newer one from the same
    # node (nothing is lost), "?" C no longer knows (freed by an acknowledgement). At most 64.
    kinds: str = ""


@dataclass(frozen=True)
class HelloUp:
    boot: int
    node: int  # C's node id
    capacity: int
    window: int
    firmware: str  # short version text, for logs


@dataclass(frozen=True)
class HeartbeatDown:
    boot: int  # the boot the acknowledgement refers to (0 = none known: C ignores `ack`)
    ack: int  # every number up to and including this one has been received (or is gone)


@dataclass(frozen=True)
class AckDown:
    boot: int
    ack: int


@dataclass(frozen=True)
class ResendDown:
    boot: int
    from_n: int


@dataclass(frozen=True)
class TransmitDown:
    dst: int  # the node the frame is for
    msg_type: int  # protocol message type (only ROUTE_UPDATE, 0x12, is accepted by C today)
    payload: bytes


UpMessage = FrameUp | StatusUp | ReplayStartUp | ReplayEndUp | GoneUp | HelloUp
DownMessage = HeartbeatDown | AckDown | ResendDown | TransmitDown
