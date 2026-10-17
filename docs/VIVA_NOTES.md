# Viva notes

Plain-language explanations of each concept the backend uses, and the key **simulated** results. The explanations are collected (and shortened) from [backend/README.md](../backend/README.md); the results tables are from the verification record in [STATUS.md](STATUS.md) and from `python -m iot_backend.simulator ... --compare-policies`. Every number below comes from the simulator with configured delays and loss. **None is a hardware measurement.** If asked "did you test this on real boards?" the honest answer today is: no, not yet; that is Phases 6 to 8.

## The project in four sentences

Four ESP8266 boards form a diamond: A makes readings, B and D relay, C receives and is plugged into the laptop. If the relay in use dies, the network notices, and the readings go through the other relay. The laptop works out the routes (the boards only forward) and a dashboard shows what is happening. Before buying boards we built and tested everything in a simulator that speaks the same packet format the boards will.

## Concepts, one at a time

**Packet header.** A small fixed label on every frame so any node can read it without guessing: version, type, source, destination, session, sequence number, TTL, payload length. Fixed size (14 bytes) keeps frames small; a frame is at most 250 bytes because that is the ESP-NOW limit.

**Session ID.** A board that reboots forgets its counter and starts again at 1, so a new reading #1 would look like the old reading #1. A random number chosen at each boot makes (source, session, sequence) unique across restarts, so an old acknowledgement can never be mistaken for a new one.

**What an acknowledgement proves.** A DELIVERY_ACK proves C received that exact reading. Not receiving one proves nothing: the ACK itself may have been lost. And the radio's "send succeeded" callback proves only that the next hop's radio heard the frame, never that the reading arrived. *Sending is not delivery; delivery is not confirmed delivery.*

**Link versus route; hop-by-hop versus end-to-end.** A link is one radio hop; a route is the chain. A link can work while the route is broken. Forwarding is local (each relay passes a frame on); delivery is end to end, confirmed only by C's ACK, which is why the *source* retries and relays never do.

**Retries keep the same identity.** A retry is the same reading sent again, so it carries the same (source, session, sequence). C uses that to notice it already has it, counts it once, and sends the ACK again (the first ACK was probably lost).

**TTL.** A hop budget in the frame; each relay subtracts one and refuses to forward at zero. If two nodes' routes point at each other the frame bounces a few times and dies instead of circulating forever.

**Physical versus logical topology.** In the classroom every board can hear every other. The diamond is a rule: each node keeps a list of permitted neighbors and refuses to send to, or accept from, anyone else. That is how we make four close-by boards behave as a diamond, and we say so in the demo.

**Heartbeats and timeouts (HELLO).** Each node says "still here" every second. A neighbor not heard for 3 s is marked *lost*. Shorter detects sooner but one lost HELLO would look like a failure; three misses make that unlikely.

**One-way links.** Hearing B's HELLO proves only that B's frames reach A. So HELLOs also list who the sender hears. Only when B's HELLO lists A do we know both directions work (*two-way*). If A's frames to B are lost, A still hears B but B never hears A: the link is *one-way* and not used.

**"Lost contact", not "powered off".** A timeout says only that nothing was heard. The controller reports lost contact, who observed it, and for how long, and needs every neighbor that still reports to agree.

**Flooding, and why it cannot storm.** Management messages (reports, ACKs, route updates) are passed to all permitted neighbors except the sender. Three limits: each node handles a message once (a 64-entry memory of identities), drops its own messages when they return, and the TTL runs out. Flooding uses every surviving link, so when B dies the news and the repair instructions still reach A through D. If they followed the data route they would be sent into the failure they are meant to fix.

**RTT, not one-way delay.** The boards' clocks are not synchronised, so subtracting one board's timestamp from another's is meaningless. A *round trip* needs one clock: A notes when it sent a probe and when the reply came back. The reply carries no time at all.

**Dijkstra on the diamond.** Every usable link has a cost. Start at A and repeatedly settle the closest unsettled node. With A-B 30, B-C 35, A-D 65, D-C 65: B is settled at 30, offering C at 65; D at 65 offers C at 130; so the route is A > B > C. Ties are broken by a fixed rule (fewer hops, then lower node ids) so the answer never depends on luck.

**Why relays are installed first.** If A is told "use B" before B knows where to send, A's frames reach B and go nowhere. So the controller installs the relays, waits for their acknowledgements, then tells A. A route is *active* only when everyone acknowledged.

**Versions and epochs.** Updates can arrive late or twice; a version number lets a node ignore an old one. A restarted controller would start at version 1 again, so an *epoch* (its start time) names the run and beats any version of an older run.

**Hold-down.** A link that comes back must stay up for 5 s before the controller uses it again, so a flapping link cannot make the route flap. Leaving a failed link is never delayed.

**"No available route" is explicit.** A source that was told there is nothing, and one that simply has not heard, look the same from outside. Saying it makes the situation visible, lets the source drop readings with a reason, and stays honest: if the source could not be told, nothing claims it was.

