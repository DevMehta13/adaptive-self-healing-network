#include <unity.h>

#include <stdlib.h>
#include <string.h>

#include "iot_codec.h"
#include "test_vectors.h"

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

} // namespace

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_radio_vectors);
  RUN_TEST(test_serial_vectors);
  RUN_TEST(test_crc_reference);
  RUN_TEST(test_output_bounds);
  return UNITY_END();
}
