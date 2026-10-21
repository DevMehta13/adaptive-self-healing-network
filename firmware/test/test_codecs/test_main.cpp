#include <unity.h>

#include <stdlib.h>
#include <string.h>

#include "c_serial_bridge.h"
#include "f3_node.h"
#include "iot_codec.h"
#include "f1_ack.h"
#include "test_vectors.h"

extern "C" void setUp() {}
extern "C" void tearDown() {}

namespace {

void check_expected_fields(const iot::SerialMessage &message,
                           const char *expected) {
  const char *cursor = expected;
  while (*cursor) {
    const char *equals = strchr(cursor, '=');
    const char *end = strchr(cursor, ';');
    if (!end)
      end = cursor + strlen(cursor);
    TEST_ASSERT_NOT_NULL(equals);
    TEST_ASSERT_TRUE(equals < end);
    char key[18] = {};
    const size_t key_len = static_cast<size_t>(equals - cursor);
    TEST_ASSERT_LESS_THAN(sizeof(key), key_len + 1);
    memcpy(key, cursor, key_len);
    const char *value = equals + 1;
    const size_t value_len = static_cast<size_t>(end - value);
    const uint32_t *number = iot::serial_number(message, key);
    if (number) {
      char text[16] = {};
      TEST_ASSERT_LESS_THAN(sizeof(text), value_len + 1);
      memcpy(text, value, value_len);
      TEST_ASSERT_EQUAL_UINT32(
          static_cast<uint32_t>(strtoul(text, nullptr, 10)), *number);
    } else {
      const iot::TextView *string = iot::serial_string(message, key);
      TEST_ASSERT_NOT_NULL(string);
      TEST_ASSERT_EQUAL_UINT(value_len, string->len);
      TEST_ASSERT_EQUAL_MEMORY(value, string->data, value_len);
    }
    cursor = *end ? end + 1 : end;
  }
}

void test_radio_vectors() {
  TEST_ASSERT_EQUAL_UINT(79, IOT_FRAME_VECTOR_COUNT);
  for (size_t i = 0; i < IOT_FRAME_VECTOR_COUNT; ++i) {
    const iot_frame_vector &vector = IOT_FRAME_VECTORS[i];
    iot::FrameView frame = {};
    const iot::DecodeError error =
        iot::decode_frame(vector.bytes, vector.len, &frame);
    TEST_ASSERT_EQUAL_STRING_MESSAGE(vector.valid ? "" : vector.reject_reason,
                                     iot::error_name(error), vector.name);
    if (!vector.valid)
      continue;
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(vector.version, frame.version, vector.name);
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(vector.type, frame.type, vector.name);
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(vector.src, frame.src, vector.name);
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(vector.dst, frame.dst, vector.name);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(vector.session, frame.session,
                                     vector.name);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(vector.seq, frame.seq, vector.name);
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(vector.ttl, frame.ttl, vector.name);
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(vector.payload_len, frame.payload_len,
                                    vector.name);
    uint8_t encoded[iot::kMaxRadioFrame] = {};
    size_t written = 0;
    TEST_ASSERT_TRUE_MESSAGE(
        iot::encode_frame(frame, encoded, sizeof(encoded), &written),
        vector.name);
    TEST_ASSERT_EQUAL_UINT_MESSAGE(vector.len, written, vector.name);
    TEST_ASSERT_EQUAL_MEMORY_MESSAGE(vector.bytes, encoded, written,
                                     vector.name);
  }
}

void test_serial_vectors() {
  TEST_ASSERT_EQUAL_UINT(48, IOT_SERIAL_VECTOR_COUNT);
  for (size_t i = 0; i < IOT_SERIAL_VECTOR_COUNT; ++i) {
    const iot_serial_vector &vector = IOT_SERIAL_VECTORS[i];
    const iot::SerialDirection direction = vector.downlink
                                               ? iot::SerialDirection::kDownlink
                                               : iot::SerialDirection::kUplink;
    const iot::SerialMessage message =
        iot::decode_serial_line(vector.line, vector.len, direction);
    if (strcmp(vector.result, "rejected") == 0) {
      TEST_ASSERT_EQUAL_INT_MESSAGE(
          static_cast<int>(iot::SerialResult::kRejected),
          static_cast<int>(message.result), vector.name);
      TEST_ASSERT_EQUAL_STRING_MESSAGE(vector.reason, message.reason,
                                       vector.name);
    } else if (strcmp(vector.result, "log") == 0) {
      TEST_ASSERT_EQUAL_INT_MESSAGE(static_cast<int>(iot::SerialResult::kLog),
                                    static_cast<int>(message.result),
                                    vector.name);
    } else if (strcmp(vector.result, "noise") == 0) {
      TEST_ASSERT_EQUAL_INT_MESSAGE(static_cast<int>(iot::SerialResult::kNoise),
                                    static_cast<int>(message.result),
                                    vector.name);
    } else {
      TEST_ASSERT_EQUAL_INT_MESSAGE(
          static_cast<int>(iot::SerialResult::kMessage),
          static_cast<int>(message.result), vector.name);
      TEST_ASSERT_TRUE_MESSAGE(iot::text_equals(message.type, vector.type),
                               vector.name);
      check_expected_fields(message, vector.fields);
      char encoded[IOT_SERIAL_MAX_UP_LINE] = {};
      size_t written = 0;
      TEST_ASSERT_TRUE_MESSAGE(
          iot::encode_serial_json(message.json.data, message.json.len, encoded,
                                  sizeof(encoded), &written),
          vector.name);
      TEST_ASSERT_EQUAL_UINT_MESSAGE(vector.len, written, vector.name);
      TEST_ASSERT_EQUAL_MEMORY_MESSAGE(vector.line, encoded, written,
                                       vector.name);
    }
  }
}

void test_crc_reference() {
  TEST_ASSERT_EQUAL_HEX16(
      IOT_SERIAL_CRC_CHECK_VALUE,
      iot::crc16_ccitt_false(
          reinterpret_cast<const uint8_t *>(IOT_SERIAL_CRC_CHECK_INPUT), 9));
}

void test_output_bounds() {
  uint8_t frame[17] = {};
  iot::FrameView decoded = {};
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(iot::DecodeError::kNone),
      static_cast<int>(iot::decode_frame(IOT_FRAME_VECTORS[52].bytes,
                                         IOT_FRAME_VECTORS[52].len, &decoded)));
  size_t written = 99;
  TEST_ASSERT_FALSE(iot::encode_frame(decoded, frame, sizeof(frame), &written));
  decoded.payload = nullptr;
  TEST_ASSERT_FALSE(iot::encode_frame(decoded, frame, sizeof(frame), &written));
  char line[8] = {};
  TEST_ASSERT_FALSE(iot::encode_serial_json("{}", 2, line, 6, &written));

