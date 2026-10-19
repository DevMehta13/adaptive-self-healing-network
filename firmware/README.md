# ESP8266 firmware

F0 provides the bounded C++ codecs shared by all four ESP8266 nodes. It does not use
ESP-NOW or a physical board yet. The radio decoder checks the 14-byte header and every
defined payload in the documented order. The serial decoder checks line limits,
printable ASCII, CRC, strict fields and embedded radio frames. Both use caller-owned
buffers and fixed-capacity views; an invalid message changes no node state.

## Prerequisite

Install Python 3.11 or newer, then install PlatformIO Core in an isolated environment.
The commands are portable between macOS Terminal and Windows PowerShell:

```sh
cd firmware
python -m venv .platformio-venv
```

Activate it on macOS:

```sh
source .platformio-venv/bin/activate
```

Or on Windows PowerShell:

```powershell
.platformio-venv\Scripts\Activate.ps1
```

Then install the pinned tool and run the checks:

```sh
python -m pip install -r requirements.txt
pio test -e native
pio run -e nodemcuv2
```

The native check runs all 79 radio vectors and all 48 serial vectors from
`tests/generated/test_vectors.h`. The NodeMCU command cross-compiles the same codec for
the selected ESP8266 board. Neither command uploads firmware or proves radio behavior.

## F1: two-board DATA/ACK bench

F1 uses exactly two ESP8266 NodeMCU boards. The sender is node A and emits one
DATA frame at a time. The receiver is node C and sends a protocol DELIVERY_ACK
only after it has decoded a valid DATA frame. A successful ESP-NOW send callback
means the radio link layer reported success; it is counted separately and is not
treated as delivery. The bench succeeds only when A reports 100 matched ACKs.

Build and upload the receiver first, then the sender. Replace the port names with
the ports shown by `pio device list`; do not commit them.

```sh
pio run -e f1_receiver --target upload --upload-port YOUR_C_PORT
pio device monitor --port YOUR_C_PORT --baud 115200

pio run -e f1_sender --target upload --upload-port YOUR_A_PORT
pio device monitor --port YOUR_A_PORT --baud 115200
```

C prints `F1_READY ... mac=AA:BB:CC:DD:EE:FF`. In A's monitor enter the following
two lines, using C's printed MAC address:

```text
peer AA:BB:CC:DD:EE:FF
start
```

Do not reset either board during the run. A prints `BENCH_COMPLETE
acknowledged=100` only after 100 end-to-end ACKs. Enter `summary` on both monitors
and record the complete output: it includes MAC-derived sender evidence at C,
send-callback success/failure counts, rejected/queued-frame counts, and free heap.
`ACK_TIMEOUT` or nonzero queue drops is a failed or inconclusive run, not a pass.
The peer MAC is entered at run time and remains only in RAM, so board-specific
addresses never enter source control.

This bench does not yet provide forwarding, retries, duplicate detection, serial
uplink, RSSI claims, or a four-node demonstration. It also cannot establish the
result until two physical boards are connected and their monitor output is saved.
The complete on-site procedure and results table are in
[F1_LAB_RUNBOOK.md](F1_LAB_RUNBOOK.md).

The complete first-session package for all four boards—power/USB layout,
role-specific F3c uploads, runtime MAC setup, result sheet and checklist—is in
[FOUR_BOARD_LAB_PACKAGE.md](FOUR_BOARD_LAB_PACKAGE.md). It is an assembly and
measurement reference, not a claim that the four-board demonstration has run.

## F2: C serial bridge (software-ready)

`f2_c` is C's USB-serial bridge image. It writes checksummed protocol lines at
115200 baud, waits for a laptop heartbeat before sending radio evidence, keeps a
fixed **20-entry** RAM buffer with a 16-entry unacknowledged window, accepts
cumulative acknowledgements and resend requests, and holds then replays entries
after a three-second heartbeat timeout. The bridge contains no dynamic allocation.

```sh
pio test -e native
pio run -e f2_c
```

The native fake-C checks cover heartbeats, cumulative acknowledgement, resend,
timeout, buffering, replay markers and an overflow boundary. The `f2_c` image
accepts laptop lines and reports its boot/status; F3 will connect its
`receive_radio(...)` entry point to C's ESP-NOW receive queue and route-update
sender. The host test and cross-compile do not prove that opening a NodeMCU USB
port avoids reset, that serial throughput is sufficient, or that the buffer fits
alongside the full radio firmware. Those are the F2 hardware-validation checks.

