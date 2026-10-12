"""Protocol tests: shared reference examples, round trips and boundaries."""

import json
import random
from pathlib import Path

import pytest

from iot_backend.protocol import (
    HEADER_SIZE,
    MAX_FRAME_SIZE,
    MAX_PAYLOAD_SIZE,
    DataPayload,
    DecodeError,
    DeliveryAckPayload,
    EncodeError,
    FailoverReason,
    Frame,
    HelloNeighbor,
    HelloPayload,
    LocalFailoverReportPayload,
    MessageType,
    NeighborCode,
    ProbePayload,
    ProbeReplyPayload,
    RejectReason,
    ReportEntry,
    RouteAckStatus,
    RouteInstallAckPayload,
    RouteUpdatePayload,
    TelemetryPayload,
    TopologyReportPayload,
    decode,
    encode,
)
from iot_backend.protocol import codec as codec_module

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "tests" / "protocol-examples"
EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("*.json"))


def frame_from_fields(fields: dict) -> Frame:
    assert fields["version"] == 1
    payload = fields["payload"]
    if fields["type"] == "DATA":
        body = DataPayload(payload["value"])
    elif fields["type"] == "DELIVERY_ACK":
        body = DeliveryAckPayload(payload["origin"], payload["session"], payload["seq"])
    elif fields["type"] == "HELLO":
        body = HelloPayload(
            tuple(HelloNeighbor(n["node"], n["session"]) for n in payload["neighbors"])
        )
    elif fields["type"] == "TOPOLOGY_REPORT":
        body = TopologyReportPayload(
            tuple(
                ReportEntry(
                    e["neighbor"],
                    NeighborCode[e["state"]],
                    e["session"],
                    e["age_ms"],
                    e.get("rtt_ms"),
                )
                for e in payload["entries"]
            )
        )
    elif fields["type"] == "TELEMETRY":
        body = TelemetryPayload(**payload)
    elif fields["type"] == "PROBE":
        body = ProbePayload()
    elif fields["type"] == "PROBE_REPLY":
        body = ProbeReplyPayload(payload["probe_session"], payload["probe_seq"])
    elif fields["type"] == "ROUTE_UPDATE":
        body = RouteUpdatePayload(
            payload["epoch"],
            payload["version"],
            payload["destination"],
            payload["next_hop"],
            payload["backup_next_hop"],
        )
    elif fields["type"] == "ROUTE_INSTALL_ACK":
        body = RouteInstallAckPayload(
            payload["epoch"],
            payload["version"],
            payload["destination"],
            RouteAckStatus[payload["status"]],
        )
    elif fields["type"] == "LOCAL_FAILOVER_REPORT":
        body = LocalFailoverReportPayload(
            payload["epoch"],
            payload["version"],
            payload["destination"],
            payload["from_next_hop"],
            payload["to_next_hop"],
            FailoverReason[payload["reason"]],
        )
    else:
        raise AssertionError(f"unexpected example type {fields['type']}")
    return Frame(
        src=fields["src"],
        dst=fields["dst"],
        session=fields["session"],
        seq=fields["seq"],
        ttl=fields["ttl"],
        payload=body,
    )


def test_examples_exist():
    valid = [p for p in EXAMPLE_FILES if p.name.startswith("valid-")]
    invalid = [p for p in EXAMPLE_FILES if p.name.startswith("invalid-")]
    assert len(valid) >= 10
    assert len(invalid) >= 20
    # Every rejection reason the decoder can produce for a *defined* type is exercised by an
    # example. `reserved_type` needs a code that has no payload definition yet; since Phase 4a
    # defines the last reserved code there is none, and it is covered by a unit test below.
    reasons = {json.loads(p.read_text())["reject_reason"] for p in invalid}
    assert reasons == {r.value for r in RejectReason} - {RejectReason.RESERVED_TYPE.value}


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda p: p.stem)
def test_reference_example(path: Path):
    example = json.loads(path.read_text())
    raw = bytes.fromhex(example["hex"])
    if example["valid"]:
        frame = frame_from_fields(example["frame"])
        assert encode(frame) == raw
        assert decode(raw) == frame
    else:
        with pytest.raises(DecodeError) as caught:
            decode(raw)
        assert caught.value.reason.value == example["reject_reason"]


def test_header_layout_constants():
    assert HEADER_SIZE == 14
    assert MAX_FRAME_SIZE == 250
    assert MAX_PAYLOAD_SIZE == 236


def test_round_trip_random_frames():
    rng = random.Random(1234)
    for _ in range(200):
        data_frame = Frame(
            src=rng.randint(1, 254),
            dst=rng.randint(1, 254),
            session=rng.randint(1, 2**32 - 1),
            seq=rng.randint(0, 2**32 - 1),
            ttl=rng.randint(0, 255),
            payload=DataPayload(rng.randint(-(2**31), 2**31 - 1)),
        )
        ack_frame = Frame(
            src=rng.randint(1, 254),
            dst=rng.randint(1, 254),
            session=rng.randint(1, 2**32 - 1),
            seq=rng.randint(0, 2**32 - 1),
            ttl=rng.randint(0, 255),
            payload=DeliveryAckPayload(
                rng.randint(1, 254), rng.randint(1, 2**32 - 1), rng.randint(0, 2**32 - 1)
            ),
        )
        for frame in (data_frame, ack_frame):
            raw = encode(frame)
            assert len(raw) <= MAX_FRAME_SIZE
            assert decode(raw) == frame