  const char too_large[] =
      "{\"v\":1,\"t\":\"hb\",\"b\":184467440737095516160,\"ack\":0}";
  char encoded[sizeof(too_large) + 5] = {};
  TEST_ASSERT_TRUE(iot::encode_serial_json(too_large, strlen(too_large),
                                           encoded, sizeof(encoded), &written));
  const iot::SerialMessage message = iot::decode_serial_line(
      encoded, written, iot::SerialDirection::kDownlink);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(iot::SerialResult::kRejected),
                        static_cast<int>(message.result));
  TEST_ASSERT_EQUAL_STRING("bad_value", message.reason);
}

void test_f1_ack_matching() {
  uint8_t payload[] = {1, 0x44, 0x33, 0x22, 0x11,
                       0x88, 0x77, 0x66, 0x55};
  iot::FrameView ack = {1, 0x02, 3, 1, 9, 10, 1,
                        static_cast<uint8_t>(sizeof(payload)), payload};
  TEST_ASSERT_TRUE(f1::delivery_ack_matches(ack, 3, 1, 1, 0x11223344,
                                            0x55667788));

  ack.type = 0x01;
  TEST_ASSERT_FALSE(f1::delivery_ack_matches(ack, 3, 1, 1, 0x11223344,
                                             0x55667788));
  ack.type = 0x02;
  ack.src = 2;
  TEST_ASSERT_FALSE(f1::delivery_ack_matches(ack, 3, 1, 1, 0x11223344,
                                             0x55667788));
  ack.src = 3;
  ack.dst = 2;
  TEST_ASSERT_FALSE(f1::delivery_ack_matches(ack, 3, 1, 1, 0x11223344,
                                             0x55667788));
  ack.dst = 1;
  payload[0] = 2;
  TEST_ASSERT_FALSE(f1::delivery_ack_matches(ack, 3, 1, 1, 0x11223344,
                                             0x55667788));
  payload[0] = 1;
  TEST_ASSERT_FALSE(f1::delivery_ack_matches(ack, 3, 1, 1, 0x11223345,
                                             0x55667788));
  TEST_ASSERT_FALSE(f1::delivery_ack_matches(ack, 3, 1, 1, 0x11223344,
                                             0x55667789));
  ack.payload_len = 8;
  TEST_ASSERT_FALSE(f1::delivery_ack_matches(ack, 3, 1, 1, 0x11223344,
                                             0x55667788));
  ack.payload_len = 9;
  ack.payload = nullptr;
  TEST_ASSERT_FALSE(f1::delivery_ack_matches(ack, 3, 1, 1, 0x11223344,
                                             0x55667788));
}

