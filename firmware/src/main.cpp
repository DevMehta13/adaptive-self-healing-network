#ifdef ARDUINO
#include "c_serial_bridge.h"
#include "f1_ack.h"
#include "f3_node.h"
#include "iot_codec.h"
#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_system.h>
#include <esp_wifi.h>

// ESP32-S (Arduino-ESP32 2.x) port.  Two differences from the ESP8266 SDK shape
// every callback below:
//   * ESP-NOW callbacks run in the Wi-Fi task, concurrently with loop() (which
//     may be on the other core).  They therefore only copy bytes into a small
//     fixed queue under a spinlock; all engine/bridge work happens in loop().
//   * There is no "self role"; every peer is added with esp_now_add_peer() and
//     all boards are pinned to the same Wi-Fi channel.
// Nothing here has been run on a board; see docs/STATUS.md.

#if defined(F1_ROLE_SENDER) || defined(F1_ROLE_RECEIVER) || defined(F2_ROLE_C) || \
    defined(F3A_ROLE_A) || defined(F3A_ROLE_B) || defined(F3A_ROLE_C) || defined(F3A_ROLE_D)
namespace {

// Session / boot identifiers must be non-zero.  esp_random() is a hardware
// generator whose entropy is best while Wi-Fi is running, so radio roles start
// the radio before calling this.
uint32_t random_nonzero() {
  uint32_t value;
  do {
    value = esp_random();
  } while (value == 0);
  return value;
}

#if !defined(F2_ROLE_C)
// Every board must use the same channel.  1 is the ESP32 default for an
// unconnected station; it is set explicitly so the choice is visible.
const uint8_t kEspNowChannel = 1;

// Guards the queues and tables shared between the Wi-Fi task and loop().
portMUX_TYPE g_radio_lock = portMUX_INITIALIZER_UNLOCKED;

void print_mac(const uint8_t *mac) {
  for (uint8_t i = 0; i < 6; ++i) {
    if (i) Serial.print(':');
    if (mac[i] < 16) Serial.print('0');
    Serial.print(mac[i], HEX);
  }
}

#if !defined(F1_ROLE_RECEIVER)
bool parse_mac(const char *text, uint8_t *out) {
  if (strlen(text) != 17) return false;
  for (uint8_t i = 0; i < 6; ++i) {
    const char high = text[i * 3], low = text[i * 3 + 1];
    const int h = isxdigit(high) ? (isdigit(high) ? high - '0' : toupper(high) - 'A' + 10) : -1;
    const int l = isxdigit(low) ? (isdigit(low) ? low - '0' : toupper(low) - 'A' + 10) : -1;
    if (h < 0 || l < 0 || (i < 5 && text[i * 3 + 2] != ':')) return false;
    out[i] = static_cast<uint8_t>((h << 4) | l);
  }
  return true;
}
#endif

// Station mode, no access point, fixed channel, then ESP-NOW with callbacks.
bool radio_start(esp_now_send_cb_t on_sent, esp_now_recv_cb_t on_received) {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  if (esp_wifi_set_channel(kEspNowChannel, WIFI_SECOND_CHAN_NONE) != ESP_OK) return false;
  return esp_now_init() == ESP_OK && esp_now_register_send_cb(on_sent) == ESP_OK &&
         esp_now_register_recv_cb(on_received) == ESP_OK;
}

// Replaces an existing entry so that re-entering a corrected MAC works.
bool radio_add_peer(const uint8_t *mac) {
  if (esp_now_is_peer_exist(mac)) esp_now_del_peer(mac);
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, mac, sizeof(peer.peer_addr));
  peer.channel = 0;  // 0 = the channel the radio is on (kEspNowChannel)
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  return esp_now_add_peer(&peer) == ESP_OK;
}
#endif

}  // namespace
#endif

