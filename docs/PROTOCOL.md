# Node protocol

**Draft — pending Dev's review.** Written by Harshal (backend owner) for Dev, who will implement the same format in C++ on the ESP8266. Scope so far: the frame header, DATA, DELIVERY_ACK (Phase 1), forwarding/delivery behaviour (Phase 2; no format change), HELLO / TOPOLOGY_REPORT payloads with management-traffic behaviour (Phase 3), probes, route updates, route acknowledgements and the local-failover report (Phase 4a), the behaviour of local failover (Phase 4b), and TELEMETRY plus the evidence node C reports about each reading (Phase 5a). The Python implementation is `backend/src/iot_backend/protocol/`; the shared reference examples are in [tests/protocol-examples/](../tests/protocol-examples/). If this document and the examples disagree, tell Harshal — they are meant to agree exactly.

## Why a compact binary format

| | Binary, fixed header (chosen) | JSON text |
| --- | --- | --- |
| Size of a reading | 18 bytes | roughly 80–120 bytes |
| ESP8266 work | `memcpy` into a packed struct | needs a parser and heap |
| Python | `struct.pack` / `unpack` | trivial |
| Human-readable | no (use the trace and hex examples) | yes |

ESP-NOW frames are limited to 250 bytes and the boards have little RAM, so binary wins. The readable form of a frame lives in the JSON examples and in the simulator trace. JSON is still used on the **USB serial** side between C and the laptop (a later phase), where size is not the constraint.

## Frame layout

All multi-byte integers are **little-endian**. There is no padding. The header is 14 bytes and is followed by the payload.

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 1 | `version` | Protocol version. Must be `1`. |
| 1 | 1 | `type` | Message type (below). |
| 2 | 1 | `src` | Node id that **created** the frame (the origin, not the previous hop). |
| 3 | 1 | `dst` | Node id of the final destination. |
| 4 | 4 | `session` | Random id chosen by `src` at each boot. `0` is reserved. |
| 8 | 4 | `seq` | Sequence number, counted by `src` within one session. |
| 12 | 1 | `ttl` | Remaining hop budget. |
| 13 | 1 | `payload_len` | Number of payload bytes that follow. |
| 14 | `payload_len` | payload | Depends on `type`. |

As a C struct: `struct __attribute__((packed)) Header { uint8_t version, type, src, dst; uint32_t session, seq; uint8_t ttl, payload_len; };` (the ESP8266 is little-endian, so fields can be copied directly).

**Packet identity** is the triple `(src, session, seq)`. Two frames with the same triple are the same packet. `seq` alone is not an identity, because it restarts when a board reboots.

### Field notes

- **Node ids:** A=1, B=2, C=3, D=4. `0` is invalid and `255` is reserved (broadcast, unused). Ids 5–254 are syntactically valid and decoders accept them.
- **session:** 32 random bits from the board's random source at boot. A new boot must pick a new value. `0` is reserved so an uninitialised session is never valid.
- **seq:** 32 bits, one counter per node per session shared by every frame the node creates (DATA and ACK). Starts at `1` after boot in the simulator; any start is legal. It wraps from `4294967295` to `0`. At one frame per second a wrap takes about 136 years, so Phase 1 treats `seq` only as an identity part and never as an ordering; ordering, if ever needed, must use wrap-safe comparison.
- **ttl:** 8-bit hop budget. Initial value for data is 8 (plan.md). Each forwarder will decrement it and drop the frame at 0; forwarding is Phase 2. In Phase 1 TTL is carried and validated as a field only.
- **Route generation** (mentioned in ARCHITECTURE.md) is not a header field. It belongs in route messages, which are reserved below.

### Size limit

The whole encoded frame must be **≤ 250 bytes** (ESP-NOW v1). That leaves a **maximum payload of 236 bytes**. Phase 1 payloads are 4 and 9 bytes.

## Message types

