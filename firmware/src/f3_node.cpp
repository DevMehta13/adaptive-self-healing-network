#include "f3_node.h"

#include <string.h>

namespace f3 {
namespace {
bool newer(uint32_t candidate, uint32_t reference) {
  return static_cast<int32_t>(candidate - reference) > 0;
}
bool due(uint32_t now, uint32_t when) {
  return static_cast<int32_t>(now - when) >= 0;
}
const DiscoverySettings kDefaultDiscovery = {1000, 3000, 5000, 50, 1000, 5000};
const DiscoverySettings kDisabledDiscovery = {0, 3000, 0, 50, 0, 0};
}

NodeEngine::NodeEngine(uint8_t node_id, uint32_t session,
                       const uint8_t *neighbors, uint8_t neighbor_count,
                       SendFn send, void *send_context, uint32_t ack_timeout_ms)
    : NodeEngine(node_id, session, neighbors, neighbor_count, send, send_context,
                 ack_timeout_ms, &kDisabledDiscovery, nullptr, nullptr) {}

NodeEngine::NodeEngine(uint8_t node_id, uint32_t session,
                       const uint8_t *neighbors, uint8_t neighbor_count,
                       SendFn send, void *send_context, uint32_t ack_timeout_ms,
                       const DiscoverySettings *settings, ManagementFn management,
                       void *management_context)
    : node_id_(node_id), session_(session), next_seq_(1), neighbor_count_(0),
      queue_head_(0), queue_tail_(0), queue_count_(0), send_(send),
      send_context_(send_context), ack_timeout_ms_(ack_timeout_ms),
      settings_(settings ? *settings : kDefaultDiscovery), next_hello_ms_(0),
      next_report_ms_(0), report_due_ms_(0), next_probe_ms_(0),
      next_telemetry_ms_(0), management_(management),
      management_context_(management_context),
      counters_() {
  if (session_ == 0) session_ = 1;
  if (neighbor_count > sizeof(neighbors_)) neighbor_count = sizeof(neighbors_);
  for (uint8_t i = 0; i < neighbor_count; ++i) {
    neighbors_[neighbor_count_] = neighbors[i];
    neighbors_state_[neighbor_count_].id = neighbors[i];
    ++neighbor_count_;
  }
  memset(routes_, 0, sizeof(routes_));
  memset(pending_, 0, sizeof(pending_));
  memset(sessions_, 0, sizeof(sessions_));
  memset(flood_cache_, 0, sizeof(flood_cache_));
  next_report_ms_ = settings_.hello_interval_ms;
  report_due_ms_ = settings_.hello_interval_ms;
  next_telemetry_ms_ = settings_.telemetry_interval_ms;
}

bool NodeEngine::permitted(uint8_t node) const {
  for (uint8_t i = 0; i < neighbor_count_; ++i)
    if (neighbors_[i] == node) return true;
  return false;
}

bool NodeEngine::set_route(uint8_t destination, uint8_t hop) {
  if (destination == 0) return false;
  if (hop != 0 && !permitted(hop)) return false;
  for (uint8_t i = 0; i < sizeof(routes_) / sizeof(routes_[0]); ++i) {
    if (routes_[i].destination == destination || routes_[i].destination == 0) {
      routes_[i].destination = destination;
      routes_[i].next_hop = hop;
      routes_[i].primary = hop;
      routes_[i].backup = 0;
      return true;
    }
  }
  return false;
}

NodeEngine::Route *NodeEngine::route(uint8_t destination) {
  for (uint8_t i = 0; i < sizeof(routes_) / sizeof(routes_[0]); ++i)
    if (routes_[i].destination == destination) return &routes_[i];
  return nullptr;
}

const NodeEngine::Route *NodeEngine::route(uint8_t destination) const {
  for (uint8_t i = 0; i < sizeof(routes_) / sizeof(routes_[0]); ++i)
    if (routes_[i].destination == destination) return &routes_[i];
  return nullptr;
}

uint8_t NodeEngine::next_hop(uint8_t destination) const {
  const Route *entry = route(destination);
  return entry ? entry->next_hop : 0;
}

uint8_t NodeEngine::installed_next_hop(uint8_t destination) const {
  return next_hop(destination);
}

bool NodeEngine::originate_route_update(uint8_t destination, const uint8_t payload[11]) {
  if (node_id_ != 3 || destination == 0 || !payload) return false;
  const iot::FrameView update = {1, kRouteUpdateType, node_id_, destination,
                                 session_, next_seq_++, 8, 11, payload};
  flood_new(update);
  return true;
}

bool NodeEngine::enqueue(uint8_t hop, const uint8_t *bytes, size_t len) {
  if (!permitted(hop)) { ++counters_.dropped_not_permitted; return false; }
  if (len > iot::kMaxRadioFrame || queue_count_ == kQueueCapacity) {
    ++counters_.dropped_queue_full;
    return false;
  }
  QueuedFrame &slot = queue_[queue_head_];
  slot.next_hop = hop;
  slot.len = static_cast<uint8_t>(len);
  memcpy(slot.bytes, bytes, len);
  queue_head_ = static_cast<uint8_t>((queue_head_ + 1) % kQueueCapacity);
  ++queue_count_;
  return true;
}

bool NodeEngine::enqueue_frame(uint8_t hop, const iot::FrameView &frame) {
  uint8_t bytes[iot::kMaxRadioFrame] = {};
  size_t len = 0;
  if (!iot::encode_frame(frame, bytes, sizeof(bytes), &len)) { ++counters_.dropped_decode; return false; }
  return enqueue(hop, bytes, len);
}

bool NodeEngine::create_data(uint8_t destination, int32_t value, uint32_t now_ms) {
  uint8_t hop = next_hop(destination);
  if (hop == 0) { ++counters_.dropped_no_route; return false; }
  Pending *entry = nullptr;
  for (uint8_t i = 0; i < kPendingCapacity; ++i)
    if (!pending_[i].used) { entry = &pending_[i]; break; }
  if (!entry) { ++counters_.dropped_queue_full; return false; }
  uint8_t payload[4] = {};
  memcpy(payload, &value, sizeof(value));
  const iot::FrameView frame = {1, kDataType, node_id_, destination, session_,
                                next_seq_++, 8, sizeof(payload), payload};
  size_t len = 0;
  if (!iot::encode_frame(frame, entry->bytes, sizeof(entry->bytes), &len)) return false;
  if (!enqueue(hop, entry->bytes, len)) return false;
  entry->used = true; entry->seq = frame.seq; entry->len = static_cast<uint8_t>(len);
  entry->attempts = 1; entry->sent_at_ms = now_ms; entry->awaiting_ack = true;
  ++counters_.created;
  return true;
}

void NodeEngine::forward(const iot::FrameView &frame) {
  if (frame.ttl <= 1) { ++counters_.dropped_ttl; return; }
  const uint8_t hop = next_hop(frame.dst);
  if (hop == 0) { ++counters_.dropped_no_route; return; }
  iot::FrameView outgoing = frame;
  outgoing.ttl = static_cast<uint8_t>(frame.ttl - 1);
  if (enqueue_frame(hop, outgoing)) ++counters_.forwarded;
}

NodeEngine::SessionWindow *NodeEngine::session_window(uint8_t source, uint32_t session,
                                                       uint32_t now_ms) {
  SessionWindow *oldest = &sessions_[0];
  for (uint8_t i = 0; i < kSessionCapacity; ++i) {
    SessionWindow &entry = sessions_[i];
    if (entry.used && entry.source == source && entry.session == session) {
      entry.last_used = now_ms;
      return &entry;
    }
    if (!entry.used) {
      entry.used = true; entry.source = source; entry.session = session;
      entry.highest = 0; entry.seen = 0; entry.last_used = now_ms;
      return &entry;
    }
    if (entry.last_used < oldest->last_used) oldest = &entry;
  }
  oldest->source = source; oldest->session = session; oldest->highest = 0;
  oldest->seen = 0; oldest->last_used = now_ms;
  return oldest;
}

NodeEngine::Verdict NodeEngine::observe(SessionWindow *window, uint32_t seq) {
  if (window->seen == 0) { window->highest = seq; window->seen = 1; return Verdict::kNew; }
  if (newer(seq, window->highest)) {
    const uint32_t shift = seq - window->highest;
    window->seen = shift >= 64 ? 1 : ((window->seen << shift) | 1u);
    window->highest = seq;
    return Verdict::kNew;
  }
  const uint32_t behind = window->highest - seq;
  if (behind >= 64) return Verdict::kTooOld;
  const uint64_t bit = UINT64_C(1) << behind;
  if (window->seen & bit) return Verdict::kDuplicate;
  window->seen |= bit;
  return Verdict::kNew;
}

void NodeEngine::send_ack(const iot::FrameView &data) {
  uint8_t payload[9] = {data.src};
  memcpy(payload + 1, &data.session, 4);
  memcpy(payload + 5, &data.seq, 4);
  const iot::FrameView ack = {1, kDeliveryAckType, node_id_, data.src, session_,
                              next_seq_++, 8, sizeof(payload), payload};
  flood_new(ack);
}

ReceiveResult NodeEngine::receive_data(const iot::FrameView &frame, uint32_t now_ms) {
  SessionWindow *window = session_window(frame.src, frame.session, now_ms);
  const Verdict verdict = observe(window, frame.seq);
  if (verdict == Verdict::kTooOld) { ++counters_.dropped_stale_sequence; return {false, false}; }
  if (verdict == Verdict::kDuplicate) ++counters_.duplicates;
  else ++counters_.delivered;
  send_ack(frame);
  return {true, verdict == Verdict::kDuplicate};
}

NodeEngine::Pending *NodeEngine::find_pending(uint32_t session, uint32_t seq) {
  if (session != session_) return nullptr;
  for (uint8_t i = 0; i < kPendingCapacity; ++i)
    if (pending_[i].used && pending_[i].seq == seq) return &pending_[i];
  return nullptr;
}

void NodeEngine::receive_ack(const iot::FrameView &frame) {
  if (frame.payload_len != 9 || frame.payload[0] != node_id_) { ++counters_.unexpected; return; }
  uint32_t session = 0, seq = 0;
  memcpy(&session, frame.payload + 1, 4); memcpy(&seq, frame.payload + 5, 4);
  Pending *entry = find_pending(session, seq);
  if (!entry || !entry->awaiting_ack) { ++counters_.late_acks; return; }
  entry->used = false; entry->awaiting_ack = false;
  ++counters_.acknowledged;
}

NodeEngine::Neighbor *NodeEngine::neighbor(uint8_t id) {
  for (uint8_t i = 0; i < neighbor_count_; ++i)
    if (neighbors_state_[i].id == id) return &neighbors_state_[i];
  return nullptr;
}

const NodeEngine::Neighbor *NodeEngine::neighbor(uint8_t id) const {
  for (uint8_t i = 0; i < neighbor_count_; ++i)
    if (neighbors_state_[i].id == id) return &neighbors_state_[i];
  return nullptr;
}

uint8_t NodeEngine::neighbor_state(uint8_t id) const {
  const Neighbor *entry = neighbor(id);
  return entry ? entry->state : 0;
}

void NodeEngine::request_report(uint32_t now_ms) {
  const uint32_t requested = now_ms + settings_.report_coalesce_ms;
  if (report_due_ms_ == 0 || due(report_due_ms_, requested)) report_due_ms_ = requested;
}

bool NodeEngine::seen_flood(const iot::FrameView &frame) const {
  for (uint8_t i = 0; i < flood_count_; ++i) {
    const FloodIdentity &entry = flood_cache_[i];
    if (entry.source == frame.src && entry.session == frame.session && entry.seq == frame.seq)
      return true;
  }
  return false;
}

void NodeEngine::remember_flood(const iot::FrameView &frame) {
  FloodIdentity &entry = flood_cache_[flood_next_];
  entry.source = frame.src; entry.session = frame.session; entry.seq = frame.seq;
  flood_next_ = static_cast<uint8_t>((flood_next_ + 1) % 64);
  if (flood_count_ < 64) ++flood_count_;
}

void NodeEngine::flood_new(const iot::FrameView &frame) {
  remember_flood(frame);
  if (frame.dst == node_id_ && management_) management_(management_context_, frame);
  for (uint8_t i = 0; i < neighbor_count_; ++i)
    if (enqueue_frame(neighbors_[i], frame)) ++counters_.flooded;
}

void NodeEngine::flood_received(uint8_t previous_hop, const iot::FrameView &frame,
                                uint32_t now_ms) {
  if (frame.src == node_id_) { ++counters_.own_frames; return; }
  if (seen_flood(frame)) { ++counters_.flood_duplicates; return; }
  remember_flood(frame);
  if (frame.dst == node_id_) {
    if (frame.type == kDeliveryAckType) receive_ack(frame);
    if (frame.type == kRouteUpdateType) receive_route_update(frame, now_ms);
    if (management_) management_(management_context_, frame);
    return;
  }
  if (frame.ttl <= 1) { ++counters_.dropped_ttl; return; }
  iot::FrameView outgoing = frame;
  outgoing.ttl = static_cast<uint8_t>(frame.ttl - 1);
  for (uint8_t i = 0; i < neighbor_count_; ++i) {
    if (neighbors_[i] != previous_hop && enqueue_frame(neighbors_[i], outgoing)) {
      ++counters_.flooded;
      ++counters_.forwarded;
    }
  }
}

void NodeEngine::send_route_ack(uint8_t destination, uint32_t epoch,
                                uint32_t version, uint8_t status) {
  uint8_t payload[10] = {};
  memcpy(payload, &epoch, 4); memcpy(payload + 4, &version, 4);
  payload[8] = destination; payload[9] = status;
  const iot::FrameView ack = {1, kRouteInstallAckType, node_id_, 3, session_,
                              next_seq_++, 8, sizeof(payload), payload};
  flood_new(ack);
}

void NodeEngine::receive_route_update(const iot::FrameView &frame, uint32_t now_ms) {
  if (frame.src != 3 || frame.payload_len != 11) {
    ++counters_.route_rejected;
    return;
  }
  uint32_t epoch = 0, version = 0;
  memcpy(&epoch, frame.payload, 4); memcpy(&version, frame.payload + 4, 4);
  const uint8_t destination = frame.payload[8];
  const uint8_t primary = frame.payload[9];
  const uint8_t backup = frame.payload[10];
  Route *held = route(destination);
  const bool valid = epoch != 0 && version != 0 && destination != 0 &&
      (primary == 0 || permitted(primary)) &&
      (backup == 0 || (primary != 0 && backup != primary && permitted(backup)));
  if (!valid) {
    ++counters_.route_rejected;
    send_route_ack(destination, epoch, version, 3);
    return;
  }
  if (held) {
    const bool newer_epoch = newer(epoch, held->epoch);
    const bool older_epoch = newer(held->epoch, epoch);
    if (older_epoch || (!newer_epoch && newer(held->version, version))) {
      ++counters_.route_stale;
      send_route_ack(destination, held->epoch, held->version, 2);
      return;
    }
    if (!newer_epoch && held->epoch == epoch && held->version == version) {
      send_route_ack(destination, epoch, version, 1);
      return;
    }
  } else {
    for (uint8_t i = 0; i < sizeof(routes_) / sizeof(routes_[0]); ++i)
      if (routes_[i].destination == 0) { held = &routes_[i]; break; }
    if (!held) { ++counters_.route_rejected; send_route_ack(destination, epoch, version, 3); return; }
  }
  held->destination = destination; held->next_hop = primary; held->primary = primary;
  held->backup = backup; held->epoch = epoch; held->version = version;
  held->failover_reports_left = 0; held->next_failover_report_ms = now_ms;
  ++counters_.route_installed;
  send_route_ack(destination, epoch, version, 1);
  if (primary && neighbor_state(primary) != 0 && neighbor_state(primary) != 2)
    failover(*held, 1, now_ms);
}

void NodeEngine::send_failover_report(const Route &entry, uint8_t from, uint8_t to,
                                      uint8_t reason) {
  uint8_t payload[12] = {};
  memcpy(payload, &entry.epoch, 4); memcpy(payload + 4, &entry.version, 4);
  payload[8] = entry.destination; payload[9] = from; payload[10] = to; payload[11] = reason;
  const iot::FrameView report = {1, kLocalFailoverReportType, node_id_, 3, session_,
                                 next_seq_++, 8, sizeof(payload), payload};
  flood_new(report);
}

void NodeEngine::failover(Route &entry, uint8_t reason, uint32_t now_ms) {
  if (entry.primary == 0 || entry.next_hop != entry.primary) return;
  const uint8_t from = entry.primary;
  const Neighbor *candidate = neighbor(entry.backup);
  const uint8_t to = (entry.backup && candidate && candidate->state == 2) ? entry.backup : 0;
  entry.next_hop = to;
  entry.failover_reports_left = 5;
  entry.next_failover_report_ms = now_ms;
  ++counters_.local_failovers;
  send_failover_report(entry, from, to, reason);
  --entry.failover_reports_left;
}

void NodeEngine::report_send_result(uint8_t next_hop, bool success, uint32_t now_ms) {
  Neighbor *entry = neighbor(next_hop);
  if (!entry) return;
  if (success) { entry->consecutive_failures = 0; return; }
  ++counters_.send_failures;
  if (entry->consecutive_failures < 255) ++entry->consecutive_failures;
  if (entry->consecutive_failures < 3) return;
  for (uint8_t i = 0; i < sizeof(routes_) / sizeof(routes_[0]); ++i)
    if (routes_[i].destination && routes_[i].next_hop == next_hop)
      failover(routes_[i], 2, now_ms);
}

void NodeEngine::send_hello(uint32_t now_ms) {
  uint8_t payload[1 + 5 * 8] = {};
  uint8_t count = 0;
  for (uint8_t i = 0; i < neighbor_count_; ++i) {
    const Neighbor &entry = neighbors_state_[i];
    if (!entry.heard || entry.state == 3) continue;
    payload[1 + count * 5] = entry.id;
    memcpy(payload + 2 + count * 5, &entry.session, 4);
    ++count;
  }
  payload[0] = count;
  for (uint8_t i = 0; i < neighbor_count_; ++i) {
    const iot::FrameView hello = {1, kHelloType, node_id_, neighbors_[i], session_,
                                  next_seq_++, 1, static_cast<uint8_t>(1 + 5 * count), payload};
    enqueue_frame(neighbors_[i], hello);
  }
  next_hello_ms_ = now_ms + settings_.hello_interval_ms;
}

void NodeEngine::receive_hello(uint8_t previous_hop, const iot::FrameView &frame,
                               uint32_t now_ms) {
  if (frame.dst != node_id_ || frame.src != previous_hop) { ++counters_.hello_not_local; return; }
  Neighbor *entry = neighbor(previous_hop);
  if (!entry) { ++counters_.dropped_not_permitted; return; }
  bool lists_us = false;
  for (uint8_t i = 0; i < frame.payload[0]; ++i) {
    const uint8_t *item = frame.payload + 1 + i * 5;
    uint32_t listed_session = 0;
    memcpy(&listed_session, item + 1, 4);
    if (item[0] == node_id_ && listed_session == session_) lists_us = true;
  }
  const uint8_t previous_state = entry->state;
  const uint32_t previous_session = entry->session;
  entry->heard = true; entry->session = frame.session; entry->last_hello_ms = now_ms;
  entry->state = lists_us ? 2 : 1;
  if (previous_state != entry->state || previous_session != entry->session) request_report(now_ms);
}

void NodeEngine::send_probe(uint32_t now_ms) {
  for (uint8_t i = 0; i < neighbor_count_; ++i) {
    Neighbor &entry = neighbors_state_[i];
    const iot::FrameView probe = {1, kProbeType, node_id_, entry.id, session_,
                                  next_seq_++, 1, 0, nullptr};
    entry.probe_outstanding = true; entry.probe_session = session_;
    entry.probe_seq = probe.seq; entry.probe_sent_ms = now_ms;
    enqueue_frame(entry.id, probe);
  }
  next_probe_ms_ = now_ms + settings_.probe_interval_ms;
}

void NodeEngine::receive_probe(uint8_t previous_hop, const iot::FrameView &frame) {
  if (frame.dst != node_id_ || frame.src != previous_hop) { ++counters_.probe_not_local; return; }
  uint8_t payload[8] = {};
  memcpy(payload, &frame.session, 4); memcpy(payload + 4, &frame.seq, 4);
  const iot::FrameView reply = {1, kProbeReplyType, node_id_, previous_hop, session_,
                                next_seq_++, 1, sizeof(payload), payload};
  enqueue_frame(previous_hop, reply);
}

uint16_t NodeEngine::median_rtt(const Neighbor &entry) const {
  uint16_t values[5] = {};
  for (uint8_t i = 0; i < entry.rtt_count; ++i) values[i] = entry.rtts[i];
  for (uint8_t i = 1; i < entry.rtt_count; ++i) {
    const uint16_t value = values[i]; uint8_t j = i;
    while (j > 0 && values[j - 1] > value) { values[j] = values[j - 1]; --j; }
    values[j] = value;
  }
  return values[entry.rtt_count / 2];
}

void NodeEngine::receive_probe_reply(uint8_t previous_hop, const iot::FrameView &frame,
                                     uint32_t now_ms) {
  Neighbor *entry = neighbor(previous_hop);
  uint32_t probe_session = 0, probe_seq = 0;
  memcpy(&probe_session, frame.payload, 4); memcpy(&probe_seq, frame.payload + 4, 4);
  if (frame.dst != node_id_ || frame.src != previous_hop || !entry || !entry->probe_outstanding ||
      entry->probe_session != probe_session || entry->probe_seq != probe_seq) {
    ++counters_.probe_unmatched; return;
  }
  entry->probe_outstanding = false;
  const uint32_t elapsed = now_ms - entry->probe_sent_ms;
  entry->rtts[entry->rtt_next] = elapsed > 65534 ? 65534 : static_cast<uint16_t>(elapsed);
  entry->rtt_times[entry->rtt_next] = now_ms;
  entry->rtt_next = static_cast<uint8_t>((entry->rtt_next + 1) % 5);
  if (entry->rtt_count < 5) ++entry->rtt_count;
}

void NodeEngine::expire_neighbors(uint32_t now_ms) {
  for (uint8_t i = 0; i < neighbor_count_; ++i) {
    Neighbor &entry = neighbors_state_[i];
    // RTT samples are local observations, retained for at most ten seconds.
    // Compact in place so an old measurement never makes a link look current.
    uint8_t kept = 0;
    for (uint8_t j = 0; j < entry.rtt_count; ++j) {
      if (now_ms - entry.rtt_times[j] > 10000) continue;
      entry.rtts[kept] = entry.rtts[j]; entry.rtt_times[kept] = entry.rtt_times[j];
      ++kept;
    }
    if (kept != entry.rtt_count) { entry.rtt_count = kept; entry.rtt_next = kept % 5; }
    if (entry.heard && entry.state != 3 && now_ms - entry.last_hello_ms >= settings_.neighbor_timeout_ms) {
      entry.state = 3; entry.rtt_count = 0; entry.rtt_next = 0; entry.probe_outstanding = false;
      request_report(now_ms);
    }
  }
}

void NodeEngine::check_primary_neighbors(uint32_t now_ms) {
  for (uint8_t i = 0; i < sizeof(routes_) / sizeof(routes_[0]); ++i) {
    Route &entry = routes_[i];
    if (!entry.destination) continue;
    if (entry.next_hop == entry.primary && entry.primary && neighbor_state(entry.primary) != 0 &&
        neighbor_state(entry.primary) != 2) failover(entry, 1, now_ms);
    if (entry.failover_reports_left && due(now_ms, entry.next_failover_report_ms)) {
      send_failover_report(entry, entry.primary, entry.next_hop, 1);
      --entry.failover_reports_left;
      entry.next_failover_report_ms = now_ms + settings_.report_interval_ms;
    }
  }
}

void NodeEngine::send_topology_report(uint32_t now_ms) {
  uint8_t payload[1 + 8 * 8 + 1 + 3 * 8] = {};
  uint8_t count = 0;
  for (uint8_t i = 0; i < neighbor_count_; ++i) {
    const Neighbor &entry = neighbors_state_[i];
    if (!entry.heard) continue;
    uint8_t *item = payload + 1 + count * 8;
    item[0] = entry.id; item[1] = entry.state;
    memcpy(item + 2, &entry.session, 4);
    const uint32_t age = now_ms - entry.last_hello_ms;
    const uint16_t capped_age = age > 65535 ? 65535 : static_cast<uint16_t>(age);
    memcpy(item + 6, &capped_age, 2); ++count;
  }
  payload[0] = count;
  uint8_t length = static_cast<uint8_t>(1 + count * 8);
  uint8_t rtt_count = 0;
  for (uint8_t i = 0; i < neighbor_count_; ++i) {
    const Neighbor &entry = neighbors_state_[i];
    if (entry.rtt_count < 3 || entry.state == 3) continue;
    if (rtt_count == 0) ++length;  // trailer count byte
    payload[length++] = entry.id;
    const uint16_t rtt = median_rtt(entry);
    memcpy(payload + length, &rtt, 2); length += 2; ++rtt_count;
  }
  if (rtt_count) payload[1 + count * 8] = rtt_count;
  const iot::FrameView report = {1, kTopologyReportType, node_id_, 3, session_,
                                 next_seq_++, 8, length, payload};
  flood_new(report); ++counters_.topology_reports;
  report_due_ms_ = 0; next_report_ms_ = now_ms + settings_.report_interval_ms;
}

void NodeEngine::send_telemetry() {
  uint8_t payload[32] = {};
  const uint32_t dropped = counters_.dropped_not_permitted + counters_.dropped_ttl +
      counters_.dropped_no_route + counters_.dropped_queue_full + counters_.dropped_stale_sequence +
      counters_.dropped_decode + counters_.unexpected;
  memcpy(payload, &counters_.created, 4); memcpy(payload + 4, &counters_.acknowledged, 4);
  memcpy(payload + 8, &counters_.lost, 4); memcpy(payload + 12, &counters_.dropped_no_route, 4);
  memcpy(payload + 16, &counters_.delivered, 4); memcpy(payload + 20, &counters_.forwarded, 4);
  memcpy(payload + 24, &dropped, 4);
  const uint16_t failures = 0; memcpy(payload + 30, &failures, 2);
  const iot::FrameView telemetry = {1, kTelemetryType, node_id_, 3, session_,
                                    next_seq_++, 8, sizeof(payload), payload};
  flood_new(telemetry); ++counters_.telemetry_sent;
}

ReceiveResult NodeEngine::receive(uint8_t previous_hop, const uint8_t *bytes, size_t len,
                                  uint32_t now_ms) {
  iot::FrameView frame = {};
  if (iot::decode_frame(bytes, len, &frame) != iot::DecodeError::kNone) { ++counters_.dropped_decode; return {false, false}; }
  if (!permitted(previous_hop)) { ++counters_.dropped_not_permitted; return {false, false}; }
  if (frame.type == kHelloType) { receive_hello(previous_hop, frame, now_ms); return {false, false}; }
  if (frame.type == kProbeType) { receive_probe(previous_hop, frame); return {false, false}; }
  if (frame.type == kProbeReplyType) { receive_probe_reply(previous_hop, frame, now_ms); return {false, false}; }
  if (frame.type == kTopologyReportType || frame.type == kDeliveryAckType ||
      frame.type == kRouteUpdateType || frame.type == kRouteInstallAckType ||
      frame.type == kLocalFailoverReportType || frame.type == kTelemetryType) {
    flood_received(previous_hop, frame, now_ms);
    return {false, false};
  }
  if (frame.dst != node_id_) { forward(frame); return {false, false}; }
  if (frame.type == kDataType) return receive_data(frame, now_ms);
  ++counters_.unexpected;
  return {false, false};
}

void NodeEngine::tick(uint32_t now_ms) {
  expire_neighbors(now_ms);
  check_primary_neighbors(now_ms);
  if (settings_.hello_interval_ms && due(now_ms, next_hello_ms_)) send_hello(now_ms);
  if (settings_.probe_interval_ms && due(now_ms, next_probe_ms_)) send_probe(now_ms);
  if ((report_due_ms_ && due(now_ms, report_due_ms_)) ||
      (settings_.report_interval_ms && due(now_ms, next_report_ms_))) send_topology_report(now_ms);
  if (settings_.telemetry_interval_ms && due(now_ms, next_telemetry_ms_)) {
    send_telemetry(); next_telemetry_ms_ = now_ms + settings_.telemetry_interval_ms;
  }
  for (uint8_t i = 0; i < kPendingCapacity; ++i) {
    Pending &entry = pending_[i];
    if (!entry.used || !entry.awaiting_ack || now_ms - entry.sent_at_ms < ack_timeout_ms_) continue;
    if (entry.attempts >= 3) { entry.used = false; entry.awaiting_ack = false; ++counters_.lost; continue; }
    iot::FrameView frame = {};
    if (iot::decode_frame(entry.bytes, entry.len, &frame) != iot::DecodeError::kNone) { entry.used = false; ++counters_.lost; continue; }
    const uint8_t hop = next_hop(frame.dst);
    if (hop == 0) { entry.used = false; entry.awaiting_ack = false; ++counters_.dropped_no_route; ++counters_.lost; continue; }
    if (enqueue(hop, entry.bytes, entry.len)) { ++entry.attempts; entry.sent_at_ms = now_ms; }
  }
  if (queue_count_ == 0) return;
  QueuedFrame &entry = queue_[queue_tail_];
  if (send_) send_(send_context_, entry.next_hop, entry.bytes, entry.len);
  queue_tail_ = static_cast<uint8_t>((queue_tail_ + 1) % kQueueCapacity);
  --queue_count_;
}

uint8_t NodeEngine::pending() const {
  uint8_t count = 0;
  for (uint8_t i = 0; i < kPendingCapacity; ++i) if (pending_[i].used) ++count;
  return count;
}

} // namespace f3
