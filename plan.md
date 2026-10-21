# Software-first plan for the Adaptive Self-Healing IoT Network

**Approved plan. Implementation is authorized one step at a time by the owners; see [docs/STATUS.md](docs/STATUS.md) for what exists and what is authorized next.** This file is the canonical phased plan; do not create a separate roadmap unless requested.

**Amendment, 2026-10-11:** Both owners approved two resilience additions: controller-computed backup next hops with local fast-failover, and fail-static forwarding with bounded buffering at C while the controller is unreachable. They are folded into Phases 4, 5, 6, 7 and 9, the interfaces, and the test scenarios below. This amendment changes the plan only; it does not authorize implementation.

**Amendment, 2026-10-19 — software-ready before lab:** The owners approved completing the remaining firmware, host-side tests, node configurations, visual build reference, and lab runbook before the first hardware session. The lab session is for assembly, flashing, measurement and targeted compatibility fixes—not for designing the system. This changes the sequencing of validation, not the ESP-NOW protocol (the target board was later changed from ESP8266 to ESP32-S; see the 2026-10-21 note in Section 5), topology, or requirement for real-board evidence. A laptop simulator may demonstrate intended logic and a circuit diagram may show physical assembly; neither proves ESP-NOW delivery, callback behaviour, radio range, USB-reset behaviour, or free heap on an ESP32-S board.

## 1. What we will finish

Build the full simulated system and the complete pre-lab firmware package first. Then use one planned hardware-validation session with four ESP32-S NodeMCU-style boards to assemble, flash, measure and correct any board-specific compatibility issues. Add the selected sensor and complete the presentation handoff after the core network works on hardware.

The finished project will demonstrate:

- Sensor data travelling through A → B → C.
- Automatic recovery through A → D → C after B fails.
- Local failover to a pre-installed backup relay without waiting for the laptop, and bounded buffering at C while the controller is unreachable.
- A dashboard showing actual delivery, routes, failures, and recovery measurements.
- A comparison between routes selected for low delay and high reliability.
- The same controller and dashboard working with either simulated nodes or real hardware.

A is the source, B and D are relays, and C is the destination and USB gateway to the laptop. Every firmware increment has two gates: a **software-ready** gate (code, host tests, target compilation and simulator evidence) and a **hardware-validated** gate (real-board measurement). We do not claim a hardware result until its latter gate passes.

## 2. Project scaffolding and technical choices

Use one repository with these responsibilities:

```text
backend/
  protocol/       Shared message definitions and validation
  controller/     Topology, route calculation, metrics
  simulator/      Virtual nodes and simulated wireless links
  transports/     Simulator and USB gateway connections
  api/            Dashboard communication
  storage/        Experiment history

dashboard/        Browser interface
firmware/         ESP32-S node software
scenarios/        Repeatable simulated experiments
tests/           Automated checks and shared protocol examples
docs/            Explanations, setup, decisions, and progress
```

Create folders only when their phase introduces working content. This tree is a plan, not the current repository structure.

| Part | Choice |
| --- | --- |
| Controller and simulator | Python |
| Dashboard connection | FastAPI with WebSockets |
| Dashboard | React, TypeScript, Vite, Cytoscape.js |
| Local storage | SQLite |
| Firmware | C++ with Arduino framework through PlatformIO |
| Wireless communication | ESP-NOW |
| Gateway connection | USB serial |
| Testing | pytest, frontend tests, and firmware protocol checks |
| Supported computers | macOS and Windows |