struct FakeSerial {
  char lines[80][iot::kMaxSerialJson + 6];
  size_t lens[80];
  uint8_t count;
};

void capture_line(void *context, const char *line, size_t len) {
  FakeSerial *serial = static_cast<FakeSerial *>(context);
  if (serial->count >= 80 || len > sizeof(serial->lines[0])) return;
  memcpy(serial->lines[serial->count], line, len);
  serial->lens[serial->count] = len;
  ++serial->count;
}

void down(cbridge::CSerialBridge &bridge, const char *json, uint32_t now) {
  char line[128] = {};
  size_t len = 0;
  TEST_ASSERT_TRUE(iot::encode_serial_json(json, strlen(json), line, sizeof(line), &len));
  bridge.receive_down(line, len, now);
}

uint8_t count_type(const FakeSerial &serial, const char *type, bool replayed) {
  uint8_t count = 0;
  for (uint8_t i = 0; i < serial.count; ++i) {
    const iot::SerialMessage message = iot::decode_serial_line(
        serial.lines[i], serial.lens[i], iot::SerialDirection::kUplink);
    if (message.result != iot::SerialResult::kMessage ||
        !iot::text_equals(message.type, type)) continue;
    const uint32_t *rp = iot::serial_number(message, "rp");
    if (!rp || (*rp != 0) == replayed) ++count;
  }
  return count;
}

size_t reading(uint8_t *out) {
  const uint8_t payload[] = {0xD0, 0x07, 0x00, 0x00};
  const iot::FrameView frame = {1, 0x01, 1, 3, 7, 9, 8,
                                sizeof(payload), payload};
  size_t written = 0;
  TEST_ASSERT_TRUE(iot::encode_frame(frame, out, iot::kMaxRadioFrame, &written));
  return written;
}

void test_f2_fake_c_heartbeat_ack_and_resend() {
  FakeSerial serial = {};
  cbridge::CSerialBridge bridge(77, capture_line, &serial);
  bridge.start(0);
  TEST_ASSERT_EQUAL_UINT(1, serial.count);
  TEST_ASSERT_EQUAL_UINT(0, count_type(serial, "fr", false));

  uint8_t frame[iot::kMaxRadioFrame] = {};
  const size_t len = reading(frame);
  TEST_ASSERT_TRUE(bridge.receive_radio(frame, len, 2, false, 10));
  TEST_ASSERT_EQUAL_UINT(1, bridge.held());  // C has not heard the laptop yet.
  down(bridge, "{\"v\":1,\"t\":\"hb\",\"b\":0,\"ack\":0}", 10);
  TEST_ASSERT_EQUAL_UINT(1, count_type(serial, "fr", false));
  TEST_ASSERT_EQUAL_UINT(1, bridge.unacknowledged());

  down(bridge, "{\"v\":1,\"t\":\"rs\",\"b\":77,\"from\":1}", 20);
  TEST_ASSERT_EQUAL_UINT(1, count_type(serial, "fr", true));
  TEST_ASSERT_EQUAL_UINT(1, bridge.counters().retransmitted);
  down(bridge, "{\"v\":1,\"t\":\"ack\",\"b\":77,\"ack\":1}", 21);
  TEST_ASSERT_EQUAL_UINT(0, bridge.unacknowledged());
}

