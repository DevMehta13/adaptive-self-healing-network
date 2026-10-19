#include "f1_ack.h"

namespace f1 {
namespace {

uint32_t read_u32(const uint8_t *bytes) {
  return static_cast<uint32_t>(bytes[0]) |
         (static_cast<uint32_t>(bytes[1]) << 8) |
         (static_cast<uint32_t>(bytes[2]) << 16) |
         (static_cast<uint32_t>(bytes[3]) << 24);
}

} // namespace

bool delivery_ack_matches(const iot::FrameView &ack, uint8_t acknowledger,
                          uint8_t recipient, uint8_t data_origin,
                          uint32_t data_session, uint32_t data_seq) {
  static const uint8_t kDeliveryAck = 0x02;
  static const uint8_t kAckPayloadSize = 9;
  if (ack.type != kDeliveryAck || ack.src != acknowledger ||
      ack.dst != recipient || ack.payload_len != kAckPayloadSize ||
      ack.payload == nullptr || ack.payload[0] != data_origin) {
    return false;
  }
  return read_u32(ack.payload + 1) == data_session &&
         read_u32(ack.payload + 5) == data_seq;
}

} // namespace f1
