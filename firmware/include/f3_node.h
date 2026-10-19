#ifndef F3_NODE_H
#define F3_NODE_H

#include "iot_codec.h"

#include <stddef.h>
#include <stdint.h>

// F3a is the fixed-memory, Phase-2 data-plane engine.  It is deliberately
// transport agnostic: ESP-NOW code owns the callback/peer-MAC boundary, while
// this class owns logical-neighbor, forwarding, retry and duplicate rules.
namespace f3 {

static const uint8_t kQueueCapacity = 16;
static const uint8_t kPendingCapacity = 16;
static const uint8_t kSessionCapacity = 8;
static const uint8_t kDataType = 0x01;
static const uint8_t kDeliveryAckType = 0x02;
static const uint8_t kHelloType = 0x10;
static const uint8_t kTopologyReportType = 0x11;
static const uint8_t kRouteUpdateType = 0x12;
static const uint8_t kRouteInstallAckType = 0x13;
static const uint8_t kProbeType = 0x14;
static const uint8_t kProbeReplyType = 0x15;
static const uint8_t kTelemetryType = 0x17;
static const uint8_t kLocalFailoverReportType = 0x16;

typedef bool (*SendFn)(void *context, uint8_t next_hop, const uint8_t *bytes,
                       size_t len);
// C uses this boundary to hand management evidence to its serial bridge.  It
// receives only frames addressed to C, never simulator or routing-table state.
typedef void (*ManagementFn)(void *context, const iot::FrameView &frame);

struct DiscoverySettings {
  uint32_t hello_interval_ms;
  uint32_t neighbor_timeout_ms;
  uint32_t report_interval_ms;
  uint32_t report_coalesce_ms;
  uint32_t probe_interval_ms;
  uint32_t telemetry_interval_ms;
};

struct Counters {
  uint32_t delivered;
  uint32_t forwarded;
  uint32_t acknowledged;
  uint32_t lost;
  uint32_t dropped_not_permitted;
  uint32_t dropped_ttl;
  uint32_t dropped_no_route;
  uint32_t dropped_queue_full;
  uint32_t dropped_stale_sequence;
  uint32_t dropped_decode;
  uint32_t unexpected;
  uint32_t duplicates;
  uint32_t late_acks;
  uint32_t created;
  uint32_t flooded;
  uint32_t flood_duplicates;
  uint32_t own_frames;
  uint32_t hello_not_local;
  uint32_t probe_not_local;
  uint32_t probe_unmatched;
  uint32_t topology_reports;
  uint32_t telemetry_sent;
  uint32_t route_installed;
  uint32_t route_rejected;
  uint32_t route_stale;
  uint32_t local_failovers;
  uint32_t send_failures;
};

struct ReceiveResult {
  bool accepted_data;
  bool duplicate;
};

class NodeEngine {
public:
  NodeEngine(uint8_t node_id, uint32_t session, const uint8_t *neighbors,
             uint8_t neighbor_count, SendFn send, void *send_context,
             uint32_t ack_timeout_ms = 1000);
  NodeEngine(uint8_t node_id, uint32_t session, const uint8_t *neighbors,
             uint8_t neighbor_count, SendFn send, void *send_context,
             uint32_t ack_timeout_ms, const DiscoverySettings *settings,
             ManagementFn management, void *management_context);

  bool set_route(uint8_t destination, uint8_t next_hop);
  // C turns a checked laptop command into a gateway-originated flooded update.
  bool originate_route_update(uint8_t destination, const uint8_t payload[11]);
  // The ESP-NOW callback is only a next-hop signal. It never acknowledges DATA.
  void report_send_result(uint8_t next_hop, bool success, uint32_t now_ms);
  bool create_data(uint8_t destination, int32_t value, uint32_t now_ms);
  ReceiveResult receive(uint8_t previous_hop, const uint8_t *bytes, size_t len,
                        uint32_t now_ms);
  // Sends at most one queued frame and schedules required end-to-end retries.
  void tick(uint32_t now_ms);

