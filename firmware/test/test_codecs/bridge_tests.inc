// Included by test_main.cpp, after its helpers (FakeSerial, capture_line, down, ...).
//
// Node C's bridge: the shared conformance cases (run identically by the Python reference in
// backend/tests/test_bridge_conformance.py) and the rules they stand for, stated directly.

#include "bridge_conformance.h"

namespace {

// ---- canonical text of what the bridge writes, as the conformance cases define it ----

struct CaseOutput {
  char lines[600][176];
  uint16_t count;
  uint32_t max_n;
  uint16_t overflow;  // lines that did not fit: must stay 0
};

CaseOutput g_case_output;

const char *frame_type_name(uint8_t type) {
  switch (type) {
    case 0x01: return "DATA";
    case 0x11: return "TOPOLOGY_REPORT";
    case 0x13: return "ROUTE_INSTALL_ACK";
    case 0x16: return "LOCAL_FAILOVER_REPORT";
    case 0x17: return "TELEMETRY";
    default: return "?";
  }
}

uint32_t num(const iot::SerialMessage &m, const char *key) {
  const uint32_t *value = iot::serial_number(m, key);
  return value ? *value : 0xFFFFFFFFu;
}

void collect_line(void *context, const char *line, size_t len) {
  CaseOutput *out = static_cast<CaseOutput *>(context);
  const iot::SerialMessage m =
      iot::decode_serial_line(line, len, iot::SerialDirection::kUplink);
  if (m.result != iot::SerialResult::kMessage) { ++out->overflow; return; }
  char text[176] = {};
  if (iot::text_equals(m.type, "hello")) return;
  if (iot::text_equals(m.type, "fr")) {
    const iot::TextView *hex_text = iot::serial_string(m, "hex");
    uint8_t bytes[iot::kMaxRadioFrame] = {};
    iot::FrameView frame = {};
    size_t count = hex_text ? hex_text->len / 2 : 0;
    for (size_t i = 0; i < count; ++i) {
      const char hi = hex_text->data[i * 2], lo = hex_text->data[i * 2 + 1];
      const int h = hi <= '9' ? hi - '0' : (hi | 32) - 'a' + 10;
      const int l = lo <= '9' ? lo - '0' : (lo | 32) - 'a' + 10;
      bytes[i] = static_cast<uint8_t>((h << 4) | l);
    }
    if (iot::decode_frame(bytes, count, &frame) != iot::DecodeError::kNone) { ++out->overflow; return; }
    int n = snprintf(text, sizeof(text), "fr n=%lu rp=%lu via=%lu type=%s src=%u seq=%lu",
                     static_cast<unsigned long>(num(m, "n")),
                     static_cast<unsigned long>(num(m, "rp")),
                     static_cast<unsigned long>(num(m, "via")), frame_type_name(frame.type),
                     frame.src, static_cast<unsigned long>(frame.seq));
    if (frame.type == 0x01)
      snprintf(text + n, sizeof(text) - n, " dup=%lu", static_cast<unsigned long>(num(m, "dup")));
    if (num(m, "n") > out->max_n) out->max_n = num(m, "n");
  } else if (iot::text_equals(m.type, "st")) {
    snprintf(text, sizeof(text),
             "st last=%lu low=%lu held=%lu unacked=%lu cap=%lu win=%lu drop=%lu rdrop=%lu coal=%lu rep=%lu rtx=%lu out=%lu",
             (unsigned long)num(m, "last"), (unsigned long)num(m, "low"), (unsigned long)num(m, "held"),
             (unsigned long)num(m, "unacked"), (unsigned long)num(m, "cap"), (unsigned long)num(m, "win"),
             (unsigned long)num(m, "drop"), (unsigned long)num(m, "rdrop"), (unsigned long)num(m, "coal"),
             (unsigned long)num(m, "rep"), (unsigned long)num(m, "rtx"), (unsigned long)num(m, "out"));
  } else if (iot::text_equals(m.type, "rps")) {
    snprintf(text, sizeof(text), "rps ent=%lu held=%lu drop=%lu rdrop=%lu coal=%lu out_ms=%lu",
             (unsigned long)num(m, "ent"), (unsigned long)num(m, "held"), (unsigned long)num(m, "drop"),
             (unsigned long)num(m, "rdrop"), (unsigned long)num(m, "coal"), (unsigned long)num(m, "out_ms"));
  } else if (iot::text_equals(m.type, "rpe")) {
    snprintf(text, sizeof(text), "rpe rep=%lu drop=%lu rdrop=%lu coal=%lu",
             (unsigned long)num(m, "rep"), (unsigned long)num(m, "drop"),
             (unsigned long)num(m, "rdrop"), (unsigned long)num(m, "coal"));
  } else if (iot::text_equals(m.type, "gone")) {
    const iot::TextView *kinds = iot::serial_string(m, "kinds");
    char kinds_text[72] = {};
    if (kinds && kinds->len < sizeof(kinds_text)) memcpy(kinds_text, kinds->data, kinds->len);
    snprintf(text, sizeof(text), "gone from=%lu to=%lu kinds=%s",
             (unsigned long)num(m, "from"), (unsigned long)num(m, "to"), kinds_text);
  } else {
    ++out->overflow;
    return;
  }
  if (out->count >= 600) { ++out->overflow; return; }
  memcpy(out->lines[out->count++], text, sizeof(text) < 176 ? sizeof(text) : 176);
}

void send_down(cbridge::CSerialBridge &bridge, uint32_t boot, const char *type,
               const char *key, uint32_t value, uint32_t now) {
  char json[96] = {}, line[128] = {};
  size_t len = 0;
  if (strcmp(type, "hb") == 0 || strcmp(type, "ack") == 0)
    snprintf(json, sizeof(json), "{\"v\":1,\"t\":\"%s\",\"b\":%lu,\"ack\":%lu}", type,
             (unsigned long)boot, (unsigned long)value);
  else
    snprintf(json, sizeof(json), "{\"v\":1,\"t\":\"%s\",\"b\":%lu,\"%s\":%lu}", type,
             (unsigned long)boot, key, (unsigned long)value);
  TEST_ASSERT_TRUE(iot::encode_serial_json(json, strlen(json), line, sizeof(line), &len));
  bridge.receive_down(line, len, now);
}

void run_bridge_case(const BridgeCase &c) {
  CaseOutput &out = g_case_output;
  out.count = 0; out.max_n = 0; out.overflow = 0;
  cbridge::CSerialBridge bridge(c.boot, collect_line, &out, nullptr, nullptr, c.capacity, c.window);
  bridge.start(0);
  out.count = 0;  // the hello is not part of a case
  uint32_t now = 0, ack_state = 0;
  for (size_t i = 0; i < c.step_count; ++i) {
    const BridgeStep &step = c.steps[i];
    while (now + 20 <= step.t) { now += 20; bridge.tick(now); }
    now = step.t;
    if (step.op == 0) {
      bridge.receive_radio(step.frame, step.frame_len, step.via, step.dup != 0, now);
    } else if (step.op == 3) {
      send_down(bridge, c.boot, "rs", "from", step.from, now);
    } else {
      if (step.ack == -2) ack_state = out.max_n;
      else if (step.ack >= 0) ack_state = static_cast<uint32_t>(step.ack);
      send_down(bridge, c.boot, step.op == 1 ? "hb" : "ack", "ack", ack_state, now);
    }
  }
  while (now + 20 <= c.end_ms) { now += 20; bridge.tick(now); }

  TEST_ASSERT_EQUAL_UINT16_MESSAGE(0, out.overflow, c.name);
  char message[160];
  const size_t common = out.count < c.expected_count ? out.count : c.expected_count;
  for (size_t i = 0; i < common; ++i) {
    snprintf(message, sizeof(message), "%s: line %u", c.name, (unsigned)i);
    TEST_ASSERT_EQUAL_STRING_MESSAGE(c.expected[i], out.lines[i], message);
  }
  snprintf(message, sizeof(message), "%s: number of lines", c.name);
  TEST_ASSERT_EQUAL_UINT_MESSAGE(c.expected_count, out.count, message);

  const cbridge::Counters &k = bridge.counters();
  char final_text[200] = {};
  snprintf(final_text, sizeof(final_text),
           "final size=%u held=%u unacked=%u drop=%lu rdrop=%lu coal=%lu rep=%lu rtx=%lu out=%lu gone=%lu evicted_sent=%lu buffered=%lu",
           bridge.size(), bridge.held(), bridge.unacknowledged(), (unsigned long)k.dropped_overflow,
           (unsigned long)k.dropped_readings, (unsigned long)k.coalesced, (unsigned long)k.replayed,
           (unsigned long)k.retransmitted, (unsigned long)k.outages, (unsigned long)k.gone,
           (unsigned long)k.evicted_sent, (unsigned long)k.buffered);
  snprintf(message, sizeof(message), "%s: final counters", c.name);
  TEST_ASSERT_EQUAL_STRING_MESSAGE(c.final_line, final_text, message);
}

void test_bridge_conforms_to_every_shared_case() {
  TEST_ASSERT_GREATER_OR_EQUAL_UINT(8, kBridgeCaseCount);
  for (size_t i = 0; i < kBridgeCaseCount; ++i) run_bridge_case(kBridgeCases[i]);
}

// ---- the rules, stated directly ----

struct Frames {
  static size_t build(uint8_t type, uint8_t src, uint32_t seq, const uint8_t *payload,
                      uint8_t payload_len, uint8_t *out) {
    const iot::FrameView frame = {1, type, src, 3, 500u + src, seq, 8, payload_len, payload};
    size_t written = 0;
    TEST_ASSERT_TRUE(iot::encode_frame(frame, out, iot::kMaxRadioFrame, &written));
    return written;
  }
  static size_t data(uint32_t seq, uint8_t *out) {
    const uint8_t p[] = {0xD0, 0x07, 0, 0};
    return build(0x01, 1, seq, p, sizeof(p), out);
  }
  static size_t report(uint8_t src, uint32_t seq, uint8_t *out) {
    const uint8_t p[] = {0};  // no entries
    return build(0x11, src, seq, p, sizeof(p), out);
  }
  static size_t telemetry(uint8_t src, uint32_t seq, uint8_t *out) {
    uint8_t p[32] = {};
    return build(0x17, src, seq, p, sizeof(p), out);
  }
  static size_t failover(uint32_t seq, uint8_t *out) {
    const uint8_t p[] = {0xE8, 3, 0, 0, 1, 0, 0, 0, 3, 2, 4, 2};  // epoch 1000, v1, C, B -> D
    return build(0x16, 1, seq, p, sizeof(p), out);
  }
};

// A bridge that has heard the laptop once and then gone silent.
struct SilentBridge {
  FakeSerial serial;
  cbridge::CSerialBridge bridge;
  uint32_t now;
  explicit SilentBridge(uint8_t capacity, uint8_t window)
      : serial(), bridge(55, capture_line, &serial, nullptr, nullptr, capacity, window), now(0) {
    bridge.start(0);
    down(bridge, "{\"v\":1,\"t\":\"hb\",\"b\":0,\"ack\":0}", 0);
    while (now < 3500) { now += 20; bridge.tick(now); }
    TEST_ASSERT_FALSE(bridge.controller_reachable());
  }
  void radio(const uint8_t *frame, size_t len, uint8_t via = 2) {
    now += 20;
    bridge.receive_radio(frame, len, via, false, now);
  }
};

void test_bridge_counts_a_dropped_reading_as_a_reading_and_a_dropped_report_as_not() {
  SilentBridge s(4, 2);
  uint8_t f[iot::kMaxRadioFrame];
  for (uint8_t i = 1; i <= 4; ++i) s.radio(f, Frames::data(i, f));
  s.radio(f, Frames::report(1, 1, f));  // worth less than every reading: dropped itself
  TEST_ASSERT_EQUAL_UINT32(1, s.bridge.counters().dropped_overflow);
  TEST_ASSERT_EQUAL_UINT32(0, s.bridge.counters().dropped_readings);
  s.radio(f, Frames::data(5, f));  // a reading must go: the oldest
  TEST_ASSERT_EQUAL_UINT32(2, s.bridge.counters().dropped_overflow);
  TEST_ASSERT_EQUAL_UINT32(1, s.bridge.counters().dropped_readings);
  TEST_ASSERT_EQUAL_UINT8(4, s.bridge.held());
}

void test_bridge_evicts_management_before_readings_and_never_a_failover_report() {
  SilentBridge s(6, 4);
  uint8_t f[iot::kMaxRadioFrame];
  s.radio(f, Frames::failover(40, f));
  for (uint8_t node = 1; node <= 4; node += 3) s.radio(f, Frames::report(node, 1, f));  // A and D
  s.radio(f, Frames::telemetry(2, 1, f));
  s.radio(f, Frames::telemetry(4, 1, f));
  for (uint8_t i = 1; i <= 8; ++i) s.radio(f, Frames::data(i, f));
  // Six slots: the failover report and five readings; every management entry was given up first.
  TEST_ASSERT_EQUAL_UINT8(6, s.bridge.held());
  TEST_ASSERT_EQUAL_UINT32(3, s.bridge.counters().dropped_readings);  // 8 readings, room for 5
  TEST_ASSERT_EQUAL_UINT32(3 + 4, s.bridge.counters().dropped_overflow);  // plus 4 management
}

void test_bridge_keeps_only_the_newest_report_and_telemetry_per_node() {
  SilentBridge s(20, 4);
  uint8_t f[iot::kMaxRadioFrame];
  for (uint8_t seq = 1; seq <= 5; ++seq) {
    s.radio(f, Frames::report(1, seq, f), seq % 2 ? 2 : 4);  // flooded: both relays
    s.radio(f, Frames::telemetry(1, seq, f), 4);
  }
  TEST_ASSERT_EQUAL_UINT8(2, s.bridge.size());
  TEST_ASSERT_EQUAL_UINT32(8, s.bridge.counters().coalesced);
  TEST_ASSERT_EQUAL_UINT32(0, s.bridge.counters().dropped_overflow);
}

void test_bridge_status_and_replay_markers_report_readings_lost() {
  SilentBridge s(6, 4);
  uint8_t f[iot::kMaxRadioFrame];
  for (uint8_t i = 1; i <= 8; ++i) s.radio(f, Frames::data(i, f));
  s.radio(f, Frames::report(1, 1, f));
  s.serial.count = 0;
  for (uint8_t beat = 0; beat < 6; ++beat) {
    down(s.bridge, "{\"v\":1,\"t\":\"hb\",\"b\":0,\"ack\":0}", s.now);  // boot 0: no ack, never matches
    for (uint8_t k = 0; k < 50; ++k) { s.now += 20; s.bridge.tick(s.now); }
  }
  const iot::SerialMessage start = [&] {
    for (uint8_t i = 0; i < s.serial.count; ++i) {
      const iot::SerialMessage m = iot::decode_serial_line(s.serial.lines[i], s.serial.lens[i],
                                                           iot::SerialDirection::kUplink);
      if (m.result == iot::SerialResult::kMessage && iot::text_equals(m.type, "rps")) return m;
    }
    return iot::SerialMessage();
  }();
  TEST_ASSERT_EQUAL_UINT32(3, num(start, "drop"));
  TEST_ASSERT_EQUAL_UINT32(2, num(start, "rdrop"));
}

void test_bridge_names_evicted_numbered_readings_in_a_gone_answer() {
  FakeSerial serial = {};
  cbridge::CSerialBridge bridge(55, capture_line, &serial, nullptr, nullptr, 6, 4);
  bridge.start(0);
  down(bridge, "{\"v\":1,\"t\":\"hb\",\"b\":0,\"ack\":0}", 0);
  uint8_t f[iot::kMaxRadioFrame];
  for (uint8_t i = 1; i <= 4; ++i) bridge.receive_radio(f, Frames::data(i, f), 2, false, 20 * i);  // live
  TEST_ASSERT_EQUAL_UINT8(4, bridge.unacknowledged());
  uint32_t now = 100;
  while (now < 4200) { now += 20; bridge.tick(now); }
  for (uint8_t i = 5; i <= 11; ++i) bridge.receive_radio(f, Frames::data(i, f), 2, false, now += 20);
  TEST_ASSERT_EQUAL_UINT32(4, bridge.counters().evicted_sent);
  down(bridge, "{\"v\":1,\"t\":\"hb\",\"b\":0,\"ack\":0}", now);
  down(bridge, "{\"v\":1,\"t\":\"rs\",\"b\":55,\"from\":1}", now + 20);
  bool found = false;
  for (uint8_t i = 0; i < serial.count; ++i) {
    const iot::SerialMessage m = iot::decode_serial_line(serial.lines[i], serial.lens[i],
                                                         iot::SerialDirection::kUplink);
    if (m.result == iot::SerialResult::kMessage && iot::text_equals(m.type, "gone")) {
      const iot::TextView *kinds = iot::serial_string(m, "kinds");
      TEST_ASSERT_TRUE(kinds != nullptr);
      TEST_ASSERT_EQUAL_UINT(4, kinds->len);
      TEST_ASSERT_EQUAL_INT(0, memcmp(kinds->data, "RRRR", 4));
      found = true;
    }
  }
  TEST_ASSERT_TRUE(found);
}

void test_bridge_default_capacity_is_the_documented_50_and_fits_the_board() {
  TEST_ASSERT_EQUAL_UINT8(50, cbridge::kDefaultCapacity);
  FakeSerial serial = {};
  cbridge::CSerialBridge bridge(1, capture_line, &serial);
  TEST_ASSERT_EQUAL_UINT8(50, bridge.capacity());
  // Fixed memory: the whole bridge, slots included, stays well inside the ESP32's free heap
  // (about 243 KB measured with the previous image).
  TEST_ASSERT_LESS_THAN_UINT32(48u * 1024u, sizeof(cbridge::CSerialBridge));
}

}  // namespace
