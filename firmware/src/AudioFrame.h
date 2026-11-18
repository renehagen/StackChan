#pragma once
#include <cstddef>
#include <cstdint>

// Wire format v1: signed PCM16 little-endian, mono, 16 kHz, 20 ms.
constexpr size_t kAudioSamples = 320;
struct AudioFrame {
  int16_t samples[kAudioSamples]{};
  uint16_t count = 0;
  uint32_t capturedMs = 0;
};
