#pragma once

#include <ArduinoJson.h>

namespace head {
enum class State { Idle, Listening, Speaking, Sleeping };
using StateReader = State (*)();

// The motion task owns UART1. The callback reads only session/audio atomics.
void begin(StateReader state);
bool requestTest();  // One short gesture while idle, using the live motion path.
bool standbySettled();
void standbyStats(JsonObject out);
void diagnostics(JsonObject out);
}
