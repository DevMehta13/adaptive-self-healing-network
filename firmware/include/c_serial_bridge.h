#ifndef C_SERIAL_BRIDGE_H
#define C_SERIAL_BRIDGE_H

#include <stddef.h>
#include <stdint.h>

namespace cbridge {

// This is deliberately a fixed-size bridge.  It owns no heap memory and can be
// driven from an Arduino loop or a native fake-C test.
//
// Behaviour follows docs/PROTOCOL.md "Laptop and C" and backend/simulator/bridge.py
// exactly; tests/bridge-conformance/ holds the cases both implementations must pass.
//
// kMaxCapacity slots are allocated (about 280 bytes each: a frame of up to 250 bytes plus
// bookkeeping).  The usable capacity is chosen at construction: kDefaultCapacity (the
// documented 50) unless the image asks for more, up to kMaxCapacity.
static const uint8_t kMaxCapacity = 128;
static const uint8_t kDefaultCapacity = 50;
static const uint8_t kWindow = 16;
static const uint32_t kHeartbeatTimeoutMs = 3000;
// Numbers of evicted, unacknowledged entries whose kind C still remembers so that a `gone`
// answer can name them.  Older ones are forgotten and reported as "?".
static const uint8_t kEvictedMemory = 64;

typedef void (*WriteLine)(void *context, const char *line, size_t len);
typedef bool (*Transmit)(void *context, uint8_t dst, const uint8_t *payload,
                         size_t payload_len);

struct Counters {
  uint32_t dropped_overflow;   // waiting entries given up because the buffer was full (any kind)
  uint32_t dropped_readings;   // of those, readings: the only kind whose loss is data loss
  uint32_t evicted_readings;   // readings handed up once, never acknowledged, then given up
  uint32_t coalesced;          // entries superseded by a newer one, or repeated copies discarded
  uint32_t replayed;           // waiting entries handed up late
  uint32_t retransmitted;      // numbered entries sent again on request
  uint32_t outages;
  uint32_t rejected_down;
  uint32_t buffered;           // entries stored while the controller was unreachable
  uint32_t gone;               // numbers asked for that C no longer held
  uint32_t evicted_sent;       // numbered entries given up when the buffer was full
};

class CSerialBridge {
public:
  CSerialBridge(uint32_t boot, WriteLine write, void *write_context,
                Transmit transmit = nullptr, void *transmit_context = nullptr,
                uint8_t capacity = kDefaultCapacity, uint8_t window = kWindow);

  // Announces the boot to a newly opened serial link.  `now_ms` is C's own
  // millis() clock; unsigned subtraction is used throughout.
  void start(uint32_t now_ms);
  // Call often.  Sends at most one buffered message per call (so a caller that ticks every
  // 20 ms reproduces the simulator's replay spacing) and watches the heartbeat.
  void tick(uint32_t now_ms);

  // A radio frame C is allowed to hand to the laptop.  The frame is copied
  // before this call returns, so an ESP-NOW callback may use a short queue and
  // call this later from loop().  Returns true if the frame was kept or sent, false if it
  // was invalid or was itself the entry given up (counted either way).
  bool receive_radio(const uint8_t *frame, size_t len, uint8_t via,
                     bool duplicate, uint32_t now_ms);

  // One complete laptop-to-C serial line, including or excluding its newline.
  // Invalid lines are counted and otherwise have no effect.
  void receive_down(const char *line, size_t len, uint32_t now_ms);

  const Counters &counters() const { return counters_; }
  uint32_t boot() const { return boot_; }
  uint32_t last_number() const { return last_number_; }
  uint8_t capacity() const { return capacity_; }
  uint8_t held() const;            // entries waiting to be sent for the first time
  uint8_t unacknowledged() const;  // numbered entries awaiting the laptop's acknowledgement
  uint8_t size() const;            // entries in use
  bool controller_reachable() const { return reachable_; }

private:
  enum Kind { kReading = 0, kReport = 1, kAck = 2, kTelemetry = 3, kFailover = 4 };

  struct Entry {
    bool used;
    bool numbered;
    bool late;        // it had to wait: it was not handed up the moment it arrived
    bool in_outage;   // stored while the controller was unreachable
    bool duplicate;
    uint8_t kind;
    uint8_t via;
    uint8_t len;
    uint8_t key_src;  // identity: readings (src, session, seq); reports, acks and telemetry
    uint32_t key_a;   // (src); failover reports (src, epoch, version, from, to)
    uint32_t key_b;
    uint16_t key_c;
    uint32_t number;
    uint32_t arrived_ms;
    uint32_t stamp;   // insertion order: "oldest first" within a kind
    uint8_t frame[250];
  };

  struct Evicted {
    uint32_t number;
    char kind;  // 'R' reading, 'M' other, 'S' superseded
  };

  void send_hello();
  void send_status();
  void send_replay_start(uint32_t now_ms);
  void send_replay_end();
  void send_gone(uint32_t first, uint32_t last);
  void send_frame(Entry &entry, uint32_t now_ms, bool again);
  void acknowledge(uint32_t ack);
  void resend_from(uint32_t from);
  void pump(uint32_t now_ms);
  bool store(Entry &incoming);
  Entry *find_number(uint32_t number);
  Entry *first_waiting();
  Entry *free_slot();
  static uint8_t rank(const Entry &entry);
  static bool same_key(const Entry &a, const Entry &b);
  void count_dropped(const Entry &entry);
  void remember_evicted(uint32_t number, char kind);
  char evicted_kind(uint32_t number) const;
  void write_json(const char *json, size_t len);

  uint32_t boot_;
  WriteLine write_;
  void *write_context_;
  Transmit transmit_;
  void *transmit_context_;
  uint8_t capacity_;
  uint8_t window_;
  Entry entries_[kMaxCapacity];
  Evicted evicted_[kEvictedMemory];
  uint8_t evicted_count_;
  uint32_t resend_[kMaxCapacity];
  uint8_t resend_count_;
  Counters counters_;
  uint32_t last_number_;
  uint32_t last_ack_;
  uint32_t last_heartbeat_ms_;
  uint32_t outage_started_ms_;
  uint32_t outage_dropped_;
  uint32_t outage_dropped_readings_;
  uint32_t outage_evicted_readings_;
  uint32_t outage_coalesced_;
  uint32_t replayed_this_time_;
  uint32_t next_stamp_;
  bool have_heartbeat_;
  bool outage_started_valid_;
  bool reachable_;
  bool replaying_;
};

} // namespace cbridge

#endif
