# Architecture

This document separates the agreed system shape from mechanisms that still need implementation and hardware validation. Nothing described here is currently running. The approved phases and technical defaults in [plan.md](../plan.md) govern upcoming work; implementation is awaiting the owner's instruction.

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

This is a design proposal, not a completed protocol. Before routing implementation, define message identities, restart handling, forwarding limits, stale-report expiry, and congestion limits. Control traffic must obey the same logical link permissions as data; it must not secretly use A–C as a shortcut.

## Startup and failure workflow

1. Powered nodes announce their identity to permitted neighbors. Reports reach C and the laptop through the control mechanism before data routes exist.
2. The laptop builds a fresh graph and calculates a route to C.
3. The controller distributes a versioned route update, including a backup next hop where one exists, and obtains evidence that the required primary and backup forwarding entries are installed before treating that route as active.
4. A sends numbered readings; each relay records forwarding, and C acknowledges application receipt.
5. When B stops responding, neighboring nodes time out their observations. A switches locally to its pre-installed backup, D, and reports reach the laptop through surviving control links.
6. The controller excludes unavailable links, computes the best remaining route (here A → D → C), and confirms A's local choice or installs a replacement route version.
7. Data resumes and observed forwarding confirms the change. When B returns, fresh reports make it eligible again.

Route-update ordering, acknowledgement retries, old/new version handling, and behavior during partial installation must be specified before implementation. TTL limits loops but does not by itself make route updates consistent.

## Local failover and controller loss

Approved by both owners on 2026-10-11 and not implemented. Phases and defaults are in [plan.md](../plan.md).

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

B is the normal relay for the scripted demonstration, not a permanently superior route. Equal or fluctuating measurements may favor D. A repeatable demo may need a declared tie-break rule or explicitly labeled test costs/delays; do not falsify measured latency to force B. Returning B becomes available again and is selected only if the policy prefers it.

Nodes cannot reliably distinguish an unplugged device from an unreachable radio link using a timeout alone. The dashboard should present loss of contact and its observation source; the demonstrator knows B was unplugged.

## Packet and reliability requirements

Proposed message families: HELLO, neighbor/topology report, route update, route-install acknowledgement, sensor data, delivery acknowledgement, probe request/response, and local-failover report. A controller heartbeat travels only over the gateway interface between the laptop and C. The radio frame is a compact binary format with a fixed 14-byte little-endian header (decision made in Phase 1, chosen over JSON for the 250-byte ESP-NOW limit and ESP8266 memory); the draft layout, decoding rules and reference examples are in [PROTOCOL.md](PROTOCOL.md) and are pending Dev's review. Only DATA and DELIVERY_ACK are specified so far; the other message families are reserved codes. Forwarding, TTL, retry and duplicate rules (Phase 2) are in the same document and add no header fields.

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
| Route computation | Time spent calculating a route, separate from distributing it |
| Service recovery | Time from a documented failure reference to the first confirmed restored delivery |
| Local failover time | Time from declared loss of the primary neighbor to the first confirmed delivery through the backup; reported separately from controller-driven recovery |
| Delivery gap | Time between the last receipt before interruption and the first receipt afterward |

Do not subtract timestamps from unsynchronized boards to claim one-way latency. Do not use Wi-Fi access-point RSSI as if it were peer ESP-NOW RSSI. If peer RSSI is unavailable, show unavailable and record the limitation.

The laptop can timestamp observations consistently, but those include reporting delays. Label recovery measured from failure declaration differently from recovery measured from an instrumented physical cut. Example timing values in earlier brainstorming were illustrative, not measured results.

## Dashboard and simulator

The intended dashboard shows node reachability, permitted and observed links, calculated and confirmed routes, sensor values, packet counts, RTT, available RSSI, history charts, and a failure/recovery timeline. Planned failure controls must label software-injected events separately from real disconnections. Recovery events state whether they came from local failover or a controller reroute, and replayed readings are labeled as delayed. Show telemetry freshness so stale state does not look live.

A deterministic simulator exists for two virtual nodes (a virtual clock, seeded links, and nodes that exchange encoded protocol frames); the four-node topology, controller and gateway interface are later phases. It reuses the protocol module and should reuse controller behavior where practical. Simulation cannot validate ESP8266 radio performance, physical RSSI, power stability, or USB drivers. The approved transition to hardware follows the working simulated demo in Phase 5, before dashboard polish and adaptive routing.

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
