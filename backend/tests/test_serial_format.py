"""The serial line format: the shared examples, the checksum, the limits, and the splitter."""

import binascii
import json
import random
from pathlib import Path

import pytest

from iot_backend.protocol import MessageType
from iot_backend.transports.serial_format import (
    MAX_DOWN_LINE,
    MAX_TX_PAYLOAD,
    MAX_UP_LINE,
    REJECT_REASONS,
    LineSplitter,
    crc16,
    decode_down,
    decode_up,
    encode,
    log_line,
)
from iot_backend.transports.wire import (
    AckDown,
    FrameUp,
    GoneUp,
    HeartbeatDown,
    HelloUp,
    ReplayEndUp,
    ReplayStartUp,
    ResendDown,
    StatusUp,
    TransmitDown,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "tests" / "serial-examples"
CASES = sorted(EXAMPLES.glob("*.json"))
U32 = 0xFFFF_FFFF


def load(path: Path) -> tuple[dict, bytes]:
    case = json.loads(path.read_text(encoding="utf-8"))
    raw = bytes.fromhex(case["line_hex"]) if "line_hex" in case else case["line"].encode("ascii")
    return case, raw


def test_there_are_examples_of_every_line_kind_and_every_reason():
    cases = [load(p)[0] for p in CASES]
    assert len(cases) >= 40
    assert {c["result"] for c in cases} == {"message", "log", "noise", "rejected"}
    reasons = {c["reason"] for c in cases if c["result"] == "rejected"}
    # "truncated" is decided by the splitter when the port closes, not by one line
    assert reasons == set(REJECT_REASONS) - {"truncated"}
    for name in ("fr", "st", "rps", "rpe", "gone", "hello", "hb", "ack", "rs", "tx"):
        assert any(c.get("fields", {}).get("t") == name for c in cases), name


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_each_example_parses_exactly_as_stated(path):
    case, raw = load(path)
    parsed = (decode_up if case["direction"] == "up" else decode_down)(raw)
    assert parsed.kind == case["result"], parsed
    if case["result"] == "rejected":
        assert parsed.reason == case["reason"], parsed
        assert parsed.message is None  # nothing from a refused line is ever used
    if case["result"] == "message":
        # the laptop writes exactly this line for the same message (one canonical form)
        if path.stem != "valid-up-checksum-lowercase":
            assert encode(parsed.message) == raw + b"\n"
        assert json.loads(raw[: raw.rindex(b"*")]) == case["fields"]
    assert path.stem.split("-")[0] in ("valid", "invalid", "log", "noise")


def test_the_checksum_is_crc16_ccitt_false():
    assert crc16(b"123456789") == 0x29B1  # the standard check value
    rng = random.Random(1)
    for _ in range(200):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 300)))
        assert crc16(data) == binascii.crc_hqx(data, 0xFFFF)  # an independent implementation


def test_every_single_changed_or_dropped_byte_of_a_line_is_caught():
    line = encode(
        FrameUp(7, 12, 0, False, 2, bytes.fromhex("01010103457c32632d0000000704dd070000"))
    )
    body = line.rstrip(b"\n")
    for i in range(len(body)):
        dropped = body[:i] + body[i + 1 :]
        assert decode_up(dropped).kind != "message"
        for bit in (0x01, 0x10):
            flipped = body[:i] + bytes([body[i] ^ bit]) + body[i + 1 :]
            assert decode_up(flipped).kind != "message", (i, flipped)


def test_the_longest_lines_fit_the_limits():
    big = bytes([1, MessageType.DATA]) + bytes(248)  # a 250-byte frame (ESP-NOW maximum)
    longest_up = encode(FrameUp(U32, U32, U32, True, 254, big, True))
    assert len(longest_up) <= MAX_UP_LINE
    longest_status = encode(StatusUp(U32, U32, U32, True, *([U32] * 11)))
    assert len(longest_status) <= MAX_UP_LINE
    for message in (
        HeartbeatDown(U32, U32),
        AckDown(U32, U32),
        ResendDown(U32, U32),
        TransmitDown(254, 255, bytes(MAX_TX_PAYLOAD)),
    ):
        assert len(encode(message)) <= MAX_DOWN_LINE, message
    assert MAX_DOWN_LINE <= 128  # under the ESP8266's 128-byte UART FIFO, half its 256 RX buffer


def test_every_message_survives_a_round_trip():
    messages = [
        FrameUp(5, 1, 0, False, 2, bytes.fromhex("01010103457c32632d0000000704dd070000"), True),
        StatusUp(5, 3, 2, True, 1, 2, 50, 16, 0, 0, 0, 4, 1, 0, 0),
        ReplayStartUp(5, 2, 2, 0, 0, 0, 1, 900),
        ReplayEndUp(5, 2, 0, 0, 0, 1),
        GoneUp(5, 1, 3, "RS?"),
        HelloUp(5, 3, 50, 16, "0.1.0"),
    ]
    for message in messages:
        assert decode_up(encode(message).rstrip(b"\n")).message == message
    for message in (
        HeartbeatDown(0, 0),
        AckDown(5, 3),
        ResendDown(5, 2),
        TransmitDown(2, 18, b"\x01\x02"),
    ):
        assert decode_down(encode(message).rstrip(b"\n")).message == message


def test_a_log_line_is_never_parsed_even_if_it_contains_a_valid_data_line():
    data = encode(HeartbeatDown(0, 0)).rstrip(b"\n")
    parsed = decode_up(b"# " + data)
    assert parsed.kind == "log" and parsed.message is None
    assert decode_up(log_line("boot \x01 done").rstrip(b"\n")).kind == "log"


def test_the_splitter_gives_the_same_lines_however_the_bytes_are_chunked():
    stream = b"".join(encode(ReplayEndUp(5, n, 0, 0, 0, 0)) for n in range(1, 30)) + b"# a log\n"
    expected = LineSplitter(MAX_UP_LINE).feed(stream)
    rng = random.Random(3)
    for _ in range(50):
        splitter, got, i = LineSplitter(MAX_UP_LINE), [], 0
        while i < len(stream):
            step = rng.randrange(1, 40)
            got += splitter.feed(stream[i : i + step])
            i += step
        assert got == expected


def test_the_splitter_drops_an_over_long_line_and_resynchronises_with_bounded_memory():
    splitter = LineSplitter(64)
    out = splitter.feed(b"x" * 1000)  # a burst with no newline at all
    assert out == [None] and len(splitter._buffer) == 0
    out = splitter.feed(b"y" * 1000 + b"\n" + b"# fine\n")
    assert out == [b"# fine"]  # the rest of the long line is skipped up to its newline
    assert splitter.feed(b"partial") == []
    assert splitter.flush() == b"partial"  # the port closed mid-line
    assert splitter.flush() is None
