#include "iot_codec.h"

#include <string.h>

namespace iot {
namespace {

uint16_t read_u16(const uint8_t *p) {
  return static_cast<uint16_t>(p[0]) | static_cast<uint16_t>(p[1] << 8);
}

uint32_t read_u32(const uint8_t *p) {
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) |
         (static_cast<uint32_t>(p[3]) << 24);
}

void write_u32(uint8_t *p, uint32_t value) {
  p[0] = static_cast<uint8_t>(value);
  p[1] = static_cast<uint8_t>(value >> 8);
  p[2] = static_cast<uint8_t>(value >> 16);
  p[3] = static_cast<uint8_t>(value >> 24);
}

bool node(uint8_t value) { return value != 0 && value != 255; }
bool optional_node(uint8_t value) { return value == 0 || node(value); }

bool known_type(uint8_t type) {
  return (type >= 0x01 && type <= 0x02) || (type >= 0x10 && type <= 0x17);
}

DecodeError validate_report(const uint8_t *p, size_t len) {
  if (len == 0)
    return DecodeError::kBadPayloadLength;
  const uint8_t count = p[0];
  if (count > 8)
    return DecodeError::kBadPayloadLength;
  const size_t base = 1 + static_cast<size_t>(count) * 8;
  if (len < base)
    return DecodeError::kBadPayloadLength;
  for (uint8_t i = 0; i < count; ++i) {
    const uint8_t *entry = p + 1 + static_cast<size_t>(i) * 8;
    if (!node(entry[0]))
      return DecodeError::kBadNodeId;
    if (entry[1] < 1 || entry[1] > 3)
      return DecodeError::kBadValue;
    if (read_u32(entry + 2) == 0)
      return DecodeError::kBadSession;
  }
  if (len == base)
    return DecodeError::kNone;

  size_t cursor = base;
  const uint8_t rtt_count = p[cursor++];
  if (rtt_count > 8)
    return DecodeError::kBadPayloadLength;
  if (rtt_count > 0) {
    const size_t bytes = static_cast<size_t>(rtt_count) * 3;
    if (cursor + bytes > len)
      return DecodeError::kBadPayloadLength;
    for (uint8_t i = 0; i < rtt_count; ++i) {
      const uint8_t *entry = p + cursor + static_cast<size_t>(i) * 3;
      if (!node(entry[0]))
        return DecodeError::kBadNodeId;
      if (read_u16(entry + 1) == 0xFFFFu)
        return DecodeError::kBadValue;
    }
    cursor += bytes;
  }
  if (cursor == len)
    return rtt_count == 0 ? DecodeError::kBadPayloadLength : DecodeError::kNone;

  const uint8_t stats_count = p[cursor++];
  if (stats_count == 0 || stats_count > 8 ||
      cursor + static_cast<size_t>(stats_count) * 3 != len) {
    return DecodeError::kBadPayloadLength;
  }
  for (uint8_t i = 0; i < stats_count; ++i) {
    const uint8_t *entry = p + cursor + static_cast<size_t>(i) * 3;
    if (!node(entry[0]))
      return DecodeError::kBadNodeId;
    if (entry[2] == 0 || entry[1] > entry[2])
      return DecodeError::kBadValue;
  }
  return DecodeError::kNone;
}

DecodeError validate_payload(uint8_t type, const uint8_t *p, size_t len) {
  switch (type) {
  case 0x01:
    return len == 4 ? DecodeError::kNone : DecodeError::kBadPayloadLength;
  case 0x02:
    if (len != 9)
      return DecodeError::kBadPayloadLength;
    if (!node(p[0]))
      return DecodeError::kBadNodeId;
    return read_u32(p + 1) == 0 ? DecodeError::kBadSession : DecodeError::kNone;
  case 0x10: {
    if (len == 0 || p[0] > 8 || len != 1 + static_cast<size_t>(p[0]) * 5)
      return DecodeError::kBadPayloadLength;
    for (uint8_t i = 0; i < p[0]; ++i) {
      const uint8_t *entry = p + 1 + static_cast<size_t>(i) * 5;
      if (!node(entry[0]))
        return DecodeError::kBadNodeId;
      if (read_u32(entry + 1) == 0)
        return DecodeError::kBadSession;
    }
    return DecodeError::kNone;
  }
  case 0x11:
    return validate_report(p, len);
  case 0x12:
    if (len != 11)
      return DecodeError::kBadPayloadLength;
    if (!node(p[8]) || !optional_node(p[9]) || !optional_node(p[10]))
      return DecodeError::kBadNodeId;
    if (read_u32(p) == 0 || read_u32(p + 4) == 0 ||
        (p[10] != 0 && (p[9] == 0 || p[10] == p[9])))
      return DecodeError::kBadValue;
    return DecodeError::kNone;
  case 0x13:
    if (len != 10)
      return DecodeError::kBadPayloadLength;
    if (!node(p[8]))
      return DecodeError::kBadNodeId;
    if (read_u32(p) == 0 || read_u32(p + 4) == 0 || p[9] < 1 || p[9] > 3)
      return DecodeError::kBadValue;
    return DecodeError::kNone;
  case 0x14:
    return len == 0 ? DecodeError::kNone : DecodeError::kBadPayloadLength;
  case 0x15:
    if (len != 8)
      return DecodeError::kBadPayloadLength;
    return read_u32(p) == 0 ? DecodeError::kBadSession : DecodeError::kNone;
  case 0x16:
    if (len != 12)
      return DecodeError::kBadPayloadLength;
    if (!node(p[8]) || !node(p[9]) || !optional_node(p[10]))
      return DecodeError::kBadNodeId;
    if (read_u32(p) == 0 || read_u32(p + 4) == 0 || p[9] == p[10] ||
        p[11] < 1 || p[11] > 2)
      return DecodeError::kBadValue;
    return DecodeError::kNone;
  case 0x17:
    return len == 32 ? DecodeError::kNone : DecodeError::kBadPayloadLength;
  default:
    return DecodeError::kUnknownType;
  }
}

bool same(TextView value, const char *expected) {
  const size_t n = strlen(expected);
  return value.len == n && memcmp(value.data, expected, n) == 0;
}

SerialMessage rejected(const char *reason) {
  SerialMessage result = {};
  result.result = SerialResult::kRejected;
  result.reason = reason;
  return result;
}

int hex_digit(char c) {
  if (c >= '0' && c <= '9')
    return c - '0';
  if (c >= 'a' && c <= 'f')
    return c - 'a' + 10;
  if (c >= 'A' && c <= 'F')
    return c - 'A' + 10;
  return -1;
}

bool valid_hex(TextView value, size_t max_bytes) {
  if ((value.len & 1u) != 0 || value.len / 2 > max_bytes)
    return false;
  for (size_t i = 0; i < value.len; ++i)
    if (hex_digit(value.data[i]) < 0)
      return false;
  return true;
}

bool parse_json(SerialMessage *out, const char *json, size_t len,
                const char **reason) {
  if (len < 2 || json[0] != '{' || json[len - 1] != '}') {
    *reason = "bad_json";
    return false;
  }
  size_t cursor = 1;
  while (cursor < len - 1) {
    if (out->field_count != 0) {
      if (json[cursor++] != ',') {
        *reason = "bad_json";
        return false;
      }
    }
    if (cursor >= len - 1 || json[cursor++] != '"') {
      *reason = "bad_json";
      return false;
    }
    const size_t key_start = cursor;
    while (cursor < len - 1 && json[cursor] != '"') {
      if (json[cursor] == '\\' ||
          static_cast<unsigned char>(json[cursor]) < 0x20) {
        *reason = "bad_json";
        return false;
      }
      ++cursor;
    }
    if (cursor >= len - 1 || cursor == key_start || out->field_count >= kMaxSerialFields) {
      *reason = "bad_json";
      return false;
    }
    TextView key = {json + key_start, cursor - key_start};
    ++cursor;
    if (cursor >= len - 1 || json[cursor++] != ':') {
      *reason = "bad_json";
      return false;
    }
    for (size_t i = 0; i < out->field_count; ++i) {
      if (out->keys[i].len == key.len &&
          memcmp(out->keys[i].data, key.data, key.len) == 0) {
        *reason = "duplicate_key";
        return false;
      }
    }
    const size_t index = out->field_count++;
    out->keys[index] = key;
    if (cursor < len - 1 && json[cursor] == '"') {
      ++cursor;
      const size_t start = cursor;
      while (cursor < len - 1 && json[cursor] != '"') {
        if (json[cursor] == '\\' ||
            static_cast<unsigned char>(json[cursor]) < 0x20) {
          *reason = "bad_json";
          return false;
        }
        ++cursor;
      }
      if (cursor >= len - 1) {
        *reason = "bad_json";
        return false;
      }
      out->strings[index] = {json + start, cursor - start};
      out->value_kinds[index] = 2;
      ++cursor;
    } else {
      const size_t start = cursor;
      while (cursor < len - 1 && json[cursor] != ',' && json[cursor] != '}')
        ++cursor;
      if (cursor == start) {
        *reason = "bad_json";
        return false;
      }
      uint32_t value = 0;
      bool digits = true;
      bool fits_u32 = true;
      for (size_t i = start; i < cursor; ++i) {
        if (json[i] < '0' || json[i] > '9') {
          digits = false;
          break;
        }
        const uint32_t digit = static_cast<uint32_t>(json[i] - '0');
        if (fits_u32 && value > (UINT32_MAX - digit) / 10u) {
          fits_u32 = false;
        } else if (fits_u32) {
          value = value * 10u + digit;
        }
      }
      if (digits && fits_u32) {
        out->numbers[index] = value;
        out->value_kinds[index] = 1;
      } else {
        out->value_kinds[index] = 3;
      }
    }
  }
  if (cursor != len - 1) {
    *reason = "bad_json";
    return false;
  }
  return true;
}

int field_index(const SerialMessage &message, const char *key) {
  for (size_t i = 0; i < message.field_count; ++i)
    if (same(message.keys[i], key))
      return static_cast<int>(i);
  return -1;
}

bool key_in(const char *key, const char *const *fields, size_t count) {
  for (size_t i = 0; i < count; ++i)
    if (strcmp(key, fields[i]) == 0)
      return true;
  return false;
}

const char *check_fields(const SerialMessage &m, const char *const *allowed,
                         size_t count) {
  for (size_t i = 0; i < count; ++i)
    if (field_index(m, allowed[i]) < 0)
      return "missing_field";
  for (size_t i = 0; i < m.field_count; ++i) {
    char key[18] = {};
    if (m.keys[i].len >= sizeof(key))
      return "unknown_field";
    memcpy(key, m.keys[i].data, m.keys[i].len);
    if (!key_in(key, allowed, count))
      return "unknown_field";
  }
  return nullptr;
}

bool all_numbers(const SerialMessage &m, const char *const *fields,
                 size_t count) {
  for (size_t i = 0; i < count; ++i) {
    const int index = field_index(m, fields[i]);
    if (index < 0 || m.value_kinds[index] != 1)
      return false;
  }
  return true;
}

bool flag(const SerialMessage &m, const char *key) {
  const uint32_t *value = serial_number(m, key);
  return value && *value <= 1;
}

bool nonzero(const SerialMessage &m, const char *key) {
  const uint32_t *value = serial_number(m, key);
  return value && *value != 0;
}

const char *validate_message(SerialMessage &m, SerialDirection direction) {
  const uint32_t *version = serial_number(m, "v");
  if (!version || *version != 1)
    return "bad_version";
  const TextView *type = serial_string(m, "t");
  if (!type)
    return "unknown_type";
  m.type = *type;

  const char *const down_types[] = {"hb", "ack", "rs", "tx"};
  const char *const up_types[] = {"fr", "st", "rps", "rpe", "gone", "hello"};
  bool known = false;
  const char *const *types =
      direction == SerialDirection::kDownlink ? down_types : up_types;
  const size_t type_count = direction == SerialDirection::kDownlink ? 4 : 6;
  for (size_t i = 0; i < type_count; ++i)
    if (same(*type, types[i]))
      known = true;
  if (!known)
    return "unknown_type";

  if (same(*type, "hb") || same(*type, "ack")) {
    const char *const fields[] = {"v", "t", "b", "ack"};
    const char *error = check_fields(m, fields, 4);
    if (error)
      return error;
    if (!all_numbers(m, fields + 2, 2))
      return "bad_value";
    if (same(*type, "ack") && !nonzero(m, "b"))
      return "bad_value";
  } else if (same(*type, "rs")) {
    const char *const fields[] = {"v", "t", "b", "from"};
    const char *error = check_fields(m, fields, 4);
    if (error)
      return error;
    if (!all_numbers(m, fields + 2, 2) || !nonzero(m, "b") ||
        !nonzero(m, "from"))
      return "bad_value";
  } else if (same(*type, "tx")) {
    const char *const fields[] = {"v", "t", "dst", "mt", "p"};
    const char *error = check_fields(m, fields, 5);
    if (error)
      return error;
    const uint32_t *dst = serial_number(m, "dst");
    const uint32_t *mt = serial_number(m, "mt");
    const TextView *payload = serial_string(m, "p");
    if (!dst || *dst == 0 || *dst >= 255 || !mt || *mt != 18 || !payload ||
        !valid_hex(*payload, 40) || payload->len != 22)
      return "bad_value";
  } else if (same(*type, "fr")) {
    const char *const common[] = {"v",   "t",  "b",   "n",
                                  "age", "rp", "via", "hex"};
    const char *const data[] = {"v",  "t",   "b",   "n",  "age",
                                "rp", "via", "dup", "hex"};
    for (size_t i = 0; i < 8; ++i)
      if (field_index(m, common[i]) < 0)
        return "missing_field";
    if (!all_numbers(m, common + 2, 5) || !nonzero(m, "b") ||
        !nonzero(m, "n") || !flag(m, "rp") || !nonzero(m, "via") ||
        *serial_number(m, "via") >= 255)
      return "bad_value";
    const TextView *value = serial_string(m, "hex");
    if (!value || !valid_hex(*value, kMaxRadioFrame) || value->len == 0)
      return "bad_value";
    uint8_t bytes[kMaxRadioFrame];
    for (size_t i = 0; i < value->len / 2; ++i)
      bytes[i] = static_cast<uint8_t>((hex_digit(value->data[i * 2]) << 4) |
                                      hex_digit(value->data[i * 2 + 1]));
    FrameView frame = {};
    if (decode_frame(bytes, value->len / 2, &frame) != DecodeError::kNone)
      return "bad_frame";
    const bool is_data = frame.type == 0x01;
    const char *error =
        check_fields(m, is_data ? data : common, is_data ? 9 : 8);
    if (error)
      return error;
    if (is_data && (!all_numbers(m, data + 7, 1) || !flag(m, "dup")))
      return "bad_value";
    if (!is_data && frame.type != 0x11 && frame.type != 0x13 &&
        frame.type != 0x16 && frame.type != 0x17)
      return "unexpected_frame";
  } else if (same(*type, "st")) {
    const char *const fields[] = {"v",     "t",    "b",       "last", "low",
                                  "reach", "held", "unacked", "cap",  "win",
                                  "drop",  "rdrop", "rev",    "coal", "rep",
                                  "rtx",   "out"};
    const char *error = check_fields(m, fields, 17);
    if (error)
      return error;
    if (!all_numbers(m, fields + 2, 15))
      return "bad_value";
    const uint32_t last = *serial_number(m, "last");
    if (!nonzero(m, "b") || !flag(m, "reach") ||
        (last != UINT32_MAX && *serial_number(m, "low") > last + 1u))
      return "bad_value";
  } else if (same(*type, "rps")) {
    const char *const fields[] = {"v",    "t",     "b",    "ent",   "held",
                                  "drop", "rdrop", "rev",  "coal",  "out_ms"};
    const char *error = check_fields(m, fields, 10);
    if (error)
      return error;
    if (!all_numbers(m, fields + 2, 8) || !nonzero(m, "b"))
      return "bad_value";
  } else if (same(*type, "rpe")) {
    const char *const fields[] = {"v", "t", "b", "rep", "drop", "rdrop", "rev", "coal"};
    const char *error = check_fields(m, fields, 8);
    if (error)
      return error;
    if (!all_numbers(m, fields + 2, 6) || !nonzero(m, "b"))
      return "bad_value";
  } else if (same(*type, "gone")) {
    const char *const fields[] = {"v", "t", "b", "from", "to", "kinds"};
    const char *error = check_fields(m, fields, 6);
    if (error)
      return error;
    const TextView *kinds = serial_string(m, "kinds");
    if (!all_numbers(m, fields + 2, 3) || !nonzero(m, "b") ||
        !nonzero(m, "from") || !kinds ||
        *serial_number(m, "to") < *serial_number(m, "from") ||
        *serial_number(m, "to") - *serial_number(m, "from") >= 64 ||
        kinds->len != *serial_number(m, "to") - *serial_number(m, "from") + 1u)
      return "bad_value";
    for (size_t i = 0; i < kinds->len; ++i)
      if (!strchr("RMS?", kinds->data[i]))
        return "bad_value";
  } else if (same(*type, "hello")) {
    const char *const fields[] = {"v", "t", "b", "node", "cap", "win", "fw"};
    const char *error = check_fields(m, fields, 7);
    if (error)
      return error;
    const TextView *fw = serial_string(m, "fw");
    if (!all_numbers(m, fields + 2, 4) || !nonzero(m, "b") ||
        *serial_number(m, "node") != 3 || !fw || fw->len == 0 || fw->len > 16)
      return "bad_value";
    for (size_t i = 0; i < fw->len; ++i) {
      const char c = fw->data[i];
      if (!(c == '.' || c == '_' || c == '-' || (c >= '0' && c <= '9') ||
            (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')))
        return "bad_value";
    }
  }
  return nullptr;
}

} // namespace

const char *error_name(DecodeError error) {
  switch (error) {
  case DecodeError::kNone:
    return "";
  case DecodeError::kOversize:
    return "oversize";
  case DecodeError::kTruncated:
    return "truncated";
  case DecodeError::kBadVersion:
    return "bad_version";
  case DecodeError::kUnknownType:
    return "unknown_type";
  case DecodeError::kBadNodeId:
    return "bad_node_id";
  case DecodeError::kBadSession:
    return "bad_session";
  case DecodeError::kLengthMismatch:
    return "length_mismatch";
  case DecodeError::kBadPayloadLength:
    return "bad_payload_length";
  case DecodeError::kBadValue:
    return "bad_value";
  }
  return "unknown";
}

DecodeError decode_frame(const uint8_t *bytes, size_t len, FrameView *frame) {
  if (len > kMaxRadioFrame)
    return DecodeError::kOversize;
  if (len < kRadioHeaderSize)
    return DecodeError::kTruncated;
  if (bytes[0] != 1)
    return DecodeError::kBadVersion;
  if (!known_type(bytes[1]))
    return DecodeError::kUnknownType;
  if (!node(bytes[2]) || !node(bytes[3]))
    return DecodeError::kBadNodeId;
  if (read_u32(bytes + 4) == 0)
    return DecodeError::kBadSession;
  if (bytes[13] != len - kRadioHeaderSize)
    return DecodeError::kLengthMismatch;
  const DecodeError payload_error =
      validate_payload(bytes[1], bytes + kRadioHeaderSize, bytes[13]);
  if (payload_error != DecodeError::kNone)
    return payload_error;
  if (frame) {
    frame->version = bytes[0];
    frame->type = bytes[1];
    frame->src = bytes[2];
    frame->dst = bytes[3];
    frame->session = read_u32(bytes + 4);
    frame->seq = read_u32(bytes + 8);
    frame->ttl = bytes[12];
    frame->payload_len = bytes[13];
    frame->payload = bytes + kRadioHeaderSize;
  }
  return DecodeError::kNone;
}

bool encode_frame(const FrameView &frame, uint8_t *output, size_t capacity,
                  size_t *written) {
  const size_t len = kRadioHeaderSize + frame.payload_len;
  if (!output || (frame.payload_len != 0 && !frame.payload) || capacity < len ||
      len > kMaxRadioFrame ||
      validate_payload(frame.type, frame.payload, frame.payload_len) !=
          DecodeError::kNone ||
      frame.version != 1 || !node(frame.src) || !node(frame.dst) ||
      frame.session == 0)
    return false;
  output[0] = frame.version;
  output[1] = frame.type;
  output[2] = frame.src;
  output[3] = frame.dst;
  write_u32(output + 4, frame.session);
  write_u32(output + 8, frame.seq);
  output[12] = frame.ttl;
  output[13] = frame.payload_len;
  if (frame.payload_len)
    memcpy(output + kRadioHeaderSize, frame.payload, frame.payload_len);
  if (written)
    *written = len;
  return true;
}

uint16_t crc16_ccitt_false(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFFu;
  for (size_t i = 0; i < len; ++i) {
    crc ^= static_cast<uint16_t>(data[i]) << 8;
    for (uint8_t b = 0; b < 8; ++b)
      crc = (crc & 0x8000u) ? static_cast<uint16_t>((crc << 1) ^ 0x1021u)
                            : static_cast<uint16_t>(crc << 1);
  }
  return crc;
}

SerialMessage decode_serial_line(const char *line, size_t len,
                                 SerialDirection direction) {
  const size_t limit = direction == SerialDirection::kDownlink ? 127 : 639;
  if (len > limit)
    return rejected("too_long");
  SerialMessage result = {};
  if (direction == SerialDirection::kUplink && len && line[0] == '#') {
    result.result = SerialResult::kLog;
    return result;
  }
  if (direction == SerialDirection::kUplink && (!len || line[0] != '{')) {
    result.result = SerialResult::kNoise;
    return result;
  }
  if (len < 6 || line[len - 5] != '*')
    return rejected("no_checksum");
  for (size_t i = 0; i < len; ++i)
    if (static_cast<unsigned char>(line[i]) < 0x20 ||
        static_cast<unsigned char>(line[i]) > 0x7E)
      return rejected("not_ascii");
  uint16_t supplied = 0;
  for (size_t i = len - 4; i < len; ++i) {
    const int digit = hex_digit(line[i]);
    if (digit < 0)
      return rejected("no_checksum");
    supplied = static_cast<uint16_t>((supplied << 4) | digit);
  }
  if (crc16_ccitt_false(reinterpret_cast<const uint8_t *>(line), len - 5) !=
      supplied)
    return rejected("bad_checksum");
  result.json = {line, len - 5};
  const char *reason = nullptr;
  if (!parse_json(&result, line, len - 5, &reason))
    return rejected(reason);
  reason = validate_message(result, direction);
  if (reason)
    return rejected(reason);
  result.result = SerialResult::kMessage;
  result.reason = "";
  return result;
}

bool encode_serial_json(const char *json, size_t json_len, char *output,
                        size_t capacity, size_t *written) {
  if (!json || !output || json_len > kMaxSerialJson || capacity < json_len + 5)
    return false;
  memcpy(output, json, json_len);
  output[json_len] = '*';
  const uint16_t crc =
      crc16_ccitt_false(reinterpret_cast<const uint8_t *>(json), json_len);
  static const char digits[] = "0123456789ABCDEF";
  for (uint8_t i = 0; i < 4; ++i)
    output[json_len + 1 + i] = digits[(crc >> (12 - 4 * i)) & 0xF];
  if (written)
    *written = json_len + 5;
  return true;
}

bool text_equals(TextView value, const char *expected) {
  return same(value, expected);
}
const uint32_t *serial_number(const SerialMessage &message, const char *key) {
  const int i = field_index(message, key);
  return i >= 0 && message.value_kinds[i] == 1 ? &message.numbers[i] : nullptr;
}
const TextView *serial_string(const SerialMessage &message, const char *key) {
  const int i = field_index(message, key);
  return i >= 0 && message.value_kinds[i] == 2 ? &message.strings[i] : nullptr;
}

} // namespace iot
