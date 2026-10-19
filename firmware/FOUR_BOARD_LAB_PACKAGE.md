# Four-board lab package

This package prepares the first real-board session for the four ESP8266
NodeMCU boards. It is an assembly and measurement guide, not evidence that
the radio network already works. Record the actual observations in the result
sheet; do not substitute the example logs below for measurements.

## What this session proves

The lab runs the prepared firmware in order: F1 confirms a two-board,
end-to-end DATA/ACK exchange; F2 confirms C's USB serial boundary; F3a/F3b
establish forwarding and discovery; F3c finally checks controller-installed
routes, local backup failover, and C's replay boundary. An ESP-NOW send
callback is only evidence about the next hop. A matching DELIVERY_ACK at A is
the delivery evidence.

Bring four NodeMCU boards labelled **A**, **B**, **C** and **D**, five
known data-capable USB cables (four for flashing, one spare), three suitable
USB power sources or a powered hub, and a laptop with the repository,
PlatformIO and the backend dependencies already installed. Do not use an
unlabelled charging-only cable for an upload or serial monitor.

## Power, USB and logical-radio layout

```text
                         USB data, 115200 baud
          Laptop  <================================>  C (node 3, gateway)
                                                        / \
                                                   B (node 2)  D (node 4)
                                                     \           /
                                                      \         /
                                                       A (node 1)

  C receives USB power from the laptop. A, B and D receive USB power from
  suitable adapters or a powered hub. Permitted logical links: A--B, B--C,
  A--D and D--C.

Normal data: A -> B -> C        Installed backup: A -> D -> C
```

Only C keeps a USB **data** cable connected to the laptop during the final
four-board run. A, B and D need power only after flashing. The diamond is a
configured logical topology: boards may physically hear a non-neighbour, but
the firmware must reject that traffic.

Keep the boards close together for bring-up. The unplug test removes **B's
power**. To test controller loss, stop the backend; never unplug C because its
USB cable powers it and its RAM buffer is then lost.

## Before flashing

From `firmware/`, activate the documented PlatformIO environment and run:

```sh
pio test -e native
pio device list
```

Write the observed port beside each board in the result sheet. macOS ports
usually look like `/dev/cu.usbserial-...` or `/dev/cu.SLAB_USBtoUART`; Windows
ports look like `COM3`. They are local lab values, not source configuration.

## Role builds and uploads

Use the F3c image set for the final four-board run. Each command may compile
before uploading; replace only the placeholder port.

| Board | Role | Build and upload command |
| --- | --- | --- |
| A | source, node 1 | `pio run -e f3c_a --target upload --upload-port YOUR_A_PORT` |
| B | relay, node 2 | `pio run -e f3c_b --target upload --upload-port YOUR_B_PORT` |
| C | destination and USB gateway, node 3 | `pio run -e f3c_c --target upload --upload-port YOUR_C_PORT` |
| D | relay/backup, node 4 | `pio run -e f3c_d --target upload --upload-port YOUR_D_PORT` |

For F1, flash C first and then A:

```sh
pio run -e f1_receiver --target upload --upload-port YOUR_C_PORT
pio run -e f1_sender --target upload --upload-port YOUR_A_PORT
```

Use one serial monitor at a time when discovering MACs:

```sh
pio device monitor --port YOUR_PORT --baud 115200
```

The F3c images are not interchangeable with the F1 pair. Reflash the four
F3c images after the F1 bench.

## MAC-address setup

Every F3c board prints its own station MAC at boot. Record it first, then
enter only the board's permitted neighbours. The addresses live only in RAM;
after a reset or reflash, enter them again. Never put real lab MAC addresses
into Git or this document.

Expected boot evidence (the hexadecimal values and heap are examples only):

```text
F3_READY role=1 mac=AA:BB:CC:DD:EE:01 free_heap=40112
F3C_READY controller-installed routes, backups and replay enabled; enter neighbor peer MACs
```

The ESP8266 boot-ROM text at 74880 baud can precede this output; it is noise,
not a failed test. `F3_SETUP_FAILED` means ESP-NOW setup failed: save the
full log and stop rather than guessing a replacement API.

With the following local placeholders:

```text
MAC_A = AA:AA:AA:AA:AA:01    MAC_B = BB:BB:BB:BB:BB:02
MAC_C = CC:CC:CC:CC:CC:03    MAC_D = DD:DD:DD:DD:DD:04
```

send these commands to the relevant board's 115200-baud serial console:

| Board | Commands to enter | Expected response per accepted peer |
| --- | --- | --- |
| A | `peer 2 MAC_B` then `peer 4 MAC_D` | `# peer_set` twice |
| B | `peer 1 MAC_A` then `peer 3 MAC_C` | `# peer_set` twice |
| C | `peer 2 MAC_B` then `peer 4 MAC_D` | `# peer_set` twice |
| D | `peer 1 MAC_A` then `peer 3 MAC_C` | `# peer_set` twice |

Replace each `MAC_*` token with the exact colon-separated MAC recorded from
that board. `# peer_failed` is a failed configuration step, not permission to
continue. Check the copied address, board role and cable/monitor connection.

On A, B and D, `summary` prints counters and `free_heap`; A additionally
accepts `send VALUE`. C shares its serial connection with the laptop bridge,
so use the laptop's serial probe or backend logs for its runtime evidence
instead of opening another monitor at the same time.

