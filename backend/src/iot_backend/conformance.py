"""Shared conformance cases for node C's bridge: the Python reference and the firmware must agree.

    uv run python -m iot_backend.conformance           # (re)write the cases and the C header
    uv run python -m iot_backend.conformance --check   # exit 1 if either is out of date

The bridge is the code that decides what survives a controller outage (a bounded buffer with
coalescing, eviction by value, and honest counts). It exists twice: `simulator/bridge.py` (the
reference, used by every simulated outage) and `firmware/src/c_serial_bridge.cpp` (what runs on
the board). They drifted apart once, and a real outage lost readings the simulator would have
kept. Each case in `tests/bridge-conformance/` is one scripted input and the exact text the bridge
must emit, and BOTH implementations are tested against it (`backend/tests/test_bridge_conformance.py`
and the firmware's native tests, through the generated header).

How a case is run, identically on both sides
---------------------------------------------
* Time is in milliseconds, all multiples of 20. The bridge is ticked every 20 ms, so it sends at
  most one buffered message per 20 ms (the simulator's replay spacing).
* Steps happen at their `t`, in order, after the clock has been advanced to `t`:
  `radio` (a frame reaches C, from a neighbor, with C's duplicate verdict), `hb` (the laptop's
  heartbeat), `ack` (a stand-alone acknowledgement) and `rs` (a resend request). For `hb` and
  `ack`, `ack` is `"none"` (repeat the last acknowledgement), `"all"` (everything C has handed up
  so far) or a number.
* After the last step the clock runs to `end_ms`.
* Everything the bridge writes is reduced to one canonical line, ages left out (they depend only
  on the clock), and compared line by line, then the final counters are compared too.

The expected lines were produced by the Python reference and reviewed by hand against the rules
in docs/PROTOCOL.md ("Laptop and C"); the named rule tests in backend/tests/test_bridge.py and
the firmware tests state the same rules independently.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from iot_backend.protocol import (
    DataPayload,
    Frame,
    LocalFailoverReportPayload,
    MessageType,
    ReportEntry,
    RouteInstallAckPayload,
    TelemetryPayload,
    TopologyReportPayload,
    decode,
    encode,
)
from iot_backend.simulator.bridge import BridgeConfig, CBridge
from iot_backend.simulator.engine import Engine
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
)

ROOT = Path(__file__).resolve().parents[3]
CASES_DIR = ROOT / "tests" / "bridge-conformance"
HEADER = ROOT / "tests" / "generated" / "bridge_conformance.h"

TICK_MS = 20
A, B, C, D = 1, 2, 3, 4
BOOT = 0xC0FFEE
LF = chr(10)

OPS = {"radio": 0, "hb": 1, "ack": 2, "rs": 3}
ACK_NONE, ACK_ALL = -1, -2


# ---------------------------------------------------------------- canonical text


def canonical(message: Any) -> str | None:
    """One line per message C writes. Ages are left out; everything else is compared."""
    if isinstance(message, FrameUp):
        frame = decode(message.frame)
        line = (
            f"fr n={message.n} rp={int(message.replayed)} via={message.via} "
            f"type={frame.msg_type.name} src={frame.src} seq={frame.seq}"
        )
        if frame.msg_type is MessageType.DATA:
            line += f" dup={int(message.duplicate)}"
        return line
    if isinstance(message, StatusUp):
        return (
            f"st last={message.last} low={message.low} held={message.held} "
            f"unacked={message.unacked} cap={message.capacity} win={message.window} "
            f"drop={message.dropped_overflow} rdrop={message.dropped_readings} "
            f"coal={message.coalesced} rep={message.replayed} rtx={message.retransmitted} "
            f"out={message.outages}"
        )
    if isinstance(message, ReplayStartUp):
        return (
            f"rps ent={message.entries} held={message.held} drop={message.dropped_overflow} "
            f"rdrop={message.dropped_readings} coal={message.coalesced} out_ms={message.outage_ms}"
        )
    if isinstance(message, ReplayEndUp):
        return (
            f"rpe rep={message.replayed} drop={message.dropped_overflow} "
            f"rdrop={message.dropped_readings} coal={message.coalesced}"
        )
    if isinstance(message, GoneUp):
        return f"gone from={message.first} to={message.last} kinds={message.kinds}"
    if isinstance(message, HelloUp):
        return None
    raise TypeError(message)


def final_line(bridge: CBridge) -> str:
    c = bridge.counters
    return (
        f"final size={bridge.size} held={bridge.held} unacked={bridge.unacked} "
        f"drop={c.dropped_overflow} rdrop={c.dropped_readings} coal={c.coalesced} "
        f"rep={c.replayed} rtx={c.retransmitted} out={c.outages} gone={c.gone} "
        f"evicted_sent={c.evicted_sent} buffered={c.buffered}"
    )


def run_reference(case: dict[str, Any]) -> tuple[list[str], str]:
    """Run a case against the Python bridge: (emitted lines, final counters line)."""
    config = case["config"]
    engine = Engine()
    out: list[Any] = []
    bridge = CBridge(
        engine,
        BridgeConfig(config["timeout_ms"], config["capacity"], TICK_MS, config["window"]),
        out.append,
    )
    bridge.boot_up(config["boot"])
    out.clear()  # the hello is not part of a case
    lines: list[str] = []
    state = {"ack": 0, "max_n": 0}

    def flush() -> None:
        for message in out:
            line = canonical(message)
            if line is not None:
                lines.append(line)
            if isinstance(message, FrameUp):
                state["max_n"] = max(state["max_n"], message.n)
        out.clear()

    def ack_value(mode: Any) -> int:
        if mode == "all":
            state["ack"] = state["max_n"]
        elif mode != "none":
            state["ack"] = int(mode)
        return state["ack"]

    for step in case["steps"]:
        engine.run_until(step["t"])
        flush()
        op = step["op"]
        if op == "radio":
            bridge.from_radio(decode(bytes.fromhex(step["hex"])), step["via"], bool(step["dup"]))
        elif op == "hb":
            bridge.receive(HeartbeatDown(bridge.boot, ack_value(step["ack"])))
        elif op == "ack":
            bridge.receive(AckDown(bridge.boot, ack_value(step["ack"])))
        elif op == "rs":
            bridge.receive(ResendDown(bridge.boot, step["from"]))
        else:
            raise ValueError(op)
        flush()
    engine.run_until(case["end_ms"])
    flush()
    return lines, final_line(bridge)


# ---------------------------------------------------------------- the cases


class Script:
    """Builds a case's steps with a running clock."""

    def __init__(self) -> None:
        self.t = 0
        self.steps: list[dict[str, Any]] = []
        self._seq = 100

    def at(self, t: int) -> "Script":
        assert t % TICK_MS == 0, f"time {t} must be a multiple of {TICK_MS}"
        self.t = t
        return self

    def hb(self, t: int, ack: Any = "all") -> "Script":
        self.at(t)
        self.steps.append({"t": t, "op": "hb", "ack": ack})
        return self

    def recover(self, t: int, beats: int = 14) -> "Script":
        """The controller returns at `t` and keeps heartbeating, acknowledging everything."""
        for k in range(beats):
            self.hb(t + k * 1000, "all")
        return self

    def ack(self, t: int, ack: Any) -> "Script":
        self.at(t)
        self.steps.append({"t": t, "op": "ack", "ack": ack})
        return self

    def rs(self, t: int, from_n: int) -> "Script":
        self.at(t)
        self.steps.append({"t": t, "op": "rs", "from": from_n})
        return self

    def radio(
        self, t: int, frame: Frame, via: int = B, dup: bool = False, note: str = ""
    ) -> "Script":
        self.at(t)
        step: dict[str, Any] = {
            "t": t,
            "op": "radio",
            "hex": encode(frame).hex(),
            "via": via,
            "dup": int(dup),
        }
        if note:
            step["note"] = note
        self.steps.append(step)
        return self

    # frames; `via` copies of the same frame (a flood reaches C by both relays) share a seq
    def reading(
        self, t: int, seq: int, via: int = B, origin: int = A, session: int = 100, dup: bool = False
    ) -> "Script":
        frame = Frame(origin, C, session, seq, 7, DataPayload(2000 + seq))
        return self.radio(t, frame, via, dup, f"reading {origin}/{session}/{seq}")

    def report(self, t: int, origin: int, seq: int, via: int = B) -> "Script":
        frame = Frame(
            origin, C, 500 + origin, seq, 8, TopologyReportPayload((ReportEntry(B, 2, 1, 100),))
        )
        return self.radio(t, frame, via, False, f"topology report {origin} #{seq}")

    def telemetry(
        self, t: int, origin: int, seq: int = 1, via: int = B, created: int = 0
    ) -> "Script":
        frame = Frame(origin, C, 500 + origin, seq, 8, TelemetryPayload(created=created))
        return self.radio(t, frame, via, False, f"telemetry {origin} #{seq}")

    def route_ack(self, t: int, origin: int, seq: int = 1, via: int = B) -> "Script":
        frame = Frame(origin, C, 500 + origin, seq, 8, RouteInstallAckPayload(1000, seq, C, 1))
        return self.radio(t, frame, via, False, f"route ack {origin} #{seq}")

    def failover(self, t: int, seq: int, via: int = B, origin: int = A) -> "Script":
        frame = Frame(origin, C, 100, seq, 8, LocalFailoverReportPayload(1000, 1, C, B, D, 2))
        return self.radio(t, frame, via, False, f"failover report {origin} #{seq}")

    def case(
        self, name: str, description: str, capacity: int, window: int, end_ms: int
    ) -> dict[str, Any]:
        return {
            "name": name,
            "description": description,
            "config": {"capacity": capacity, "window": window, "timeout_ms": 3000, "boot": BOOT},
            "steps": sorted(self.steps, key=lambda step: step["t"]),  # stable: ties keep order
            "end_ms": end_ms,
        }