void test_f2_fake_c_timeout_buffers_and_replays() {
  FakeSerial serial = {};
  cbridge::CSerialBridge bridge(88, capture_line, &serial);
  bridge.start(0);
  down(bridge, "{\"v\":1,\"t\":\"hb\",\"b\":0,\"ack\":0}", 0);
  bridge.tick(3000);
  TEST_ASSERT_FALSE(bridge.controller_reachable());
  uint8_t frame[iot::kMaxRadioFrame] = {};
  const size_t len = reading(frame);
  TEST_ASSERT_TRUE(bridge.receive_radio(frame, len, 2, false, 3010));
  TEST_ASSERT_EQUAL_UINT(1, bridge.held());
  down(bridge, "{\"v\":1,\"t\":\"hb\",\"b\":88,\"ack\":0}", 4010);
  TEST_ASSERT_TRUE(bridge.controller_reachable());
  TEST_ASSERT_EQUAL_UINT(1, count_type(serial, "rps", false));
  TEST_ASSERT_EQUAL_UINT(1, count_type(serial, "rpe", false));
  TEST_ASSERT_EQUAL_UINT(1, count_type(serial, "fr", true));
  TEST_ASSERT_EQUAL_UINT(1, bridge.counters().outages);
}

void test_f2_fake_c_buffer_is_bounded() {
  FakeSerial serial = {};
  cbridge::CSerialBridge bridge(99, capture_line, &serial);
  bridge.start(0);
  uint8_t frame[iot::kMaxRadioFrame] = {};
  const size_t len = reading(frame);
  for (uint8_t i = 0; i < cbridge::kCapacity; ++i)
    TEST_ASSERT_TRUE(bridge.receive_radio(frame, len, 2, false, i));
  TEST_ASSERT_FALSE(bridge.receive_radio(frame, len, 2, false, 30));
  TEST_ASSERT_EQUAL_UINT(cbridge::kCapacity, bridge.held());
  TEST_ASSERT_EQUAL_UINT(1, bridge.counters().dropped_overflow);
}

struct RouteCommandCapture {
  uint8_t destination;
  uint8_t payload[11];
  uint8_t calls;
};

bool capture_route_command(void *context, uint8_t destination, const uint8_t *payload,
                           size_t length) {
  RouteCommandCapture *capture = static_cast<RouteCommandCapture *>(context);
  if (length != 11 || capture->calls) return false;
  capture->destination = destination;
  memcpy(capture->payload, payload, length);
  ++capture->calls;
  return true;
}

void test_f3c_c_accepts_only_route_update_serial_commands() {
  FakeSerial serial = {};
  RouteCommandCapture command = {};
  cbridge::CSerialBridge bridge(44, capture_line, &serial, capture_route_command, &command);
  down(bridge, "{\"v\":1,\"t\":\"tx\",\"dst\":1,\"mt\":18,\"p\":\"BC02000001000000030204\"}", 0);
  TEST_ASSERT_EQUAL_UINT8(1, command.calls);
  TEST_ASSERT_EQUAL_UINT8(1, command.destination);
  TEST_ASSERT_EQUAL_HEX8(0xBC, command.payload[0]);
  TEST_ASSERT_EQUAL_HEX8(0x04, command.payload[10]);
  down(bridge, "{\"v\":1,\"t\":\"tx\",\"dst\":1,\"mt\":17,\"p\":\"BC02000001000000030204\"}", 1);
  TEST_ASSERT_EQUAL_UINT8(1, command.calls);
  TEST_ASSERT_EQUAL_UINT32(1, bridge.counters().rejected_down);
}