def make_data(**overrides) -> Frame:
    fields = {"src": 1, "dst": 2, "session": 5, "seq": 1, "ttl": 8, "payload": DataPayload(0)}
    fields.update(overrides)
    return Frame(**fields)


@pytest.mark.parametrize(
    "overrides",
    [
        {"seq": -1},
        {"seq": 2**32},
        {"ttl": 256},
        {"ttl": -1},
        {"session": 0},
        {"session": 2**32},
        {"src": 0},
        {"dst": 255},
        {"src": 256},
        {"payload": DataPayload(2**31)},
        {"payload": DataPayload(-(2**31) - 1)},
        {"payload": DeliveryAckPayload(0, 1, 1)},
        {"payload": DeliveryAckPayload(1, 0, 1)},
    ],
)
def test_encode_rejects_out_of_range_fields(overrides):
    with pytest.raises(EncodeError):
        encode(make_data(**overrides))


def test_field_limits_are_accepted():
    frame = make_data(seq=2**32 - 1, ttl=255, session=2**32 - 1)
    assert decode(encode(frame)) == frame


def test_phase1_frames_are_far_below_the_radio_limit():
    assert len(encode(make_data())) == HEADER_SIZE + 4
    ack = Frame(2, 1, 1, 1, 8, DeliveryAckPayload(1, 1, 1))
    assert len(encode(ack)) == HEADER_SIZE + 9


def test_frame_at_limit_is_checked_by_type_not_just_size():
    # 250 bytes with a consistent length field is within the radio limit, but DATA carries 4.
    raw = bytes([1, MessageType.DATA, 1, 2, 5, 0, 0, 0, 1, 0, 0, 0, 8, 236]) + bytes(236)
    assert len(raw) == MAX_FRAME_SIZE
    with pytest.raises(DecodeError) as caught:
        decode(raw)
    assert caught.value.reason is RejectReason.BAD_PAYLOAD_LENGTH
    with pytest.raises(DecodeError) as caught:
        decode(raw + b"\x00")
    assert caught.value.reason is RejectReason.OVERSIZE


def test_every_message_type_is_now_defined_and_a_code_without_payload_stays_reserved(monkeypatch):
    """Phase 4a defined the last reserved codes. The `reserved_type` path still works for a
    future code that is added to MessageType before its payload is specified."""
    assert codec_module.IMPLEMENTED_TYPES == frozenset(MessageType)
    raw = bytes([1, 0x14, 1, 2, 5, 0, 0, 0, 1, 0, 0, 0, 1, 0])  # a PROBE
    assert decode(raw).msg_type is MessageType.PROBE
    monkeypatch.setattr(
        codec_module, "IMPLEMENTED_TYPES", frozenset(MessageType) - {MessageType.PROBE}
    )
    with pytest.raises(DecodeError) as caught:
        decode(raw)
    assert caught.value.reason is RejectReason.RESERVED_TYPE
    with pytest.raises(DecodeError) as caught:  # a code that is not in the enum at all
        decode(bytes([1, 0x18, 1, 2, 5, 0, 0, 0, 1, 0, 0, 0, 1, 0]))
    assert caught.value.reason is RejectReason.UNKNOWN_TYPE


def test_decoder_never_raises_anything_but_decode_error():
    rng = random.Random(99)
    for _ in range(500):
        raw = bytes(rng.randrange(256) for _ in range(rng.randint(0, 40)))
        try:
            decode(raw)
        except DecodeError:
            pass


# ---- Phase 3 payloads ----


def hello_frame(neighbors):
    return Frame(1, 2, 5, 9, 1, HelloPayload(tuple(neighbors)))


def report_frame(entries):
    return Frame(1, 3, 5, 9, 8, TopologyReportPayload(tuple(entries)))


def test_hello_and_report_sizes_are_bounded_and_small():
    empty = encode(hello_frame([]))
    assert len(empty) == HEADER_SIZE + 1
    full_hello = encode(hello_frame([HelloNeighbor(i, 1) for i in range(1, 9)]))
    assert len(full_hello) == HEADER_SIZE + 1 + 8 * 5
    full_report = encode(
        report_frame([ReportEntry(i, NeighborCode.LOST, 1, 0) for i in range(1, 9)])
    )
    assert len(full_report) == HEADER_SIZE + 1 + 8 * 8
    assert len(full_report) <= MAX_FRAME_SIZE


def test_hello_and_report_round_trip():
    hello = hello_frame([HelloNeighbor(2, 7), HelloNeighbor(4, 2**32 - 1)])
    assert decode(encode(hello)) == hello
    report = report_frame(
        [
            ReportEntry(2, NeighborCode.TWO_WAY, 11, 0),
            ReportEntry(4, NeighborCode.ONE_WAY, 12, 65535),
            ReportEntry(3, NeighborCode.LOST, 13, 3100),
        ]
    )
    assert decode(encode(report)) == report