## Expected serial evidence

These are patterns to look for, not values promised by the project:

| Stage | Evidence that should appear | What it means |
| --- | --- | --- |
| F1 start | `F1_READY role=receiver(C) mac=...`, then A's `F1_READY role=sender(A) mac=...` | Both images started and exposed their MACs. |
| F1 pass | A prints `BENCH_COMPLETE acknowledged=100`; A summary has `acks_matched=100`; C reports A as `last_radio_sender` | The 100 DATA readings had matching end-to-end ACKs. |
| F1 failure | `ACK_TIMEOUT`, nonzero `receive_queue_drops`, or missing matching count | Preserve both summaries; this is not a pass. |
| F3 boot | `F3_READY role=N mac=... free_heap=...`, followed by `F3C_READY ...` | Correct F3c role image started and its initial heap/MAC were captured. |
| Peer setup | `# peer_set` for exactly the two logical neighbours | The local ESP-NOW peer entry was added. |
| C serial bring-up | A checksummed `hello` line beginning `{"v":1,"t":"hello"...}*` | C's serial bridge started. A serial probe labels it `DATA`, `LOG`, `NOISE`, or `BAD`; it must not label the checked hello `BAD`. |
| Normal F3c delivery | A's `queued`; B's `forwarded` rises; C-side serial output includes checked `fr` lines; A's `acknowledged` rises | Evidence is consistent with A -> B -> C. Record all counters. |
| B unplug | B stops; after detection A changes only to its installed D backup; D's `forwarded` rises and C receives new `fr` lines | Record the actual interruption and losses. Do not claim a fixed recovery time. |
| Controller loss | After stopping the backend, C continues radio work but holds serial evidence; after restart, its checked `rps`, replayed `fr`, and `rpe` lines appear | This exercises bounded replay, not a guarantee of zero loss. |

To inspect C without the dashboard, run the backend serial probe from
`backend/` (with C as the only process using its port):

```sh
uv run python -m iot_backend.serial_probe --port YOUR_C_PORT
```

For the integrated hardware run, start the backend instead:

```sh
# macOS / Git Bash
IOT_MODE=hardware IOT_SERIAL_PORT=YOUR_C_PORT uv run python -m iot_backend
```

```powershell
# Windows PowerShell
$env:IOT_MODE = "hardware"; $env:IOT_SERIAL_PORT = "YOUR_C_PORT"; uv run python -m iot_backend
```

Run `uv run python -m iot_backend.watch --no-heartbeats` in another terminal
to preserve the controller-side event record. The backend/serial integration
has only been tested against a fake C so far; a real C result must be recorded
as a new measurement.

## Result sheet

Copy this sheet into the lab notes. Capture complete terminal logs separately.

| Field | A | B | C | D |
| --- | --- | --- | --- | --- |
| Board marking / USB-chip type | | | | |
| USB port while flashed | | | | |
| F3c MAC address | | | | |
| Initial free heap | | | | |
| Permitted peers accepted | B, D | A, C | B, D | A, C |
| Final free heap | | | | |
| Delivered | | | | |
| Forwarded | | | | |
| Acknowledged | | | | |
| Duplicates | | | | |
| Queue drops | | | | |
| Notes / saved-log filename | | | | |

| Trial | Planned observation | Actual observation and timestamps | Pass / fail / inconclusive |
| --- | --- | --- | --- |
| F1 DATA/ACK | 100 matching ACKs | | |
| F2 serial probe | hello/status parsed; no port-open reset observed | | |
| F3a forwarding | A -> B -> C delivery | | |
| F3b discovery | logical neighbours become two-way; reports reach C | | |
| F3c normal route | controller installs/acknowledges route | | |
| F3c B-power removal | A uses pre-installed D backup; new readings reach C | | |
| F3c B return | controller decides whether/when B returns to service | | |
| Controller outage | C holds and replays bounded evidence after backend restart | | |

## Test checklist and recovery path

- [ ] Label boards A/B/C/D and record USB ports, boot MACs and initial heap.
- [ ] Run the native tests before the session; record the PlatformIO version.
- [ ] Complete F1 using the detailed [F1 runbook](F1_LAB_RUNBOOK.md); save both summaries.
- [ ] Reflash F3c A/B/C/D, record every `F3_READY` line and enter the eight peer entries.
- [ ] Confirm exactly two `# peer_set` responses per board and no `# peer_failed`.
- [ ] Use the serial probe to check C's checksum-framed hello/status before starting the backend.
- [ ] Start the backend in hardware mode and preserve watcher output for normal delivery.
- [ ] Remove only B's power, record delivery gap/losses/route evidence, then restore B.
- [ ] Stop and restart the backend while C stays powered; record replay markers and counts.
- [ ] Save complete logs, fill the result sheet and report failures without relabelling them as passes.

If an upload fails, try the spare data cable and re-check the selected port. If
the port never appears, identify its CH340/CP210x USB chip and install the
vendor driver appropriate to the operating system. If the F1 bench fails,
follow its runbook: confirm roles and C's copied MAC, reset both boards once,
then preserve both failed summaries. If F3 peer setup fails, do not alter
routes or timing constants; correct the role/MAC configuration first. If a
second identical attempt fails, stop and use the saved logs to choose the next
evidence-based change.
