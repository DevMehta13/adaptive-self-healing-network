#ifndef C_SERIAL_BRIDGE_H
#define C_SERIAL_BRIDGE_H

#include <stddef.h>
#include <stdint.h>

namespace cbridge {

// This is deliberately a fixed-size bridge.  It owns no heap memory and can be
// driven from an Arduino loop or a native fake-C test.
static const uint8_t kCapacity = 20;
static const uint8_t kWindow = 16;
static const uint32_t kHeartbeatTimeoutMs = 3000;

typedef void (*WriteLine)(void *context, const char *line, size_t len);
typedef bool (*Transmit)(void *context, uint8_t dst, const uint8_t *payload,
                         size_t payload_len);

struct Counters {
  uint32_t dropped_overflow;
  uint32_t replayed;
  uint32_t retransmitted;
  uint32_t outages;
  uint32_t rejected_down;
};

class CSerialBridge {
public:
  CSerialBridge(uint32_t boot, WriteLine write, void *write_context,
                Transmit transmit = nullptr, void *transmit_context = nullptr);

  // Announces the boot to a newly opened serial link.  `now_ms` is C's own
  // millis() clock; unsigned subtraction is used throughout.
  void start(uint32_t now_ms);
  void tick(uint32_t now_ms);

  // A radio frame C is allowed to hand to the laptop.  The frame is copied
  // before this call returns, so an ESP-NOW callback may use a short queue and
  // call this later from loop().
  bool receive_radio(const uint8_t *frame, size_t len, uint8_t via,
                     bool duplicate, uint32_t now_ms);

  // One complete laptop-to-C serial line, including or excluding its newline.
  // Invalid lines are counted and otherwise have no effect.
  void receive_down(const char *line, size_t len, uint32_t now_ms);

  const Counters &counters() const { return counters_; }
  uint32_t boot() const { return boot_; }
  uint32_t last_number() const { return last_number_; }
  uint8_t held() const;
  uint8_t unacknowledged() const;
  bool controller_reachable() const { return reachable_; }

private:
  struct Entry {
    bool used;
    bool numbered;
    uint32_t number;
    uint32_t arrived_ms;
    uint8_t via;
    bool duplicate;
    uint8_t len;
    uint8_t frame[250];
  };

  void send_hello();
  void send_status();
  void send_replay_start(uint32_t now_ms);
  void send_replay_end();
  void send_frame(Entry &entry, uint32_t now_ms, bool retransmit);
  void acknowledge(uint32_t ack);
  void pump(uint32_t now_ms);
  Entry *oldest_waiting();
  Entry *find_number(uint32_t number);
  void write_json(const char *json, size_t len);

  uint32_t boot_;
  WriteLine write_;
  void *write_context_;
  Transmit transmit_;
  void *transmit_context_;
  Entry entries_[kCapacity];
  Counters counters_;
  uint32_t last_number_;
  uint32_t last_ack_;
  uint32_t last_heartbeat_ms_;
  uint32_t outage_started_ms_;
  uint32_t replay_sent_;
  bool have_heartbeat_;
  bool reachable_;
  bool replaying_;
};

} // namespace cbridge

#endif
