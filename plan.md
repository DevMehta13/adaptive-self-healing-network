# Software-first plan for the Adaptive Self-Healing IoT Network

**Approved plan. Implementation has not started.** The owner authorized saving this plan only and will explicitly say when to begin. All phases below are pending. This file is the canonical phased plan; do not create a separate roadmap unless requested.

## 1. What we will finish

Build a working simulation first, connect four real ESP8266 NodeMCU boards next, then add the sensor, adaptive routing, and presentation polish.

The finished project will demonstrate:

- Sensor data travelling through A → B → C.
- Automatic recovery through A → D → C after B fails.
- A dashboard showing actual delivery, routes, failures, and recovery measurements.
- A comparison between routes selected for low delay and high reliability.
- The same controller and dashboard working with either simulated nodes or real hardware.

A is the source, B and D are relays, and C is the destination and USB gateway to the laptop. Each phase ends with a working demonstration and recorded checks. We move forward when its checks pass, without waiting for a fixed calendar deadline.

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
firmware/         ESP8266 node software
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

PlatformIO provides an ESP8266 NodeMCU target, and Cytoscape.js provides network graph visualization. See [PlatformIO documentation](https://docs.platformio.org/en/latest/boards/espressif8266/nodemcuv2.html) and [Cytoscape.js documentation](https://js.cytoscape.org/index.html).

The controller must receive information through a gateway interface in both modes. It must not inspect the simulator's private state to learn that a node failed.

## 3. Phases and completion checkpoints

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

Use measured probe RTT as the initial route cost. For repeatable simulation, configure lower link delays through B and label them as simulated settings.

**Complete when:** A → B → C automatically changes to A → D → C without manually selecting D. Removing both relays produces “No available route.”

**Learn:** How the controller chooses a route and safely communicates that choice.

### Phase 5 — Usable simulation dashboard

**Build:**

- Live topology with node reachability and link freshness.
- Separate calculated route and observed packet path.
- Sensor readings, delivered/pending/lost counts, RTT, and event history.
- Controls to stop/restart virtual nodes and change simulated links.
- A recovery timeline showing detection, route installation, and restored delivery.

Animations must come from packet events. Clicking “Stop B” changes the simulated environment; it must not directly tell the controller to reroute.

**Complete when:** Either contributor can perform the complete simulated unplug-and-recover demonstration from the browser.

**This is the hardware transition checkpoint.** The software works end to end; visual polish can wait.

### Phase 6 — Hardware purchase and two-board validation

**Build:** The first ESP8266 firmware and a two-board communication experiment.

Verify programming, node identity, ESP-NOW delivery, acknowledgements, and USB serial. Use one firmware codebase with per-node configuration.

Compare encoded firmware packets against the reference examples from Phase 1. Keep radio packets within the ESP-NOW v1 limit of 250 bytes. See [Espressif documentation](https://docs.espressif.com/projects/esp-faq/en/latest/application-solution/esp-now.html).

**Complete when:** Two real boards exchange verified packets and C's gateway connection works with the laptop.

Record macOS and Windows checks separately. Verify peer RSSI feasibility here; show unavailable if reliable collection is unsupported.

### Phase 7 — Four-board self-healing network

**Build:** Port the tested node behavior to all four boards: forwarding, discovery, management traffic, route installation, retries, and telemetry.

Connect C through USB and switch the controller's gateway connection from simulation to serial. Keep the controller and dashboard behavior shared.

**Complete when:** Physically unplugging the active relay restores delivery through the alternative relay. Packet evidence confirms both paths, and no prohibited direct link is used.

Repeat the test at least 10 times and record delivery gaps, loss, and restoration results. Exact recovery targets will follow these measurements.

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

Require a candidate route to remain better for three evaluations before switching. A confirmed broken route bypasses that waiting period.

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
- **Gateway interface:** Send management commands and receive node events through either simulation or USB serial.
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
| C or controller fails | Limitation is reported; recovery is not falsely claimed |
| Repeated identical simulation | Reproducible event sequence and results |

Use automated macOS/Windows checks for software and real manual checks for USB, radio, and sensor behavior.

## 5. Assumptions and working rules

- Four ESP8266 NodeMCU boards remain the hardware choice.
- The preferred ₹800 budget and below-₹1,000 target remain constraints; verify the complete purchase total before ordering.
- Simulation validates protocol behavior, not radio range, RSSI, power quality, or hardware recovery time.
- A short delivery interruption and some loss are acceptable; there is no zero-loss promise.
- Either relay may be preferred under real measurements. A scripted B-first hardware demo must label any configured cost or injected delay.
- Before splitting development between contributors, agree on the interface involved. Each contribution includes relevant tests, a simple explanation, and an updated status handoff.
- At the owner's request, keep the approved phases here in `plan.md` rather than creating a separate `docs/ROADMAP.md`.
- Plan approval and documentation publication do not authorize implementation. Wait for the owner's explicit instruction to start.
