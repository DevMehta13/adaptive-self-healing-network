#ifndef IOT_SENSOR_VALUE_H
#define IOT_SENSOR_VALUE_H

#include <cmath>
#include <stdint.h>

namespace sensor {

// Converts a DHT temperature in degrees Celsius to the DATA value carried on the radio:
// signed hundredths of a degree (23.4 -> 2340, -5.5 -> -550).  A failed DHT read is NaN;
// that, or any non-finite or absurd value, returns false and leaves *out untouched, so
// the caller sends nothing rather than a made-up number.  The DHT11 itself only measures
// 0-50 C in whole degrees; the limits here only guard the int32 conversion.
inline bool celsius_to_hundredths(float celsius, int32_t *out) {
  if (!std::isfinite(celsius) || celsius < -1000.0f || celsius > 1000.0f) return false;
  *out = static_cast<int32_t>(std::lround(static_cast<double>(celsius) * 100.0));
  return true;
}

}  // namespace sensor

#endif