struct RadioMessage {
  uint8_t from;
  uint8_t to;
  uint8_t bytes[iot::kMaxRadioFrame];
  size_t len;
};

struct MockRadio {
  RadioMessage messages[128];
  uint8_t count;
  bool drop_acks;  // while set, every DELIVERY_ACK copy is lost (ACKs are flooded)
};

struct Endpoint { MockRadio *radio; uint8_t from; };

bool mock_send(void *context, uint8_t next_hop, const uint8_t *bytes, size_t len) {
  Endpoint *endpoint = static_cast<Endpoint *>(context);
  MockRadio *radio = endpoint->radio;
  if (radio->count >= 128) return false;
  RadioMessage &message = radio->messages[radio->count++];
  message.from = endpoint->from;
  message.to = next_hop;
  message.len = len;
  memcpy(message.bytes, bytes, len);
  return true;
}

struct Diamond {
  MockRadio radio;
  Endpoint ea, eb, ec, ed;
  f3::NodeEngine a, b, c, d;

  Diamond()
      : radio(), ea{&radio, 1}, eb{&radio, 2}, ec{&radio, 3}, ed{&radio, 4},
        a(1, 101, neighbors_a, 2, mock_send, &ea),
        b(2, 102, neighbors_b, 2, mock_send, &eb),
        c(3, 103, neighbors_c, 2, mock_send, &ec),
        d(4, 104, neighbors_d, 2, mock_send, &ed) {
    a.set_route(3, 2); b.set_route(3, 3); b.set_route(1, 1);
    c.set_route(1, 2); d.set_route(3, 3); d.set_route(1, 1);
  }

  void deliver(uint32_t now) {
    RadioMessage pending[128] = {};
    const uint8_t count = radio.count;
    memcpy(pending, radio.messages, sizeof(RadioMessage) * count);
    radio.count = 0;
    for (uint8_t i = 0; i < count; ++i) {
      iot::FrameView frame = {};
      const bool ack = iot::decode_frame(pending[i].bytes, pending[i].len, &frame) ==
                           iot::DecodeError::kNone && frame.type == f3::kDeliveryAckType;
      if (ack && radio.drop_acks) continue;
      if (pending[i].to == 1) a.receive(pending[i].from, pending[i].bytes, pending[i].len, now);
      else if (pending[i].to == 2) b.receive(pending[i].from, pending[i].bytes, pending[i].len, now);
      else if (pending[i].to == 3) c.receive(pending[i].from, pending[i].bytes, pending[i].len, now);
      else if (pending[i].to == 4) d.receive(pending[i].from, pending[i].bytes, pending[i].len, now);
    }
  }

  void pump(uint32_t now) {
    for (uint8_t pass = 0; pass < 32; ++pass) {
      a.tick(now); b.tick(now); c.tick(now); d.tick(now);
      if (radio.count == 0) break;
      deliver(now);
    }
  }

  static const uint8_t neighbors_a[2];
  static const uint8_t neighbors_b[2];
  static const uint8_t neighbors_c[2];
  static const uint8_t neighbors_d[2];
};

const uint8_t Diamond::neighbors_a[2] = {2, 4};
const uint8_t Diamond::neighbors_b[2] = {1, 3};
const uint8_t Diamond::neighbors_c[2] = {2, 4};
const uint8_t Diamond::neighbors_d[2] = {1, 3};

struct ManagementCapture {
  uint8_t types[32];
  uint8_t sources[32];
  uint8_t payload_lengths[32];
  uint8_t count;
};

void capture_management(void *context, const iot::FrameView &frame) {
  ManagementCapture *capture = static_cast<ManagementCapture *>(context);
  if (capture->count >= 32) return;
  const uint8_t index = capture->count++;
  capture->types[index] = frame.type;
  capture->sources[index] = frame.src;
  capture->payload_lengths[index] = frame.payload_len;
}