**Local failover.** The laptop computes both the best route and a spare next hop for each node. A node only has to notice its next hop failed and use the spare: compute centrally, react locally (IP fast reroute does the same). The spare must be *loop-free* and *node-protecting* (not depend on the relay whose failure it covers). Two triggers: the next hop is no longer two-way (T1), or 3 sends in a row fail at the link layer (T2). Nodes never switch back by themselves: the controller decides, with the hold-down.

**Controller outage (fail-static).** The laptop sends C a heartbeat every second. If three go missing C treats the controller as gone, keeps the radio network running, and holds what it would have sent up in a **bounded 50-entry buffer**, replayed in order when the controller returns. Readings are kept in preference to reports; the oldest are dropped *and counted*. C has no calendar clock, so each held entry is replayed with its **age**, and the laptop subtracts it from its own clock. Each reading is counted once by its identity, so a replay cannot double count.

**The serial link.** The USB line carries JSON lines, because a person can read them in a terminal, with a **CRC-16** on each (a lost byte turns `"n":12` into `"n":2`, still valid JSON and wrong), and `#` marks human log lines so debugging text is never parsed as data. The laptop and C use **cumulative acknowledgement and resend on a gap**, like TCP: even a 1 s interruption loses nothing silently.

**One controller, simulated or real.** The controller is written against a small gateway interface (events in, commands out). The simulator and the serial port both sit behind it, and a test fails if the controller ever imports either. That is why the same code can run on boards.

**What the dashboard shows.** What the controller *knows*, not the simulator's truth (after "Stop B" B still looks reachable until the network notices: the gap is the detection time). The planned route and the observed path (built only from what node C can see) are different things and are drawn differently.

**Routing policies (Phase 9).** *Lowest delay* minimises the sum of smoothed round-trip times: an **EWMA** moves a number a fifth of the way to each new sample (`new = 0.2 × sample + 0.8 × old`). *Highest reliability* uses the last 20 probes of each link: if 18 were answered, p = 0.9. A path survives with the **product** of its links' probabilities, but Dijkstra adds; **`−log p`** turns the product into a sum (`−ln 0.9 − ln 0.8 = −ln 0.72`), is never negative, and the cheapest path is the most reliable. A link that answered nothing is floored at p = 0.05 so its cost is finite. **Hysteresis:** a better route must be better for 3 evaluations in a row (every 5 s) before the route changes; a *broken* route is replaced at once; local failover is not delayed. Delay policies cannot see loss because RTT is measured only from probes that came back.

**Run history (Phase 10).** A bounded SQLite file with each run's configuration, events and readings (10 runs, 20 000 events and 10 000 readings per run; the oldest are dropped and counted), exportable as CSV. It records what the controller knew.

## Key simulated results

Reference time is the scripted failure time (exact in simulation); on boards it would be an instrumented cut and reporting delays would add to it. Link delays are *simulated settings* (B's links cheaper than D's), not measurements.

### Detection (Phase 3; 50 seeds; min / median / max)

| | After the scripted event |
| --- | --- |
| Loss of contact with B declared | 2176 / 2604 / 3117 ms after B stops |
| B's return declared | 86 / 277 / 817 ms after B starts |

Meaning: the controller notices a stopped relay in about 2.2 to 3.1 s, dominated by the 3 s neighbor timeout.

### Recovery by the controller (Phase 4a; 50 seeds; B stops)

| Measure | min / median / max |
| --- | --- |
| Route active (new route confirmed) | 2229 / 2708 / 3237 ms |
| of which distribution (computed to all acknowledged) | 200 / 200 / 206 ms |
| Delivery gap at C (receipt to receipt) | 5023 / 5030 / 6037 ms |
| Readings lost | 1 / 1 / 1 |

Meaning: *service recovery* is quantised by the 2 s reading interval and 1 s retry timer, so it says more about those timers than about routing speed. Route computation itself takes microseconds on the laptop (median 1 µs, worst 76 µs).

### Local failover versus waiting for the controller (Phase 4b; 50 seeds; B stops; median ms after the failure)

| Mode | A's table switched to the backup | Delivery gap at C | Readings lost (median) |
| --- | --- | --- | --- |
| Controller only | 2643 | 5030 | 1 |
| Local failover, T1 | 2450 | 5030 | 1 |
| Local failover, T1 + T2 | **753** | **3030** | **0** |

Meaning: T1 waits for the same 3 s timeout as the controller, so it gains little. T2 (failed sends) needs no timer: the switch moves from about 2.6 s to about 0.75 s. The gap improves less than the switch because the first reading over the new path is a retry.

### The price of T2: false failovers with no failure (30 seeds x 120 s per cell, per simulated hour, K = failed sends in a row)