PlatformIO provides the ESP32 `esp32dev` target, and Cytoscape.js provides network graph visualization. See [PlatformIO documentation](https://docs.platformio.org/en/latest/boards/espressif32/esp32dev.html) and [Cytoscape.js documentation](https://js.cytoscape.org/index.html).

The controller must receive information through a gateway interface in both modes. It must not inspect the simulator's private state to learn that a node failed.

## 3. Phases and completion checkpoints

### Dev's implementation increments (approved 2026-10-19)

Dev approved proceeding one increment at a time, with an explanation before each starts and a result/verification handoff afterward. These **12 increments** divide the remaining work within the original phases; they do not replace Phases 0–10 or authorize edits to Harshal's backend, scenarios, or shared test data. Under the software-ready-before-lab amendment, F1 through F3c are implemented in order without waiting for a board between them. Their real-board checks are executed together in the lab-validation session. Hardware-dependent contract decisions remain subject to measurements.

| Increment | Scope | Software-ready checkpoint | Hardware-validation checkpoint |
| --- | --- | --- | --- |
| D0 | Dashboard shell: React/TypeScript/Vite, mode label, backend connection and reconnect | Starts from documented commands; connected/stale/disconnected and snapshot resynchronization verified. Phase 0 still requires both owners' OS checks. | No board required. |
| D1 | Fixture-based network graph, readings, counts and timeline | Both recorded runs render evidence, freshness and replay correctly, including a mid-run snapshot. | No board required. |
| D2 | Live dashboard, presenter controls and policy selector | Browser-only failure/return/outage demos and reconnect/reset work against the backend (dashboard Phase 5). | Hardware-mode display is checked after C's serial bridge works. |
| F0 | Native C++ radio/serial codecs and validation in PlatformIO | All 79 radio examples and 48 serial cases checked against generated vectors; target configurations compile. | No radio claim; the codecs are exercised by F1 onward. |
| F1 | Two-board ESP-NOW DATA/ACK bench | Sender/receiver images compile; host tests cover frame and ACK matching; runbook names expected logs and pass/fail criteria. | Two boards deliver 100 matched DATA/ACK exchanges; sender metadata, callback counts and free heap are recorded. |
| F2 | C's bounded serial bridge, heartbeat and reliable uplink | Bridge logic has host tests, a fake-C/integration run, target compilation and documented reset/replay/load cases. | Real-board serial probe and backend parsing pass; reset-on-open, replay and load are measured. |
| F3a | Data forwarding, TTL, neighbors, bounded queues/retries and deduplication | Node engine is exercised against a deterministic mock radio for bypass, loop, ACK-loss, duplicate and restart cases; all four roles compile. | Four boards show multi-hop delivery and pass the same cases where physically practical. |
| F3b | Discovery, management flooding, probes/statistics and telemetry | Deterministic tests prove topology reports and the backend's representation of one-way/stale evidence. | Four boards report topology through real permitted links; observed timing and heap are recorded. |
| F3c | Route installation, epochs/versions, backups and local failover | Tests prove versioned installation, local backup selection, controller-loss buffering and replay through simulated radio/serial transports. | Controller installs confirmed routes; physical local/controller recovery and outage replay have distinct evidence. |
| F4a | Four-board evaluation | Lab script, result sheet and dashboard views are prepared; no simulated result is relabelled as hardware. | At least 10 unplug trials plus partition, restart, outage and soak checks are recorded (Phase 7). |
| F4b | Sensor on A with explicit generated-reading mode | Generated-reading mode and a sensor adapter are tested; exact firmware and wiring follow selection from the lab's available, 3.3 V-compatible sensors. | The chosen sensor produces real readings on both paths; sensor failure leaves networking running (Phase 8). |
| D3 | Dashboard polish, history/CSV, charts and offline handoff | Fixture-backed history, exports, charts, offline setup and presenter guide are complete. | Hardware run data renders correctly; both owners reproduce and explain the demonstration. |

Before each increment, explain the goal, prerequisites, expected outcome, networking/embedded concepts and completion checks in plain language. Commit/push small contributions on `dev-work` and open PRs to `main` for review; do not merge them automatically.

### Pre-lab package and lab-validation session

Before the hardware visit, finish these reusable artifacts:

- One firmware codebase with explicit A, B, C and D build configurations; no device MAC address or serial path is committed.
- Host-side deterministic tests using mock radio and serial transports for every node rule that does not depend on physical radio behaviour.
- The existing laptop simulator demonstrating normal delivery, B failure, recovery through D, controller loss and replay.
- A visual four-board power/USB layout and, after choosing the sensor, its exact A wiring diagram. The visual layout is an assembly reference, not an ESP-NOW simulation or proof of radio behaviour.
- A lab runbook: upload commands, how to discover each board's MAC and serial port, expected logs, pass/fail criteria, a result sheet, and a short recovery path for common upload or serial failures.

The first lab session has a narrow goal: transfer the prebuilt firmware to real boards, run F1 through F3c in the written order, measure callback behaviour and free heap, then make only evidence-based compatibility or timing adjustments. It is not the place to invent protocol behaviour, write untested features, or substitute a radio callback for an end-to-end acknowledgement. Select the sensor from parts available in the lab before F4b; its final wiring diagram and firmware then need to be completed and checked with that exact module.

### Phase 0 — Runnable foundation

**Build:** Minimal backend, dashboard shell, configuration, dependency locks, and automated checks. Provide documented commands for both operating systems.

Include a visible “Simulation” mode label and backend connection status.

**Complete when:** Both contributors can start the application from a fresh checkout and see the dashboard connected to the backend.

**Learn:** Which parts run on the laptop and how the browser receives updates.

### Phase 1 — Two virtual nodes exchange packets

**Build:** Virtual A and B, a simulated link, numbered readings, and an acknowledgement.

Introduce a shared packet contract covering message type, source, destination, restart identity, sequence number, TTL, and bounded payload. Store reference packet examples for later firmware compatibility checks.

The simulator supports repeatable results using a fixed random seed and controllable clock.

**Complete when:** A sends readings to B; delivery, acknowledgement, and a deliberately dropped packet are observable and tested.

**Learn:** Packets, delivery acknowledgements, and why sending does not guarantee receiving.

### Phase 2 — Four virtual nodes and real forwarding logic

**Build:** The A–B–C–D diamond with only the four permitted links: A–B, B–C, A–D, and D–C.

Start with manually configured routes to isolate forwarding from route calculation. Add TTL handling, duplicate detection, bounded queues, and limited retransmission.

The simulator supports node shutdown, directional link failure, delay, and packet loss.

**Complete when:** Packets travel hop by hop through B or D. A cannot bypass the relays, duplicates are counted once, and packets cannot circulate indefinitely.

**Learn:** Multi-hop forwarding and the difference between a link and a complete route.

### Phase 3 — Discovery and failure detection

**Build:** HELLO messages, neighbor observations, freshness timers, and topology reports.

Management reports travel through surviving permitted links using bounded flooding. This means forwarding a new management message to neighbors once, with duplicate suppression and a hop limit.

Start with configurable defaults:

- HELLO every 1 second.
- Loss of contact after 3 seconds without a HELLO.
- Sensor traffic every 2 seconds.
- Data TTL of 8 hops.
- At most 3 transmission attempts per reading.

These are initial experiment settings, not hardware performance guarantees.

**Complete when:** Turning virtual B off causes the controller to discover its loss through received reports and timeouts. B's return is also detected.

**Learn:** Heartbeats, timeouts, and why “unreachable” does not prove a device lost power.

### Phase 4 — Automatic routing and simulated healing

**Build:** A topology graph, Dijkstra routing, route versions, installation acknowledgements, and automatic recalculation.

Install relay entries before enabling the new route at A. Carry route updates through the independent management path so B's failure does not prevent instructions reaching A through D.

Each route version also carries a **backup next hop** computed by the controller (for A: primary B, backup D). The controller installs and confirms the backup relay's forwarding entry before the route version becomes active, so the backup can be used without further instructions. When a node's primary neighbor goes stale (HELLO timeout), the node switches to its backup locally and reports the switch through management flooding. Nodes never calculate routes. A backup must not lead back through the node using it, and a node does not switch back locally when its primary returns; the controller decides that by policy.

Use measured probe RTT as the initial route cost. For repeatable simulation, configure lower link delays through B and label them as simulated settings.

**Complete when:** A → B → C automatically changes to A → D → C without manually selecting D. Removing both relays produces “No available route.” When B stops, A moves to D through its pre-installed backup before the controller's replacement route arrives, and the controller then confirms or replaces that choice.

**Learn:** How the controller chooses a route and safely communicates that choice, and why reacting locally with a precomputed backup is faster than waiting for the controller.

### Phase 5 — Usable simulation dashboard

**Build:**

- Live topology with node reachability and link freshness.
- Separate calculated route and observed packet path.
- Sensor readings, delivered/pending/lost counts, RTT, and event history.
- Controls to stop/restart virtual nodes and change simulated links.
- A recovery timeline showing detection, route installation, and restored delivery.
- Recovery events labeled as either local failover or controller reroute.
- A simulated gateway-disconnect control. The controller sends C a heartbeat; after 3 seconds without one, C treats the controller as unreachable. Nodes keep their last confirmed routes and backups (fail-static), and C keeps readings and node events in a bounded ring buffer (initial default: 50 entries). When full, the oldest entry is dropped and counted. On reconnection, C replays the buffer; the controller counts each reading once by source, session and sequence, and the dashboard labels replayed readings as delayed.

Animations must come from packet events. Clicking “Stop B” changes the simulated environment; it must not directly tell the controller to reroute.

**Complete when:** Either contributor can perform the complete simulated unplug-and-recover demonstration from the browser, including B failing while the gateway is disconnected, followed by buffered replay on reconnection.

**This is the software-readiness checkpoint.** The software works end to end; the pre-lab firmware package and lab artifacts are completed next, then real boards validate the assumptions.

### Phase 6 — Pre-lab firmware completion and two-board validation

**Build:** Complete the ESP32-S node roles and C's serial bridge with host-side tests and target builds, then validate the first sender/receiver and C/laptop links on two real boards.

Verify programming, node identity, ESP-NOW delivery, acknowledgements, and USB serial. Use one firmware codebase with per-node configuration.

Compare encoded firmware packets against the reference examples from Phase 1. Keep radio packets within the ESP-NOW v1 limit of 250 bytes. See [Espressif documentation](https://docs.espressif.com/projects/esp-faq/en/latest/application-solution/esp-now.html).

**Software-ready when:** The F1 and F2 images, configuration instructions and fake-transport tests are complete; the full target build succeeds without a board.

**Hardware-validated when:** Two real boards exchange verified packets and C's gateway connection works with the laptop.

Record macOS and Windows checks separately. Verify peer RSSI feasibility here; show unavailable if reliable collection is unsupported. Measure free heap to size C's buffer and the node tables.

### Phase 7 — Four-board self-healing network and lab validation

**Build:** Complete and host-test the node behaviour before the lab, then run it on all four boards: forwarding, discovery, management traffic, route installation, backup next hops and local failover, fail-static behaviour with C's buffer, retries, and telemetry.

Connect C through USB and switch the controller's gateway connection from simulation to serial. Keep the controller and dashboard behavior shared.

**Complete when:** Physically unplugging the active relay restores delivery through the alternative relay. Packet evidence confirms both paths, and no prohibited direct link is used.

Repeat the test at least 10 times and record delivery gaps, loss, and restoration results. Exact recovery targets will follow these measurements.

Also stop the controller software, then unplug B. Do not unplug C's USB cable for this test, because it also powers C. Confirm local failover to D, bounded buffering at C, and replay without double counting once the controller restarts. Record local failover time separately from controller-driven recovery.

**Learn:** Which assumptions survived real radio timing and which required adjustment.

### Phase 8 — Add the real sensor

**Build:** Connect one inexpensive sensor to A and replace generated readings with actual measurements.

Keep generated readings available as an explicitly labeled test mode. Sensor failures must be reported without taking down forwarding or heartbeats.

The exact sensor and wiring are selected at purchase time; they do not change the network design.

**Complete when:** Real measurements arrive through both relay paths and continue after a relay failure.

### Phase 9 — Adaptive routing and policy comparison

**Build:** Two selectable policies:

- **Lowest delay:** Minimize the sum of smoothed link RTTs.
- **Highest reliability:** Maximize estimated path delivery probability using recent acknowledged link probes.

Use a rolling window of 20 completed probes for reliability and EWMA smoothing with an initial factor of 0.2 for RTT. Translate reliability into non-negative costs so Dijkstra remains usable.

Require a candidate route to remain better for three evaluations before switching. A confirmed broken route bypasses that waiting period. Local failover is not delayed by this rule; the controller then evaluates whether the backup should remain in use.

**Complete when:** A repeatable scenario with a fast-but-lossy path and a slower reliable path produces different route choices. Compare delivery rate, RTT, and route-change count under the same scenario.

RSSI remains an observed indicator where available; it is not required for this phase.

### Phase 10 — Final dashboard, experiments, and handoff

**Build:** Polish the graph, history charts, event timeline, experiment controls, and CSV export.

Package a local demonstration that runs without internet after installation. Provide setup instructions, an experiment procedure, and a short explanation of each networking concept for evaluation.

**Complete when:** Both contributors can run and explain the project, reproduce its main demonstrations, and distinguish measured results from simulation.

AI explanations, cloud hosting, and larger physical networks remain optional future work.

## 4. Interfaces and verification rules

Freeze these boundaries before building dependent components:

- **Node protocol:** Versioned, size-bounded messages with shared encoding examples for Python and C++.
- **Gateway interface:** Send management commands and receive node events through either simulation or USB serial. Include a periodic controller heartbeat so C can detect an unreachable controller and start buffering.
- **Serial interface:** Versioned newline-delimited JSON between C and the laptop; keep diagnostic output separate from structured messages.
- **Dashboard interface:** HTTP for snapshots and commands; WebSockets for ordered live events. Reconnecting fetches a fresh snapshot.
- **Experiment record:** Run identifier, simulation/hardware mode, configuration, packet identities, events, and metric definitions.

Delivery acknowledgements return through the bounded management mechanism initially. Report the resulting end-to-end timing as application RTT, including that acknowledgement path.

Required test scenarios across the phases:

| Scenario | Expected result |
| --- | --- |
| Normal delivery | Correct hop path and unique receipt at C |
| Active relay fails | Detection and restoration through a surviving path |
| Both relays fail | No-route state; bounded memory and retries |
| Relay restarts | Fresh session identity; no confusion with old packets |
| Acknowledgement is lost | Retry does not double-count the reading |
| One direction of a link fails | Link is not falsely treated as healthy both ways |
| Route update is lost or delayed | Unconfirmed route is not presented as installed |
| Dashboard or USB disconnects | Stale/disconnected state appears honestly |
| Active relay fails while the controller is unreachable | A switches locally to its confirmed backup; the switch is reported when the controller returns |
| Controller or gateway connection is lost | Nodes keep their last confirmed routes; C buffers within its bound and counts overflow |
| Buffered readings are replayed | Each reading is counted once and labeled delayed |
| Primary relay returns after local failover | No local switch-back; the controller decides by policy |
| C fails | Limitation is reported; recovery is not falsely claimed |
| Repeated identical simulation | Reproducible event sequence and results |

Use automated macOS/Windows checks for software and real manual checks for USB, radio, and sensor behavior.

## 5. Assumptions and working rules

- Four ESP32-S NodeMCU-style boards are the hardware choice (changed from ESP8266 on 2026-10-21 at the owner's direction; the sensor is a three-pin DHT11 on A's GPIO4).
- The preferred ₹800 budget and below-₹1,000 target remain constraints; verify the complete purchase total before ordering.
- Simulation validates protocol behavior, not radio range, RSSI, power quality, or hardware recovery time.
- A short delivery interruption and some loss are acceptable; there is no zero-loss promise.
- Either relay may be preferred under real measurements. A scripted B-first hardware demo must label any configured cost or injected delay.
- C is powered through its USB connection to the laptop, so unplugging that cable removes C entirely. C's buffer is RAM-only and does not survive power loss.
- C (the only gateway) and A (the only source) remain single points of failure. Dual gateways, a Wi-Fi backup link to the laptop, a standby controller, and distributed routing are possible future work, not planned phases.
- Before splitting development between contributors, agree on the interface involved. Each contribution includes relevant tests, a simple explanation, and an updated status handoff.
- At the owner's request, keep the approved phases here in `plan.md` rather than creating a separate `docs/ROADMAP.md`.
- Plan approval and documentation publication do not authorize implementation. Wait for the owner's explicit instruction to start.