## F3a: four-node data forwarding (software-ready)

F3a adds a fixed-memory forwarding engine shared by A, B, C and D. It enforces
the configured diamond links (A–B, A–D, B–C and D–C), forwards only through a
manual route table, decrements TTL at relays, limits outgoing and pending work
to 16 entries each, and retries a source reading at most three times. C keeps
eight least-recently-used `(source, session)` duplicate windows with a 64-bit
wrap-safe sequence bitmap; a repeat is acknowledged again but counted once.
The session is part of the identity, so a restarted A may reuse sequence 1.

The native test uses a deterministic mock radio for normal delivery, a rejected
direct A→C bypass, a route loop stopped by TTL, a lost ACK/retry/duplicate, and
a restarted source. It does not claim radio delivery. Build all images with:

```sh
pio test -e native
pio run -e f3a_a -e f3a_b -e f3a_c -e f3a_d
```

The F3a images require each logical neighbor's ESP-NOW MAC to be entered at
runtime, for example `peer 2 AA:BB:CC:DD:EE:FF`; addresses remain in RAM.
Only A accepts `send VALUE` to create a generated reading. `summary` prints
engine counters and free heap. The routes are intentionally manual in F3a:
A→B→C for data and C→B→A for acknowledgements. F3b adds discovery/management
behaviour and F3c replaces those routes with controller-installed entries and
backup failover. No F3a image has been uploaded or tested with a board.

If Harshal changes a protocol example, regenerate the shared header from `backend/`
before rerunning F0:

```sh
uv run python -m iot_backend.vectors --check
```

## F3b: discovery and bounded control traffic (software-ready)

F3b builds on F3a. Each node sends a unicast HELLO to each configured logical
neighbor every second, records whether that neighbor is one-way, two-way, or
lost after three seconds, and emits coalesced topology reports plus a five-second
periodic report. Reports, delivery acknowledgements, and telemetry are flooded
once over permitted links with an eight-hop limit and a 64-identity cache; they
do not depend on the data route. Probes are one hop and record only round-trip
time on the originating node's clock. Telemetry is cumulative and every five
seconds. These are configurable initial experiment settings, not timing claims.

The C-side engine exposes management frames addressed to C through a small
callback boundary, so the serial gateway can represent only received evidence;
the host test uses that same boundary. It does not give nodes access to the
laptop's topology or calculate routes locally. F3c will attach controller route
installation and backup failover.

```sh
pio test -e native
pio run -e f3b_a -e f3b_b -e f3b_c -e f3b_d
```

The deterministic native tests check two-way HELLO confirmation, one-way and
stale observations, topology-report arrival at C, management flooding,
telemetry, probe replies, and the fixed queue boundary. The target builds do
not test ESP-NOW timing, packet delivery, callback semantics, heap at run time,
or USB serial on a real board. Configure peer MACs over the serial console just
as for F3a; `summary` shows counters and free heap.

## F3c: installed routes, backup failover, and C replay (verification pending)

F3c removes manual data routes. C accepts only checked serial route commands,
creates the radio header itself, and floods the resulting `ROUTE_UPDATE`.
Nodes accept updates only from C, retain epoch/version/primary/backup entries,
acknowledge accepted, repeated, stale, or rejected commands, and never
calculate a route. A failed primary (lost/one-way neighbor or three consecutive
next-hop callback failures) selects only its installed two-way backup and
floods bounded failover reports; returning to a primary requires a newer
controller version.

The F3c C image joins the radio engine to the fixed-size serial bridge: it
continues to acknowledge readings, buffers bounded evidence while the laptop
heartbeat is absent, and brackets replay on return. ESP-NOW callbacks are
next-hop signals, never end-to-end delivery acknowledgements.

```sh
pio test -e native
pio run -e f3c_a -e f3c_b -e f3c_c -e f3c_d
```

Native mock-radio checks cover ordered installation, stale-version rejection,
backup selection and delivery through D; existing fake-C checks cover bounded
outage buffering and replay. They do not validate real radio callbacks, USB,
heap, delivery, or recovery timing.
