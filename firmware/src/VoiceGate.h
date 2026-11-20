#pragma once

#include <algorithm>
#include <cstdint>
#include "AudioFrame.h"

// Combine an adaptive noise floor with sustained energy in the speech band.
// This is a local wake gate. No audio leaves the device while waiting.
class VoiceGate {
 public:
  bool observe(const AudioFrame& frame, bool waking) {
    int energy = 0;
    unsigned crossings = 0;
    for (const int16_t sample : frame.samples) {
      lowPass_ += (sample - lowPass_) / 16;
      const int filtered = sample - lowPass_;
      energy += filtered < 0 ? -filtered : filtered;
      if (filtered > 35 && negative_) {
        negative_ = false;
        ++crossings;
      } else if (filtered < -35 && !negative_) {
        negative_ = true;
        ++crossings;
      }
    }
    level_ = energy / kAudioSamples;
    threshold_ = waking ? std::max(450, floor_ * 3) : std::max(175, floor_ * 2);
    const bool voiced = level_ >= threshold_ && crossings >= 3 && crossings <= 95;
    if (!voiced) {
      const int delta = std::min(level_, 450) - floor_;
      floor_ += delta / 32 + (delta > 0 && delta < 32 ? 1 : 0);
    }
    recent_ = ((recent_ << 1) | voiced) & 0x1ffffff;
    return __builtin_popcount(recent_) >= (waking ? 16 : 5);
  }

  void resetWindow() { recent_ = 0; }
  int level() const { return level_; }
  int threshold() const { return threshold_; }

 private:
  int lowPass_ = 0;
  int floor_ = 50;
  int level_ = 0;
  int threshold_ = 450;
  bool negative_ = false;
  uint32_t recent_ = 0;
};
