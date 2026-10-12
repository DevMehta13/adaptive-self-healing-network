# Architecture

This document separates the agreed system shape from mechanisms that still need implementation and hardware validation. Mechanisms are labelled by phase: those marked as decisions or implemented run only in the simulator today; nothing has run on hardware. The approved phases and technical defaults in [plan.md](../plan.md) govern upcoming work; implementation is awaiting the owner's instruction.

## Connections and responsibilities

Four ESP8266 NodeMCU boards form the permitted wireless links A–B, B–C, A–D, and D–C. The links must support communication in both directions for probes, acknowledgements, and management traffic. Each board needs power. C connects through a USB data cable to the laptop; a future sensor connects to A's pins using the wiring appropriate to the selected sensor.

The current communication proposal is **ESP-NOW** between boards and USB serial between C and the laptop. ESP-NOW carries individual device-to-device frames; our firmware must implement the forwarding and routing behavior. The laptop communicates through C, not directly through its own Wi-Fi radio. A router and internet connection are not required for this design.

ESP8266-specific SDK behavior must be checked. ESP32 examples and callback signatures are not automatically compatible with ESP8266. Choose and pin the firmware toolchain after a minimal real-board communication test.

| Component | Responsibility |
| --- | --- |
| All nodes | Identify themselves, send HELLOs, track permitted neighbors, handle bounded messages and report telemetry |
| A | Produce readings and track delivery acknowledgements |
| B and D | Forward data and management messages |
| C | Receive readings and bridge messages between serial and radio |
| Laptop controller | Maintain fresh topology, calculate routes, coordinate route updates, record events |
| Browser dashboard | Display controller state and observed events; never invent delivery evidence |

Only one laptop controls the physical network at a time. Either contributor's computer should eventually be able to fill that role.

## Two kinds of traffic

**Data traffic** is the reading being delivered: A → relay → C. Nodes use the controller's selected route to choose the next hop.

**Control traffic** keeps the network working: HELLOs, neighbor reports, probes, route instructions, and installation acknowledgements. This traffic must remain deliverable when the previously selected data route breaks.

The proposed control mechanism is bounded flooding over all currently permitted surviving links. A node forwards each new management message once, with a hop limit and a bounded duplicate cache. This is independent of the installed data route. After B fails, a report or route instruction can still travel through C–D–A. HELLOs themselves remain local neighbor messages, not multi-hop flooded heartbeats.

This mechanism is now implemented in simulation (Phase 3); the byte-level payloads and rules are in [PROTOCOL.md](PROTOCOL.md) and await Dev's review. Control traffic obeys the same logical link permissions as data and does not use A–C as a shortcut.

Decisions recorded in Phase 3:

- **HELLO is a unicast to each permitted neighbor** every second with small seeded jitter, rather than one broadcast. This enforces the logical topology at the sender and matches ESP-NOW's unicast peers; a broadcast would be heard by non-neighbors that must then discard it.
- **Two-way needs the neighbor's list.** A node marks a neighbor `two_way` only when that neighbor's HELLO lists it with its current session; hearing a HELLO alone is `one_way`. Loss of contact is declared 3 s after the last HELLO.
- **Delivery acknowledgements are management frames** and are flooded, so they reach the source even when the data route's relay is dead (plan.md). The data route still uses the routing tables.
- **Flooding rule:** each node handles a management frame once (bounded identity cache), forwards to permitted neighbors except the sender, drops its own frames and expired TTLs. TTL, not the cache, is what finally bounds a flood.
- **The controller sees the network only through a gateway interface** (events, clock, timers) implemented by `SimulatedGateway` now and a serial gateway later; a test shows the controller packages do not import the simulator.
- **"Lost contact", never "powered off".** The controller declares a node lost only when every neighbor with a fresh report lists it as lost, each report newer than the node's own latest report, and records the observers as the source. One neighbor's claim is not enough: a one-way link failure is seen by only one end, and the node is still heard elsewhere.
- **Reports expire.** A report is fresh for 15 s (three report intervals); after that nodes and links are `stale`, and only `up` links with fresh reports from both ends are usable.

