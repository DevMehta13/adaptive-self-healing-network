# F1 two-board DATA/ACK lab runbook

This runbook validates the prepared F1 sender (A) and receiver (C) images. It
is a hardware check, not a development exercise: do not change frame format,
retry policy, or routing logic during this run.

## Bring

- Two ESP8266 NodeMCU boards and two known data-capable USB cables.
- A laptop with this repository, PlatformIO, and the firmware dependencies
  already installed.
- Two terminal windows, one for each board's serial monitor.

## Before starting

1. Place the two boards near each other and power both through USB.
2. Run `pio device list` in `firmware/` and write down the two actual ports.
   They must not be committed to Git.
3. Build and upload `f1_receiver` to the board acting as C. Open its 115200
   baud monitor and record its `F1_READY` line, especially C's MAC and initial
   `free_heap`.
4. Build and upload `f1_sender` to the board acting as A. Open its 115200 baud
   monitor and record its `F1_READY` line.

Exact upload commands are in [README.md](README.md#f1-two-board-dataack-bench).

## Run

On A's monitor, enter C's MAC from C's `F1_READY` line, then start the bench:

```text
peer AA:BB:CC:DD:EE:FF
start
```

Wait for either `BENCH_COMPLETE acknowledged=100` or `ACK_TIMEOUT`. Then enter
`summary` on **both** monitors and preserve the complete output.

## Pass criteria

The run passes only when all of these are true:

- A prints `BENCH_COMPLETE acknowledged=100`.
- A's `acks_matched=100` and its `ACK_TIMEOUT` never appears.
- C's `last_radio_sender` equals A's printed MAC.
- Both summaries show `receive_queue_drops=0`.
- Both summaries are saved with their `free_heap`, callback counts and send-call
  failure counts. These are measurements, not values to predict in advance.

`send_callback_ok` is useful radio-link evidence, but is not a pass criterion
by itself. The matching DELIVERY_ACK is the end-to-end F1 evidence.

## Result record

| Item | A / sender | C / receiver |
| --- | --- | --- |
| Board MAC | | |
| Initial free heap | | |
| Final free heap | | |
| Send callback OK / failed | | |
| Send-call failures | | |
| Receive queue drops | | |
| Matched ACKs | | n/a |
| Callback-reported sender | n/a | |
| Result and notes | | |

## If it does not pass

1. Confirm that both boards run the correct F1 image, the ports are not swapped,
   and C's MAC was copied exactly into A's `peer` command.
2. Reset both boards, repeat the run once, and save both failed summaries.
3. If upload fails, first change the USB cable or port; a charging-only cable
   cannot upload firmware or provide serial logs.
4. If `F1_SETUP_FAILED` appears, preserve the complete monitor output and the
   PlatformIO version. Do not replace ESP8266-specific APIs with ESP32 examples.
5. If the second run also fails, stop and record the evidence. The next change
   must be based on that evidence, not a guessed timing value.
