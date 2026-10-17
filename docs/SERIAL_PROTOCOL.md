# Serial protocol between node C and the laptop

**Draft — pending Dev's review.** Written by Harshal (backend owner) for Dev, whose firmware on node C must speak it. Nothing here is frozen until Dev approves it. The laptop side is implemented and tested in simulation (`backend/src/iot_backend/transports/`); **no board has run it yet**. The reference cases are in [tests/serial-examples/](../tests/serial-examples/) and, as C test vectors, in [tests/generated/test_vectors.h](../tests/generated/test_vectors.h). If this document and the examples disagree, tell Harshal: they are meant to agree exactly.

This link is **USB serial between C and the laptop only**. It never travels on the radio and changes nothing in the radio frame format of [PROTOCOL.md](PROTOCOL.md).

## 1. Lines

| Item | Rule |
| --- | --- |
| Port settings | 115200 baud, 8 data bits, no parity, 1 stop bit (8N1), no flow control. The baud rate is configurable on the laptop (`IOT_SERIAL_BAUD`); both sides must agree. |
| Framing | One message per line. A line ends with `\n` (0x0A). A `\r` before it is ignored. |
| Characters | Printable ASCII only (0x20 to 0x7E) in a data line. |
| Data line | `<JSON object>*<CRC>` — compact JSON, then `*`, then the CRC-16 of the JSON text as 4 hex digits. Example: `{"v":1,"t":"hb","b":0,"ack":0}*` followed by its checksum (see [valid-down-heartbeat-first](../tests/serial-examples/valid-down-heartbeat-first.json)). |
| Log line | Starts with `#`. Shown to people, **never parsed as data**. All firmware diagnostics must use it: `# C booted, session a4e8c214`. |
| Anything else | Noise (for example the ESP8266 boot ROM's text at 74880 baud after a reset). Counted, shown as a log, never parsed. |
| Longest line, C to laptop | 640 bytes including `\n`. The longest legitimate one (a 250-byte radio frame in hex with every field at its maximum) is 603; the largest real frame today, a full topology report, makes a 303-byte line. |
| Longest line, laptop to C | **128 bytes** including `\n`. The longest the laptop sends (a ROUTE_UPDATE `tx`) is 69. |

**Why 128 bytes down.** The ESP8266 Arduino core gives `Serial` a 256-byte software receive buffer by default (the `HardwareSerial` constructor sets `_rx_size(256)`), behind the UART's 128-byte hardware FIFO. Keeping every downlink line under 128 bytes means a whole line fits the FIFO, and two lines fit the buffer, even if C's `loop()` is busy for a while. `Serial.setRxBufferSize()` can raise the buffer if Phase 6 shows it is needed.

**Why a checksum on every data line.** The USB part of the link is protected by USB's own CRC, but the last part, the UART between the ESP8266 and the board's USB-to-serial chip, is not. The typical fault there is a lost byte when a buffer overflows. JSON parsing does not catch that: `"n":12` with one digit lost is `"n":2`, still valid JSON, and wrong. A CRC-16 catches every single-byte loss or change and every burst of up to 16 bits; a test checks all single-byte drops and bit flips of a real line. The cost is 5 bytes per line and about 20 lines of C. CRC-16 was preferred to an 8-bit XOR (as in NMEA), which misses two equal changes and any reordering.

**The checksum.** CRC-16/CCITT-FALSE: polynomial 0x1021, initial value 0xFFFF, no reflection, no final XOR, computed over the bytes between the start of the line and the `*` (the JSON text only). The check value of the ASCII string `123456789` is `0x29B1`. Written as 4 hex digits; the laptop writes upper case and accepts either.

```c
uint16_t crc16(const uint8_t *data, size_t len) {
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)(data[i] << 8);
        for (int b = 0; b < 8; b++)
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
    return crc;
}
```

## 2. Checking a line

A received line is checked completely before anything in it is used. A line that fails any check is **dropped whole and counted**; nothing in it is ever applied in part. In this order:

| Reason | Meaning |
| --- | --- |
| `too_long` | Longer than the limit for its direction. The receiver discards bytes up to the next `\n` and starts again; memory never grows past the limit. |
| `truncated` | The port closed in the middle of a line (decided by the reader, not by one line). |
| `no_checksum` | It starts with `{` but does not end with `*` and 4 hex digits. |
| `not_ascii` | A byte outside 0x20–0x7E. |
| `bad_checksum` | The CRC does not match. |
| `bad_json` | Not one JSON object. |
| `duplicate_key` | A key appears twice. |
| `bad_version` | `v` is not 1. |
| `unknown_type` | `t` is not a type for this direction (C must not accept `st`; the laptop must not accept `hb`). |
| `missing_field` / `unknown_field` | A field is missing, or one that the type does not have is present. Typos must not pass silently. |
| `bad_value` | Wrong type or out of range. All numbers are integers 0 to 4294967295 (no floats, no `true` for 1); flags are 0 or 1; node ids 1 to 254; hex is whole bytes. |
| `bad_frame` | (Uplink `fr`) the radio frame inside fails the protocol codec's checks ([PROTOCOL.md](PROTOCOL.md) *Decoding rules*). |
| `unexpected_frame` | (Uplink `fr`) a valid frame of a type C never hands up (HELLO, PROBE, ...). |

Every reason has an example in [tests/serial-examples/](../tests/serial-examples/).

## 3. Messages

Keys are short to keep lines short. The order shown is the order both sides write; a parser must not depend on it. `b` is always the **boot of C**: C's own random session id from the radio protocol (never 0), which changes every time C starts. All counters are cumulative since that boot.

### C to the laptop

| `t` | Fields | Meaning |
| --- | --- | --- |
| `fr` | `b`, `n`, `age`, `rp`, `via`, `dup` (DATA only), `hex` | One radio frame for the controller. `n`: its number (section 4). `age`: ms C held it before this line, on C's own clock (0 when sent at once). `rp`: 1 if not sent the moment it arrived (it waited, or this is a resend). `via`: node id of the neighbor whose radio handed C the frame (the ESP-NOW sender, mapped from its MAC). `dup`: for DATA, 1 if C's duplicate filter had already seen it. `hex`: the frame **exactly as received**, header included. |
| `st` | `b`, `last`, `low`, `reach`, `held`, `unacked`, `cap`, `win`, `drop`, `coal`, `rep`, `rtx`, `out` | C's answer to every heartbeat. `last`: highest number assigned. `low`: lowest number still held (`last`+1 if none). `reach`: 1 (C just heard the controller). `held`: entries waiting to be sent for the first time. `unacked`: numbered entries not yet acknowledged. `cap`, `win`: buffer capacity and window (0 if C keeps nothing). `drop`: waiting entries lost to overflow. `coal`: entries superseded by newer ones. `rep`: entries sent late. `rtx`: entries resent on request. `out`: outages declared. |
| `rps` | `b`, `ent`, `held`, `drop`, `coal`, `out_ms` | Replay start, after an outage: `ent` entries waiting, `held` of them stored while the controller was unreachable, `drop`/`coal` during this outage, `out_ms` C's measure of the outage. |
| `rpe` | `b`, `rep`, `drop`, `coal` | Replay end: `rep` sent in this replay. |
| `gone` | `b`, `from`, `to`, `kinds` | Numbers `from`..`to` were asked for and C no longer holds them. `kinds` has one letter per number: `R` a reading, `M` another frame, `S` a report, acknowledgement or telemetry superseded by a newer one from the same node (nothing is lost), `?` unknown (C freed it after an acknowledgement). At most 64 numbers per line; C sends several lines for more. |
| `hello` | `b`, `node`, `cap`, `win`, `fw` | C has just booted. `node`: C's node id (3). `fw`: up to 16 characters of `A-Z a-z 0-9 . _ -`. Not numbered: if it is missed, the next `st` carries the same boot. |

C hands up exactly these radio frames: every TOPOLOGY_REPORT, ROUTE_INSTALL_ACK, LOCAL_FAILOVER_REPORT and TELEMETRY addressed to C, and every DATA frame C accepts (new or repeated, with `dup`). Nothing else.

### The laptop to C

| `t` | Fields | Meaning |
| --- | --- | --- |
| `hb` | `b`, `ack` | Heartbeat: the controller is alive (every 1 s). Also the acknowledgement: the laptop has every number up to `ack` of boot `b`. `b` 0 means the laptop does not know C's boot yet; C then ignores `ack`. |
| `ack` | `b`, `ack` | The acknowledgement alone (sent after every 8 new frames). It does **not** count as a heartbeat. |
| `rs` | `b`, `from` | Resend: please send again your numbered messages from `from` on. |
| `tx` | `dst`, `mt`, `p` | Originate a management frame. `mt`: the radio message type; `dst`: the node it is for; `p`: the payload in hex (at most 40 bytes). |

**Who builds the header.** C does. For `tx`, the laptop sends only type, destination and payload; C writes the 14-byte header with **its own** `src` (3), its current session, its next sequence number and the default TTL, exactly as for any frame it creates, then floods it. This must be C's job: nodes accept a ROUTE_UPDATE only from the gateway's `src`, sequence numbers must be unique per C boot (C cannot share a counter with the laptop), and the session must be the one C's neighbors know. C accepts `mt` 18 (ROUTE_UPDATE) only, checks that `p` has that type's exact size (11 bytes), and refuses anything else with a `#` log line.

### Mapping from the gateway's typed messages

Every typed message of the gateway interface (`transports/gateway.py`) maps to exactly one line type:

| Typed message | Line |
| --- | --- |
| `Heartbeat` | `hb` |
| `RouteUpdateCommand` | `tx` with `mt` 18 |
| `TopologyReportEvent`, `RouteInstallAckEvent`, `LocalFailoverEvent`, `ReadingEvent`, `TelemetryEvent` | `fr` (decoded on the laptop with the protocol codec) |
| `GatewayStatusEvent` | `st` |
| `ReplayStartEvent` | `rps` |
| `ReplayEndEvent` | `rpe` |
| `UplinkLossEvent` | none: the laptop makes it from `gone`, or from a change of `b` |

`ack`, `rs`, `gone` and `hello` belong to the reliable uplink below; the controller never sees them.

**Why frames go up as hex.** C does not decode what it hands up: it copies the received bytes, adds what only C knows (which neighbor handed the frame over, whether its duplicate filter had seen it, how long it held it) and writes a line. The laptop decodes with the protocol codec that already passes every reference example. The firmware stays small and there is one decoder, not two that could disagree. The price is about twice the bytes on the serial line, which at 115200 baud (11.5 KB/s) is negligible next to a few frames per second.

## 4. Reliable uplink

The problem it solves: before this, if the laptop missed C's lines for less than C's 3 s heartbeat timeout, C never noticed, thought it had delivered them, and they were lost without being counted (measured in simulation: a 1 s outage lost every reading made in it).

**On C:**

1. Every `fr` gets a number `n` when it is **first sent**: 1, 2, 3, ... per boot of C. It stays in C's buffer, now *numbered*, until acknowledged.
2. An acknowledgement (`hb` or `ack` with `b` equal to C's boot) frees every numbered entry with `n` ≤ `ack`. It is cumulative: one number says "everything up to here". An `ack` for another boot, for a number never sent, or older than one already received is ignored.
3. The **window**: at most 16 numbered entries unacknowledged. Beyond that, new entries wait unsent (subject to the buffer rules in [PROTOCOL.md](PROTOCOL.md) *Laptop and C*), so a laptop that stops acknowledging cannot make C's memory grow.
4. On `rs` with `from`: resend every numbered entry held with `n` ≥ `from`, in order, with `rp` 1 and their current age. First, for numbers from `from` up to the newest that C no longer holds, send `gone` with their kinds.
5. While the controller is reachable C sends at once if nothing is waiting and the window is open; otherwise one message per 20 ms (simulated serial throughput) from the buffer, resends first.
6. A reboot loses everything (RAM only) and numbering starts again at 1 under a new `b`.

**On the laptop** (implemented in `transports/laptop_gateway.py`):

1. It keeps `ack`, the highest number up to which it has everything (or has been told is gone), and the set of numbers above it already received (bounded: 256).
2. A number received again is dropped and counted: nothing is delivered twice.
3. A gap (a number above `ack` missing, seen from a later `fr` or from `last` in `st`) triggers `rs` from `ack`+1. One request at a time; asked again after 1 s if the gap is still open, so a lost request or a lost answer costs one second.
4. `gone` numbers are counted as lost, by kind (readings separately), and `ack` moves past them. `S` (superseded) is not a loss.
5. A new `b` means C rebooted: whatever C held is lost. The laptop counts the numbers it knew were missing (a lower bound) and starts again from C's `low`.
6. A laptop that starts fresh takes C's `low` as its starting point: numbers below it were acknowledged by an earlier laptop process.

This is the same idea as TCP's cumulative acknowledgement and retransmission. Differences: the receiver asks for a resend (a negative acknowledgement) instead of the sender timing out, because the laptop sees a gap within one heartbeat and C then needs no retransmission timer; the window is fixed, not negotiated; and when data is truly gone the sender says so instead of retrying forever.

## 5. Heartbeat and fail-static (unchanged from Phase 5b)

The laptop sends `hb` every 1 s. C declares the controller unreachable after 3 s without one and holds what it would have sent up; the laptop declares its link to C lost after 3 s without any line from C. On the first `hb` after an outage C answers `st`, then `rps`, the held entries, `rpe`. C keeps working on the radio throughout. Details and the buffer rules are in [PROTOCOL.md](PROTOCOL.md) *Laptop and C*.

## 6. Hardware notes for the firmware

- **Opening the port must not reset C.** On a NodeMCU the USB chip's DTR and RTS lines are wired to the ESP8266's reset and GPIO0, and most programs pulse them when they open a port, which reboots C and loses its buffer. The laptop clears DTR and RTS before opening. Whether that avoids the reset with the board's chip (CH340 or CP2102) on both Windows and macOS **must be checked in Phase 6**; if it does not, a capacitor on the reset line or a different board setting is needed.
- **Unplugging C's USB cable powers C off.** Controller loss is tested by stopping the backend, not by unplugging.
- **Writing.** The ESP8266 core has no software transmit buffer: `Serial.write` waits while the 128-byte hardware FIFO is full, so a 300-byte line takes about 26 ms at 115200 baud. Write lines from `loop()`, never from the ESP-NOW receive callback (which runs in the Wi-Fi task); queue received frames in the callback and print them later.
- **Reading.** Accumulate bytes up to `\n`; if 128 bytes arrive without one, drop them up to the next `\n` and count it.
- **Ages.** Use `millis()` (unsigned 32-bit) and unsigned subtraction: `age = millis() - arrived` stays correct across the 49-day wrap.
- **Logs.** Only lines starting with `#`, and never in the middle of a data line (write each line in one go).
- **Memory.** With the frame bytes kept as received: a reading entry is about 28 bytes, a topology report about 114. 50 entries are about 2.3 KB typically and 5.7 KB at worst. To be measured against free heap in Phase 6.

## 7. Versioning

`v` is 1. A change that an old receiver would misread gets `v` 2; a receiver refuses versions it does not know (`bad_version`) rather than guessing.