| Loss on the A-B link (each way) | K = 2 | K = 3 | K = 4 |
| --- | --- | --- | --- |
| 0 % | 0 | 0 | 0 |
| 2 % | 21 | 1 | 0 |
| 5 % | 108 | 20 | 13 |
| 10 % | 283 | 101 | 26 |
| 20 % | 456 | 281 | 157 |

Meaning: K = 3 is quiet on a clean link and at 2 % loss, and noisy at 5 % or more; each false failover costs one hold-down of the good link. K is tuned on hardware.

### Controller outage (Phase 5b and 6-prep; 10 seeds; buffer 50)

| Reading interval | Outage | Made | Delivered late | Lost (counted) | Delay median / max |
| --- | --- | --- | --- | --- | --- |
| 2 s | 20 s | 100 | 100 | 0 | 10.1 / 19.0 s |
| 2 s | 80 s | 400 | 400 | 0 | 40.4 / 79.0 s |
| 2 s | 160 s | 800 | 500 | 300 | 50.5 / 99.0 s |
| 500 ms | 1 s | 20 | 20 | 0 | 0.76 / 1.0 s |
| 500 ms | 20 s | 400 | 400 | 0 | 10.6 / 20.0 s |
| 500 ms | 40 s | 800 | 500 | 300 | 13.2 / 25.0 s |

Meaning: the buffer holds 50 readings per outage (about 100 s of 2 s readings, 25 s of 500 ms readings); beyond that the **oldest are dropped and counted**, never hidden. A 1 s outage used to lose readings uncounted; the numbered, acknowledged uplink fixed it. No reading was counted twice in any row.

### Policy comparison (Phase 9; `fast-lossy-vs-slow-reliable.json`, 30 seeds x 180 s; A>B>C is 10 ms per link and loses 15 % each way, A>D>C is 25 ms per link and clean)

| | lowest delay | highest reliability |
| --- | --- | --- |
| Delivery rate, pooled | 99.01 % | **99.96 %** |
| Readings lost per run, mean (max) | 0.87 (5) | **0.03 (1)** |
| Delivery time (first attempt to ACK), mean | 240 ms | **108 ms** |
| Delivery time, median / 95th percentile | 90 / 1095 ms | 90 / **126 ms** |
| Route changes per run, mean (max) | 6.97 (11) | **0.50 (1)** |

Higher delivery rate for highest reliability in 17 of 30 seeds, equal in 12, lower in 1.

How the result changes with the loss on the fast path (20 seeds each; 15 % is the 30-seed table above):

| Loss each way | Delivery rate (delay / reliability) | Delivery time mean, ms | Route changes per run |
| --- | --- | --- | --- |
| 0 % | 100 % / 100 % | 61 / 67 | 0 / 0 |
| 5 % | 99.94 % / 100 % | 155 / 99 | 1.70 / 0.55 |
| 15 % | 99.01 % / 99.96 % | 240 / 108 | 6.97 / 0.50 |
| 30 % | 99.83 % / 99.88 % | 117 / 114 | 1.00 / 0.35 |

**Read it honestly.** (1) Retries hide loss (3 attempts), so the delivery *rate* differs by about one reading in a hundred; the difference is in *time*: each retry costs a second. (2) Lowest delay changes route often because the lossy links keep losing HELLOs, the neighbor is declared lost, and the broken route is replaced at once; it is mostly the cost of failure detection, not indecision. (3) With **no loss** both paths cost 0, equal is not better, and highest reliability may stay on the slower path (4 of 20 seeds; 67 against 61 ms). (4) At 30 % loss lowest delay spent about 98 % of its time on D anyway, so both policies look alike. (5) The loss rates are settings, so this shows what each policy *does* in this network, not what real radios do.

## Likely questions, short answers

- **Why not let the boards compute routes?** The laptop has the whole picture and plenty of memory; boards react locally to what they were told. It also keeps the firmware small and the logic testable. The trade-off: the laptop is a single point of failure for *new* routes, which is why local failover and fail-static buffering exist.
- **What happens if C dies?** Not healed: C is the only gateway. The dashboard shows the network view as stale and the limitation is reported; recovery is not claimed.
- **Did you test on hardware?** No. Everything is simulation, the laptop side of the USB link only against a fake C. The hardware plan is in [HANDOFF_FOR_DEV.md](HANDOFF_FOR_DEV.md) section 7.
- **Is "recovery in 3 s" a promise?** No. It is a simulated median with configured timers. Hardware numbers will differ and will be reported as measured.
- **Do you guarantee zero packet loss?** No. A short interruption and some loss are expected: simulated B failure lost 0 or 1 readings.
- **Why is the unplug test not done on C?** The USB cable also powers C, and its buffer is RAM. Controller loss is tested by stopping the software.
- **How do you know you count each reading once?** Identity (source, session, sequence), a bounded duplicate window at C, and a ledger on the laptop; tests replay, retry and reorder to check.
- **Why is B preferred?** Only because its *configured* simulated delay is lower; on a tie the rule prefers the lower node id. We do not falsify measured latency to force B.
