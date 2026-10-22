"""The laptop's side of the reliable uplink, in isolation: acknowledgements, gaps, resend
requests, duplicates, gone numbers and C restarts, with a hand-driven clock."""

from iot_backend.protocol import DataPayload, Frame, TopologyReportPayload, encode
from iot_backend.transports import Heartbeat, ReadingEvent, UplinkLossEvent
from iot_backend.transports.laptop_gateway import LaptopGateway, UplinkConfig
from iot_backend.transports.wire import (
    AckDown,
    FrameUp,
    GoneUp,
    HeartbeatDown,
    HelloUp,
    ResendDown,
    StatusUp,
)

BOOT = 77
READING = encode(Frame(1, 3, 100, 1, 7, DataPayload(2000)))


class Clock:
    def __init__(self) -> None:
        self.ms = 0

    def now_ms(self) -> int:
        return self.ms

    def call_later(self, delay_ms, callback) -> None:  # not needed here
        raise AssertionError("no timers in these tests")


def make(**config):
    clock, down, events = Clock(), [], []
    gw = LaptopGateway(clock, down.append, UplinkConfig(**config))
    gw.subscribe(events.append)
    return clock, gw, down, events


def frame(n, boot=BOOT, seq=None, age=0):
    data = encode(Frame(1, 3, 100, n if seq is None else seq, 7, DataPayload(2000 + n)))
    return FrameUp(boot, n, age, age > 0, 2, data)


def status(last, low=None, boot=BOOT):
    return StatusUp(boot, last, low or last + 1, True, 0, 0, 50, 16, 0, 0, 0, 0, 0, 0)


def readings(events):
    return [e.seq for e in events if isinstance(e, ReadingEvent)]


def synced(**config):
    clock, gw, down, events = make(**config)
    gw.receive(HelloUp(BOOT, 3, 50, 16, "t"))
    return clock, gw, down, events


def test_in_order_frames_advance_the_acknowledgement_carried_by_heartbeats():
    clock, gw, down, events = synced()
    for n in range(1, 4):
        gw.receive(frame(n))
    assert gw.ack == 3 and readings(events) == [1, 2, 3] and down == []
    gw.send_heartbeat(Heartbeat(0))
    assert down == [HeartbeatDown(BOOT, 3)]


def test_an_acknowledgement_also_goes_after_every_few_frames():
    clock, gw, down, events = synced(ack_every=4)
    for n in range(1, 10):
        gw.receive(frame(n))
    assert down == [AckDown(BOOT, 4), AckDown(BOOT, 8)]


def test_a_gap_asks_once_from_the_first_missing_number_and_again_only_after_a_timeout():
    clock, gw, down, events = synced(resend_timeout_ms=1000)
    gw.receive(frame(1))
    gw.receive(frame(4))  # 2 and 3 are missing
    assert down == [ResendDown(BOOT, 2)] and gw.ack == 1
    clock.ms = 500
    gw.receive(frame(5))
    assert down == [ResendDown(BOOT, 2)]  # one request at a time
    clock.ms = 1_200  # the request (or its answer) was lost: ask again
    gw.receive(status(5))
    assert down == [ResendDown(BOOT, 2), ResendDown(BOOT, 2)]
    assert gw.stats.resend_requests == 2 and gw.stats.resend_repeats == 1
    gw.receive(frame(2, age=900))
    gw.receive(frame(3, age=900))
    assert gw.ack == 5 and readings(events) == [1, 4, 5, 2, 3]
    clock.ms = 5_000
    gw.receive(status(5))
    assert len(down) == 2  # nothing missing any more: no request


def test_the_tail_is_found_missing_from_the_status_even_when_nothing_newer_arrives():
    clock, gw, down, events = synced()
    gw.receive(frame(1))
    gw.receive(status(3))  # C has sent 2 and 3; they never arrived
    assert down == [ResendDown(BOOT, 2)]


def test_a_message_that_arrives_twice_is_counted_once():
    clock, gw, down, events = synced()
    for n in (1, 2, 2, 1, 3, 3):
        gw.receive(frame(n))
    assert readings(events) == [1, 2, 3]
    assert gw.stats.frames == 3 and gw.stats.duplicates == 3


