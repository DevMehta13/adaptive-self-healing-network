#ifndef F1_ACK_H
#define F1_ACK_H

#include <stdint.h>

#include "iot_codec.h"

namespace f1 {

// Returns true only for the DELIVERY_ACK that names the pending DATA frame.
// This is deliberately independent of ESP-NOW so it has a native host test.
bool delivery_ack_matches(const iot::FrameView &ack, uint8_t acknowledger,
                          uint8_t recipient, uint8_t data_origin,
                          uint32_t data_session, uint32_t data_seq);

} // namespace f1

#endif
