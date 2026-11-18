#pragma once

#include <stddef.h>
#include <stdint.h>
#include <atomic>

// CoreS3 / official StackChan, Arduino-ESP32 2.0.17 (ESP-IDF 4.4).
// M5.begin() must have run with internal_mic=false and internal_spk=false.
// This class owns I2S1; never call M5.Mic or M5.Speaker while it is active.
// One reader and one writer may run concurrently. begin/end/silence require
// the caller to stop/join the audio workers first. No acoustic echo canceller.
class AudioDuplex {
 public:
  static constexpr uint32_t kSampleRate = 16000;
  static constexpr size_t kChunkSamples = 320;

  AudioDuplex() = default;
  AudioDuplex(const AudioDuplex&) = delete;
  AudioDuplex& operator=(const AudioDuplex&) = delete;

  bool begin();
  // Reads at most kChunkSamples samples; returns a possibly short frame on
  // timeout. The wire stream is stereo, downmixed here to signed mono PCM16.
  size_t read(int16_t* mono, size_t samples, uint32_t timeoutMs);
  // PCM16 mono is duplicated into both I2S slots with adjustable attenuation.
  // Returns false on a timeout/error, including a partially queued write.
  bool write(const int16_t* mono, size_t samples, uint32_t timeoutMs);
  // 0=silent, 50=original -18 dB, 100=-6 dB. No digital boost/clipping.
  void setVolume(uint8_t percent);
  void silence();
  void end();
  bool ready() const { return ready_; }

 private:
  bool installed_ = false;
  bool ready_ = false;
  std::atomic<int32_t> gainQ15_{4096};
  int32_t previousGainQ15_ = 4096; // owned by the single playback writer
};
