#ifndef IOT_CODEC_H
#define IOT_CODEC_H

#include <stddef.h>
#include <stdint.h>

namespace iot {

static const size_t kMaxRadioFrame = 250;
static const size_t kRadioHeaderSize = 14;
static const size_t kMaxSerialJson = 634;
// Most fields one serial line may carry (the longest, C's status `st`, has 17).
static const size_t kMaxSerialFields = 24;

enum class DecodeError : uint8_t {
  kNone,
  kOversize,
  kTruncated,
  kBadVersion,
  kUnknownType,
  kBadNodeId,
  kBadSession,
  kLengthMismatch,
  kBadPayloadLength,
  kBadValue,
};

const char *error_name(DecodeError error);

struct FrameView {
  uint8_t version;
  uint8_t type;
  uint8_t src;
  uint8_t dst;
  uint32_t session;
  uint32_t seq;
  uint8_t ttl;
  uint8_t payload_len;
  const uint8_t *payload;
};

DecodeError decode_frame(const uint8_t *bytes, size_t len, FrameView *frame);
bool encode_frame(const FrameView &frame, uint8_t *output, size_t capacity,
                  size_t *written);

enum class SerialDirection : uint8_t { kUplink, kDownlink };
enum class SerialResult : uint8_t { kMessage, kLog, kNoise, kRejected };

struct TextView {
  const char *data;
  size_t len;
};

struct SerialMessage {
  SerialResult result;
  const char *reason;
  TextView type;
  TextView json;
  uint32_t numbers[kMaxSerialFields];
  TextView strings[kMaxSerialFields];
  TextView keys[kMaxSerialFields];
  uint8_t value_kinds[kMaxSerialFields]; // 1 = unsigned integer, 2 = string, 3 = other JSON
                           // value.
  size_t field_count;
};

uint16_t crc16_ccitt_false(const uint8_t *data, size_t len);
SerialMessage decode_serial_line(const char *line, size_t len,
                                 SerialDirection direction);
bool encode_serial_json(const char *json, size_t json_len, char *output,
                        size_t capacity, size_t *written);

bool text_equals(TextView value, const char *expected);
const uint32_t *serial_number(const SerialMessage &message, const char *key);
const TextView *serial_string(const SerialMessage &message, const char *key);

} // namespace iot

#endif