struct ControlDiamond {
  MockRadio radio;
  Endpoint ea, eb, ec, ed;
  ManagementCapture capture;
  f3::DiscoverySettings settings;
  f3::NodeEngine a, b, c, d;

  ControlDiamond()
      : radio(), ea{&radio, 1}, eb{&radio, 2}, ec{&radio, 3}, ed{&radio, 4}, capture(),
        settings{10, 30, 1000, 5, 10, 40},
        a(1, 201, Diamond::neighbors_a, 2, mock_send, &ea, 1000, &settings, nullptr, nullptr),
        b(2, 202, Diamond::neighbors_b, 2, mock_send, &eb, 1000, &settings, nullptr, nullptr),
        c(3, 203, Diamond::neighbors_c, 2, mock_send, &ec, 1000, &settings,
          capture_management, &capture),
        d(4, 204, Diamond::neighbors_d, 2, mock_send, &ed, 1000, &settings, nullptr, nullptr) {}

  void deliver(uint32_t now) {
    RadioMessage pending[128] = {};
    const uint8_t count = radio.count;
    memcpy(pending, radio.messages, sizeof(RadioMessage) * count);
    radio.count = 0;
    for (uint8_t i = 0; i < count; ++i) {
      if (pending[i].to == 1) a.receive(pending[i].from, pending[i].bytes, pending[i].len, now);
      else if (pending[i].to == 2) b.receive(pending[i].from, pending[i].bytes, pending[i].len, now);
      else if (pending[i].to == 3) c.receive(pending[i].from, pending[i].bytes, pending[i].len, now);
      else if (pending[i].to == 4) d.receive(pending[i].from, pending[i].bytes, pending[i].len, now);
    }
  }

  void pump(uint32_t now) {
    for (uint8_t pass = 0; pass < 64; ++pass) {
      a.tick(now); b.tick(now); c.tick(now); d.tick(now);
      if (radio.count == 0) break;
      deliver(now);
    }
  }
};

bool captured(const ManagementCapture &capture, uint8_t type, uint8_t source = 0) {
  for (uint8_t i = 0; i < capture.count; ++i)
    if (capture.types[i] == type && (source == 0 || capture.sources[i] == source)) return true;
  return false;
}

void test_f3b_hello_reports_one_way_and_stale_evidence_to_c() {
  ControlDiamond network;
  network.pump(0);   // First HELLOs are necessarily one-way: lists are empty.
  network.pump(10);  // The next HELLO round confirms both directions.
  TEST_ASSERT_EQUAL_UINT8(2, network.a.neighbor_state(2));
  network.pump(20);  // Coalesced reports reach C via permitted links.
  TEST_ASSERT_TRUE(captured(network.capture, f3::kTopologyReportType, 1));

  // A valid local HELLO from B that omits A proves only one direction.
  const uint8_t payload[] = {0};
  const iot::FrameView one_way = {1, f3::kHelloType, 2, 1, 202, 900, 1, 1, payload};
  uint8_t bytes[iot::kMaxRadioFrame] = {};
  size_t len = 0;
  TEST_ASSERT_TRUE(iot::encode_frame(one_way, bytes, sizeof(bytes), &len));
  network.a.receive(2, bytes, len, 21);
  TEST_ASSERT_EQUAL_UINT8(1, network.a.neighbor_state(2));
  network.a.tick(51);
  TEST_ASSERT_EQUAL_UINT8(3, network.a.neighbor_state(2));
}

void test_f3b_flooding_probes_and_telemetry_are_bounded_and_reach_c() {
  ControlDiamond network;
  network.pump(0);
  network.pump(10);
  network.pump(20);
  network.pump(30);  // Third probe completes A's first median-capable RTT sample set.
  network.pump(40);  // Telemetry from every node is flooded, not routed as data.
  TEST_ASSERT_TRUE(captured(network.capture, f3::kTelemetryType, 1));
  TEST_ASSERT_TRUE(captured(network.capture, f3::kTelemetryType, 2));
  TEST_ASSERT_TRUE(captured(network.capture, f3::kTopologyReportType, 1));
  TEST_ASSERT_GREATER_THAN_UINT32(0, network.a.counters().flooded);
  TEST_ASSERT_LESS_OR_EQUAL_UINT8(16, network.a.queued());
}