#if defined(F1_ROLE_SENDER) || defined(F1_ROLE_RECEIVER)
namespace {

const uint8_t kNodeA = 1;
const uint8_t kNodeC = 3;
const uint8_t kDataType = 0x01;
const uint8_t kAckType = 0x02;
const uint8_t kBenchTtl = 8;
const uint16_t kTargetReadings = 100;
const uint32_t kAckTimeoutMs = 1500;
const uint32_t kSpacingMs = 100;
const uint8_t kReceiveQueueSize = 4;

struct ReceiveSlot {
  uint8_t mac[6];
  uint8_t bytes[iot::kMaxRadioFrame];
  uint8_t len;
};

// Filled by the Wi-Fi task, emptied by loop(); head/tail/last-sender are
// touched only under g_radio_lock.  The counters have a single writer.
ReceiveSlot g_receive_queue[kReceiveQueueSize];
uint8_t g_receive_head = 0;
uint8_t g_receive_tail = 0;
uint8_t g_last_radio_sender[6] = {};
bool g_have_radio_sender = false;
volatile uint32_t g_receive_queue_drops = 0;
volatile uint32_t g_radio_receives = 0;
volatile uint32_t g_send_callback_ok = 0;
volatile uint32_t g_send_callback_failed = 0;

uint32_t g_session = 0;
uint32_t g_next_seq = 1;
#if defined(F1_ROLE_SENDER)
uint8_t g_peer_mac[6] = {};
bool g_peer_configured = false;
uint16_t g_acknowledged = 0;
uint16_t g_data_sent = 0;
uint32_t g_waiting_session = 0;
uint32_t g_waiting_seq = 0;
uint32_t g_waiting_since_ms = 0;
bool g_waiting_for_ack = false;
bool g_running = false;
uint32_t g_next_send_ms = 0;
#endif

uint16_t g_data_send_calls_failed = 0;
uint16_t g_decode_rejected = 0;
uint16_t g_unexpected = 0;

void on_send(const uint8_t *, esp_now_send_status_t status) {
  if (status == ESP_NOW_SEND_SUCCESS)
    ++g_send_callback_ok;
  else
    ++g_send_callback_failed;
}

void on_receive(const uint8_t *mac, const uint8_t *data, int len) {
  ++g_radio_receives;
  if (len <= 0 || len > static_cast<int>(iot::kMaxRadioFrame)) {
    ++g_receive_queue_drops;
    return;
  }
  portENTER_CRITICAL(&g_radio_lock);
  memcpy(g_last_radio_sender, mac, sizeof(g_last_radio_sender));
  g_have_radio_sender = true;
  const uint8_t next = static_cast<uint8_t>((g_receive_head + 1) % kReceiveQueueSize);
  if (next == g_receive_tail) {
    ++g_receive_queue_drops;
  } else {
    ReceiveSlot &slot = g_receive_queue[g_receive_head];
    memcpy(slot.mac, mac, sizeof(slot.mac));
    memcpy(slot.bytes, data, static_cast<size_t>(len));
    slot.len = static_cast<uint8_t>(len);
    g_receive_head = next;
  }
  portEXIT_CRITICAL(&g_radio_lock);
}

bool send_frame(const uint8_t *mac, const iot::FrameView &frame) {
  uint8_t bytes[iot::kMaxRadioFrame] = {};
  size_t written = 0;
  if (!iot::encode_frame(frame, bytes, sizeof(bytes), &written))
    return false;
  return esp_now_send(mac, bytes, written) == ESP_OK;
}

#if defined(F1_ROLE_RECEIVER)
void send_ack(const uint8_t *to, const iot::FrameView &data) {
  uint8_t payload[9] = {};
  payload[0] = data.src;
  memcpy(payload + 1, &data.session, sizeof(data.session));
  memcpy(payload + 5, &data.seq, sizeof(data.seq));
  const iot::FrameView ack = {1, kAckType, kNodeC, kNodeA, g_session,
                              g_next_seq++, 1, sizeof(payload), payload};
  if (!send_frame(to, ack))
    ++g_data_send_calls_failed;
}
#endif

void process_receive(const ReceiveSlot &slot) {
  iot::FrameView frame = {};
  if (iot::decode_frame(slot.bytes, slot.len, &frame) != iot::DecodeError::kNone) {
    ++g_decode_rejected;
    return;
  }
#if defined(F1_ROLE_RECEIVER)
  if (frame.type != kDataType || frame.src != kNodeA || frame.dst != kNodeC) {
    ++g_unexpected;
    return;
  }
  if (!radio_add_peer(slot.mac)) {
    ++g_data_send_calls_failed;
    return;
  }
  send_ack(slot.mac, frame);
#else
  if (!f1::delivery_ack_matches(frame, kNodeC, kNodeA, kNodeA,
                                g_waiting_session, g_waiting_seq)) {
    ++g_unexpected;
    return;
  }
  uint32_t data_seq = 0;
  memcpy(&data_seq, frame.payload + 5, sizeof(data_seq));
  if (g_waiting_for_ack) {
    ++g_acknowledged;
    g_waiting_for_ack = false;
    Serial.print("ACK seq=");
    Serial.println(data_seq);
  } else {
    ++g_unexpected;
  }
#endif
}

void drain_receive_queue() {
  for (;;) {
    static ReceiveSlot slot;  // single consumer: loop() only
    bool have = false;
    portENTER_CRITICAL(&g_radio_lock);
    if (g_receive_tail != g_receive_head) {
      slot = g_receive_queue[g_receive_tail];
      g_receive_tail = static_cast<uint8_t>((g_receive_tail + 1) % kReceiveQueueSize);
      have = true;
    }
    portEXIT_CRITICAL(&g_radio_lock);
    if (!have) return;
    process_receive(slot);
  }
}

void print_summary() {
  uint8_t last_sender[6] = {};
  bool have_sender = false;
  portENTER_CRITICAL(&g_radio_lock);
  memcpy(last_sender, g_last_radio_sender, sizeof(last_sender));
  have_sender = g_have_radio_sender;
  portEXIT_CRITICAL(&g_radio_lock);

  Serial.println("F1_SUMMARY");
#if defined(F1_ROLE_RECEIVER)
  Serial.println("role=receiver(C)");
#else
  Serial.println("role=sender(A)");
  Serial.print("data_sent="); Serial.println(g_data_sent);
  Serial.print("acks_matched="); Serial.println(g_acknowledged);
#endif
  Serial.print("radio_receives="); Serial.println(g_radio_receives);
  Serial.print("last_radio_sender=");
  if (have_sender) print_mac(last_sender);
  else Serial.print("none");
  Serial.println();
  Serial.print("send_callback_ok="); Serial.println(g_send_callback_ok);
  Serial.print("send_callback_failed="); Serial.println(g_send_callback_failed);
  Serial.print("send_call_failed="); Serial.println(g_data_send_calls_failed);
  Serial.print("decode_rejected="); Serial.println(g_decode_rejected);
  Serial.print("unexpected="); Serial.println(g_unexpected);
  Serial.print("receive_queue_drops="); Serial.println(g_receive_queue_drops);
  Serial.print("free_heap="); Serial.println(ESP.getFreeHeap());
  Serial.print("min_free_heap="); Serial.println(ESP.getMinFreeHeap());
}

void handle_serial_command() {
  static char line[40] = {};
  const size_t n = Serial.readBytesUntil('\n', line, sizeof(line) - 1);
  if (!n)
    return;
  line[n] = '\0';
#if defined(F1_ROLE_SENDER)
  if (strncmp(line, "peer ", 5) == 0 && parse_mac(line + 5, g_peer_mac)) {
    g_peer_configured = radio_add_peer(g_peer_mac);
    Serial.print(g_peer_configured ? "peer_set=" : "peer_failed=");
    print_mac(g_peer_mac); Serial.println();
  } else if (strcmp(line, "start") == 0 && g_peer_configured) {
    g_running = true; g_next_send_ms = millis();
    Serial.println("bench_started target=100");
  } else if (strcmp(line, "summary") == 0) {
    print_summary();
  } else {
    Serial.println("commands: peer AA:BB:CC:DD:EE:FF | start | summary");
  }
#else
  if (strcmp(line, "summary") == 0) print_summary();
  else Serial.println("commands: summary");
#endif
}

void sender_tick() {
#if defined(F1_ROLE_SENDER)
  if (!g_running || !g_peer_configured)
    return;
  const uint32_t now = millis();
  if (g_waiting_for_ack) {
    if (now - g_waiting_since_ms >= kAckTimeoutMs) {
      Serial.print("ACK_TIMEOUT seq="); Serial.println(g_waiting_seq);
      g_waiting_for_ack = false;
      g_running = false;
      print_summary();
    }
    return;
  }
  if (g_acknowledged >= kTargetReadings) {
    g_running = false;
    Serial.println("BENCH_COMPLETE acknowledged=100");
    print_summary();
    return;
  }
  if (static_cast<int32_t>(now - g_next_send_ms) < 0)
    return;
  const int32_t value = static_cast<int32_t>(g_data_sent) * 100;
  uint8_t payload[4] = {};
  memcpy(payload, &value, sizeof(value));
  const uint32_t seq = g_next_seq++;
  const iot::FrameView data = {1, kDataType, kNodeA, kNodeC, g_session, seq,
                               kBenchTtl, sizeof(payload), payload};
  if (!send_frame(g_peer_mac, data)) {
    ++g_data_send_calls_failed;
    Serial.print("SEND_CALL_FAILED seq="); Serial.println(seq);
    g_next_send_ms = now + kSpacingMs;
    return;
  }
  ++g_data_sent;
  g_waiting_session = g_session;
  g_waiting_seq = seq;
  g_waiting_since_ms = now;
  g_waiting_for_ack = true;
#endif
}

} // namespace
#endif

