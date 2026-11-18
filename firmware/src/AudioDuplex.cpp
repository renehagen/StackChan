// CoreS3 codec initialization adapted from M5Unified 0.2.12 (MIT):
// https://github.com/m5stack/M5Unified/blob/0.2.12/src/M5Unified.cpp
// See _microphone_enabled_cb_cores3 and _speaker_enabled_cb_cores3.
// Original copyright (c) M5Stack. All rights reserved.
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
// of the Software, and to permit persons to whom the Software is furnished to do
// so, subject to the following conditions:
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
// THE SOFTWARE.
//
// 16 kHz / 4.096 MHz MCLK verified against Espressif ES7210 coefficient table:
// https://github.com/espressif/esp-adf/blob/master/components/esp_codec_dev/device/es7210/es7210.c
// Pins: https://docs.m5stack.com/en/core/CoreS3
// This uses two microphones in ordinary I2S stereo. It does not enable the
// additional reference/TDM channel or claim acoustic echo cancellation.

#include "AudioDuplex.h"

#include <Arduino.h>
#include <M5Unified.h>
#include <algorithm>
#include <driver/i2s.h>
#include <esp_intr_alloc.h>

namespace {
constexpr i2s_port_t kPort = I2S_NUM_1;
constexpr uint8_t kMicAddress = 0x40;
constexpr uint8_t kAmpAddress = 0x36;
constexpr uint8_t kIoAddress = 0x58;
constexpr uint32_t kI2cFrequency = 400000;

struct MicRegister { uint8_t reg; uint8_t value; };
// MCLK = 256 * 16000. 16-bit standard stereo I2S; microphones 1+2 only.
constexpr MicRegister kMicInit[] = {
    {0x00, 0xFF}, {0x00, 0x41}, {0x01, 0x1F}, {0x06, 0x00},
    {0x07, 0x20}, {0x08, 0x10}, {0x09, 0x30}, {0x0A, 0x30},
    {0x20, 0x0A}, {0x21, 0x2A}, {0x22, 0x0A}, {0x23, 0x2A},
    {0x02, 0xC1}, {0x04, 0x01}, {0x05, 0x00}, {0x11, 0x60},
    {0x40, 0x42}, {0x41, 0x70}, {0x42, 0x70}, {0x43, 0x1B},
    {0x44, 0x1B}, {0x45, 0x00}, {0x46, 0x00}, {0x47, 0x00},
    {0x48, 0x00}, {0x49, 0x00}, {0x4A, 0x00}, {0x4B, 0x00},
    {0x4C, 0xFF}, {0x01, 0x14},
};

bool ampWrite(uint8_t reg, uint16_t value) {
  // AW88298 register values use big-endian I2C order.
  const uint8_t bytes[] = {static_cast<uint8_t>(value >> 8),
                           static_cast<uint8_t>(value)};
  return M5.In_I2C.writeRegister(kAmpAddress, reg, bytes, sizeof(bytes),
                                kI2cFrequency);
}

TickType_t timeoutTicks(uint32_t ms) {
  return ms == 0 ? 0 : std::max<TickType_t>(1, pdMS_TO_TICKS(ms));
}
}  // namespace

bool AudioDuplex::begin() {
  if (ready_) return true;
  const auto board = M5.getBoard();
  if (board != m5::board_t::board_M5StackCoreS3 &&
      board != m5::board_t::board_M5StackCoreS3SE &&
      board != m5::board_t::board_M5StackChan) return false;
  // M5GFX 0.2.36 recognizes the official robot as StackChan, while
  // M5Unified 0.2.12 has no pinmap for that new board ID. Its display/touch
  // already use the CoreS3 hardware; establish that same internal codec bus.
  // This application does not use the missing M5Unified PMIC/IMU/RTC mapping.
  if (!busInitialized_ && board == m5::board_t::board_M5StackChan &&
      !M5.In_I2C.begin(I2C_NUM_1, 12, 11)) return false;
  if (!M5.In_I2C.isEnabled()) return false;
  busInitialized_ = true;

  // Do not uninstall an existing driver's port: accidental M5.Mic/Speaker
  // ownership should fail initialization, rather than corrupt another task.
  i2s_config_t config = {};
  config.mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_TX);
  config.sample_rate = kSampleRate;
  config.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  config.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  config.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  config.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  config.dma_buf_count = 4;
  config.dma_buf_len = 160;  // 10 ms per stereo DMA buffer, 40 ms total.
  config.use_apll = false;
  config.tx_desc_auto_clear = true;
  config.mclk_multiple = I2S_MCLK_MULTIPLE_256;
  config.bits_per_chan = I2S_BITS_PER_CHAN_16BIT;
  if (i2s_driver_install(kPort, &config, 0, nullptr) != ESP_OK) return false;
  installed_ = true;

  i2s_pin_config_t pins = {};
  pins.mck_io_num = 0;
  pins.bck_io_num = 34;
  pins.ws_io_num = 33;
  pins.data_in_num = 14;
  pins.data_out_num = 13;
  if (i2s_set_pin(kPort, &pins) != ESP_OK ||
      i2s_zero_dma_buffer(kPort) != ESP_OK) {
    end();
    return false;
  }

  for (const auto& item : kMicInit) {
    if (!M5.In_I2C.writeRegister8(kMicAddress, item.reg, item.value,
                                  kI2cFrequency)) {
      end();
      return false;
    }
  }
  // Enable the amplifier supply/reset through the CoreS3 AW9523, not through
  // the robot-base PY32. This does not switch servo power or move any motor.
  if (!M5.In_I2C.bitOn(kIoAddress, 0x02, 0x04, kI2cFrequency)) {
    end();
    return false;
  }
  delay(5);
  // The sample-rate selector is 3 for 16 kHz. I2SBCK=0: 16 bits * 2 slots.
  // Digital attenuation is applied in write(), not an undocumented register.
  if (!ampWrite(0x61, 0x0673) || !ampWrite(0x04, 0x4040) ||
      !ampWrite(0x05, 0x0008) || !ampWrite(0x06, 0x14C3) ||
      !ampWrite(0x0C, 0x0064)) {
    end();
    return false;
  }
  ready_ = true;
  return true;
}