@pytest.mark.parametrize(
    "frame",
    [
        hello_frame([HelloNeighbor(i, 1) for i in range(1, 10)]),  # 9 neighbors
        hello_frame([HelloNeighbor(0, 1)]),
        hello_frame([HelloNeighbor(2, 0)]),
        report_frame([ReportEntry(i, NeighborCode.LOST, 1, 0) for i in range(1, 10)]),
        report_frame([ReportEntry(2, 0, 1, 0)]),  # state 0
        report_frame([ReportEntry(2, 4, 1, 0)]),  # state 4
        report_frame([ReportEntry(2, NeighborCode.LOST, 1, 65536)]),  # age too large
        report_frame([ReportEntry(2, NeighborCode.LOST, 0, 0)]),
        report_frame([ReportEntry(255, NeighborCode.LOST, 1, 0)]),
    ],
)
def test_encode_rejects_bad_hello_and_report_fields(frame):
    with pytest.raises(EncodeError):
        encode(frame)


# ---- Phase 4a payloads ----


def test_phase4a_sizes_are_small_and_bounded():
    probe = Frame(1, 2, 5, 9, 1, ProbePayload())
    assert len(encode(probe)) == HEADER_SIZE
    reply = Frame(2, 1, 5, 9, 1, ProbeReplyPayload(5, 9))
    assert len(encode(reply)) == HEADER_SIZE + 8
    update = Frame(3, 1, 5, 9, 8, RouteUpdatePayload(1, 1, 3, 2, 4))
    assert len(encode(update)) == HEADER_SIZE + 11
    ack = Frame(1, 3, 5, 9, 8, RouteInstallAckPayload(1, 1, 3, RouteAckStatus.INSTALLED))
    assert len(encode(ack)) == HEADER_SIZE + 10
    failover = Frame(1, 3, 5, 9, 8, LocalFailoverReportPayload(1, 1, 3, 2, 4, 1))
    assert len(encode(failover)) == HEADER_SIZE + 12
    # the largest report: 8 entries, every one with a measured RTT
    full = Frame(
        1,
        3,
        5,
        9,
        8,
        TopologyReportPayload(
            tuple(ReportEntry(i, NeighborCode.TWO_WAY, 1, 0, 100 + i) for i in range(1, 9))
        ),
    )
    assert len(encode(full)) == HEADER_SIZE + 1 + 8 * 8 + 1 + 8 * 3 <= MAX_FRAME_SIZE
    assert decode(encode(full)) == full


def test_report_without_measurements_keeps_its_phase3_bytes():
    entries = (ReportEntry(2, NeighborCode.TWO_WAY, 7, 100),)
    assert encode(report_frame(entries)) == encode(
        report_frame((ReportEntry(2, NeighborCode.TWO_WAY, 7, 100, None),))
    )
    assert len(encode(report_frame(entries))) == HEADER_SIZE + 1 + 8  # no trailer at all


def test_unmeasured_is_never_encoded_as_a_number():
    with pytest.raises(EncodeError):
        encode(report_frame((ReportEntry(2, NeighborCode.TWO_WAY, 7, 100, 0xFFFF),)))
    with pytest.raises(EncodeError):
        encode(report_frame((ReportEntry(2, NeighborCode.TWO_WAY, 7, 100, -1),)))
    decoded = decode(encode(report_frame((ReportEntry(2, NeighborCode.TWO_WAY, 7, 100, 0),))))
    assert decoded.payload.entries[0].rtt_ms == 0  # a measured zero is still "measured"


@pytest.mark.parametrize(
    "payload",
    [
        RouteUpdatePayload(0, 1, 3, 2),  # epoch 0
        RouteUpdatePayload(1, 0, 3, 2),  # version 0
        RouteUpdatePayload(1, 1, 0, 2),  # destination 0
        RouteUpdatePayload(1, 1, 3, 255),
        RouteUpdatePayload(1, 1, 3, None, 4),  # backup without a primary
        RouteUpdatePayload(1, 1, 3, 2, 2),  # backup equals primary
        RouteInstallAckPayload(1, 1, 3, 0),
        RouteInstallAckPayload(1, 1, 3, 4),
        LocalFailoverReportPayload(1, 1, 3, 2, 2, 1),
        LocalFailoverReportPayload(1, 1, 3, 2, 4, 9),
        ProbeReplyPayload(0, 1),
    ],
)
def test_encode_rejects_bad_phase4a_fields(payload):
    with pytest.raises(EncodeError):
        encode(Frame(3, 1, 5, 9, 8, payload))


def test_telemetry_is_32_bytes_and_saturating_values_are_encoded_not_wrapped():
    frame = Frame(1, 3, 5, 9, 8, TelemetryPayload(created=2**32 - 1, failovers=0xFFFF))
    assert len(encode(frame)) == HEADER_SIZE + 32
    assert decode(encode(frame)) == frame
    for bad in (TelemetryPayload(created=2**32), TelemetryPayload(failovers=0x10000)):
        with pytest.raises(EncodeError):
            encode(Frame(1, 3, 5, 9, 8, bad))
