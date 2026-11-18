#include "VoiceTest.h"
#include <Arduino.h>
#include <algorithm>
#include <atomic>
#include <esp_heap_caps.h>
#include <mbedtls/base64.h>

namespace voiceTest {
namespace {
constexpr size_t kMaxSamples = 16000*6;
int16_t* pcm = nullptr;
size_t length = 0, offset = 0;
portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
std::atomic<bool> active{false}, playing{false};
std::atomic<unsigned> promptFrames{0}, promptMicFrames{0}, replyMicFrames{0}, voicedReplyFrames{0};
uint32_t started = 0, lastHost = 0;
}

bool reset() {
  if (active) return false;
  if (!pcm) pcm = static_cast<int16_t*>(heap_caps_malloc(kMaxSamples*2, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  length = 0;
  return pcm != nullptr;
}

bool append(const char* encoded) {
  if (!pcm || active) return false;
  const size_t size = strlen(encoded);
  uint8_t decoded[640];
  size_t n = 0;
  if (!size || size > 856 || mbedtls_base64_decode(decoded, sizeof(decoded), &n,
      reinterpret_cast<const uint8_t*>(encoded), size) || !n || n%2 || length+n/2 > kMaxSamples) return false;
  portENTER_CRITICAL(&lock);
  memcpy(pcm+length, decoded, n);
  length += n/2;
  portEXIT_CRITICAL(&lock);
  return true;
}

bool arm() {
  if (active || length < 8000) return false;
  started = lastHost = millis();
  promptFrames = 0;
  promptMicFrames = 0;
  replyMicFrames = 0;
  voicedReplyFrames = 0;
  active = true;
  return true;
}
bool enabled() { return active.load(); }

bool say() {
  portENTER_CRITICAL(&lock);
  const bool ok = active && !playing;
  if (ok) { offset = 0; playing = true; }
  portEXIT_CRITICAL(&lock);
  return ok;
}
bool expired() {
  return active && (millis()-started > 180000 || millis()-lastHost > 12000);
}
void stop() {
  portENTER_CRITICAL(&lock);
  active = false;
  playing = false;
  portEXIT_CRITICAL(&lock);
}
void playPrompt(AudioFrame& frame) {
  if (!playing) return;
  portENTER_CRITICAL(&lock);
  if (active && playing) {
    const size_t n = std::min(kAudioSamples, length-offset);
    frame.count = kAudioSamples;
    memcpy(frame.samples, pcm+offset, n*2);
    if (n < kAudioSamples) memset(frame.samples+n, 0, (kAudioSamples-n)*2);
    offset += n;
    ++promptFrames;
    if (offset == length) playing = false;
  }
  portEXIT_CRITICAL(&lock);
}
void markVoicedReply() { ++voicedReplyFrames; }
void measureMic(const AudioFrame& frame, bool echoGuard) {
  const bool prompt = playing.load();
  if (!prompt && !echoGuard) return;
  uint32_t sum = 0;
  for (unsigned i = 0; i < frame.count; ++i) sum += abs(static_cast<int>(frame.samples[i]));
  if (sum > frame.count*150u) {
    if (prompt) ++promptMicFrames;
    else ++replyMicFrames;
  }
}
void diagnostics(JsonObject out) {
  if (active) lastHost = millis();
  out["active"] = active.load();
  out["prompt_playing"] = playing.load();
  out["prompt_frames"] = promptFrames.load();
  out["prompt_mic_frames"] = promptMicFrames.load();
  out["reply_mic_frames"] = replyMicFrames.load();
  out["voiced_reply_frames"] = voicedReplyFrames.load();
}
}