// Advances a monotonic clock in HELLO-interval steps so neighbors stay two-way
// (the test's neighbor timeout is 30 ms), then installs one route.
void install(ControlDiamond &network, uint32_t &now, uint8_t node, uint32_t version,
             uint8_t primary, uint8_t backup) {
  for (uint8_t round = 0; round < 2; ++round) network.pump(now += 10);
  uint8_t payload[11] = {};
  const uint32_t epoch = 700;
  memcpy(payload, &epoch, 4); memcpy(payload + 4, &version, 4);
  payload[8] = 3; payload[9] = primary; payload[10] = backup;
  TEST_ASSERT_TRUE(network.c.originate_route_update(node, payload));
  network.pump(now);
}

void test_f3c_installs_versioned_routes_and_fails_over_to_confirmed_backup() {
  ControlDiamond network;
  uint32_t now = 0;
  network.pump(now); network.pump(now += 10);  // Establish two-way neighbors first.
  install(network, now, 4, 1, 3, 0);  // Backup relay before the source.
  install(network, now, 2, 1, 3, 0);
  install(network, now, 1, 1, 2, 4);
  TEST_ASSERT_EQUAL_UINT8(2, network.a.installed_next_hop(3));
  TEST_ASSERT_TRUE(captured(network.capture, f3::kRouteInstallAckType, 1));

  // T2: three next-hop send failures move A to its controller-installed backup.
  network.a.report_send_result(2, false, now);
  network.a.report_send_result(2, false, now);
  network.a.report_send_result(2, false, now);
  TEST_ASSERT_EQUAL_UINT8(4, network.a.installed_next_hop(3));
  network.pump(now);
  TEST_ASSERT_TRUE(captured(network.capture, f3::kLocalFailoverReportType, 1));
  TEST_ASSERT_TRUE(network.a.create_data(3, 88, now));
  network.pump(now);
  TEST_ASSERT_EQUAL_UINT32(1, network.c.counters().delivered);

  // Only a newer controller version can choose a primary again; an older late
  // update cannot pull A back to the abandoned relay.
  install(network, now, 1, 2, 4, 2);
  install(network, now, 1, 1, 2, 4);
  TEST_ASSERT_EQUAL_UINT8(4, network.a.installed_next_hop(3));
  TEST_ASSERT_GREATER_THAN_UINT32(0, network.a.counters().route_stale);
}

void test_f3a_forwards_only_over_permitted_neighbors() {
  Diamond network;
  TEST_ASSERT_TRUE(network.a.create_data(3, 2345, 0));
  network.pump(0);
  TEST_ASSERT_EQUAL_UINT32(1, network.c.counters().delivered);
  TEST_ASSERT_EQUAL_UINT32(1, network.a.counters().acknowledged);
  // B forwards DATA toward C and then the return ACK toward A.
  TEST_ASSERT_EQUAL_UINT32(2, network.b.counters().forwarded);

  uint8_t bytes[iot::kMaxRadioFrame] = {};
  size_t len = 0;
  const uint8_t value[4] = {};
  const iot::FrameView bypass = {1, f3::kDataType, 1, 3, 99, 1, 8, 4, value};
  TEST_ASSERT_TRUE(iot::encode_frame(bypass, bytes, sizeof(bytes), &len));
  network.c.receive(1, bytes, len, 1);  // A is not C's logical neighbor.
  TEST_ASSERT_EQUAL_UINT32(1, network.c.counters().dropped_not_permitted);
  TEST_ASSERT_EQUAL_UINT32(1, network.c.counters().delivered);
}