#if defined(F3A_ROLE_A) || defined(F3A_ROLE_B) || defined(F3A_ROLE_C) || defined(F3A_ROLE_D)
namespace {

#if defined(F3A_ROLE_A)
const uint8_t kF3Id = 1; const uint8_t kF3Neighbors[] = {2, 4};
#elif defined(F3A_ROLE_B)
const uint8_t kF3Id = 2; const uint8_t kF3Neighbors[] = {1, 3};
#elif defined(F3A_ROLE_C)
const uint8_t kF3Id = 3; const uint8_t kF3Neighbors[] = {2, 4};
#else
const uint8_t kF3Id = 4; const uint8_t kF3Neighbors[] = {1, 3};
#endif

const uint8_t kF3ReceiveSlots = 4;
const uint8_t kF3SendResultSlots = 16;

struct F3Peer { bool set; uint8_t mac[6]; };
struct F3Receive { uint8_t from; uint8_t bytes[iot::kMaxRadioFrame]; uint8_t len; };
struct F3SendResult { uint8_t peer; bool ok; };

// The peer table is written by loop() and read by the Wi-Fi task's callbacks, so
// every access from a callback, and every write, is under g_radio_lock.  The
// queues are single-producer (Wi-Fi task) / single-consumer (loop()).
F3Peer g_f3_peers[5] = {};
F3Receive g_f3_receive[kF3ReceiveSlots] = {};
F3SendResult g_f3_send_results[kF3SendResultSlots] = {};
uint8_t g_f3_head = 0, g_f3_tail = 0;
uint8_t g_f3_result_head = 0, g_f3_result_tail = 0;
volatile uint32_t g_f3_receive_drops = 0;
volatile uint32_t g_f3_send_result_drops = 0;
f3::NodeEngine *g_f3_engine = nullptr;
#if defined(F3C_GATEWAY)
cbridge::CSerialBridge *g_f3_bridge = nullptr;
char g_f3_serial_line[128] = {};
size_t g_f3_serial_line_len = 0;
bool g_f3_serial_discard = false;
#endif
#if defined(F3B_ENABLED)
// These are conservative starting values, not radio-performance claims.
const f3::DiscoverySettings kF3bSettings = {1000, 3000, 5000, 50, 1000, 5000};
#endif

// Called from the callbacks only.
uint8_t f3_peer_id(const uint8_t *mac) {
  uint8_t found = 0;
  portENTER_CRITICAL(&g_radio_lock);
  for (uint8_t id = 1; id <= 4 && found == 0; ++id)
    if (g_f3_peers[id].set && memcmp(g_f3_peers[id].mac, mac, 6) == 0) found = id;
  portEXIT_CRITICAL(&g_radio_lock);
  return found;
}

// Called from loop() only, which is the only writer of g_f3_peers.
bool f3_send(void *, uint8_t next_hop, const uint8_t *bytes, size_t len) {
  if (next_hop == 0 || next_hop > 4 || !g_f3_peers[next_hop].set) return false;
  return esp_now_send(g_f3_peers[next_hop].mac, bytes, len) == ESP_OK;
}

bool f3_set_peer_from_line(const char *line) {
  if (strncmp(line, "peer ", 5) != 0 || !isdigit(line[5]) || line[6] != ' ') return false;
  const uint8_t id = static_cast<uint8_t>(line[5] - '0');
  uint8_t mac[6] = {};
  if (id == 0 || id >= 5 || !parse_mac(line + 7, mac)) return false;
  const bool added = radio_add_peer(mac);
  portENTER_CRITICAL(&g_radio_lock);
  memcpy(g_f3_peers[id].mac, mac, sizeof(mac));
  g_f3_peers[id].set = added;
  portEXIT_CRITICAL(&g_radio_lock);
  Serial.println(added ? "# peer_set" : "# peer_failed");
  return true;
}

// Link-layer result for one frame to one neighbor.  It is queued, not applied:
// the engine is not thread-safe and must only run in loop().
void f3_send_callback(const uint8_t *mac, esp_now_send_status_t status) {
  const uint8_t peer = f3_peer_id(mac);
  if (peer == 0) return;
  portENTER_CRITICAL(&g_radio_lock);
  const uint8_t next = static_cast<uint8_t>((g_f3_result_head + 1) % kF3SendResultSlots);
  if (next == g_f3_result_tail) {
    ++g_f3_send_result_drops;
  } else {
    g_f3_send_results[g_f3_result_head].peer = peer;
    g_f3_send_results[g_f3_result_head].ok = (status == ESP_NOW_SEND_SUCCESS);
    g_f3_result_head = next;
  }
  portEXIT_CRITICAL(&g_radio_lock);
}

void f3_receive_callback(const uint8_t *mac, const uint8_t *bytes, int len) {
  const uint8_t from = f3_peer_id(mac);
  if (from == 0 || len <= 0 || len > static_cast<int>(iot::kMaxRadioFrame)) return;
  portENTER_CRITICAL(&g_radio_lock);
  const uint8_t next = static_cast<uint8_t>((g_f3_head + 1) % kF3ReceiveSlots);
  if (next == g_f3_tail) {
    ++g_f3_receive_drops;
  } else {
    F3Receive &slot = g_f3_receive[g_f3_head];
    slot.from = from; slot.len = static_cast<uint8_t>(len);
    memcpy(slot.bytes, bytes, static_cast<size_t>(len));
    g_f3_head = next;
  }
  portEXIT_CRITICAL(&g_radio_lock);
}

void f3_drain() {
  for (;;) {
    F3SendResult result = {};
    bool have = false;
    portENTER_CRITICAL(&g_radio_lock);
    if (g_f3_result_tail != g_f3_result_head) {
      result = g_f3_send_results[g_f3_result_tail];
      g_f3_result_tail = static_cast<uint8_t>((g_f3_result_tail + 1) % kF3SendResultSlots);
      have = true;
    }
    portEXIT_CRITICAL(&g_radio_lock);
    if (!have) break;
    g_f3_engine->report_send_result(result.peer, result.ok, millis());
  }
  for (;;) {
    static F3Receive slot;  // single consumer: loop() only
    bool have = false;
    portENTER_CRITICAL(&g_radio_lock);
    if (g_f3_tail != g_f3_head) {
      slot = g_f3_receive[g_f3_tail];
      g_f3_tail = static_cast<uint8_t>((g_f3_tail + 1) % kF3ReceiveSlots);
      have = true;
    }
    portEXIT_CRITICAL(&g_radio_lock);
    if (!have) break;
    const f3::ReceiveResult result = g_f3_engine->receive(slot.from, slot.bytes, slot.len, millis());
#if defined(F3C_GATEWAY)
    if (g_f3_bridge) g_f3_bridge->receive_radio(slot.bytes, slot.len, slot.from,
                                                 result.duplicate, millis());
#else
    (void)result;
#endif
  }
}

#if defined(F3C_GATEWAY)
void f3_write(void *, const char *line, size_t len) {
  Serial.write(reinterpret_cast<const uint8_t *>(line), len);
}
bool f3_transmit_route(void *, uint8_t destination, const uint8_t *payload,
                       size_t payload_len) {
  return g_f3_engine && payload_len == 11 &&
         g_f3_engine->originate_route_update(destination, payload);
}
void f3_read_serial() {
  while (Serial.available()) {
    const int value = Serial.read(); if (value < 0) return;
    const char byte = static_cast<char>(value);
    if (byte == '\n') {
      g_f3_serial_line[g_f3_serial_line_len] = '\0';
      if (!g_f3_serial_discard && f3_set_peer_from_line(g_f3_serial_line)) {
        // Runtime peer setup is a diagnostic command, never a laptop protocol line.
      } else if (!g_f3_serial_discard && g_f3_bridge) {
        g_f3_bridge->receive_down(g_f3_serial_line, g_f3_serial_line_len, millis());
      }
      g_f3_serial_line_len = 0; g_f3_serial_discard = false;
    } else if (!g_f3_serial_discard && g_f3_serial_line_len < sizeof(g_f3_serial_line) - 1) {
      g_f3_serial_line[g_f3_serial_line_len++] = byte;
    } else g_f3_serial_discard = true;
  }
}
#else
void f3_command() {
  static char line[40] = {};
  const size_t n = Serial.readBytesUntil('\n', line, sizeof(line) - 1);
  if (!n) return;
  line[n] = '\0';
  if (f3_set_peer_from_line(line)) return;
#if defined(F3A_ROLE_A)
  if (strncmp(line, "send ", 5) == 0) {
    const int32_t value = static_cast<int32_t>(strtol(line + 5, nullptr, 10));
    Serial.println(g_f3_engine->create_data(3, value, millis()) ? "queued" : "dropped");
    return;
  }
#endif
  if (strcmp(line, "summary") == 0) {
    const f3::Counters &c = g_f3_engine->counters();
    Serial.print("F3");
#if defined(F3B_ENABLED)
    Serial.print("B");
#else
    Serial.print("A");
#endif
    Serial.print(" role="); Serial.println(kF3Id);
    Serial.print("delivered="); Serial.println(c.delivered);
    Serial.print("forwarded="); Serial.println(c.forwarded);
    Serial.print("acknowledged="); Serial.println(c.acknowledged);
    Serial.print("duplicates="); Serial.println(c.duplicates);
    Serial.print("queue_drops="); Serial.println(c.dropped_queue_full + g_f3_receive_drops);
    Serial.print("send_result_drops="); Serial.println(g_f3_send_result_drops);
    Serial.print("free_heap="); Serial.println(ESP.getFreeHeap());
    Serial.print("min_free_heap="); Serial.println(ESP.getMinFreeHeap());
    return;
  }
  Serial.println("commands: peer N AA:BB:CC:DD:EE:FF | send VALUE (A) | summary");
}
#endif

} // namespace
#endif

