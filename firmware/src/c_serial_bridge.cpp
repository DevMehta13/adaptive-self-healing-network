#include "c_serial_bridge.h"

#include "iot_codec.h"

#include <stdio.h>
#include <string.h>

namespace cbridge {
namespace {

bool is_uplink_type(uint8_t type) {
  return type == 0x01 || type == 0x11 || type == 0x13 || type == 0x16 ||
         type == 0x17;
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

} // namespace

CSerialBridge::CSerialBridge(uint32_t boot, WriteLine write, void *write_context,
                             Transmit transmit, void *transmit_context)
    : boot_(boot), write_(write), write_context_(write_context), transmit_(transmit),
      transmit_context_(transmit_context), counters_(), last_number_(0), last_ack_(0),
      last_heartbeat_ms_(0), outage_started_ms_(0), replay_sent_(0),
      have_heartbeat_(false), reachable_(false), replaying_(false) {
  memset(entries_, 0, sizeof(entries_));
  memset(&counters_, 0, sizeof(counters_));
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
                         static_cast<unsigned long>(boot_), kCapacity, kWindow);
  if (n > 0 && static_cast<size_t>(n) < sizeof(json)) write_json(json, n);
}

uint8_t CSerialBridge::held() const {
  uint8_t count = 0;
  for (uint8_t i = 0; i < kCapacity; ++i)
    if (entries_[i].used && !entries_[i].numbered) ++count;
  return count;
}

uint8_t CSerialBridge::unacknowledged() const {
  uint8_t count = 0;
  for (uint8_t i = 0; i < kCapacity; ++i)
    if (entries_[i].used && entries_[i].numbered) ++count;
  return count;
}

void CSerialBridge::send_status() {
  uint32_t low = last_number_ + 1;
  for (uint8_t i = 0; i < kCapacity; ++i)
    if (entries_[i].used && entries_[i].numbered && entries_[i].number < low)
      low = entries_[i].number;
  char json[260] = {};
  const int n = snprintf(
      json, sizeof(json),
      "{\"v\":1,\"t\":\"st\",\"b\":%lu,\"last\":%lu,\"low\":%lu,\"reach\":%u,\"held\":%u,\"unacked\":%u,\"cap\":%u,\"win\":%u,\"drop\":%lu,\"coal\":0,\"rep\":%lu,\"rtx\":%lu,\"out\":%lu}",
      static_cast<unsigned long>(boot_), static_cast<unsigned long>(last_number_),
      static_cast<unsigned long>(low), reachable_ ? 1 : 0, held(), unacknowledged(),
      kCapacity, kWindow, static_cast<unsigned long>(counters_.dropped_overflow),
      static_cast<unsigned long>(counters_.replayed),
      static_cast<unsigned long>(counters_.retransmitted),
      static_cast<unsigned long>(counters_.outages));
  if (n > 0 && static_cast<size_t>(n) < sizeof(json)) write_json(json, n);
}

void CSerialBridge::send_replay_start(uint32_t now_ms) {
  char json[180] = {};
  const int n = snprintf(json, sizeof(json),
                         "{\"v\":1,\"t\":\"rps\",\"b\":%lu,\"ent\":%u,\"held\":%u,\"drop\":0,\"coal\":0,\"out_ms\":%lu}",
                         static_cast<unsigned long>(boot_), held(), held(),
                         static_cast<unsigned long>(now_ms - outage_started_ms_));
  if (n > 0 && static_cast<size_t>(n) < sizeof(json)) write_json(json, n);
}

void CSerialBridge::send_replay_end() {
  char json[130] = {};
  const int n = snprintf(json, sizeof(json),
                         "{\"v\":1,\"t\":\"rpe\",\"b\":%lu,\"rep\":%lu,\"drop\":0,\"coal\":0}",
                         static_cast<unsigned long>(boot_),
                         static_cast<unsigned long>(replay_sent_));
  if (n > 0 && static_cast<size_t>(n) < sizeof(json)) write_json(json, n);
}

void CSerialBridge::start(uint32_t now_ms) {
  (void)now_ms;
  send_hello();
}

CSerialBridge::Entry *CSerialBridge::oldest_waiting() {
  Entry *oldest = nullptr;
  for (uint8_t i = 0; i < kCapacity; ++i) {
    Entry &entry = entries_[i];
    if (entry.used && !entry.numbered &&
        (!oldest || static_cast<int32_t>(entry.arrived_ms - oldest->arrived_ms) < 0))
      oldest = &entry;
  }
  return oldest;
}

CSerialBridge::Entry *CSerialBridge::find_number(uint32_t number) {
  for (uint8_t i = 0; i < kCapacity; ++i)
    if (entries_[i].used && entries_[i].numbered && entries_[i].number == number)
      return &entries_[i];
  return nullptr;
}

void CSerialBridge::send_frame(Entry &entry, uint32_t now_ms, bool retransmit) {
  if (!entry.numbered) {
    entry.numbered = true;
    entry.number = ++last_number_;
  }
  char frame_hex[iot::kMaxRadioFrame * 2 + 1] = {};
  hex(entry.frame, entry.len, frame_hex);
  iot::FrameView decoded = {};
  const bool is_data = iot::decode_frame(entry.frame, entry.len, &decoded) ==
                       iot::DecodeError::kNone && decoded.type == 0x01;
  const uint32_t age = now_ms - entry.arrived_ms;
  const bool late = retransmit || replaying_ || age != 0;
  char json[iot::kMaxSerialJson + 1] = {};
  const int n = is_data
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
  if (late) ++counters_.replayed;
  if (retransmit) ++counters_.retransmitted;
  if (replaying_) ++replay_sent_;
}

void CSerialBridge::pump(uint32_t now_ms) {
  if (!reachable_) return;
  if (unacknowledged() < kWindow) {
    Entry *entry = oldest_waiting();
    if (entry) send_frame(*entry, now_ms, false);
  }
  if (replaying_ && held() == 0) {
    replaying_ = false;
    send_replay_end();
  }
}

void CSerialBridge::acknowledge(uint32_t ack) {
  if (ack <= last_ack_ || ack > last_number_) return;
  last_ack_ = ack;
  for (uint8_t i = 0; i < kCapacity; ++i)
    if (entries_[i].used && entries_[i].numbered && entries_[i].number <= ack)
      entries_[i].used = false;
}

bool CSerialBridge::receive_radio(const uint8_t *frame, size_t len, uint8_t via,
                                  bool duplicate, uint32_t now_ms) {
  iot::FrameView decoded = {};
  if (!frame || via == 0 || via == 255 || len > iot::kMaxRadioFrame ||
      iot::decode_frame(frame, len, &decoded) != iot::DecodeError::kNone ||
      !is_uplink_type(decoded.type))
    return false;
  Entry *free_slot = nullptr;
  for (uint8_t i = 0; i < kCapacity; ++i)
    if (!entries_[i].used) { free_slot = &entries_[i]; break; }
  if (!free_slot) {
    ++counters_.dropped_overflow;
    return false;
  }
  free_slot->used = true;
  free_slot->numbered = false;
  free_slot->number = 0;
  free_slot->arrived_ms = now_ms;
  free_slot->via = via;
  free_slot->duplicate = duplicate;
  free_slot->len = static_cast<uint8_t>(len);
  memcpy(free_slot->frame, frame, len);
  pump(now_ms);
  return true;
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
    const bool returning = have_heartbeat_ && !reachable_;
    have_heartbeat_ = true;
    reachable_ = true;
    last_heartbeat_ms_ = now_ms;
    send_status();
    if (returning) {
      replaying_ = true;
      replay_sent_ = 0;
      send_replay_start(now_ms);
    }
    pump(now_ms);
  } else if (iot::text_equals(message.type, "ack")) {
    if (number_or(message, "b", 0) == boot_) acknowledge(number_or(message, "ack", 0));
    pump(now_ms);
  } else if (iot::text_equals(message.type, "rs")) {
    if (number_or(message, "b", 0) != boot_) return;
    const uint32_t from = number_or(message, "from", 0);
    for (uint32_t n = from; n <= last_number_; ++n) {
      Entry *entry = find_number(n);
      if (entry) send_frame(*entry, now_ms, true);
      if (n == UINT32_MAX) break;
    }
  } else if (iot::text_equals(message.type, "tx")) {
    const uint32_t type = number_or(message, "mt", 0);
    const iot::TextView *payload = iot::serial_string(message, "p");
    uint8_t bytes[11] = {};
    // C is the radio gateway, not a general remote transmitter: F3c admits
    // only the fixed ROUTE_UPDATE command described by the serial contract.
    if (type != 0x12 || !payload || payload->len != 22) { ++counters_.rejected_down; return; }
    for (uint8_t i = 0; i < 11; ++i) {
      const char high = payload->data[i * 2];
      const char low = payload->data[i * 2 + 1];
      // The laptop writes lowercase hex (valid-down-transmit-route-update); accept both cases.
      const int h = hex_value(high), l = hex_value(low);
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
    outage_started_ms_ = now_ms;
    ++counters_.outages;
  }
  pump(now_ms);
}

} // namespace cbridge
