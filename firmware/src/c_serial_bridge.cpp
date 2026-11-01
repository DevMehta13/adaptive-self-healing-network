#include "c_serial_bridge.h"

#include "iot_codec.h"

#include <stdio.h>
#include <string.h>

// Node C's side of the laptop link.  The behaviour is defined by docs/PROTOCOL.md
// ("Laptop and C") and implemented in backend/src/iot_backend/simulator/bridge.py; the two
// must agree, which tests/bridge-conformance/ checks on both sides.

namespace cbridge {
namespace {

const uint8_t kDataType = 0x01;
const uint8_t kTopologyReportType = 0x11;
const uint8_t kRouteInstallAckType = 0x13;
const uint8_t kLocalFailoverReportType = 0x16;
const uint8_t kTelemetryType = 0x17;
// One resend answer never names more than this many numbers per call (a safety bound against a
// laptop that asks from a very old number after a very long run).
const uint8_t kMaxGoneMessages = 32;
const uint8_t kMaxGoneRange = 64;

bool is_uplink_type(uint8_t type) {
  return type == kDataType || type == kTopologyReportType || type == kRouteInstallAckType ||
         type == kLocalFailoverReportType || type == kTelemetryType;
}

void hex(const uint8_t *bytes, size_t len, char *out) {
  static const char digits[] = "0123456789ABCDEF";
  for (size_t i = 0; i < len; ++i) {
    out[i * 2] = digits[bytes[i] >> 4];
    out[i * 2 + 1] = digits[bytes[i] & 0x0F];
  }
  out[len * 2] = '\0';
}

int hex_value(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

uint32_t number_or(const iot::SerialMessage &message, const char *key,
                   uint32_t fallback) {
  const uint32_t *value = iot::serial_number(message, key);
  return value ? *value : fallback;
}

uint32_t read_u32(const uint8_t *p) {
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) | (static_cast<uint32_t>(p[3]) << 24);
}

} // namespace

CSerialBridge::CSerialBridge(uint32_t boot, WriteLine write, void *write_context,
                             Transmit transmit, void *transmit_context,
                             uint8_t capacity, uint8_t window)
    : boot_(boot), write_(write), write_context_(write_context), transmit_(transmit),
      transmit_context_(transmit_context), capacity_(capacity), window_(window),
      evicted_count_(0), resend_count_(0), counters_(), last_number_(0), last_ack_(0),
      last_heartbeat_ms_(0), outage_started_ms_(0), outage_dropped_(0),
      outage_dropped_readings_(0), outage_evicted_readings_(0), outage_coalesced_(0), replayed_this_time_(0),
      next_stamp_(0), have_heartbeat_(false), outage_started_valid_(false),
      reachable_(false), replaying_(false) {
  if (capacity_ > kMaxCapacity) capacity_ = kMaxCapacity;
  if (capacity_ < 2) capacity_ = 2;
  if (window_ < 1) window_ = 1;
  if (window_ >= capacity_) window_ = static_cast<uint8_t>(capacity_ - 1);
  memset(entries_, 0, sizeof(entries_));
  memset(evicted_, 0, sizeof(evicted_));
  memset(resend_, 0, sizeof(resend_));
}

void CSerialBridge::write_json(const char *json, size_t len) {
  char line[iot::kMaxSerialJson + 6] = {};
  size_t written = 0;
  if (write_ && iot::encode_serial_json(json, len, line, sizeof(line), &written))
    write_(write_context_, line, written);
}

void CSerialBridge::send_hello() {
  char json[100] = {};
  const int n = snprintf(json, sizeof(json),
                         "{\"v\":1,\"t\":\"hello\",\"b\":%lu,\"node\":3,\"cap\":%u,\"win\":%u,\"fw\":\"f2-dev\"}",
                         static_cast<unsigned long>(boot_), capacity_, window_);
  if (n > 0 && static_cast<size_t>(n) < sizeof(json)) write_json(json, n);
}

uint8_t CSerialBridge::held() const {
  uint8_t count = 0;
  for (uint8_t i = 0; i < capacity_; ++i)
    if (entries_[i].used && !entries_[i].numbered) ++count;
  return count;
}

uint8_t CSerialBridge::unacknowledged() const {
  uint8_t count = 0;
  for (uint8_t i = 0; i < capacity_; ++i)
    if (entries_[i].used && entries_[i].numbered) ++count;
  return count;
}

uint8_t CSerialBridge::size() const {
  uint8_t count = 0;
  for (uint8_t i = 0; i < capacity_; ++i)
    if (entries_[i].used) ++count;
  return count;
}

void CSerialBridge::send_status() {
  uint32_t low = last_number_ + 1;
  for (uint8_t i = 0; i < capacity_; ++i)
    if (entries_[i].used && entries_[i].numbered && entries_[i].number < low)
      low = entries_[i].number;
  char json[300] = {};
  const int n = snprintf(
      json, sizeof(json),
      "{\"v\":1,\"t\":\"st\",\"b\":%lu,\"last\":%lu,\"low\":%lu,\"reach\":1,\"held\":%u,\"unacked\":%u,\"cap\":%u,\"win\":%u,\"drop\":%lu,\"rdrop\":%lu,\"rev\":%lu,\"coal\":%lu,\"rep\":%lu,\"rtx\":%lu,\"out\":%lu}",
      static_cast<unsigned long>(boot_), static_cast<unsigned long>(last_number_),
      static_cast<unsigned long>(low), held(), unacknowledged(), capacity_, window_,
      static_cast<unsigned long>(counters_.dropped_overflow),
      static_cast<unsigned long>(counters_.dropped_readings),
      static_cast<unsigned long>(counters_.evicted_readings),
      static_cast<unsigned long>(counters_.coalesced),
      static_cast<unsigned long>(counters_.replayed),
      static_cast<unsigned long>(counters_.retransmitted),
      static_cast<unsigned long>(counters_.outages));
  if (n > 0 && static_cast<size_t>(n) < sizeof(json)) write_json(json, n);
}

void CSerialBridge::send_replay_start(uint32_t now_ms) {
  uint8_t in_outage = 0;
  for (uint8_t i = 0; i < capacity_; ++i)
    if (entries_[i].used && !entries_[i].numbered && entries_[i].in_outage) ++in_outage;
  const uint32_t out_ms = outage_started_valid_ ? now_ms - outage_started_ms_ : 0;
  char json[220] = {};
  const int n = snprintf(json, sizeof(json),
                         "{\"v\":1,\"t\":\"rps\",\"b\":%lu,\"ent\":%u,\"held\":%u,\"drop\":%lu,\"rdrop\":%lu,\"rev\":%lu,\"coal\":%lu,\"out_ms\":%lu}",
                         static_cast<unsigned long>(boot_), held(), in_outage,
                         static_cast<unsigned long>(outage_dropped_),
                         static_cast<unsigned long>(outage_dropped_readings_),
                         static_cast<unsigned long>(outage_evicted_readings_),
                         static_cast<unsigned long>(outage_coalesced_),
                         static_cast<unsigned long>(out_ms));
  if (n > 0 && static_cast<size_t>(n) < sizeof(json)) write_json(json, n);
}

void CSerialBridge::send_replay_end() {
  char json[180] = {};
  const int n = snprintf(json, sizeof(json),
                         "{\"v\":1,\"t\":\"rpe\",\"b\":%lu,\"rep\":%lu,\"drop\":%lu,\"rdrop\":%lu,\"rev\":%lu,\"coal\":%lu}",
                         static_cast<unsigned long>(boot_),
                         static_cast<unsigned long>(replayed_this_time_),
                         static_cast<unsigned long>(outage_dropped_),
                         static_cast<unsigned long>(outage_dropped_readings_),
                         static_cast<unsigned long>(outage_evicted_readings_),
                         static_cast<unsigned long>(outage_coalesced_));
  if (n > 0 && static_cast<size_t>(n) < sizeof(json)) write_json(json, n);
}

void CSerialBridge::send_gone(uint32_t first, uint32_t last) {
  char kinds[kMaxGoneRange + 1] = {};
  const uint32_t count = last - first + 1;
  for (uint32_t i = 0; i < count; ++i) kinds[i] = evicted_kind(first + i);
  char json[200] = {};
  const int n = snprintf(json, sizeof(json),
                         "{\"v\":1,\"t\":\"gone\",\"b\":%lu,\"from\":%lu,\"to\":%lu,\"kinds\":\"%s\"}",
                         static_cast<unsigned long>(boot_), static_cast<unsigned long>(first),
                         static_cast<unsigned long>(last), kinds);
  if (n > 0 && static_cast<size_t>(n) < sizeof(json)) write_json(json, n);
}

void CSerialBridge::start(uint32_t now_ms) {
  (void)now_ms;
  send_hello();
}

CSerialBridge::Entry *CSerialBridge::first_waiting() {
  Entry *oldest = nullptr;
  for (uint8_t i = 0; i < capacity_; ++i) {
    Entry &entry = entries_[i];
    if (entry.used && !entry.numbered && (!oldest || entry.stamp < oldest->stamp))
      oldest = &entry;
  }
  return oldest;
}

CSerialBridge::Entry *CSerialBridge::find_number(uint32_t number) {
  for (uint8_t i = 0; i < capacity_; ++i)
    if (entries_[i].used && entries_[i].numbered && entries_[i].number == number)
      return &entries_[i];
  return nullptr;
}

CSerialBridge::Entry *CSerialBridge::free_slot() {
  for (uint8_t i = 0; i < capacity_; ++i)
    if (!entries_[i].used) return &entries_[i];
  return nullptr;
}

// How much an entry is worth keeping: lower is given up first.  A numbered entry was already
// handed up once, so the laptop probably has it and it goes first (failover reports excepted).
uint8_t CSerialBridge::rank(const Entry &entry) {
  if (entry.numbered && entry.kind != kFailover) return 0;
  switch (entry.kind) {
    case kReport:
    case kAck:
    case kTelemetry: return 1;
    case kReading: return 2;
    default: return 3;  // kFailover: never given up while anything else can be
  }
}

bool CSerialBridge::same_key(const Entry &a, const Entry &b) {
  return a.kind == b.kind && a.key_src == b.key_src && a.key_a == b.key_a &&
         a.key_b == b.key_b && a.key_c == b.key_c;
}

void CSerialBridge::count_dropped(const Entry &entry) {
  ++counters_.dropped_overflow;
  ++outage_dropped_;
  if (entry.kind == kReading) {
    ++counters_.dropped_readings;
    ++outage_dropped_readings_;
  }
}

void CSerialBridge::remember_evicted(uint32_t number, char kind) {
  for (uint8_t i = 0; i < evicted_count_; ++i)
    if (evicted_[i].number == number) { evicted_[i].kind = kind; return; }
  if (evicted_count_ == kEvictedMemory) {  // forget the oldest: it will be reported as "?"
    memmove(&evicted_[0], &evicted_[1], sizeof(Evicted) * (kEvictedMemory - 1));
    --evicted_count_;
  }
  evicted_[evicted_count_].number = number;
  evicted_[evicted_count_].kind = kind;
  ++evicted_count_;
}

char CSerialBridge::evicted_kind(uint32_t number) const {
  for (uint8_t i = 0; i < evicted_count_; ++i)
    if (evicted_[i].number == number) return evicted_[i].kind;
  return '?';
}

// True if the incoming frame is now held (or is a repeat of one that is); false if it was the
// entry given up.
bool CSerialBridge::store(Entry &incoming) {
  // A repeat: a node repeats its failover report, and A retries a reading whose acknowledgement
  // was lost.  Keep the first copy and discard the repeat (counted as coalesced, not as lost:
  // the reading is still held).
  for (uint8_t i = 0; i < capacity_; ++i) {
    Entry &held_entry = entries_[i];
    if (held_entry.used && same_key(held_entry, incoming) &&
        (incoming.kind == kFailover || incoming.kind == kReading)) {
      ++counters_.coalesced;
      ++outage_coalesced_;
      return true;
    }
  }
  // A newer report, acknowledgement or telemetry from the same node says everything the older
  // ones did.  One sent once and not acknowledged is named as superseded, not as lost.
  for (uint8_t i = 0; i < capacity_; ++i) {
    Entry &older = entries_[i];
    if (older.used && same_key(older, incoming)) {
      older.used = false;
      ++counters_.coalesced;
      ++outage_coalesced_;
      if (older.numbered) remember_evicted(older.number, 'S');
    }
  }
  while (size() >= capacity_) {
    Entry *victim = nullptr;
    for (uint8_t i = 0; i < capacity_; ++i) {
      Entry &candidate = entries_[i];
      if (!candidate.used) continue;
      if (!victim || rank(candidate) < rank(*victim) ||
          (rank(candidate) == rank(*victim) && candidate.stamp < victim->stamp))
        victim = &candidate;
    }
    if (!victim) break;
    if (rank(incoming) < rank(*victim)) {
      // The newcomer is worth less than everything held (a report arriving at a buffer full of
      // readings): it is the one given up, and it is counted.
      count_dropped(incoming);
      return false;
    }
    victim->used = false;
    if (!victim->numbered) {
      count_dropped(*victim);
    } else {
      ++counters_.evicted_sent;
      if (victim->kind == kReading) {
        // Handed up once and never acknowledged, now gone from C.  The laptop may have it
        // (then it is not lost) or may not (a controller that died or restarted never learns
        // these numbers).  C cannot tell, so it counts them.
        ++counters_.evicted_readings;
        ++outage_evicted_readings_;
      }
      remember_evicted(victim->number, victim->kind == kReading ? 'R' : 'M');
    }
  }
  Entry *slot = free_slot();
  if (!slot) {  // cannot happen after the loop above; never lose track silently if it does
    count_dropped(incoming);
    return false;
  }
  *slot = incoming;
  slot->used = true;
  slot->stamp = next_stamp_++;
  return true;
}

bool CSerialBridge::receive_radio(const uint8_t *frame, size_t len, uint8_t via,
                                  bool duplicate, uint32_t now_ms) {
  iot::FrameView decoded = {};
  if (!frame || via == 0 || via == 255 || len > iot::kMaxRadioFrame ||
      iot::decode_frame(frame, len, &decoded) != iot::DecodeError::kNone ||
      !is_uplink_type(decoded.type))
    return false;

  Entry entry;
  memset(&entry, 0, sizeof(entry));
  entry.duplicate = duplicate;
  entry.via = via;
  entry.len = static_cast<uint8_t>(len);
  entry.arrived_ms = now_ms;
  entry.key_src = decoded.src;
  memcpy(entry.frame, frame, len);
  switch (decoded.type) {
    case kDataType:
      entry.kind = kReading;
      entry.key_a = decoded.session;
      entry.key_b = decoded.seq;
      break;
    case kTopologyReportType: entry.kind = kReport; break;
    case kRouteInstallAckType: entry.kind = kAck; break;
    case kTelemetryType: entry.kind = kTelemetry; break;
    default:  // kLocalFailoverReportType: identity is (src, epoch, version, from, to)
      entry.kind = kFailover;
      if (decoded.payload && decoded.payload_len >= 11) {
        entry.key_a = read_u32(decoded.payload);
        entry.key_b = read_u32(decoded.payload + 4);
        entry.key_c = static_cast<uint16_t>((decoded.payload[9] << 8) | decoded.payload[10]);
      }
      break;
  }

  if (reachable_ && held() == 0 && unacknowledged() < window_) {
    Entry *slot = free_slot();
    if (slot) {  // live: numbered now, kept until acknowledged
      *slot = entry;
      slot->used = true;
      slot->stamp = next_stamp_++;
      send_frame(*slot, now_ms, false);
      return true;
    }
  }
  entry.late = true;
  entry.in_outage = !reachable_;
  if (entry.in_outage) ++counters_.buffered;
  return store(entry);
}

void CSerialBridge::send_frame(Entry &entry, uint32_t now_ms, bool again) {
  if (!entry.numbered) {
    entry.numbered = true;
    entry.number = ++last_number_;
  }
  char frame_hex[iot::kMaxRadioFrame * 2 + 1] = {};
  hex(entry.frame, entry.len, frame_hex);
  const uint32_t age = now_ms - entry.arrived_ms;
  const bool late = entry.late || again;
  char json[iot::kMaxSerialJson + 1] = {};
  const int n = entry.kind == kReading
      ? snprintf(json, sizeof(json),
                 "{\"v\":1,\"t\":\"fr\",\"b\":%lu,\"n\":%lu,\"age\":%lu,\"rp\":%u,\"via\":%u,\"dup\":%u,\"hex\":\"%s\"}",
                 static_cast<unsigned long>(boot_), static_cast<unsigned long>(entry.number),
                 static_cast<unsigned long>(age), late ? 1 : 0, entry.via,
                 entry.duplicate ? 1 : 0, frame_hex)
      : snprintf(json, sizeof(json),
                 "{\"v\":1,\"t\":\"fr\",\"b\":%lu,\"n\":%lu,\"age\":%lu,\"rp\":%u,\"via\":%u,\"hex\":\"%s\"}",
                 static_cast<unsigned long>(boot_), static_cast<unsigned long>(entry.number),
                 static_cast<unsigned long>(age), late ? 1 : 0, entry.via, frame_hex);
  if (n > 0 && static_cast<size_t>(n) < sizeof(json)) write_json(json, n);
}

void CSerialBridge::pump(uint32_t now_ms) {
  if (!reachable_) return;
  // A resend the laptop asked for goes before anything waiting.
  uint8_t used = 0;
  while (used < resend_count_) {
    const uint32_t number = resend_[used++];
    Entry *entry = find_number(number);
    if (entry) {
      memmove(&resend_[0], &resend_[used], sizeof(uint32_t) * (resend_count_ - used));
      resend_count_ = static_cast<uint8_t>(resend_count_ - used);
      ++counters_.retransmitted;
      send_frame(*entry, now_ms, true);
      return;
    }
  }
  resend_count_ = 0;
  Entry *waiting = first_waiting();
  if (waiting) {
    if (unacknowledged() >= window_) return;  // wait for an acknowledgement
    ++counters_.replayed;
    ++replayed_this_time_;
    send_frame(*waiting, now_ms, false);
    return;
  }
  if (replaying_) {
    replaying_ = false;
    send_replay_end();
  }
}

void CSerialBridge::acknowledge(uint32_t ack) {
  if (ack <= last_ack_ || ack > last_number_) return;  // old news, or a number never sent
  last_ack_ = ack;
  for (uint8_t i = 0; i < capacity_; ++i)
    if (entries_[i].used && entries_[i].numbered && entries_[i].number <= ack)
      entries_[i].used = false;
  uint8_t kept = 0;
  for (uint8_t i = 0; i < resend_count_; ++i)
    if (resend_[i] > ack) resend_[kept++] = resend_[i];
  resend_count_ = kept;
  kept = 0;
  for (uint8_t i = 0; i < evicted_count_; ++i)
    if (evicted_[i].number > ack) evicted_[kept++] = evicted_[i];
  evicted_count_ = kept;
}

void CSerialBridge::resend_from(uint32_t from) {
  if (from == 0 || from > last_number_) return;
  // Held numbers at or above `from`, ascending.
  uint32_t numbers[kMaxCapacity];
  uint8_t count = 0;
  for (uint8_t i = 0; i < capacity_; ++i)
    if (entries_[i].used && entries_[i].numbered && entries_[i].number >= from)
      numbers[count++] = entries_[i].number;
  for (uint8_t i = 1; i < count; ++i) {  // insertion sort: count is small and bounded
    const uint32_t value = numbers[i];
    uint8_t j = i;
    while (j > 0 && numbers[j - 1] > value) { numbers[j] = numbers[j - 1]; --j; }
    numbers[j] = value;
  }
  // Every number from `from` up to the newest that is not held is gone: say so, with its kind.
  uint8_t messages = 0;
  uint32_t expected = from;
  for (uint8_t i = 0; i <= count && expected <= last_number_; ++i) {
    const uint32_t limit = (i < count) ? numbers[i] : last_number_ + 1;  // first held number
    if (limit > expected) {
      counters_.gone += limit - expected;
      for (uint32_t start = expected; start < limit && messages < kMaxGoneMessages;
           start += kMaxGoneRange, ++messages) {
        uint32_t end = start + kMaxGoneRange - 1;
        if (end > limit - 1) end = limit - 1;
        send_gone(start, end);
      }
    }
    if (i < count) expected = numbers[i] + 1;
  }
  for (uint8_t i = 0; i < count; ++i) {  // queue the held ones, once each
    bool queued = false;
    for (uint8_t j = 0; j < resend_count_; ++j)
      if (resend_[j] == numbers[i]) queued = true;
    if (!queued && resend_count_ < kMaxCapacity) resend_[resend_count_++] = numbers[i];
  }
}

void CSerialBridge::receive_down(const char *line, size_t len, uint32_t now_ms) {
  const iot::SerialMessage message =
      iot::decode_serial_line(line, len, iot::SerialDirection::kDownlink);
  if (message.result != iot::SerialResult::kMessage) {
    ++counters_.rejected_down;
    return;
  }
  if (iot::text_equals(message.type, "hb")) {
    const uint32_t boot = number_or(message, "b", 0);
    if (boot == boot_) acknowledge(number_or(message, "ack", 0));
    last_heartbeat_ms_ = now_ms;
    const bool was_reachable = reachable_;
    have_heartbeat_ = true;
    reachable_ = true;
    send_status();
    if (was_reachable) return;
    if (held() > 0) {
      replaying_ = true;
      replayed_this_time_ = 0;
      send_replay_start(now_ms);
    }
  } else if (iot::text_equals(message.type, "ack")) {
    if (number_or(message, "b", 0) == boot_) acknowledge(number_or(message, "ack", 0));
  } else if (iot::text_equals(message.type, "rs")) {
    if (number_or(message, "b", 0) != boot_) return;
    resend_from(number_or(message, "from", 0));
  } else if (iot::text_equals(message.type, "tx")) {
    const uint32_t type = number_or(message, "mt", 0);
    const iot::TextView *payload = iot::serial_string(message, "p");
    uint8_t bytes[11] = {};
    // C is the radio gateway, not a general remote transmitter: F3c admits
    // only the fixed ROUTE_UPDATE command described by the serial contract.
    if (type != 0x12 || !payload || payload->len != 22) { ++counters_.rejected_down; return; }
    for (uint8_t i = 0; i < 11; ++i) {
      // The laptop writes lowercase hex (valid-down-transmit-route-update); accept both cases.
      const int h = hex_value(payload->data[i * 2]), l = hex_value(payload->data[i * 2 + 1]);
      if (h < 0 || l < 0) { ++counters_.rejected_down; return; }
      bytes[i] = static_cast<uint8_t>((h << 4) | l);
    }
    if (!transmit_ || !transmit_(transmit_context_, static_cast<uint8_t>(number_or(message, "dst", 0)), bytes, sizeof(bytes)))
      ++counters_.rejected_down;
  }
}

void CSerialBridge::tick(uint32_t now_ms) {
  if (have_heartbeat_ && reachable_ && now_ms - last_heartbeat_ms_ >= kHeartbeatTimeoutMs) {
    reachable_ = false;
    replaying_ = false;       // a replay in progress stops; what was not yet sent stays held
    resend_count_ = 0;
    ++counters_.outages;
    outage_started_ms_ = last_heartbeat_ms_;
    outage_started_valid_ = true;
    outage_dropped_ = 0;
    outage_dropped_readings_ = 0;
    outage_evicted_readings_ = 0;
    outage_coalesced_ = 0;
  }
  pump(now_ms);
}

} // namespace cbridge
