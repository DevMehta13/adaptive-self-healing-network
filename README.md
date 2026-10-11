# Adaptive Self-Healing IoT Network

A college project about keeping sensor data moving when a wireless relay fails, using four inexpensive ESP8266 NodeMCU boards and a laptop.

**Status: documentation and design only. No firmware, controller, dashboard, or simulator has been implemented or tested.** The software-first phased plan is approved in [plan.md](plan.md). Implementation is waiting for the owner's explicit instruction to start.

## What we are building

Node A produces sensor data. B normally forwards it to C. If B loses power, the network detects the failure and automatically uses D instead. C passes received data to the laptop through USB, where a live dashboard shows the network and its recovery.

```text
         B
        / \
       A   C ── USB ── Laptop controller + browser dashboard
        \ /
         D

Normal data route:  A → B → C
Backup data route:  A → D → C
```

The lines between boards represent wireless links. Each board also needs electrical power. Only C needs a USB data connection to the laptop during the final demo; the other boards can use suitable existing USB power sources.

| Part | Purpose |
| --- | --- |
| A — source | Generates simulated readings initially; connects to a sensor later |
| B — relay | Forwards packets on the normal demonstration route |
| C — destination/gateway | Receives data and bridges the wireless network to the laptop |
| D — relay | Provides an alternative path |
| Laptop | Maintains the network map, calculates routes, stores telemetry, and runs the dashboard |

## Hardware and cost

- Four **ESP8266 NodeMCU development boards** are the selected hardware. ESP32-C3 was considered but is not the current choice.
- A cheap sensor and necessary wires will be selected and purchased later.
- Suitable USB cables and power sources are required; use existing equipment where possible. Programming and C's laptop connection require a data-capable cable.
- Preferred hardware spend is **₹800**, with a target below **₹1,000**. This is a budget constraint, not a verified shopping total. Delivery, tax, cables, and available power supplies still need checking.
- No hardware has been purchased or tested as of the initial documentation handoff.

## Intended finished features

- Real wireless packet forwarding through multiple nodes.
- HELLO/heartbeat messages to discover permitted neighbors and detect missing links.
- A laptop topology graph and Dijkstra route calculation.
- Automatic rerouting when an alternative path exists, and detection when a node returns.
- Local failover: the controller pre-installs a backup next hop, so a node can move away from a failed relay without waiting for the laptop.
- Fail-static forwarding while the laptop is unreachable, with bounded buffering of readings at C and replay on reconnection.
- Packet identifiers, bounded retries, delivery acknowledgements, duplicate detection, and TTL to limit forwarding.
- A local dashboard showing node state, links, intended and observed routes, readings, and failure/recovery events.
- Delivery counts, packet loss, RTT, and recovery measurements; RSSI where reliable ESP8266 measurement is validated.
- Performance history and clearly labeled simulated failure testing.
- Adaptive routing with a measured comparison of lowest-delay and highest-reliability policies after the hardware core works.
- A real sensor after communication works. Software simulation is planned to support development without all boards present.

These are intended outcomes, not completed functionality. Timing values and performance claims require measurements on hardware.

## What the demonstration proves

1. All four powered boards appear in the dashboard.
2. Readings reach C through A → B → C, with forwarding evidence from the nodes.
3. A presenter physically unplugs B.
4. The system detects loss of contact, updates its map, and delivers new routing instructions through the surviving network.
5. Delivery resumes through A → D → C; the dashboard records the interruption, losses, and recovery.
6. Reconnecting B restores its availability. The controller reevaluates routes; B is used again only if the routing policy selects it.

The classroom topology deliberately permits only A–B, B–C, A–D, and D–C, even if all radios can hear each other. This is a **configured logical topology**, not proof that A and C are physically out of radio range. Forwarding and the unplugged-node failure still occur on real hardware.

Healing requires a surviving path. Failure of C or of both relay paths is outside the first version's recovery guarantee. If the laptop is unreachable, nodes keep forwarding, can switch to a pre-installed backup relay, and C buffers a bounded number of readings, but no new routes are calculated until the laptop returns. A short interruption and some packet loss are possible; zero-loss delivery is not promised.

## Start here

| Document | What it answers |
| --- | --- |
| [plan.md](plan.md) | Approved software-first phases, technical choices, and completion checkpoints |
| [Architecture](docs/ARCHITECTURE.md) | Connections, responsibilities, proposed mechanisms, and limitations |
| [Status](docs/STATUS.md) | What actually exists, pending decisions, and the next discussion |
| [Contributing](CONTRIBUTING.md) | How to collaborate from macOS and Windows |
| [AGENTS.md](AGENTS.md) | Shared working instructions for coding agents |
| [CLAUDE.md](CLAUDE.md) | Claude entry point to the same shared context |

Read the status before starting work. There are no application installation or run commands yet.