#if defined(F2_ROLE_C)
namespace {

cbridge::CSerialBridge *g_f2_bridge = nullptr;
char g_f2_line[128] = {};
size_t g_f2_line_len = 0;
bool g_f2_discard = false;

void f2_write(void *, const char *line, size_t len) {
  // Called only from loop(), never the ESP-NOW callback.
  Serial.write(reinterpret_cast<const uint8_t *>(line), len);
}

void f2_read_serial() {
  while (Serial.available()) {
    const int value = Serial.read();
    if (value < 0) return;
    const char byte = static_cast<char>(value);
    if (byte == '\n') {
      if (!g_f2_discard && g_f2_bridge)
        g_f2_bridge->receive_down(g_f2_line, g_f2_line_len, millis());
      g_f2_line_len = 0;
      g_f2_discard = false;
    } else if (!g_f2_discard && g_f2_line_len < sizeof(g_f2_line) - 1) {
      g_f2_line[g_f2_line_len++] = byte;
    } else {
      // The bridge must never retain an unbounded or partial command line.
      g_f2_discard = true;
    }
  }
}

} // namespace
#endif

void setup() {
  Serial.begin(115200);
  delay(100);
#if defined(F2_ROLE_C)
  static cbridge::CSerialBridge bridge(random_nonzero(), f2_write, nullptr);
  g_f2_bridge = &bridge;
  bridge.start(millis());
  Serial.println("# F2 C serial bridge; radio ingress is connected in F3");
  return;
#elif defined(F3A_ROLE_A) || defined(F3A_ROLE_B) || defined(F3A_ROLE_C) || defined(F3A_ROLE_D)
  // Radio first, so the session numbers below are drawn with Wi-Fi running.
  const bool radio_ok = radio_start(f3_send_callback, f3_receive_callback);
#if defined(F3B_ENABLED)
  static f3::NodeEngine engine(kF3Id, random_nonzero(), kF3Neighbors, 2, f3_send,
                               nullptr, 1000, &kF3bSettings, nullptr, nullptr);
#else
  static f3::NodeEngine engine(kF3Id, random_nonzero(), kF3Neighbors, 2, f3_send, nullptr);
#endif
  g_f3_engine = &engine;
#if defined(F3C_GATEWAY)
  static cbridge::CSerialBridge bridge(random_nonzero(), f3_write, nullptr,
                                       f3_transmit_route, nullptr);
  g_f3_bridge = &bridge;
  bridge.start(millis());
#endif
  // F3a/F3b use manual Phase-2 routes. F3c starts with no data route and
  // accepts commands only through C's checked serial boundary.
#if !defined(F3C_ENABLED)
#if defined(F3A_ROLE_A)
  engine.set_route(3, 2);
#elif defined(F3A_ROLE_B)
  engine.set_route(1, 1); engine.set_route(3, 3);
#elif defined(F3A_ROLE_C)
  engine.set_route(1, 2);
#else
  engine.set_route(1, 1); engine.set_route(3, 3);
#endif
#endif
  if (!radio_ok) {
    Serial.println("\n# F3_SETUP_FAILED");
    return;
  }
  uint8_t local_mac[6] = {};
  WiFi.macAddress(local_mac);
  // Leading newline + "#": C shares this line with the checksummed serial protocol,
  // and the bridge's hello line may not have been terminated yet.
  Serial.print("\n# F3_READY role=");
  Serial.print(kF3Id);
  Serial.print(" mac=");
  print_mac(local_mac);
  Serial.print(" channel=");
  Serial.print(kEspNowChannel);
  Serial.print(" free_heap=");
  Serial.println(ESP.getFreeHeap());
#if defined(F3C_ENABLED)
  Serial.println("# F3C_READY controller-installed routes, backups and replay enabled; enter neighbor peer MACs");
#elif defined(F3B_ENABLED)
  Serial.println("# F3B_READY HELLO/report/probe/telemetry enabled; enter neighbor peer MACs");
#else
  Serial.println("# F3A_READY enter configured-neighbor peer MACs, then use send on A");
#endif
  return;
#elif !defined(F1_ROLE_SENDER) && !defined(F1_ROLE_RECEIVER)
  Serial.println("# F0 codec build; build f1_sender or f1_receiver for the radio bench");
  return;
#else
  if (!radio_start(on_send, on_receive)) {
    Serial.println("F1_SETUP_FAILED");
    return;
  }
  g_session = random_nonzero();
  uint8_t local_mac[6] = {};
  WiFi.macAddress(local_mac);
  Serial.print("F1_READY role=");
#if defined(F1_ROLE_SENDER)
  Serial.print("sender(A)");
#else
  Serial.print("receiver(C)");
#endif
  Serial.print(" mac="); print_mac(local_mac);
  Serial.print(" channel="); Serial.print(kEspNowChannel);
  Serial.print(" session="); Serial.println(g_session);
  Serial.print("free_heap="); Serial.println(ESP.getFreeHeap());
#endif
}

void loop() {
#if defined(F2_ROLE_C)
  f2_read_serial();
  if (g_f2_bridge) g_f2_bridge->tick(millis());
#elif defined(F3A_ROLE_A) || defined(F3A_ROLE_B) || defined(F3A_ROLE_C) || defined(F3A_ROLE_D)
  f3_drain();
  if (Serial.available()) {
#if defined(F3C_GATEWAY)
    f3_read_serial();
#else
    f3_command();
#endif
  }
  if (g_f3_engine) g_f3_engine->tick(millis());
#if defined(F3C_GATEWAY)
  if (g_f3_bridge) g_f3_bridge->tick(millis());
#endif
#elif defined(F1_ROLE_SENDER) || defined(F1_ROLE_RECEIVER)
  drain_receive_queue();
  if (Serial.available()) handle_serial_command();
  sender_tick();
#else
  delay(1000);
#endif
}
#endif