def _heartbeats(s: Script, start: int, stop: int, every: int = 1000) -> None:
    for t in range(start, stop + 1, every):
        s.hb(t, "all")


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    # 1. Each flooded report/telemetry/ack reaches C by both relays; during an outage only the
    #    latest per node and kind may survive, however many arrive.
    s = Script()
    _heartbeats(s, 0, 2000)  # controller present, then gone: unreachable from 5000
    for i, t in enumerate(range(5200, 17200, 1500)):
        for node in (A, B, D):
            s.report(t, node, seq=1 + i, via=B if node != B else D)
            s.report(
                t + 20, node, seq=1 + i, via=D if node != D else B
            )  # the same frame, other relay
            s.telemetry(t + 40, node, seq=50 + i, via=B if node != B else D)
        s.route_ack(t + 60, B, seq=1 + i, via=D)
    s.recover(24000)
    cases.append(
        s.case(
            "coalesce-reports-telemetry-acks-per-node",
            "A 12 s outage with flooded reports, telemetry and route acks arriving by both relays: "
            "only the newest per node and kind is replayed; every superseded copy is counted as "
            "coalesced and nothing is counted as lost.",
            capacity=50,
            window=16,
            end_ms=40000,
        )
    )

    # 2. Overflow: management traffic goes first, readings last, failover reports never.
    s = Script()
    _heartbeats(s, 0, 2000)
    t = 5200
    for node in (A, B, D):
        s.report(t, node, seq=1)
        s.telemetry(t + 20, node, seq=2)
        t += 40
    s.route_ack(t, B, seq=1)
    t += 20
    s.failover(t, seq=40)
    t += 20
    for i in range(1, 11):  # ten readings into a buffer of 12 that already holds 8 entries
        s.reading(t + i * 20, seq=i)
    t += 11 * 20
    for i in range(11, 19):  # more readings: the management entries are evicted first
        s.reading(t + (i - 10) * 20, seq=i)
    s.recover(12000)
    cases.append(
        s.case(
            "evict-management-before-readings-keep-failover",
            "Capacity 12. Reports, telemetry and an ack fill the buffer with a failover report; "
            "readings then arrive. Waiting management entries are evicted first (oldest first, counted "
            "as dropped but not as readings); the failover report is never evicted; no reading is "
            "dropped while any management entry could go instead.",
            capacity=12,
            window=4,
            end_ms=30000,
        )
    )

    # 3. Only when the buffer holds nothing else are readings dropped, oldest first, and counted.
    s = Script()
    _heartbeats(s, 0, 2000)
    s.failover(5200, seq=40)
    for i in range(1, 15):  # 14 readings into 11 free slots
        s.reading(5200 + i * 20, seq=i)
    s.recover(12000)
    cases.append(
        s.case(
            "readings-dropped-last-and-counted",
            "Capacity 12 holding one failover report: 14 readings arrive. The first three readings "
            "are given up, oldest first, and every one is counted in `drop` and in `rdrop`.",
            capacity=12,
            window=4,
            end_ms=30000,
        )
    )

    # 4. A management newcomer is itself dropped when only readings are held.
    s = Script()
    _heartbeats(s, 0, 2000)
    for i in range(1, 7):
        s.reading(5200 + i * 20, seq=i)
    s.report(5400, A, seq=1)  # worth less than every reading held
    s.telemetry(5420, A, seq=1)
    s.recover(12000)
    cases.append(
        s.case(
            "management-newcomer-dropped-when-full-of-readings",
            "Capacity 6 full of readings: a topology report and telemetry arrive and are themselves "
            "dropped (counted in `drop`, not in `rdrop`). No reading is lost.",
            capacity=6,
            window=4,
            end_ms=30000,
        )
    )

    # 5. Repeats keep the first copy: the failover report repeat and the retried reading.
    s = Script()
    _heartbeats(s, 0, 2000)
    s.failover(5200, seq=40, via=B)
    s.failover(5220, seq=41, via=D)  # a later copy of the same report
    s.failover(5240, seq=42, via=B)
    s.reading(5300, seq=7, via=B)
    s.reading(5320, seq=7, via=D, dup=True)  # A retried it: same identity
    s.reading(5340, seq=8, via=B)
    s.recover(12000)
    cases.append(
        s.case(
            "repeats-keep-the-first-copy",
            "A failover report repeated three times and a reading retried once: only the first copy of "
            "each is replayed (a repeat is counted as coalesced, not as lost).",
            capacity=12,
            window=4,
            end_ms=30000,
        )
    )

    # 6. The reliable uplink: numbering, window, resend, and `gone` naming what was given up.
    s = Script()
    s.hb(0, "none")
    for i in range(1, 5):  # live: numbered at once, kept until acknowledged
        s.reading(1000 + i * 20, seq=i)
    s.hb(1100, "none")
    # no more heartbeats: unreachable from 4100. During the outage the buffer (capacity 6, four
    # numbered entries) fills and then evicts the numbered ones
    for i in range(5, 12):
        s.reading(5000 + i * 20, seq=i)
    s.report(5400, A, seq=1)
    s.hb(20000, "none")  # controller back: replays what waited (window 4: four are sent)
    s.hb(21000, "none")
    s.rs(21500, 1)  # asks for everything from number 1: some numbers are gone
    s.hb(22000, "none")
    s.recover(23000, 10)  # now acknowledges everything: the rest is sent
    cases.append(
        s.case(
            "numbered-entries-evicted-are-named-gone",
            "Capacity 6, window 4. Four readings are sent live and numbered; an outage fills the buffer "
            "and evicts the numbered ones first; after the controller returns and asks for a resend, "
            "C names what is gone (readings R) and resends what it still holds.",
            capacity=6,
            window=4,
            end_ms=30000,
        )
    )

    # 7. A long outage at the documented capacity: 40 readings and management traffic all fit.
    s = Script()
    _heartbeats(s, 0, 2000)
    seq = 0
    for k in range(40):
        t = 5200 + k * 1500
        seq += 1
        s.reading(t, seq=seq)
        if k % 3 == 0:
            for node in (A, B, D):
                s.report(t + 100, node, seq=1 + k, via=B if node != B else D)
                s.telemetry(t + 120, node, seq=100 + k, via=D if node != D else B)
    s.recover(5200 + 40 * 1500 + 1000, 14)
    cases.append(
        s.case(
            "long-outage-fits-at-default-capacity",
            "Capacity 50, window 16: a 60 s outage with 40 readings and repeated management traffic. "
            "Management traffic coalesces to one slot per node and kind, so all 40 readings are "
            "replayed in order and none is lost.",
            capacity=50,
            window=16,
            end_ms=5200 + 40 * 1500 + 20000,
        )
    )

    # 8. A longer outage than the buffer can cover: readings are lost, counted exactly.
    s = Script()
    _heartbeats(s, 0, 2000)
    seq = 0
    for k in range(70):
        t = 5200 + k * 1500
        seq += 1
        s.reading(t, seq=seq)
        if k % 3 == 0:
            for node in (A, B, D):
                s.report(t + 100, node, seq=1 + k, via=B if node != B else D)
                s.telemetry(t + 120, node, seq=100 + k, via=D if node != D else B)
    s.failover(5200 + 35 * 1500 + 140, seq=40)
    s.recover(5200 + 70 * 1500 + 1000, 14)
    cases.append(
        s.case(
            "outage-longer-than-the-buffer-counts-every-loss",
            "Capacity 50, window 16: a 105 s outage with 70 readings. The oldest readings are given up "
            "once management entries (at most a handful) and the failover report fill their share; "
            "every given-up reading is counted in `rdrop`, and the failover report survives.",
            capacity=50,
            window=16,
            end_ms=5200 + 70 * 1500 + 20000,
        )
    )

    return cases