## Startup and failure workflow

1. Powered nodes announce their identity to permitted neighbors. Reports reach C and the laptop through the control mechanism before data routes exist.
2. The laptop builds a fresh graph and calculates a route to C.
3. The controller distributes a versioned route update, including a backup next hop where one exists, and obtains evidence that the required primary and backup forwarding entries are installed before treating that route as active.
4. A sends numbered readings; each relay records forwarding, and C acknowledges application receipt.
5. When B stops responding, neighboring nodes time out their observations. A switches locally to its pre-installed backup, D, and reports reach the laptop through surviving control links.
6. The controller excludes unavailable links, computes the best remaining route (here A → D → C), and confirms A's local choice or installs a replacement route version.
7. Data resumes and observed forwarding confirms the change. When B returns, fresh reports make it eligible again.

Route-update ordering, acknowledgement retries, old/new version handling, and behavior during partial installation are specified and implemented in simulation for Phase 4a (primary routes only; the backup next hop and local failover of steps 3 and 5 are Phase 4b). The decisions are listed under *Topology and routing*. TTL limits loops but does not by itself make route updates consistent; versions and epochs do.

## Local failover and controller loss

Approved by both owners on 2026-10-11. Backup next hops and local failover are implemented in simulation (Phase 4b); controller-loss buffering and replay (C's ring buffer, the controller heartbeat) are not (Phase 5). Phases and defaults are in [plan.md](../plan.md).

The controller calculates routes, but surviving a single relay failure should not depend on reaching it. Two mechanisms reduce that dependence.

**Backup next hop (local fast-failover).** Each route version installs a primary and a backup next hop, both computed by the controller: for A, primary B and backup D. The backup relay's forwarding entry is installed and confirmed before the version becomes active. When a node's primary neighbor goes stale (HELLO timeout), it switches to the backup without waiting for the controller and reports the switch through management flooding. This follows the same idea as IP and MPLS fast reroute: compute centrally, react locally.

- Nodes use only confirmed, controller-installed backups; they never calculate routes.
- A backup must be loop-free: it must not forward back through the node using it. The controller checks this before installing it. In the diamond, D reaches C directly.
- Returning to the primary is a controller decision under the active policy, which prevents flapping.
- A HELLO timeout cannot distinguish a powered-off relay from a broken link, so the switch is reported as loss of contact.
- If the backup is also unavailable, the node enters the no-route state.

**Fail-static forwarding and buffering at C.** The controller sends C a periodic heartbeat over the gateway interface. When the heartbeat stops, C marks the controller unreachable. Nodes keep forwarding on their last confirmed routes and backups, but no new routes are calculated. C continues to acknowledge readings, because those acknowledgements come from C rather than the laptop. C stores readings and node events in a bounded ring buffer, dropping and counting the oldest entry when full. On reconnection, C replays the buffer; the controller counts each reading once by source, session and sequence, and the dashboard labels replayed readings as delayed.

- C is powered by the same USB cable that connects it to the laptop, so unplugging it removes C entirely. Controller loss is tested by stopping the controller software. The buffer is RAM-only and is lost if C loses power.
- Buffer size depends on ESP8266 free heap, measured in Phase 6.
- Pre-installed backups cover only the failures the last route version anticipated. A second failure while the controller is unreachable may leave no route.

## Topology and routing

Store freshness and measurements for each neighbor observation. Hearing a HELLO proves recent contact in that direction, not automatically that the reverse direction works. Confirm usable directions before presenting a bidirectional link.

Use Dijkstra with non-negative costs. The first measured cost proposal is per-link round-trip probe time; describe it as RTT cost, not synchronized one-way latency. Missing measurements must not become zero-cost links. Specify expiry, tie-breaking, and route-change rules before implementing adaptive decisions.

Decisions recorded in Phase 4a (simulation; pending Dev's review where they touch the protocol):

- **Link cost is round-trip probe time measured by the prober on its own clock.** PROBE and PROBE_REPLY are one-hop frames that carry no timestamps, so no node ever subtracts another node's clock. Each node keeps a window of 5 recent samples per neighbor and reports their median; fewer than 3 fresh samples (older than 10 s are forgotten) means *unmeasured*. EWMA smoothing stays in Phase 9.
- **A link's routing cost is the larger of its two ends' measurements, and both must exist.** Both ends measure the same round trip independently; taking the larger never makes a link look better than either end saw it. A link with a missing end is left out of the graph. It is never given a default or zero cost.
- **The graph contains only links that are up (two-way, both reports fresh) and measured.** One-way, stale, down and unmeasured links are excluded. Lost-contact nodes have no usable links.
- **Dijkstra over summed link costs, with a documented tie-break:** equal cost, then fewer hops, then the lexicographically smaller list of node ids (B before D). The tie-break is a rule for repeatability, not a statement that B is better.
- **Recalculation is triggered by changes in the *set* of usable links or by a restarted node on the route, never by RTT values alone.** Measurement noise therefore cannot flip the route. (Policy-driven switching on RTT, with a three-evaluation rule, is Phase 9.) Before the first route the controller waits until no new link has appeared for 1 s, because links are measured one at a time at startup and the first path found is often replaced milliseconds later.
- **A link that returns is held down for 5 s (configurable) before routes may use it.** A link that comes and goes would otherwise move the route at every flap, and each move costs installation traffic and a window in which packets can be lost. Waiting until the link has been stable for the hold-down means switching back only on evidence that it is staying up. Failures are never delayed: a lost link leaves the graph immediately. A link seen for the first time is not held down.
- **Relays are installed first; the source is told only after every relay has acknowledged; a route is active only after the source acknowledges too.** Until the source is switched nothing is sent toward a new relay, so installing relays cannot cause loss. The reverse order creates a black hole.
- **Route state at the controller:** computed → installing → active, or failed (a node refused it), unconfirmed (acknowledgements did not arrive within the bounded retries), superseded (a newer plan replaced it). Retries are bounded per update (3 transmissions, 1 s apart), re-planning after a failure is bounded (3 times in a row), and a topology change starts afresh.
- **Updates travel by management flooding and are originated by C.** The laptop never transmits on the radio; C creates the frame (its own `src`), and nodes accept route updates only when `src` is the gateway.
- **Epoch plus version.** Nodes ignore any update older than the `(epoch, version)` pair they hold and accept a newer epoch regardless of version, so a restarted controller (new epoch, versions from 1) is never stuck behind old numbers, and a late duplicate can never undo a newer route. A node that refuses an update says which pair it holds, so a controller started with a too-small epoch advances itself.
- **"No available route" is explicit.** The controller says so and sends the source a ROUTE_UPDATE with next hop none. The source then drops new readings immediately (`no_route`, counted) instead of queueing and retrying. If the source cannot be reached (both relays dead, so nothing leaves C) the announcement is retried a bounded number of times and ends `unconfirmed`; the controller does not claim the source knows. Making the source *discover* this itself is a node-side local decision and belongs with Phase 4b.
- **Route computation time and distribution time are different quantities.** Computation is wall-clock CPU on the laptop (microseconds, Python); distribution is simulated milliseconds from "computed" to "every node confirmed". They are never added, and neither is a hardware result.

Decisions recorded in Phase 4b (simulation; pending Dev's review where they touch the protocol):

- **Compute centrally, react locally.** The controller computes primary and backup next hops with global knowledge; each node merely follows the installed table and switches to the backup when its next hop fails. This is the idea of IP fast reroute and MPLS fast reroute: the expensive, global computation is not on the failure path, and the reaction is a table lookup.
- **Backup rule: loop-free and node-protecting.** For node N with primary next hop P, a neighbor n is a valid backup only if n's own shortest path to the destination passes through neither N nor P. In the diamond A's backup is D and B and D have none. "Loop-free" alone (not through N) is not enough: a backup that routes through P fails together with P.
- **Backup entries are installed and confirmed first.** Order: backup relays, primary relays, source. A route is active only when the primary path and all backup entries are acknowledged.
- **Two triggers, measured separately.** T1: the next hop is no longer two-way (lost or one-way). T2: K consecutive link-layer failures to it. T1 must test two-way, not a timeout: in a one-way A→B failure A keeps hearing B and a timeout never fires.
- **The link-layer callback is a next-hop signal.** It can say "failed" for a frame that arrived (the acknowledgement was lost) and "ok" for a frame the relay then drops. It is never end-to-end confirmation. Real ESP8266 behaviour is unverified until Phase 6.
- **No local switch-back.** Only a newer controller version ends a failover; the controller decides by policy and the existing hold-down. A node that re-receives a dead primary abandons it again immediately, so the controller cannot fight the failover.
- **The controller treats a failover report as a distinct recovery.** It records `local_failover`, holds the abandoned link down for the hold-down (as if it had left), and installs the resulting route as a new version: the backup path promoted to primary, with a new backup if one exists.
- **Expired information is not a failure.** A link whose reports have merely expired keeps its last known cost and is not treated as having left, and no route is withdrawn because the controller stopped hearing anything. Otherwise a detached controller would announce "no route" on its own blindness (found by the failover-without-controller scenario). A link the view says is down or one-way still leaves the graph.
- **Resolved open item (local failover trigger).** Both triggers are kept; T2 is the faster, T1 the safe net. Measured in simulation (below and in STATUS.md): T1 alone gains little over the controller because both wait for the same 3 s neighbor timeout; T1 + T2 switches in about 0.1 to 1 s. T2 produces false failovers under loss; K = 3 is the default compromise.

Phase 4b limitations: the controller computes backups for the one configured flow (A to C); a failure of the *destination's* only gateway links cannot be protected; and a node cut off from every neighbor cannot report at all.

B is the normal relay for the scripted demonstration, not a permanently superior route. Equal or fluctuating measurements may favor D. A repeatable demo may need a declared tie-break rule or explicitly labeled test costs/delays; do not falsify measured latency to force B. Returning B becomes available again and is selected only if the policy prefers it.

Nodes cannot reliably distinguish an unplugged device from an unreachable radio link using a timeout alone. The dashboard should present loss of contact and its observation source; the demonstrator knows B was unplugged.

## Packet and reliability requirements

Proposed message families: HELLO, neighbor/topology report, route update, route-install acknowledgement, sensor data, delivery acknowledgement, probe request/response, and local-failover report. A controller heartbeat travels only over the gateway interface between the laptop and C. The radio frame is a compact binary format with a fixed 14-byte little-endian header (decision made in Phase 1, chosen over JSON for the 250-byte ESP-NOW limit and ESP8266 memory); the draft layout, decoding rules and reference examples are in [PROTOCOL.md](PROTOCOL.md) and are pending Dev's review. Every message family above now has a payload definition (LOCAL_FAILOVER_REPORT is defined but not yet sent; that is Phase 4b). Forwarding, TTL, retry and duplicate rules (Phase 2) are in the same document and add no header fields.

Protocol design must include a version, type, source/destination, restart/session identity, sequence number, hop limit, route generation where applicable, and a bounded payload. Do not assume a sequence number stays unique after a node restarts.

- A final application acknowledgement means C accepted a reading. Radio send success alone does not prove end-to-end delivery.
- Retries and queues are bounded so a disconnected network does not exhaust node memory.
- Duplicate readings must not be counted twice. A duplicate caused by a lost acknowledgement should be acknowledged again when appropriate. *Decision (Phase 2):* the destination keeps, per (source, session), the highest sequence number and a 64-bit sliding-window bitmap, compared with wrap-safe arithmetic, for at most 8 sessions; a duplicate is counted once and re-acknowledged, and a number older than the window is refused without an ACK.
- *Decision (Phase 2):* retries are end to end from the origin, reuse the identical frame (same source, session and sequence), and stop after 3 transmissions. Relays never acknowledge or retry.
- *Decision (Phase 2):* TTL is checked by forwarders only, before the route lookup: a frame arriving with TTL 1 or 0 is dropped, otherwise it is forwarded with TTL minus one. The destination does not check TTL.
- *Decision (Phase 2):* the logical topology is enforced by each node's permitted-neighbor list (no sending to, and rejection of frames from, non-permitted neighbors), not by the absence of a link, so a misconfigured node cannot bypass a relay even though the radios can hear each other.
- TTL is decremented on forwarding; exhausted packets are dropped and counted.
- Clearly define what happens when no route exists: report the condition and use a documented bounded drop/buffer policy.

## Measurement rules

| Measurement | Meaning and constraint |
| --- | --- |
| Delivered readings | Unique readings accepted at C, identified by source, session, and sequence |
| Packet loss | Generated readings not received within a stated evaluation window; distinguish pending readings, retries, and duplicates |
| RTT | Time from a probe/send to its response on the same device's clock; label per-link versus end-to-end |
| RSSI | Received signal strength for the actual peer observation, only if the ESP8266 implementation can obtain it reliably |
| Failure detection | Time until loss of contact is declared; exact physical unplug time needs a recorded reference |
| Route computation | Time spent calculating a route, separate from distributing it. Simulation: wall-clock CPU time of the Dijkstra call on the laptop. Not simulated time, so it is reported separately from every other row |
| Service recovery | Time from a documented failure reference to the first confirmed restored delivery |
| Local failover time | Time from declared loss of the primary neighbor to the first confirmed delivery through the backup; reported separately from controller-driven recovery. Simulation (Phase 4b) reports: when the source's table switched to the backup, when it first sent a reading that way, the delivery gap at C, readings lost, and when the controller's route was confirmed, for three modes (controller-only, T1, T1 + T2) |
| Delivery gap | Time between the last receipt before interruption and the first receipt afterward |

Do not subtract timestamps from unsynchronized boards to claim one-way latency. Do not use Wi-Fi access-point RSSI as if it were peer ESP-NOW RSSI. If peer RSSI is unavailable, show unavailable and record the limitation.

The laptop can timestamp observations consistently, but those include reporting delays. Label recovery measured from failure declaration differently from recovery measured from an instrumented physical cut. Example timing values in earlier brainstorming were illustrative, not measured results.

## Dashboard and simulator

The intended dashboard shows node reachability, permitted and observed links, calculated and confirmed routes, sensor values, packet counts, RTT, available RSSI, history charts, and a failure/recovery timeline. Planned failure controls must label software-injected events separately from real disconnections. Recovery events state whether they came from local failover or a controller reroute, and replayed readings are labeled as delayed. Show telemetry freshness so stale state does not look live.

A deterministic simulator exists for four virtual nodes (a virtual clock, seeded links, and nodes that exchange encoded protocol frames), with a gateway interface and a controller that reuse the same protocol module. Since Phase 5a the simulation also runs live inside the backend and is served to the dashboard. Simulation cannot validate ESP8266 radio performance, physical RSSI, power stability, or USB drivers. The approved transition to hardware follows the working simulated demo in Phase 5, before dashboard polish and adaptive routing.

Decisions recorded in Phase 5a (simulation; pending Dev's review where they touch the protocol or the API):

- **A discrete-event simulation becomes live by pacing, not by changing it.** The engine keeps its virtual clock and its queue of future events. A task wakes every 50 ms, multiplies the elapsed real time by a speed factor, and asks the engine to run until its clock has moved by that much. The engine stays deterministic; only the moments slices are requested depend on the wall clock. Tests ask for slices directly (a manual `step`), so they are fast and repeatable. If the laptop cannot keep up, simulated time falls behind instead of spiralling (at most 5 s of simulated time per wake-up).
- **Three parts, and the dashboard sees only two of them.** The simulated environment (nodes, links) is ground truth. The gateway on C is the only channel out of it. The controller learns only through the gateway. The dashboard model consumes the gateway and the controller's own outputs and never reads node, link or trace state. A test inspects the runtime for any such access.
- **The dashboard shows what the controller knows, not what is true.** After "Stop B" the snapshot still says B is reachable until the network notices; that delay is part of the system being demonstrated. Showing ground truth would make every detection delay look instant and every failure look known.
- **Observed paths come from evidence C has on hardware too:** the link-layer sender and the TTL left. The planned route and the observed path are different objects. A controller that has stopped reacting still shows readings arriving via D while its plan says B, and a test freezes the controller to prove it.
- **Facts only a node holds travel in TELEMETRY.** Created, acknowledged, lost and dropped-for-no-route counts exist only at A. They arrive by flooding every ~5 s and are shown with their age, so "pending" is an estimate with a stated freshness, not a live gauge.
- **Presenter actions go on a separate environment channel.** They are not part of the controller's knowledge, and they are what a viewer needs next to the network's reactions to follow the story ("we stopped B at 12.0 s; A noticed at 13.0 s; the controller at 14.5 s"). Mixing them into node status would present the presenter's knowledge as the system's.
- **Controls change only the environment.** No control calls the controller or the topology view; a test compares the controller's state and the commands it has sent before and after each one. State-changing endpoints require a JSON body (a cross-site form cannot send one), reject a foreign `Origin`, and the server binds to localhost.
- **Everything the live process holds is bounded:** the state-changing event history (2000), recent readings (50), the simulator trace (5000), the controller and view event logs (1000 each), and the pending-work queue. A long accelerated run test checks each of them. The check found a real unbounded structure, a duplicate expiry timer re-armed by every timer in the topology view (about 44 pending entries per simulated minute); it is fixed.

Limitations of Phase 5a: while the gateway is detached the dashboard also sees nothing and C buffers nothing (Phase 5b); presenter controls act on the single live run (no per-client sessions); and there is no persistence, so a backend restart starts a new run.

## Selected stack, not installed dependencies

- Firmware: C++ with the Arduino framework through PlatformIO.
- Controller and simulator: Python; FastAPI/WebSockets for browser communication.
- Storage: SQLite.
- Dashboard: React, TypeScript, Vite, and Cytoscape.js.

Avoid creating package files, selecting versions, or claiming run commands until the relevant implementation task is authorized. Phase 6 validates radio/serial hardware assumptions before four-board integration, adaptive routing, and final polish.

## Boundaries

- A surviving route to C and a working controller are required for centrally computed healing. Local failover needs only a confirmed, pre-installed backup and a surviving path through it.
- C is a single point of failure in this version because it is the only gateway. The laptop is required to calculate new routes, store data, and run the dashboard. During controller loss, nodes keep forwarding, can fail over only to pre-installed backups, and C buffers a bounded number of readings. Dual gateways, a Wi-Fi backup link to the laptop, a standby controller, and distributed routing are possible future work.
- Failure of A removes the data source. Failure of both B and D partitions A from C.
- Delivery may pause and readings may be lost during recovery. Unlimited buffering and zero-loss guarantees are not in scope.
- Logical link filtering reproduces a topology; it does not demonstrate physical radio isolation or security.
- This is a laboratory prototype. Authentication/encryption decisions remain open; production security is not claimed.

## Primary references

- [Espressif ESP-NOW user guide](https://www.espressif.com/sites/default/files/documentation/esp-now_user_guide_en.pdf)
- [ESP8266 Non-OS SDK API reference](https://www.espressif.com/sites/default/files/documentation/2c-esp8266_non_os_sdk_api_reference_en.pdf)
- [PlatformIO installation documentation](https://docs.platformio.org/en/latest/core/installation/)

These references support tool and transport investigation, not claims that this project's proposed implementation has been validated.