void test_f3a_ttl_stops_a_misconfigured_route_loop() {
  Diamond network;
  network.b.set_route(3, 1);  // Both hops are permitted, but form A <-> B loop.
  TEST_ASSERT_TRUE(network.a.create_data(3, 1, 0));
  network.pump(0);
  TEST_ASSERT_EQUAL_UINT32(0, network.c.counters().delivered);
  TEST_ASSERT_EQUAL_UINT32(1, network.a.counters().dropped_ttl);
}

void test_f3a_lost_ack_retries_identically_and_destination_deduplicates() {
  Diamond network;
  network.radio.drop_acks = true;
  TEST_ASSERT_TRUE(network.a.create_data(3, 7, 0));
  network.pump(0);
  TEST_ASSERT_EQUAL_UINT32(1, network.c.counters().delivered);
  TEST_ASSERT_EQUAL_UINT32(0, network.a.counters().acknowledged);
  network.radio.drop_acks = false;  // the retry's re-ACK gets through
  network.pump(1000);
  TEST_ASSERT_EQUAL_UINT32(1, network.c.counters().delivered);
  TEST_ASSERT_EQUAL_UINT32(1, network.c.counters().duplicates);
  TEST_ASSERT_EQUAL_UINT32(1, network.a.counters().acknowledged);
}

void test_f3a_new_session_after_restart_is_not_a_duplicate() {
  MockRadio radio = {};
  Endpoint endpoint{&radio, 1};
  const uint8_t a_neighbors[] = {2, 4};
  f3::NodeEngine first(1, 500, a_neighbors, 2, mock_send, &endpoint);
  Diamond destination;
  TEST_ASSERT_TRUE(first.set_route(3, 2));
  TEST_ASSERT_TRUE(first.create_data(3, 1, 0));
  first.tick(0);
  TEST_ASSERT_EQUAL_UINT(1, radio.count);
  destination.b.receive(1, radio.messages[0].bytes, radio.messages[0].len, 0);
  destination.b.tick(0);
  TEST_ASSERT_EQUAL_UINT(1, destination.radio.count);
  destination.c.receive(2, destination.radio.messages[0].bytes,
                        destination.radio.messages[0].len, 0);

  radio.count = 0;
  f3::NodeEngine restarted(1, 501, a_neighbors, 2, mock_send, &endpoint);
  TEST_ASSERT_TRUE(restarted.set_route(3, 2));
  TEST_ASSERT_TRUE(restarted.create_data(3, 2, 1));
  restarted.tick(1);
  destination.b.receive(1, radio.messages[0].bytes, radio.messages[0].len, 1);
  destination.radio.count = 0;
  destination.b.tick(1);
  TEST_ASSERT_EQUAL_UINT(1, destination.radio.count);
  destination.c.receive(2, destination.radio.messages[0].bytes,
                        destination.radio.messages[0].len, 1);
  TEST_ASSERT_EQUAL_UINT32(2, destination.c.counters().delivered);
}

} // namespace

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_radio_vectors);
  RUN_TEST(test_serial_vectors);
  RUN_TEST(test_crc_reference);
  RUN_TEST(test_output_bounds);
  RUN_TEST(test_f1_ack_matching);
  RUN_TEST(test_f2_fake_c_heartbeat_ack_and_resend);
  RUN_TEST(test_f2_fake_c_timeout_buffers_and_replays);
  RUN_TEST(test_f2_fake_c_buffer_is_bounded);
  RUN_TEST(test_f3c_c_accepts_only_route_update_serial_commands);
  RUN_TEST(test_f3a_forwards_only_over_permitted_neighbors);
  RUN_TEST(test_f3a_ttl_stops_a_misconfigured_route_loop);
  RUN_TEST(test_f3a_lost_ack_retries_identically_and_destination_deduplicates);
  RUN_TEST(test_f3a_new_session_after_restart_is_not_a_duplicate);
  RUN_TEST(test_f3b_hello_reports_one_way_and_stale_evidence_to_c);
  RUN_TEST(test_f3b_flooding_probes_and_telemetry_are_bounded_and_reach_c);
  RUN_TEST(test_f3c_installs_versioned_routes_and_fails_over_to_confirmed_backup);
  return UNITY_END();
}
