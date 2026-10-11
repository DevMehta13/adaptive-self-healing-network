# Node protocol

**Draft — pending Dev's review.** Written by Harshal (backend owner) for Dev, who will implement the same format in C++ on the ESP8266. Scope so far: the frame header, DATA, DELIVERY_ACK (Phase 1), and forwarding/delivery behaviour (Phase 2; no format change). The Python implementation is `backend/src/iot_backend/protocol/`; the shared reference examples are in [tests/protocol-examples/](../tests/protocol-examples/). If this document and the examples disagree, tell Harshal — they are meant to agree exactly.

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
| `0x10` | HELLO | Reserved |
| `0x11` | TOPOLOGY_REPORT | Reserved |
| `0x12` | ROUTE_UPDATE | Reserved |
| `0x13` | ROUTE_INSTALL_ACK | Reserved |
| `0x14` | PROBE | Reserved |
| `0x15` | PROBE_REPLY | Reserved |
| `0x16` | LOCAL_FAILOVER_REPORT | Reserved |

Codes `0x01–0x0F` are the data plane; `0x10` and above are management. Reserved types have no payload definition yet and **are rejected** by a Phase 1 decoder.

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
| 9 | Payload size is wrong for the type (DATA ≠ 4, DELIVERY_ACK ≠ 9) | `bad_payload_length` |
| 10 | (DELIVERY_ACK) `origin` is 0 or 255 | `bad_node_id` |
| 11 | (DELIVERY_ACK) acknowledged `session` is 0 | `bad_session` |

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

- **Only the final destination acknowledges.** It creates one DELIVERY_ACK per accepted DATA frame, addressed to the reading's origin; relays only forward it. The ACK is routed back through the routing tables like any frame. In Phase 3 plan.md moves acknowledgements onto bounded management flooding; the ACK format does not change.
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

`not_permitted_neighbor`, `ttl_expired`, `no_route`, `queue_full`, `node_stopped` (frame arrived at a powered-off node; simulator only), `stale_sequence`, `unexpected_type`, and the decoding reasons. Firmware should keep a small counter per reason.

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

## Open for Dev's review

- Is a 14-byte header with 32-bit session and sequence acceptable on the ESP8266, or should `seq` shrink?
- Is `value` as int32 hundredths fine for the eventual sensor? (Phase 8 may refine this without changing the header.)
- Rejection-reason names and order: confirm they are practical to implement and count in firmware.
