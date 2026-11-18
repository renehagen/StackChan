#pragma once
#include <ArduinoJson.h>
#include "AudioFrame.h"

namespace voiceTest {
bool reset();
bool append(const char* encoded);
bool arm();
bool enabled();
bool say();
bool expired();
void stop();
void measureMic(const AudioFrame& frame, bool echoGuard);
void markVoicedReply();
void playPrompt(AudioFrame& frame);
void diagnostics(JsonObject out);
}
