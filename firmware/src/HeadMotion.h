#pragma once

#include <ArduinoJson.h>
#include <cstdint>

namespace head {
enum class State { Idle, Listening, Speaking, Sleeping };
enum class Alignment : uint8_t { Off, Opening, Ready, Busy, PositionError, SaveError, Saved };
enum class AlignmentAction : uint8_t { None, Left, Right, Save, Cancel };
using StateReader = State (*)();

// The motion task owns UART1 and saved servo settings.
void begin(StateReader state);
void requestConversationPose();
bool requestAlignment();
Alignment alignment();
bool submitAlignment(AlignmentAction action);
bool takeAlignmentSaved();
constexpr bool isAligning(Alignment state) { return state != Alignment::Off && state != Alignment::Saved; }
constexpr bool canAdjust(Alignment state) {
  return state == Alignment::Ready || state == Alignment::PositionError || state == Alignment::SaveError;
}
bool requestTest();  // One short gesture while idle, using the live motion path.
bool standbySettled();
void standbyStats(JsonObject out);
void diagnostics(JsonObject out);
}