# ---------------------------------------------------------------- files


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + LF


def render() -> dict[str, str]:
    """File name -> content for every case, with the expected output filled in."""
    files: dict[str, str] = {}
    for case in build_cases():
        lines, final = run_reference(case)
        case = {**case, "expect": {"emitted": lines, "final": final}}
        files[f"{case['name']}.json"] = _json(case)
    return files


def _c_hex(data: bytes) -> str:
    return ", ".join(f"0x{b:02X}" for b in data)


def render_header(cases: list[dict[str, Any]]) -> str:
    out = [
        "/* Generated by `uv run python -m iot_backend.conformance`. Do not edit.",
        " * One scripted input and the exact lines C's bridge must write, per case. The Python",
        " * reference produced them; the firmware's native tests must reproduce them exactly. */",
        "#ifndef BRIDGE_CONFORMANCE_H",
        "#define BRIDGE_CONFORMANCE_H",
        "",
        "#include <stddef.h>",
        "#include <stdint.h>",
        "",
        "/* op: 0 radio, 1 hb, 2 ack, 3 rs. ack: -1 none, -2 all, otherwise the number. */",
        "typedef struct {",
        "  uint32_t t;",
        "  uint8_t op;",
        "  const uint8_t *frame;",
        "  uint8_t frame_len;",
        "  uint8_t via;",
        "  uint8_t dup;",
        "  int32_t ack;",
        "  uint32_t from;",
        "} BridgeStep;",
        "",
        "typedef struct {",
        "  const char *name;",
        "  uint8_t capacity;",
        "  uint8_t window;",
        "  uint32_t boot;",
        "  uint32_t end_ms;",
        "  const BridgeStep *steps;",
        "  size_t step_count;",
        "  const char *const *expected;",
        "  size_t expected_count;",
        "  const char *final_line;",
        "} BridgeCase;",
        "",
    ]
    names = []
    for index, case in enumerate(cases):
        lines, final = case["expect"]["emitted"], case["expect"]["final"]
        prefix = f"bc{index}"
        for i, step in enumerate(case["steps"]):
            if step["op"] == "radio":
                out.append(
                    f"static const uint8_t {prefix}_f{i}[] = {{{_c_hex(bytes.fromhex(step['hex']))}}};"
                )
        out.append(f"static const BridgeStep {prefix}_steps[] = {{")
        for i, step in enumerate(case["steps"]):
            op = OPS[step["op"]]
            if step["op"] == "radio":
                frame, flen = f"{prefix}_f{i}", len(step["hex"]) // 2
                row = f"{{{step['t']}u, {op}, {frame}, {flen}, {step['via']}, {step['dup']}, 0, 0}}"
            else:
                ack = step.get("ack")
                value = (
                    0
                    if ack is None
                    else (ACK_NONE if ack == "none" else ACK_ALL if ack == "all" else int(ack))
                )
                row = f"{{{step['t']}u, {op}, 0, 0, 0, 0, {value}, {step.get('from', 0)}u}}"
            out.append("  " + row + ",")
        out.append("};")
        out.append(f"static const char *const {prefix}_expected[] = {{")
        for line in lines:
            out.append(f'  "{line}",')
        out.append("};")
        cfg = case["config"]
        names.append(
            f'  {{"{case["name"]}", {cfg["capacity"]}, {cfg["window"]}, {cfg["boot"]}u, '
            f"{case['end_ms']}u, {prefix}_steps, {len(case['steps'])}, {prefix}_expected, "
            f'{len(lines)}, "{final}"}},'
        )
        out.append("")
    out.append("static const BridgeCase kBridgeCases[] = {")
    out.extend(names)
    out.append("};")
    out.append(f"static const size_t kBridgeCaseCount = {len(cases)};")
    out.append("")
    out.append("#endif")
    return LF.join(out) + LF


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the files are out of date")
    args = parser.parse_args(argv)
    files = render()
    cases = [json.loads(text) for text in files.values()]
    header = render_header(cases)
    stale: list[str] = []
    existing = {p.name for p in CASES_DIR.glob("*.json")} if CASES_DIR.exists() else set()
    for name, text in files.items():
        path = CASES_DIR / name
        current = (
            path.read_text(encoding="utf-8").replace(chr(13) + LF, LF) if path.exists() else None
        )
        if current != text:
            stale.append(name)
    stale += sorted(existing - set(files))
    current_header = (
        HEADER.read_text(encoding="utf-8").replace(chr(13) + LF, LF) if HEADER.exists() else None
    )
    if current_header != header:
        stale.append(HEADER.name)
    if args.check:
        if stale:
            print("stale:", stale)
            return 1
        return 0
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (CASES_DIR / name).write_text(text, encoding="utf-8", newline="")
    for name in existing - set(files):
        (CASES_DIR / name).unlink()
    HEADER.write_text(header, encoding="utf-8", newline="")
    print(f"wrote {len(files)} cases to {CASES_DIR} and {HEADER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
