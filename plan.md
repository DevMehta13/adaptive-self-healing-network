# Project plan — agreed scope

This is the scope agreement, not an implementation roadmap. The owners want to review the detailed roadmap separately. Do not create `ROADMAP.md` or `docs/ROADMAP.md` until that review is requested.

## Purpose

Demonstrate reliable communication between low-cost IoT nodes: detect a failed relay, find a surviving route, and show how data delivery recovers. The networking behavior and evidence are the main contribution; a sensor provides useful sample traffic.

## Confirmed decisions

| Decision | Agreement |
| --- | --- |
| Physical network | Four ESP8266 NodeMCU boards, named A, B, C, D |
| Roles | A source, B normal relay, C destination/USB gateway, D alternative relay |
| Intelligence | Route calculation and topology management run on a laptop |
| Main demonstration | A → B → C changes automatically to A → D → C after B is unplugged |
| Input data | Simulated sensor readings initially; sensor and wires purchased later |
| Cost | Prefer ₹800; aim below ₹1,000; complete purchase cost still unverified |
| Development computers | One macOS computer and one Windows computer |
| Schedule | No fixed deadline; work incrementally and understand each part |
| Collaboration | Shared GitHub context usable by humans, Claude, and Codex |
| AI | May assist development or later explanations; basic routing must work without AI |

## Target experience

Power the nodes, connect C to the laptop, start the local controller, and open the browser dashboard. Observe arriving readings and the actual relay path. Unplug B, see the failure and route change, and inspect the measured delivery gap. Reconnect B and see it become available again.

The demonstration should work without internet after dependencies and firmware are installed. A cloud account or paid runtime service is not part of the design.

## Completion criteria for the core project

- Evidence shows real forwarding on both relay paths; a dashboard animation alone is insufficient.
- Removing B causes automatic restoration through D without manually changing a route.
- Failure reports and route instructions can travel through surviving permitted links even when the old data route is broken.
- Rejoining nodes are rediscovered, and an unavailable destination is shown honestly.
- The dashboard reports packet delivery, loss, round-trip delay, and recovery events with documented definitions.
- The project distinguishes configured links, measured links, calculated routes, and observed packet paths.
- A sensor can supply data after the network is validated; the exact sensor remains open.
- Setup and verification instructions are reproducible on macOS and Windows, with untested platforms explicitly identified.
- Both contributors can explain heartbeats, forwarding, route choice, TTL, acknowledgements, and the system's limits.

The core is allowed to lose packets during detection and recovery. Recovery time will be measured; example numbers from planning conversations are not acceptance guarantees.

## Quality goals and later extensions

A clear live graph, event timeline, history charts, and repeatable experiments should make the system easy to demonstrate. A simulator should eventually use the same protocol meaning as real devices, with all simulated results labeled.

After the core works, consider combined latency/loss/link-quality costs, selectable routing policies, smoothing to avoid frequent route changes, larger virtual networks, experiment exports, anomaly detection, or an AI explanation assistant. These extensions do not expand the current implementation authorization.

## Details still requiring design or experiments

The exact firmware versions, packet encoding, control-message transport, route-update installation procedure, timeout/retry limits, RSSI measurement method, dashboard library, sensor model, and final bill of materials remain open. See [architecture](docs/ARCHITECTURE.md) for the current proposals and [status](docs/STATUS.md) for the pending work.

## Working agreement

Implement small, reviewable changes with a demonstration or meaningful check for each behavior. Explain concepts in everyday language, record evidence, and update the shared context when decisions change. Detailed implementation ordering and milestone approval are deferred to the separate roadmap discussion.
