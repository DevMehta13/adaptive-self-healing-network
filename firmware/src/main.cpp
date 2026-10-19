#ifdef ARDUINO
#include "iot_codec.h"
#include <Arduino.h>

void setup() {
  Serial.begin(115200);
  Serial.println("# F0 codec build; radio integration starts in F1");
}

void loop() { delay(1000); }
#endif
