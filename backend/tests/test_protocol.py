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
    Frame,
    MessageType,
    RejectReason,
    decode,
    encode,
)

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "tests" / "protocol-examples"
EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("*.json"))


def frame_from_fields(fields: dict) -> Frame:
    assert fields["version"] == 1
    payload = fields["payload"]
    if fields["type"] == "DATA":
        body = DataPayload(payload["value"])
    elif fields["type"] == "DELIVERY_ACK":
        body = DeliveryAckPayload(payload["origin"], payload["session"], payload["seq"])
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
    assert len(valid) >= 4
    assert len(invalid) >= 8
    # every rejection reason the decoder can produce is exercised by at least one example
    reasons = {json.loads(p.read_text())["reject_reason"] for p in invalid}
    assert reasons == {r.value for r in RejectReason}


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


@pytest.mark.parametrize("type_code", [0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16])
def test_reserved_types_are_rejected(type_code):
    raw = bytes([1, type_code, 1, 2, 5, 0, 0, 0, 1, 0, 0, 0, 8, 0])
    with pytest.raises(DecodeError) as caught:
        decode(raw)
    assert caught.value.reason is RejectReason.RESERVED_TYPE


def test_decoder_never_raises_anything_but_decode_error():
    rng = random.Random(99)
    for _ in range(500):
        raw = bytes(rng.randrange(256) for _ in range(rng.randint(0, 40)))
        try:
            decode(raw)
        except DecodeError:
            pass