| Code | Name | Status |
| --- | --- | --- |
| `0x01` | DATA | Implemented |
| `0x02` | DELIVERY_ACK | Implemented |
| `0x10` | HELLO | Implemented (Phase 3, pending Dev's review) |
| `0x11` | TOPOLOGY_REPORT | Implemented (Phase 3, pending Dev's review) |
| `0x12` | ROUTE_UPDATE | Implemented (Phase 4a, pending Dev's review) |
| `0x13` | ROUTE_INSTALL_ACK | Implemented (Phase 4a, pending Dev's review) |
| `0x14` | PROBE | Implemented (Phase 4a, pending Dev's review) |
| `0x15` | PROBE_REPLY | Implemented (Phase 4a, pending Dev's review) |
| `0x16` | LOCAL_FAILOVER_REPORT | Defined in Phase 4a; sent by nodes from Phase 4b (pending Dev's review) |
| `0x17` | TELEMETRY | Implemented (Phase 5a, pending Dev's review) |

Codes `0x01–0x0F` are the data plane; `0x10` and above are management. Every code in this table now has a payload definition. A code that is added to the table before its payload is specified must be **rejected** (`reserved_type`) by the decoder; the reason is kept for that purpose, though no code is reserved today.

### DATA (`0x01`) — payload 4 bytes

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 4 | `value` | Signed 32-bit reading in hundredths of a unit: `2345` means 23.45. |

### DELIVERY_ACK (`0x02`) — payload 9 bytes

Created by the node that accepted a reading, addressed (`dst`) to the reading's origin. `src`, `session` and `seq` in the header are the **acknowledger's own** (its ACK is itself a numbered frame). The payload names the reading being acknowledged:

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 1 | `origin` | `src` of the acknowledged reading. |
| 1 | 4 | `session` | `session` of the acknowledged reading. |
| 5 | 4 | `seq` | `seq` of the acknowledged reading. |

An ACK means *the destination node accepted the frame and built a valid acknowledgement for it*. It says nothing about what the application did with the reading afterwards, and the ACK itself can be lost even when the reading arrived.

### HELLO (`0x10`) — payload 1 + 5n bytes, n ≤ 8 (Phase 3, pending Dev's review)

A local keep-alive sent to **one** neighbor (`dst` is that neighbor, `ttl` 1). The sender's own `session` is in the header. The payload lists the neighbors the sender currently hears, so the receiver can check that the sender hears *it* (two-way confirmation).

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 1 | `count` | Number of entries, 0 to 8. |
| 1 + 5i | 1 | `node` | Id of a neighbor the sender hears. |
| 2 + 5i | 4 | `session` | The session that neighbor had in the last HELLO the sender heard from it. |

The payload length must be exactly `1 + 5 × count`. An empty list is the single byte `00`.

### TOPOLOGY_REPORT (`0x11`) — payload 1 + 8n bytes, n ≤ 8, plus an optional RTT trailer (Phase 3; trailer Phase 4a; pending Dev's review)

A node's current neighbor observations, addressed to C (`dst`) and carried by bounded flooding (see *Management traffic* below).

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 1 | `count` | Number of entries, 0 to 8. |
| 1 + 8i | 1 | `neighbor` | The observed neighbor's node id. |
| 2 + 8i | 1 | `state` | `1` one-way, `2` two-way, `3` lost. `0` and values above 3 are invalid. A neighbor never heard is simply absent. |
| 3 + 8i | 4 | `session` | The neighbor's session from its latest HELLO; a changed value means it restarted. |
| 7 + 8i | 2 | `age_ms` | Milliseconds since that neighbor's last HELLO on the reporter's clock, saturating at 65535. Ages let the controller use its own clock without synchronised clocks. |

The payload length must be exactly `1 + 8 × count`. Maximum payload is 65 bytes, 79 with the header.

**RTT trailer (Phase 4a).** If the reporter has a measured round-trip time for at least one neighbor, the entries are followed by:

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| `1 + 8n` | 1 | `rtt_count` | Number of measured neighbors, 1 to 8. |
| `2 + 8n + 3j` | 1 | `neighbor` | Node id the measurement is for. |
| `3 + 8n + 3j` | 2 | `rtt_ms` | Round-trip time to that neighbor in milliseconds, measured by the reporter on **its own clock** (see *Probes*). `0xFFFF` is never sent. |

The payload length is therefore exactly `1 + 8n` (no measurements) or `1 + 8n + 1 + 3m`. A neighbor that is in the entries but not in the trailer is **unmeasured**. A receiver must treat "unmeasured" as *no cost known*, never as zero or as a default. The trailer is left out entirely when nothing is measured, so a report without measurements is byte-for-byte the Phase 3 report, and every Phase 3 reference example is unchanged. Largest report: `14 + 1 + 64 + 1 + 24 = 104` bytes.

*Why a trailer and not a wider entry:* widening the 8-byte entry would have changed the bytes of every existing report example and of any firmware already written against them. A trailer keeps the Phase 3 prefix valid. One consequence to be aware of: a damaged Phase 3 report whose extra bytes happen to form a valid trailer would parse as a report with measurements; frames are not checksummed at this layer (the radio does that), so this is accepted.

States: **one-way** means the reporter hears the neighbor's HELLOs but the neighbor's HELLOs do not list the reporter's current session; **two-way** means both directions are confirmed; **lost** means the neighbor was heard before but not within the neighbor timeout (default 3 s). Lost is "no HELLO heard", not proof that the neighbor lost power.

### PROBE (`0x14`) — payload 0 bytes (Phase 4a, pending Dev's review)

One hop, `ttl` 1, `dst` is the probed neighbor. The frame carries nothing: its own header `session` and `seq` identify it.

### PROBE_REPLY (`0x15`) — payload 8 bytes (Phase 4a, pending Dev's review)

One hop, `ttl` 1, `dst` is the prober. The replier's own `session` and `seq` are in the header. The payload echoes the probe so the prober can match it:

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 4 | `probe_session` | `session` of the PROBE being answered (non-zero). |
| 4 | 4 | `probe_seq` | `seq` of the PROBE being answered. |

There is **no timestamp** in either frame. See *Probes and link cost* below.

### ROUTE_UPDATE (`0x12`) — payload 11 bytes (Phase 4a, pending Dev's review)

An instruction from the controller. `src` is **C** (the gateway node originates it on the controller's behalf), `dst` is the node that must install it, `ttl` is the management default. The payload:

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 4 | `epoch` | Identifies one controller run; larger means newer. `0` is invalid. |
| 4 | 4 | `version` | Route version within the epoch, from `1`. `0` is invalid. |
| 8 | 1 | `destination` | Node id the entry is for (C in this project). |
| 9 | 1 | `next_hop` | Node id to send frames for `destination` to, or `0` for **"no route" (told explicitly)**. |
| 10 | 1 | `backup_next_hop` | Backup node id, or `0` for none. Phase 4a always sends `0`; Phase 4b will use it. A backup needs a primary and must differ from it. |

### ROUTE_INSTALL_ACK (`0x13`) — payload 10 bytes (Phase 4a, pending Dev's review)

The installing node's answer, created by that node (`src`), addressed to C, carried by management flooding.

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 4 | `epoch` | The update's epoch; for `status` 2 the epoch the node **holds**. |
| 4 | 4 | `version` | The update's version; for `status` 2 the version the node **holds**. |
| 8 | 1 | `destination` | Same as in the update. |
| 9 | 1 | `status` | `1` installed (also sent again for a repeat of the held version), `2` stale (the node holds a newer pair and ignored the update), `3` rejected (the node cannot use it: next hop is not a permitted neighbor, or its route table is full). `0` and values above 3 are invalid. |

### LOCAL_FAILOVER_REPORT (`0x16`) — payload 12 bytes (defined in Phase 4a, sent from Phase 4b)

Created by a node that switched to its backup, or found it had none; addressed to C; carried by management flooding. The byte layout is the Phase 4a one. Phase 4b changed only the meaning of two values: `to_next_hop` may be `0` and a second `reason` exists (both explained below and in *Local failover*).

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 4 | `epoch` | Epoch of the route the node was using. |
| 4 | 4 | `version` | Version of that route. |
| 8 | 1 | `destination` | Destination of the entry. |
| 9 | 1 | `from_next_hop` | The primary next hop that was abandoned. |
| 10 | 1 | `to_next_hop` | The backup now in use (must differ from `from_next_hop`), or `0` when the node had **no usable backup** and now has no route. *(0 added in Phase 4b.)* |
| 11 | 1 | `reason` | `1` the next hop stopped being a two-way neighbor (trigger T1: lost *or* one-way); `2` K consecutive link-layer send failures (trigger T2). Other values are invalid. *(2 added in Phase 4b.)* |

### TELEMETRY (`0x17`) — payload 32 bytes (Phase 5a, pending Dev's review)

A node's **own counters**, created by that node (`src`), addressed to C, carried by management flooding. It exists because some facts are known only to the node that holds them: C knows what it received, but only A knows how many readings it created and how many were never acknowledged. Sent every `telemetry_interval` (default 5 s in the live demo, 0 = off, jittered ±10 %).

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 4 | `created` | Readings created (source nodes). |
| 4 | 4 | `acknowledged` | Readings whose delivery ACK came back (source). |
| 8 | 4 | `lost` | Readings given up on after the last attempt (source). |
| 12 | 4 | `no_route` | Readings dropped at creation for lack of a route (source). |
| 16 | 4 | `delivered` | Unique readings accepted (destination). |
| 20 | 4 | `forwarded` | Frames relayed for others. |
| 24 | 4 | `dropped` | Frames discarded or rejected, all reasons. |
| 28 | 2 | `failovers` | Local failovers performed. |
| 30 | 2 | `send_failures` | Link-layer failure callbacks received. |

Counters are cumulative since the node's boot (the frame's `session` says which boot) and saturate rather than wrap. Fields that do not apply to a node are zero. Any value is valid; only the size is checked (`bad_payload_length`). A receiver must label counts with the time they were received: they describe the node a moment ago, not now.

## Decoding rules

A receiver must validate in this order and stop at the first failure. The frame is dropped and the reason recorded (the simulator traces `frame_rejected`; firmware should count by reason). Rejection never changes node state and never stops the node.

| Order | Check | Reason code |
| --- | --- | --- |
| 1 | Total length > 250 | `oversize` |
| 2 | Total length < 14 | `truncated` |
| 3 | `version` ≠ 1 | `bad_version` |
| 4 | `type` is not a defined code | `unknown_type` |
| 5 | `type` is defined but reserved | `reserved_type` |
| 6 | `src` or `dst` is 0 or 255 | `bad_node_id` |
| 7 | `session` is 0 | `bad_session` |
| 8 | `payload_len` ≠ total length − 14 | `length_mismatch` |
| 9 | Payload size is wrong for the type (TELEMETRY ≠ 32, DATA ≠ 4, DELIVERY_ACK ≠ 9, PROBE ≠ 0, PROBE_REPLY ≠ 8, ROUTE_UPDATE ≠ 11, ROUTE_INSTALL_ACK ≠ 10, LOCAL_FAILOVER_REPORT ≠ 12); HELLO or TOPOLOGY_REPORT payload empty, `count` above 8, or length ≠ 1 + entry size × `count` (a report may add an RTT trailer of exactly `1 + 3 × rtt_count` bytes, `rtt_count` 1 to 8) | `bad_payload_length` |
| 10 | (DELIVERY_ACK `origin`, HELLO `node`, report `neighbor`, report RTT `neighbor`, route `destination`, `next_hop` or `backup_next_hop` equal to 255, failover node ids) is 0 or 255 (`next_hop` and `backup_next_hop` may be 0 = none) | `bad_node_id` |
| 11 | (DELIVERY_ACK, HELLO entry, report entry, PROBE_REPLY `probe_session`) session is 0 | `bad_session` |
| 12 | (report entry) `state` is not 1, 2 or 3; (report RTT) `rtt_ms` is `0xFFFF`; (route messages) `epoch` or `version` is 0; (ROUTE_UPDATE) a backup without a primary or equal to it; (ROUTE_INSTALL_ACK) `status` not 1–3; (LOCAL_FAILOVER_REPORT) `from_next_hop` = `to_next_hop` or `reason` not 1 or 2 | `bad_value` |

Whether a valid frame is *for this node*, from a *permitted neighbor*, or an *expected type* is behaviour, not decoding; see the next section.

An encoder must refuse (not truncate or wrap) any field outside its range.

## Forwarding and delivery behaviour (Phase 2)

**Draft — pending Dev's review.** These rules describe how nodes *act* on frames. They add no fields and do not change the byte layout above. They are implemented by the simulator and must be reproduced by the firmware.

### Neighbors: physical versus logical

Every board may be physically able to hear every other. The classroom topology is a **logical** rule that each node enforces itself with a *permitted-neighbor list* (A: B, D; B: A, C; C: B, D; D: A, C):

- A node never transmits to a node that is not in its list (drop `not_permitted_neighbor`).
- A node rejects every frame whose link-layer sender is not in its list (reject `not_permitted_neighbor`), even if the frame is otherwise valid. The link-layer sender is the radio-reported previous hop (the ESP-NOW sender MAC), not the header's `src`.

### Receive procedure

In this order; stop at the first step that discards the frame:

1. Decode and validate (decoding rules above).
2. The link-layer sender must be a permitted neighbor, else reject `not_permitted_neighbor`.
3. If `dst` is another node: **forward** (below).
4. If `dst` is this node: DATA is delivered (below); DELIVERY_ACK is matched against pending readings; any other valid type is rejected `unexpected_type`.

### Forwarding and TTL

`ttl` counts the transmissions a frame may still make. A relay that receives a frame for another node:

1. If the received `ttl` is 1 or 0, drop it (`ttl_expired`). The forwarder would decrement it to 0, which is not allowed to travel.
2. Otherwise look up `dst` in its routing table. No entry: drop `no_route`.
3. Transmit the frame to the next hop with `ttl` decremented by 1 (and every other field unchanged). If the next hop is not permitted, drop `not_permitted_neighbor`; if the send queue is full, drop `queue_full`.

The final destination never checks `ttl`. A frame created with `ttl` 8 can therefore make at most 8 transmissions in total (the origin's plus 7 forwards).

### Routing tables

A routing table maps destination node id to next-hop node id. In Phase 2 tables are set by hand; there is no discovery or calculation. The same table is used by the origin for the frames it creates, so an origin with no entry drops `no_route` too.

### Delivery, acknowledgement, retries

- **Only the final destination acknowledges.** It creates one DELIVERY_ACK per accepted DATA frame, addressed to the reading's origin; relays only forward it. From Phase 3 the ACK is a management frame and is **flooded** back (see *Discovery and management traffic*), so it reaches the origin even when the data route's relay is dead. The simulator keeps a `routed` mode (ACK follows the routing tables, the Phase 2 behaviour) for comparison; the ACK format never changed.
- **Retries are end to end, from the origin only.** The origin keeps a reading pending until its ACK arrives. If none arrives within `ack_timeout` (default 1 s) it re-sends **the identical frame**: same `src`, `session`, `seq`, `ttl` and payload. At most 3 transmissions per reading (default); then the reading is *lost*. Reusing the identity is what lets the destination recognise a repeat.
- **Late or unmatched ACKs are ignored** (reading not pending: already acknowledged, already given up on, or from an earlier session) and are not counted again.
- **One sequence counter per node and session** numbers every frame the node creates (DATA and DELIVERY_ACK). It wraps from 4294967295 to 0.

### Duplicate detection at the destination

For each `(src, session)` the destination keeps the highest `seq` seen and a bitmap of the 64 numbers just below it, and tracks at most 8 sessions (least recently used is forgotten first). A DATA frame is judged using wrap-safe (serial-number) comparison: the signed difference of two sequence numbers modulo 2^32, so 0 counts as one step after 4294967295.

| Verdict | Condition | Action |
| --- | --- | --- |
| new | newer than the highest, or inside the window and not yet seen | count as delivered once; send DELIVERY_ACK |
| duplicate | inside the window and already seen | do not count again; **send DELIVERY_ACK again** |
| too old | more than 63 below the highest | reject `stale_sequence`; no ACK (it cannot be verified) |

The repeat is acknowledged again because the earlier ACK may be exactly what was lost; without a fresh one the origin would retry until it gave up. The ACK for a repeat is a new frame with its own `seq`.

Limits, stated honestly: a session forgotten after more than 8 newer sessions could be counted again if a very late retry arrived; this bounds memory and cannot happen in the classroom topology.

### Send queue

Each node has a bounded outgoing queue (default 16 frames, including the one being transmitted) and spends a service time per frame (default 5 ms in simulation; to be measured on hardware). A frame arriving when the queue is full is dropped (`queue_full`) and counted; nothing is buffered without bound.

### Counted discard reasons

`not_permitted_neighbor`, `ttl_expired`, `no_route`, `queue_full`, `node_stopped` (frame arrived at a powered-off node; simulator only), `stale_sequence`, `unexpected_type`, `flood_duplicate`, `own_frame`, `hello_not_local`, and the decoding reasons. Firmware should keep a small counter per reason.

## Discovery and management traffic (Phase 3)

**Pending Dev's review.** No new header fields; HELLO and TOPOLOGY_REPORT payloads are defined above.

### Three kinds of frame

| Kind | Types | How it travels |
| --- | --- | --- |
| Data | DATA | Hop by hop by the routing tables. |
| Local | HELLO, PROBE, PROBE_REPLY | One hop only, never forwarded. |
| Management | TOPOLOGY_REPORT, DELIVERY_ACK, ROUTE_UPDATE, ROUTE_INSTALL_ACK, LOCAL_FAILOVER_REPORT | Bounded flooding over permitted links; **ignores the routing tables**, so a broken data route cannot cut it off. |

### HELLO: one unicast per permitted neighbor (decision)

Every `hello_interval` (default 1 s) a node sends one HELLO to **each** permitted neighbor, not one broadcast.

- *Logical links are enforced at the sender.* A node only ever addresses boards in its permitted list. A broadcast is physically heard by every board and would have to be discarded by the non-neighbors, which is exactly the "physically reachable is not the same as permitted" case the project must show honestly.
- *Per-neighbor delivery.* ESP-NOW unicast reaches one known peer MAC, which is how forwarding to a next hop already works; the receiver also learns the sender from the radio callback.
- *Cost.* With two neighbors this is two frames per second per node (15 to 25 bytes each). Broadcast would save one. Nothing in the protocol prevents switching later; the HELLO format is the same.

A HELLO's `dst` must be the receiver and its `src` must equal the link-layer sender, otherwise it is rejected `hello_not_local`. A receiver ignores HELLOs from senders that are not permitted neighbors (`not_permitted_neighbor`).

**Jitter.** Each node waits `hello_interval ± hello_jitter` (default ±100 ms) between rounds and starts at a random point in the first interval, drawn from a node-specific seeded generator. Boards that power up together would otherwise transmit in lock-step forever, so on a shared radio channel their frames would repeatedly collide; the jitter spreads them out. (The simulator does not model collisions, so it shows the desynchronised timing, not the benefit.)

### Neighbor table

One entry per permitted neighbor (at most 8): its latest `session`, the local time it was last heard, and a state.

| State | Meaning |
| --- | --- |
| (absent) | Never heard. |
| `one_way` | We hear its HELLOs, but its latest HELLO does not list **us in our current session**. Frames from it reach us; we cannot confirm ours reach it. |
| `two_way` | We hear it **and** its HELLO lists us with our current session, so both directions work. |
| `lost` | Heard before, but no HELLO within `neighbor_timeout` (default 3 s). |

Rules:

- Hearing a HELLO proves only that the neighbor's frames reach us. Two-way needs the neighbor's HELLO to list us; this is the same idea as OSPF's 2-Way check. The listed `session` stops a neighbor that heard our *previous* life from confirming our new one.
- Loss is declared exactly `neighbor_timeout` after the last HELLO (one timer per neighbor). A HELLO from a `lost` neighbor revives it as `one_way` or `two_way`.
- A HELLO whose header `session` differs from the stored one means the neighbor restarted; the entry's session is replaced and a change is reported.
- Any change of state or session schedules a TOPOLOGY_REPORT within `report_coalesce` (default 50 ms), so several changes share one report. A report is also sent every `report_interval` (default 5 s) and once about one HELLO interval after boot.
- A node never concludes "powered off". `lost` means only that nothing was heard.

### Bounded flooding of management frames

For a TOPOLOGY_REPORT (and a DELIVERY_ACK) a node:

1. **Creates:** records the frame's identity in its flood cache and sends it to every permitted neighbor (the gateway node delivers its own reports locally).
2. **Receives** a management frame (after the permitted-neighbor check):
   - If `src` is this node: drop (`own_frame`).
   - If the identity is in the flood cache: drop (`flood_duplicate`).
   - Otherwise remember it. If `dst` is this node, consume it. If not: drop when `ttl` ≤ 1 (`ttl_expired`), else send it with `ttl` − 1 to every permitted neighbor **except the one it came from**.

So each node handles a given management frame at most once, and TTL (default 8) ends the flood even if a cache has forgotten an identity. The cache holds the last 64 identities (9 bytes each); the oldest is evicted first. Frames follow permitted links only: management traffic never uses A–C as a shortcut.

### Gateway and controller (Phase 3; commands added in Phase 4a)

C hands every TOPOLOGY_REPORT and ROUTE_INSTALL_ACK addressed to it to the gateway interface as a plain event; the controller sees nothing else of the network. In the simulator that is a `SimulatedGateway`; on hardware it will be a line of JSON on C's USB serial port. In the other direction the controller gives the gateway a *route update command* (node, epoch, version, destination, next hop, backup) and **C originates the ROUTE_UPDATE frame on the controller's behalf**: the frame's `src`, `session` and `seq` are C's own. The laptop never transmits on the radio itself, and nodes use `src` to check that a ROUTE_UPDATE came from the gateway (see below).

### Defaults

| Setting | Default |
| --- | --- |
| `hello_interval` | 1000 ms |
| `hello_jitter` | ±100 ms |
| `neighbor_timeout` | 3000 ms |
| `report_interval` | 5000 ms |
| `report_coalesce` | 50 ms |
| `flood_cache_size` | 64 identities |
| management `ttl` | 8 |

These are initial experiment settings, not hardware guarantees.

### Added RAM per node (estimate for the ESP8266, not measured)

| Item | Bytes |
| --- | --- |
| Neighbor table, 2 neighbors (up to 8: ≤ 80) | about 30 |
| Flood cache, 64 × (1 + 4 + 4) | 576 |
| Frame build buffers (HELLO ≤ 55, report ≤ 79) | ≤ 134 |
| Total | about 0.7 KB |

This is small next to the 4 KB send queue from Phase 2 but should be checked against measured free heap in Phase 6.

## Probes and link cost (Phase 4a)

**Pending Dev's review.**

### Why RTT, never one-way delay

The ESP8266 boards have no shared clock. A frame stamped "sent at 1000 ms" by A means nothing to B, whose counter started at another moment and drifts at another rate. So no node ever subtracts a timestamp that another node wrote. A **probe** needs only the prober's own clock: it notes the time when it creates a PROBE, and the time when the matching PROBE_REPLY arrives, and the difference is the round trip, measured entirely on one board. Neither frame carries a time. The reply only echoes the probe's identity (`probe_session`, `probe_seq`).

### Rules

- Every `probe_interval` (default 1000 ms, jittered ±100 ms from a separate seeded generator, **0 = off**) a node sends one PROBE to each permitted neighbor.
- One probe is outstanding per neighbor. A new probe replaces an unanswered one, so a silent neighbor costs one small record and never a growing list.
- A PROBE is answered with a PROBE_REPLY to the link-layer sender. A PROBE whose `dst` is not the receiver, or whose `src` is not the link-layer sender, is rejected `probe_not_local`; one from a non-permitted neighbor is rejected `not_permitted_neighbor`.
- A PROBE_REPLY is accepted only if it comes from the probed neighbor and echoes the outstanding probe's own `session` and `seq`; otherwise it is dropped `probe_unmatched`. A late reply for a replaced probe is therefore ignored, and a reply from before a reboot can never match.
- The prober keeps the last `rtt_window` (default 5) samples per neighbor, each with the time it was taken. Samples older than `rtt_max_age` (default 10 s) are forgotten.
- The reported value is the **median** of the fresh samples, and only when there are at least `rtt_min_samples` (default 3). Otherwise the neighbor is **unmeasured**. The median ignores one outlier; EWMA smoothing is Phase 9.
- A report is requested when a neighbor *gains or loses* a measurement, not when a value changes (values ride on the next periodic report). This keeps measurement noise from generating traffic.

The measured RTT includes the radio service time at both ends and queueing behind other frames on the same board. That is intended: it is what a frame on this link really experiences. It is a round trip, so a link whose two directions differ shows up as the sum.

### Cost of a link at the controller

Both ends of a link measure it independently, each on its own clock. The controller uses the **larger of the two measurements** as the link's cost, and only when **both** ends have one. A link with a missing end is *unmeasured*: it is left out of the route calculation altogether. It is never given a default or zero cost, because a free link would attract every route to the one link nobody has measured.

## Route installation (Phase 4a)

**Pending Dev's review.** Route messages are management frames: they travel by bounded flooding (rules above), not along the data route. That is the point: when B dies, the update that moves A to D must be able to reach A through D.

### Who may install a route

A node installs a ROUTE_UPDATE only if it is addressed to it (`dst`), it passed the permitted-neighbor check, and its header `src` is the **gateway node's id** (C). Anything else is rejected `not_from_gateway`. This stops a misbehaving or misconfigured node from rerouting the network, and it makes C the only entry point for commands.

### Acceptance: epochs and versions

A node keeps, per destination, the pair `(epoch, version, next_hop, backup)` it last installed (RAM only; lost on reboot). For an incoming update:

1. A next hop that is not a permitted neighbor is **refused** (`status` 3) and not installed. So is a new destination when the table is full (`max_routes`, default 8).
2. The pair `(epoch, version)` is compared with the held one, with wrap-safe (serial-number) arithmetic: **a newer epoch wins outright, whatever the version; within the same epoch the higher version wins.**
3. Newer: install, acknowledge with `status` 1.
4. Equal: do not install again; acknowledge with `status` 1 (the earlier acknowledgement may have been lost; without a fresh one the controller would retry until it gave up).
5. Older: **ignore it** and answer `status` 2 carrying the *held* epoch and version. A late or duplicate old update can therefore never undo a newer route.

A node with nothing held accepts any valid update (a rebooted node has lost its table; the controller notices the new session and installs again).

### Why an epoch as well as a version

Versions are just a counter in the controller's memory. If the controller restarts, that counter would restart at 1, below the version the nodes hold, and every update would look stale: the network would be stuck on old routes. The **epoch** names one controller run and always goes up across restarts, so a new run's version 1 beats the previous run's version 57. On hardware the epoch should be the controller's start time in seconds since 1970 (fits 32 bits until 2106; needs no storage). If a controller ever starts with an epoch no larger than what nodes hold (a laptop clock set backwards), the nodes answer `status` 2 with their held pair and the controller **advances its epoch past it** instead of staying stuck.

### Order of installation and acknowledgements

For a path `A → B → C` the controller sends, in this order:

1. **Relays first**: ROUTE_UPDATE to every relay (B) telling it the next hop for C.
2. It waits for each relay's ROUTE_INSTALL_ACK.
3. **Only then** ROUTE_UPDATE to the source (A).
4. The route is **active** only when the source has acknowledged too.

Reason: until A is switched nothing sends toward the new relay, so installing a relay changes nothing for traffic. If A were switched first, A could transmit to a relay that does not yet know where to send the frame, which would silently drop it: a black hole.

Each update is retried when no acknowledgement arrives within `install_timeout` (default 1 s), at most `install_max_attempts` (default 3) transmissions in total. If the attempts run out the route is `unconfirmed` and is never reported active; the controller re-plans after `replan_delay` (default 3 s) at most `max_failed_plans` (default 3) times in a row, then waits for a topology change. A route is reported active only with acknowledgements from every node that forwards on it; the destination C needs no entry. Entries on relays that are no longer on the route are left in place (harmless: nothing is sent to them, and they are replaced by version when the relay is used again).

### No route

If no usable measured path exists, the controller sends the source a ROUTE_UPDATE whose `next_hop` is `0`. This is an explicit statement ("there is no route"), distinct from never having been told. A source that holds it drops each new reading immediately with reason `no_route` (counted, nothing queued, no retries). Readings already pending finish their at most 3 attempts. A source that has *never* been told also holds no route and drops the same way, so the difference is visible only in the trace (`told_by_controller`).

### Added RAM per node (estimate for the ESP8266, not measured)

| Item | Bytes (2 neighbors, 1 destination) | Bytes (worst case: 8 neighbors, 8 destinations) |
| --- | --- | --- |
| Installed routes: epoch 4 + version 4 + next hop 1 + backup 1 + destination 1 = 11 each | 11 | 88 |
| Outstanding probes: seq 4 + sent time 4 = 8 per neighbor | 16 | 64 |
| RTT windows: 5 samples × (time 4 + rtt 2) per neighbor | 60 | 240 |
| Measured flags | 2 | 8 |
| Largest new frame buffer (report with trailer 104 B instead of 79 B) | 25 | 25 |
| **Total** | **about 0.1 KB** | **about 0.4 KB** |

These come on top of the earlier estimates (0.7 KB for discovery, about 4 KB for the send queue); check against measured free heap in Phase 6. Probes also consume sequence numbers from the node's single counter. At the default intervals A creates roughly 6 or 7 numbers per second, so a reading retried within its 3 s window is about 20 numbers behind: well inside the 64-number duplicate window at C, with about 10 s of headroom. A probe interval much shorter than 1 s would erode that headroom.

### Defaults added in Phase 4a

| Setting | Default | Where |
| --- | --- | --- |
| `probe_interval` | 1000 ms (0 = off) | node |
| `probe_jitter` | ±100 ms | node |
| `rtt_window` | 5 samples | node |
| `rtt_min_samples` | 3 | node |
| `rtt_max_age` | 10 s | node |
| `max_routes` | 8 | node |
| `hold_down` | 5 s | controller |
| `install_timeout` | 1 s | controller |
| `install_max_attempts` | 3 | controller |
| `replan_delay` / `max_failed_plans` | 3 s / 3 | controller |
| `initial_settle` | 1 s | controller |
| `no_route_grace` | 5 s | controller |

These are initial experiment settings, not hardware guarantees.

## Local failover (Phase 4b)

**Pending Dev's review.** Nodes never calculate routes. A node uses only the backup next hop the controller installed (ROUTE_UPDATE's `backup_next_hop`), and only when its current next hop has failed.

### Backup next hops (what the controller installs)

For a node N on the route with primary next hop P, a neighbor n of N is a valid backup only if n's own shortest path to the destination passes through **neither N nor P**. Not N: the backup would send the frame straight back (a loop). Not P: the backup would depend on the very relay whose failure it is meant to cover ("loop-free" but not "node-protecting"). In the diamond with route A > B > C, A's backup is D (D reaches C directly); B has none (its only other neighbor, A, reaches C through B). Among valid candidates the cheapest wins. Every node on a backup path that is off the primary route gets its own entry, in the same route version; a candidate that would give one node two different next hops is dropped. No valid backup is encoded as `backup_next_hop` 0.

### Installation order

Backup relays (nodes off the route that a backup points at, here D) are installed and acknowledged first, then the primary relays (which may hold backups of their own), then the source. A node therefore never holds a backup that points at a node with no entry. A route version is reported active only when all three rounds are confirmed.

### Triggers (per node, per installed route, configurable)

| | Trigger | Fires when | Why it exists |
| --- | --- | --- | --- |
| T1 | neighbor state | the current next hop is no longer a **two-way** neighbor: `lost` (no HELLO for `neighbor_timeout`) **or** `one_way` (its HELLO no longer lists this node) | A timeout-only rule cannot see a one-way failure: if the frames A sends to B are lost, A still hears B's HELLOs, so A's timer for B never expires. B stops hearing A, drops A from its HELLO list, and A sees B become one-way |
| T2 | link-layer delivery | `K` (default 3) **consecutive** unicast transmissions to the current next hop report failure; any send counts (DATA, HELLO, PROBE, forwarded frames); one success resets the count | Faster than T1: it needs a few sends, not a 3 s timeout |

The trigger is checked when a neighbor's state changes, when a send result arrives, and when a route is installed (a newly installed primary that is already failing is abandoned at once, so a controller that re-installs a dead primary cannot pull the node back onto it).

### What the link-layer callback is, and is not

The simulator models the ESP-NOW send callback: after a unicast send the sender is told, after a short delay, whether the next hop's **radio** acknowledged the frame. Results are processed in the order the frames were sent. A send is reported failed when the frame did not arrive, when the receiver is powered off, **or when the reverse direction is down or loses the acknowledgement** — so a frame can arrive and still be reported failed (`send_failed` with `delivered=true` in the trace). It is a next-hop signal. It is never an end-to-end delivery confirmation: a relay whose radio acknowledges a frame and then drops it still produces "success". The end-to-end acknowledgement remains DELIVERY_ACK from C.

*Not yet verified:* the ESP8266 ESP-NOW send-callback semantics (what it reports for unicast to an unreachable peer, how long the failure takes, whether retries are visible, and callback ordering) must be confirmed on hardware in Phase 6. `send_fail_ms` (default 30 ms) is a placeholder.

### What a node does on a trigger

1. Switch to the backup immediately, if it is a two-way neighbor and has not itself failed K sends. Frames already queued for the primary are re-routed at transmit time (DATA only; trace `frame_rerouted`).
2. Flood a LOCAL_FAILOVER_REPORT (`from_next_hop` the abandoned hop, `to_next_hop` the backup, `reason` 1 or 2). It is repeated every `report_interval` up to `failover_report_repeats` times (default 5) or until a newer version replaces the entry, so a controller that was unreachable the first time can still learn of it.
3. Trace `local_failover` with the trigger, the failed hop, and its timing (`since_last_hello_ms` for T1, `consecutive_failures` and `since_first_failure_ms` for T2).
4. If there is no backup, or the backup is unusable, or the backup itself fails: the entry becomes **no route** locally (`local_no_route`), new readings are dropped with reason `no_route`, and a report with `to_next_hop` 0 is sent (same bounded repeats).

### No local switch-back

A node that failed over stays on the backup until a **newer version** from the controller replaces the entry. A duplicate of the held version changes nothing. Returning to the primary is the controller's decision, taken by policy and with the hold-down; this keeps a flapping neighbor from making the node flap, and keeps two nodes from disagreeing about the route.

### Added RAM per node (estimate, not measured)

| Item | Bytes |
| --- | --- |
| Per installed entry: failed-over and no-route flags, reports left | 3 per entry (about 24 for 8) |
| Send-failure counter and first-failure time per neighbor | 6 per neighbor (about 12 for 2, 48 for 8) |
| Out-of-order verdict slots (ticket, neighbor, ok) | a few bytes; only reordering fills them |
| **Total** | **about 0.05 KB typical, 0.1 KB worst case** |

### Defaults added in Phase 4b

| Setting | Default | Where |
| --- | --- | --- |
| `local_failover` | off (experiments turn it on) | node |
| `failover_t1` | on | node |
| `failover_t2_k` | 3 (0 = T2 off) | node |
| `send_fail_ms` | 30 ms (placeholder, verify in Phase 6) | simulator link layer |
| `failover_report_repeats` | 5 | node |
| `backup_next_hops` | off (experiments turn it on) | controller |

Local failover and backups are off by default only so that Phase 4a scenarios keep their exact behaviour; the Phase 4b scenarios turn them on.

### Change to a defined payload (Phase 4b)

LOCAL_FAILOVER_REPORT was defined in Phase 4a but never sent. Phase 4b needed to say "no backup" and to name the second trigger, so `to_next_hop` may now be `0` and `reason` may be `2`. The layout and size (12 bytes) are unchanged. `valid-failover-report` is unchanged; five examples were added (`valid-failover-report-send-failures`, `valid-failover-report-no-backup`, `invalid-failover-reason-3`, `invalid-failover-from-zero`, `invalid-failover-to-255`). A firmware decoder written against the Phase 4a text must accept `to_next_hop` 0 and `reason` 2.

## What node C observes about a reading (Phase 5a)

**Pending Dev's review.** For the dashboard, C reports every DATA frame it accepts (and every repeat) to the gateway with the evidence C itself has. On hardware this is the line C writes to the serial port; in simulation it is a `ReadingEvent`.

| Field | Source |
| --- | --- |
| `origin`, `session`, `seq`, `value` | the frame |
| `via` | the **link-layer sender**: the neighbor whose radio handed C the frame (ESP-NOW reports the sender's MAC) |
| `ttl` | the frame's remaining TTL |
| `hops` | `data_ttl − ttl + 1`, the number of transmissions, assuming every node uses the configured data TTL (8) |
| `duplicate` | the duplicate filter's verdict |

C does **not** know the relays before `via`. An observed path is therefore `origin, (unknown relays...), via, C`, and with the diamond's two-hop paths `via` is the whole story: `hops` 2 and `via` B means A → B → C. Nothing here comes from a routing table or from the simulator's trace; a path computed by the controller is a *plan*, a different thing.

## Reference examples

Each file in `tests/protocol-examples/` holds one case:

```json
{
  "name": "data-basic",
  "description": "Typical reading 23.45 from A to B, TTL 8.",
  "valid": true,
  "frame": { "version": 1, "type": "DATA", "src": 1, "dst": 2, "session": 168496141,
             "seq": 7, "ttl": 8, "payload": { "value": 2345 } },
  "hex": "010101020d0c0b0a07000000080429090000"
}
```

- `valid: true` files have `frame` and `hex`: encoding `frame` must give exactly `hex`, and decoding `hex` must give `frame`.
- `valid: false` files have `hex` and `reject_reason`: decoding must fail with that reason (and must not crash).

File names start with `valid-` or `invalid-`. The Python tests load every file in the directory, so adding a case there tests it automatically.

### Change to an existing example (Phase 3)

Defining HELLO made one Phase 1 example wrong by construction: `invalid-reserved-type-hello.json` expected HELLO (0x10) to be rejected as `reserved_type`. Its bytes are unchanged, but HELLO is now a defined type, so the same frame (header only, no `count` byte) is rejected as `bad_payload_length`. The file was renamed `invalid-hello-empty-payload.json` and its `reject_reason` changed; `invalid-reserved-type-route-update.json` took over the "still reserved" case until Phase 4a (see below). All other Phase 1 and Phase 2 examples are untouched.

### Change to an existing example (Phase 4a)

Defining ROUTE_UPDATE made the last "still reserved" example wrong by construction, exactly as HELLO did in Phase 3. `invalid-reserved-type-route-update.json` held a header-only frame of type `0x12` and expected `reserved_type`. Its bytes are unchanged, but ROUTE_UPDATE is now a defined type with an 11-byte payload, so the same frame is rejected as `bad_payload_length`. The file was renamed `invalid-route-update-empty-payload.json` and its `reject_reason` changed. No code is reserved any more, so no example expects `reserved_type`; the test suite covers that path with a unit test. The Phase 3 TOPOLOGY_REPORT examples are unchanged (the RTT trailer is optional); 27 new examples cover the Phase 4a payloads. A firmware implementation written against the Phase 1 example must accept this change.

## Open for Dev's review

- Is a 14-byte header with 32-bit session and sequence acceptable on the ESP8266, or should `seq` shrink?
- Is `value` as int32 hundredths fine for the eventual sensor? (Phase 8 may refine this without changing the header.)
- Rejection-reason names and order: confirm they are practical to implement and count in firmware.
- **Phase 4a:** the optional RTT trailer on TOPOLOGY_REPORT (is a trailer easier or harder than a wider entry for the firmware packer?), `0` as "none" in the next-hop fields, the 11- and 10-byte route payloads, the epoch rule (start time in seconds), and whether `max_routes` 8 and the 5-sample RTT window fit the ESP8266's heap.
- **Phase 5a:** TELEMETRY (counters, 32 bytes, flooded every 5 s) and whether the nine counters are the right set; the reading evidence C reports (`via` from the link-layer sender, `hops` from TTL) and that the ESP8266 can report the ESP-NOW sender MAC and the frame TTL to the serial line per reading; what the serial JSON line looks like (not specified yet).
- **Phase 4b:** the node-protecting backup rule and three-round installation order; T1 (two-way lost or one-way) and T2 (K consecutive failures, default 3); `to_next_hop` 0 and reason 2 in LOCAL_FAILOVER_REPORT; whether report repeats (5, one per report interval) are acceptable; and that the ESP8266 ESP-NOW send callback behaves as modelled (to be verified in Phase 6).
- **Phase 4a:** a probe consumes a sequence number, like every created frame. If that is too many numbers per second for your counter or the duplicate window, say so and probes can use a separate counter.