def test_a_repeat_of_a_number_received_out_of_order_is_counted_once():
    """3 arrives before 2 (2 was lost and is resent later): a second copy of 3, from a resend,
    is above the acknowledgement point and must still be recognised."""
    clock, gw, down, events = synced()
    gw.receive(frame(1))
    gw.receive(frame(3))
    gw.receive(frame(3, age=500))  # the resend answer repeats 3 as well
    assert readings(events) == [1, 3] and gw.stats.duplicates == 1 and gw.ack == 1
    gw.receive(frame(2, age=500))
    gw.receive(frame(3, age=500))
    assert readings(events) == [1, 3, 2] and gw.ack == 3 and gw.stats.duplicates == 2


def test_gone_numbers_are_counted_by_kind_and_never_block_the_acknowledgement():
    clock, gw, down, events = synced()
    gw.receive(frame(1))
    gw.receive(frame(5))  # 2, 3, 4 missing
    gw.receive(GoneUp(BOOT, 2, 4, "RSM"))
    loss = [e for e in events if isinstance(e, UplinkLossEvent)]
    assert len(loss) == 1
    assert (loss[0].first, loss[0].last, loss[0].count, loss[0].readings) == (2, 4, 2, 1)
    assert gw.stats.superseded == 1  # a superseded report is not a loss
    assert gw.ack == 5


def test_a_gone_number_the_laptop_already_has_is_not_counted_as_lost():
    clock, gw, down, events = synced()
    for n in (1, 3):
        gw.receive(frame(n))
    gw.receive(GoneUp(BOOT, 2, 3, "RR"))  # C gave up 2 and 3, but 3 had arrived
    loss = [e for e in events if isinstance(e, UplinkLossEvent)]
    assert loss[0].count == 1 and loss[0].first == 2 and loss[0].last == 2


def test_a_restart_of_c_is_reported_with_what_was_known_to_be_missing():
    clock, gw, down, events = synced()
    gw.receive(frame(1))
    gw.receive(status(4))  # 2..4 are missing
    gw.receive(HelloUp(BOOT + 1, 3, 50, 16, "t"))  # C rebooted: its buffer is gone
    loss = [e for e in events if isinstance(e, UplinkLossEvent)]
    assert loss[-1].reason == "c_restarted" and loss[-1].count == 3
    assert gw.boot == BOOT + 1 and gw.ack == 0
    gw.receive(frame(1, boot=BOOT + 1, seq=900))
    assert readings(events)[-1] == 900  # numbering starts again; not mistaken for a repeat


def test_a_restart_noticed_from_a_frame_of_the_new_boot_is_handled_the_same():
    clock, gw, down, events = synced()
    gw.receive(frame(1))
    gw.receive(frame(1, boot=BOOT + 5, seq=901))
    assert gw.stats.c_restarts == 1 and readings(events) == [1, 901]


def test_frames_before_the_boot_is_known_are_delivered_and_remembered():
    clock, gw, down, events = make()
    gw.receive(frame(5))  # hello and status were missed
    assert readings(events) == [5]
    gw.receive(status(6, low=5))  # now synchronised: 5 is had, 6 is missing
    assert gw.ack == 5 and down == [ResendDown(BOOT, 6)]
    gw.receive(frame(5))
    assert readings(events) == [5]  # not counted twice


def test_a_fresh_laptop_starts_from_what_c_still_holds():
    """A restarted backend: numbers below C's lowest held one went to the previous process."""
    clock, gw, down, events = make()
    gw.receive(status(40, low=37))
    assert gw.ack == 36 and down == [ResendDown(BOOT, 37)]
    assert not [e for e in events if isinstance(e, UplinkLossEvent)]


def test_memory_is_bounded_whatever_numbers_arrive():
    clock, gw, down, events = synced(max_out_of_order=32)
    gw.receive(frame(1))
    for n in range(3, 2_000):  # 2 never arrives
        gw.receive(frame(n))
    assert len(gw._above) <= 32 and gw.stats.out_of_window > 0


def test_a_report_frame_becomes_a_topology_event():
    clock, gw, down, events = synced()
    data = encode(Frame(2, 3, 9, 4, 8, TopologyReportPayload(())))
    gw.receive(FrameUp(BOOT, 1, 1500, True, 2, data))
    clock.ms = 10_000
    gw.receive(FrameUp(BOOT, 2, 0, False, 2, READING, False))
    report = events[0]
    assert type(report).__name__ == "TopologyReportEvent" and report.replayed
    assert report.received_ms == -1500  # now (0) minus C's age: never a clock from C