  uint8_t node_id() const { return node_id_; }
  uint32_t session() const { return session_; }
  const Counters &counters() const { return counters_; }
  uint8_t queued() const { return queue_count_; }
  uint8_t pending() const;
  // 0 absent, 1 one-way, 2 two-way, 3 lost.  Intended for concise diagnostics
  // and host tests; the controller learns the same information through reports.
  uint8_t neighbor_state(uint8_t neighbor) const;
  uint8_t installed_next_hop(uint8_t destination) const;

private:
  struct Route {
    uint8_t destination;
    uint8_t next_hop;
    uint8_t primary;
    uint8_t backup;
    uint32_t epoch;
    uint32_t version;
    uint8_t failover_reports_left;
    uint32_t next_failover_report_ms;
  };
  struct QueuedFrame { uint8_t next_hop; uint8_t bytes[iot::kMaxRadioFrame]; uint8_t len; };
  struct Pending {
    bool used;
    uint32_t seq;
    uint8_t bytes[iot::kMaxRadioFrame];
    uint8_t len;
    uint8_t attempts;
    uint32_t sent_at_ms;
    bool awaiting_ack;
  };
  struct SessionWindow {
    bool used;
    uint8_t source;
    uint32_t session;
    uint32_t highest;
    uint64_t seen;
    uint32_t last_used;
  };
  struct Neighbor {
    uint8_t id;
    uint8_t state;
    uint32_t session;
    uint32_t last_hello_ms;
    bool heard;
    bool probe_outstanding;
    uint32_t probe_session;
    uint32_t probe_seq;
    uint32_t probe_sent_ms;
    uint16_t rtts[5];
    uint32_t rtt_times[5];
    uint8_t rtt_count;
    uint8_t rtt_next;
    uint8_t consecutive_failures;
  };
  struct FloodIdentity { uint8_t source; uint32_t session; uint32_t seq; };

  bool permitted(uint8_t node) const;
  uint8_t next_hop(uint8_t destination) const;
  bool enqueue(uint8_t next_hop, const uint8_t *bytes, size_t len);
  bool enqueue_frame(uint8_t next_hop, const iot::FrameView &frame);
  void forward(const iot::FrameView &frame);
  ReceiveResult receive_data(const iot::FrameView &frame, uint32_t now_ms);
  void receive_ack(const iot::FrameView &frame);
  void send_ack(const iot::FrameView &data);
  void send_hello(uint32_t now_ms);
  void receive_hello(uint8_t previous_hop, const iot::FrameView &frame,
                     uint32_t now_ms);
  void send_probe(uint32_t now_ms);
  void receive_probe(uint8_t previous_hop, const iot::FrameView &frame);
  void receive_probe_reply(uint8_t previous_hop, const iot::FrameView &frame,
                           uint32_t now_ms);
  void send_topology_report(uint32_t now_ms);
  void send_telemetry();
  void flood_new(const iot::FrameView &frame);
  void flood_received(uint8_t previous_hop, const iot::FrameView &frame,
                      uint32_t now_ms);
  void receive_route_update(const iot::FrameView &frame, uint32_t now_ms);
  void send_route_ack(uint8_t destination, uint32_t epoch, uint32_t version,
                      uint8_t status);
  void failover(Route &route, uint8_t reason, uint32_t now_ms);
  void send_failover_report(const Route &route, uint8_t from, uint8_t to,
                            uint8_t reason);
  Route *route(uint8_t destination);
  const Route *route(uint8_t destination) const;
  bool seen_flood(const iot::FrameView &frame) const;
  void remember_flood(const iot::FrameView &frame);
  void request_report(uint32_t now_ms);
  Neighbor *neighbor(uint8_t id);
  const Neighbor *neighbor(uint8_t id) const;
  uint16_t median_rtt(const Neighbor &neighbor) const;
  void expire_neighbors(uint32_t now_ms);
  void check_primary_neighbors(uint32_t now_ms);
  Pending *find_pending(uint32_t session, uint32_t seq);
  SessionWindow *session_window(uint8_t source, uint32_t session, uint32_t now_ms);
  enum class Verdict : uint8_t { kNew, kDuplicate, kTooOld };
  Verdict observe(SessionWindow *window, uint32_t seq);

  uint8_t node_id_;
  uint32_t session_;
  uint32_t next_seq_;
  uint8_t neighbors_[8];
  uint8_t neighbor_count_;
  Neighbor neighbors_state_[8];
  Route routes_[8];
  QueuedFrame queue_[kQueueCapacity];
  uint8_t queue_head_;
  uint8_t queue_tail_;
  uint8_t queue_count_;
  Pending pending_[kPendingCapacity];
  SessionWindow sessions_[kSessionCapacity];
  FloodIdentity flood_cache_[64];
  uint8_t flood_count_;
  uint8_t flood_next_;
  SendFn send_;
  void *send_context_;
  uint32_t ack_timeout_ms_;
  DiscoverySettings settings_;
  uint32_t next_hello_ms_;
  uint32_t next_report_ms_;
  uint32_t report_due_ms_;
  uint32_t next_probe_ms_;
  uint32_t next_telemetry_ms_;
  ManagementFn management_;
  void *management_context_;
  Counters counters_;
};

} // namespace f3

#endif