size_t AudioDuplex::read(int16_t* mono, size_t samples, uint32_t timeoutMs) {
  if (!ready_ || !mono || samples == 0) return 0;
  int16_t stereo[kChunkSamples * 2];
  const size_t requested = std::min(samples, kChunkSamples);
  size_t received = 0;
  const esp_err_t result = i2s_read(kPort, stereo, requested * 4, &received,
                                    timeoutTicks(timeoutMs));
  if (result != ESP_OK || (received % 4) != 0) return 0;
  const size_t frames = received / 4;
  for (size_t i = 0; i < frames; ++i) {
    const int32_t sum = static_cast<int32_t>(stereo[i * 2]) + stereo[i * 2 + 1];
    mono[i] = static_cast<int16_t>(sum / 2);
  }
  return frames;
}

void AudioDuplex::setVolume(uint8_t percent) {
  const uint32_t volume = std::min<uint32_t>(100, percent);
  gainQ15_.store((16384 * volume * volume) / 10000);
}

bool AudioDuplex::write(const int16_t* mono, size_t samples, uint32_t timeoutMs) {
  if (!ready_ || (!mono && samples != 0)) return false;
  int16_t stereo[kChunkSamples * 2];
  const uint32_t started = millis();
  size_t offset = 0;
  while (offset < samples) {
    const size_t frames = std::min(samples - offset, kChunkSamples);
    const int32_t gain = gainQ15_.load();
    for (size_t i = 0; i < frames; ++i) {
      // Ramp over one frame so a volume button doesn't produce a click.
      const int32_t ramp = previousGainQ15_ +
          (gain - previousGainQ15_) * static_cast<int32_t>(i + 1) / static_cast<int32_t>(frames);
      const int16_t quiet = static_cast<int32_t>(mono[offset + i]) * ramp / 32768;
      stereo[i * 2] = quiet;
      stereo[i * 2 + 1] = quiet;
    }
    previousGainQ15_ = gain;
    const uint32_t elapsed = millis() - started;
    const uint32_t remaining = elapsed >= timeoutMs ? 0 : timeoutMs - elapsed;
    size_t written = 0;
    const esp_err_t result = i2s_write(kPort, stereo, frames * 4, &written,
                                      timeoutTicks(remaining));
    if (result != ESP_OK || written != frames * 4) return false;
    offset += frames;
  }
  return true;
}

void AudioDuplex::silence() {
  if (installed_) i2s_zero_dma_buffer(kPort);
}

bool AudioDuplex::end() {
  // Owner must stop/join workers first. M5 I2C remains owned by M5Unified.
  ready_ = false;
  if (!installed_) return true;
  i2s_zero_dma_buffer(kPort);
  ampWrite(0x04, 0x4000);
  auto& bus = M5.In_I2C;
  const bool ampOff = bus.bitOff(kIoAddress, 0x02, 0x04, kI2cFrequency);
  // ES7210 standby disables mic bias, ADC, analog stages and clocks.
  // https://github.com/espressif/esp-audio-dev/blob/main/esp_codec_dev/device/es7210/es7210.c
  bool micOff = true;
  for (uint8_t reg = 0x47; reg <= 0x4C; ++reg) {
    if (!bus.writeRegister8(kMicAddress, reg, 0xFF, kI2cFrequency)) micOff = false;
  }
  if (!bus.writeRegister8(kMicAddress, 0x40, 0xC0, kI2cFrequency)) micOff = false;
  if (!bus.writeRegister8(kMicAddress, 0x01, 0x7F, kI2cFrequency)) micOff = false;
  if (!bus.writeRegister8(kMicAddress, 0x06, 0x07, kI2cFrequency)) micOff = false;
  installed_ = i2s_driver_uninstall(kPort) != ESP_OK;
  return !installed_ && ampOff && micOff;
}
