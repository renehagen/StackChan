#include "DeviceUi.h"

#include <Arduino.h>
#include <M5Unified.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <esp_heap_caps.h>
#include <new>

namespace {
constexpr uint32_t kBackground = 0x0D1825;
constexpr uint32_t kButton = 0x2C3847;
constexpr uint32_t kMint = 0x78FAB2;
constexpr uint32_t kPeach = 0xFFB48C;
constexpr uint32_t kWhite = 0xFAF9F2;
constexpr uint32_t kMuted = 0x91A2B4;
constexpr uint32_t kStop = 0x602635;
constexpr uint32_t kCoral = 0xFF6869;
constexpr int kWidth = 320, kHeight = 240;
constexpr uint32_t kIdleFrameMs = 100, kWaveFrameMs = 40, kWaveSampleMs = 80;
constexpr uint32_t kTouchDebounceMs = 180;
constexpr uint32_t kControlsTimeoutMs = 10000;
constexpr uint32_t kStandbyTimeoutMs = 60000, kTopTouchPollMs = 20;
constexpr uint8_t kTopTouchAddress = 0x68, kTopTouchOutput = 0x10;
constexpr uint32_t kTopTouchI2cHz = 100000;
// StackChan-BSP drives the two six-pixel strips through the PY32 expander.
// https://github.com/m5stack/StackChan-BSP/blob/main/src/drivers/PY32IOExpander/PY32IOExpander.cpp
constexpr uint8_t kLedExpander = 0x6f, kLedConfig = 0x24, kLedRam = 0x30;
constexpr uint8_t kLedCount = 12, kLedRefresh = 1 << 6;
constexpr uint32_t kLedI2cHz = 100000;
constexpr uint16_t kLedOff = 0, kLedGreen = m5gfx::color565(0, 144, 0),
                   kLedOrange = m5gfx::color565(144, 52, 0);
constexpr int kSpeechPattern[] = {1, 2, 1, 3, 1};
constexpr int kBarPitch = 10;

using Canvas = m5gfx::M5Canvas;
struct Rect {
  int x, y, w, h;
  bool contains(int px, int py) const {
    return px >= x && py >= y && px < x+w && py < y+h;
  }
};
constexpr Rect kHeader{0, 0, 320, 40};
constexpr Rect kFooter{0, 193, 320, 47};
constexpr Rect kBody{0, 40, 320, 153};
constexpr Rect kVolumePanel{0, 145, 320, 48};
constexpr Rect kMic{6, 196, 98, 39}, kVolume{111, 196, 98, 39}, kSession{216, 196, 98, 39};
constexpr Rect kMinus{58, 148, 56, 42}, kPlus{206, 148, 56, 42};
constexpr Rect kAlignLeft{24, 83, 128, 67}, kAlignRight{168, 83, 128, 67};
constexpr Rect kAlignCancel{24, 186, 128, 43}, kAlignSave{168, 186, 128, 43};

Rect unite(Rect a, Rect b) {
  if (!a.h) return b;
  if (!b.h) return a;
  const int x = std::min(a.x, b.x), y = std::min(a.y, b.y);
  return {x, y, std::max(a.x + a.w, b.x + b.w) - x,
                std::max(a.y + a.h, b.y + b.h) - y};
}

void pushRegion(Canvas& c, Rect r) {
  // M5GFX clips the strided source before transferring to SPI.
  M5.Display.setClipRect(r.x, r.y, r.w, r.h);
  c.pushSprite(0, 0);
  M5.Display.clearClipRect();
}

bool showStop(const UiSnapshot& s) { return s.active || s.starting || s.closing; }
bool canSession(const UiSnapshot& s) { return !s.closing && (s.active || s.starting || s.connected); }
bool canMute(const UiSnapshot& s) { return s.active && !s.closing && !s.mutePending; }

void valueText(char (&text)[8], uint8_t percent) {
  std::snprintf(text, sizeof(text), "%u%%", static_cast<unsigned>(percent));
}

void statusLine(Canvas& c, const UiSnapshot& s) {
  c.fillRect(0, 0, kWidth, kHeader.h, kBackground);
  if (s.active && (s.muted || s.mutePending)) {
    c.fillSmoothCircle(13, 17, 5, kMuted);
    c.setTextSize(1);
    c.setFont(&fonts::FreeSansBold12pt7b);
    c.setTextColor(kWhite, kBackground);
    c.setTextDatum(middle_left);
    const char* label = s.mutePending ? "Microfoon omschakelen..." : "Microfoon gedempt";
    const int maxWidth = s.batteryPercent >= 0 ? 186 : 263;
    if (c.textWidth(label) > maxWidth) c.setFont(&fonts::FreeSansBold9pt7b);
    if (c.textWidth(label) > maxWidth) c.setFont(&fonts::Font2);
    c.drawString(label, 26, 17);
  }
  if (s.batteryPercent >= 0) {
    const uint32_t ink = s.batteryPercent <= 15 ? kPeach : kMuted;
    c.drawRoundRect(222, 11, 17, 12, 2, ink);
    c.fillRect(239, 15, 2, 4, ink);
    const int chargeWidth = (13 * s.batteryPercent + 50) / 100;
    if (chargeWidth) c.fillRect(224, 13, chargeWidth, 8, ink);
    char percent[8];
    valueText(percent, s.batteryPercent);
    c.setTextSize(1);
    c.setFont(&fonts::FreeSansBold9pt7b);
    c.setTextColor(ink, kBackground);
    c.setTextDatum(middle_right);
    c.drawString(percent, 285, 17);
  }
  const uint32_t color = s.wifiConnected ? kMuted : kButton;
  c.drawWideLine(294, 12, 298, 9, 1, color);
  c.drawWideLine(298, 9, 302, 8, 1, color);
  c.drawWideLine(302, 8, 306, 8, 1, color);
  c.drawWideLine(306, 8, 310, 9, 1, color);
  c.drawWideLine(310, 9, 314, 12, 1, color);
  c.drawWideLine(298, 15, 302, 13, 1, color);
  c.drawWideLine(302, 13, 306, 13, 1, color);
  c.drawWideLine(306, 13, 310, 15, 1, color);
  c.fillSmoothCircle(304, 20, 1, color);
}

void idleStatus(Canvas& c, const UiSnapshot& s) {
  const char* label = std::strcmp(s.status, "Tik op Start") == 0 ? "Gesprek gestopt" : s.status;
  c.setTextSize(1);
  c.setFont(&fonts::FreeSansBold12pt7b);
  if (c.textWidth(label) > 290) c.setFont(&fonts::FreeSansBold9pt7b);
  if (c.textWidth(label) > 290) c.setFont(&fonts::Font2);
  c.setTextColor(kWhite, kBackground);
  c.setTextDatum(middle_center);
  c.drawString(label, kWidth / 2, kHeight / 2);
}

void alignmentScreen(Canvas& c, const UiSnapshot& s) {
  c.fillScreen(kBackground);
  statusLine(c, s);
  c.setTextSize(1);
  c.setTextDatum(middle_center);
  c.setFont(&fonts::FreeSansBold12pt7b);
  c.setTextColor(kWhite, kBackground);
  c.drawString("Middenstand uitlijnen", kWidth / 2, 57);

  const bool adjustable = head::canAdjust(s.alignment);
  const uint32_t arrowColor = adjustable ? kWhite : kMuted;
  for (const Rect& r : {kAlignLeft, kAlignRight}) {
    c.fillSmoothRoundRect(r.x, r.y, r.w, r.h, 12, kButton);
    const int x = r.x + r.w / 2, y = r.y + r.h / 2;
    const int direction = r.x < kWidth / 2 ? -1 : 1;
    const int tip = x + direction * 13, base = x - direction * 3;
    c.drawWideLine(x - direction * 14, y, tip, y, 3, arrowColor);
    c.drawWideLine(base, y - 13, tip, y, 3, arrowColor);
    c.drawWideLine(base, y + 13, tip, y, 3, arrowColor);
  }

  c.setFont(&fonts::FreeSansBold9pt7b);
  const char* status = "";
  switch (s.alignment) {
    case head::Alignment::Opening: status = "Hoofdpositie lezen..."; break;
    case head::Alignment::Busy: status = "Even wachten..."; break;
    case head::Alignment::PositionError: status = "Hoofdpositie niet bevestigd"; break;
    case head::Alignment::SaveError: status = "Bewaren mislukt. Probeer opnieuw."; break;
    default: break;
  }
  c.setTextColor(kMuted, kBackground);
  c.drawString(status, kWidth / 2, 166);
  for (const Rect& r : {kAlignCancel, kAlignSave}) {
    const bool save = r.x > kWidth / 2;
    const uint32_t bg = save && adjustable ? 0x21463E : kButton;
    c.fillSmoothRoundRect(r.x, r.y, r.w, r.h, 8, bg);
    c.setTextColor(adjustable ? (save ? kMint : kWhite) : kMuted, bg);
    c.drawString(save ? "Bewaar" : "Annuleer", r.x + r.w / 2, r.y + r.h / 2);
  }
}

enum class Icon { Mic, Muted, Speaker, Stop, Play };

void icon(Canvas& c, Icon shape, int x, int y, uint32_t fg, uint32_t bg) {
  if (shape == Icon::Mic || shape == Icon::Muted) {
    c.fillSmoothRoundRect(x-3, y-9, 7, 12, 3, fg);
    c.fillSmoothRoundRect(x-7, y-3, 15, 11, 6, fg);
    c.fillSmoothRoundRect(x-5, y-5, 11, 11, 5, bg);
    c.fillSmoothRoundRect(x-3, y-8, 7, 12, 3, fg);
    c.drawWideLine(x, y+7, x, y+10, 1, fg);
    c.drawWideLine(x-4, y+10, x+4, y+10, 1, fg);
    if (shape == Icon::Muted) c.drawWideLine(x-8, y-9, x+8, y+8, 1.3, kPeach);
  } else if (shape == Icon::Speaker) {
    c.fillSmoothRoundRect(x-8, y-3, 6, 7, 1, fg);
    c.fillTriangle(x-4, y-3, x+1, y-8, x+1, y+8, fg);
    c.fillTriangle(x-4, y-3, x+1, y+8, x-4, y+4, fg);
    c.drawWideLine(x-4, y-3, x+1, y-8, .5f, fg);
    c.drawWideLine(x-4, y+4, x+1, y+8, .5f, fg);
    c.drawWideLine(x+5, y-5, x+7, y-2, 1, fg);
    c.drawWideLine(x+7, y-2, x+7, y+2, 1, fg);
    c.drawWideLine(x+7, y+2, x+5, y+5, 1, fg);
  } else if (shape == Icon::Stop) {
    c.fillSmoothRoundRect(x-7, y-7, 15, 15, 3, fg);
  } else {
    c.fillTriangle(x-5, y-8, x-5, y+8, x+8, y, fg);
    c.drawWideLine(x-5, y-8, x+8, y, .6f, fg);
    c.drawWideLine(x-5, y+8, x+8, y, .6f, fg);
  }
}

void button(Canvas& c, Rect r, Icon shape, const char* label, uint32_t bg, uint32_t fg, uint32_t ink) {
  c.fillSmoothRoundRect(r.x, r.y, r.w, r.h, 8, bg);
  c.setFont(&fonts::FreeSansBold9pt7b);
  c.setTextSize(1);
  const int group = 19 + 6 + c.textWidth(label);
  const int left = r.x + (r.w-group)/2;
  const int mid = r.y + r.h/2;
  icon(c, shape, left+9, mid, ink, bg);
  c.setTextColor(fg, bg);
  c.setTextDatum(middle_left);
  c.drawString(label, left+25, mid);
}

void toolbar(Canvas& c, const UiSnapshot& s, bool volumeOpen) {
  c.fillRect(0, kFooter.y, kWidth, kFooter.h, kBackground);
  const uint32_t micInk = canMute(s) ? kWhite : kMuted;
  const bool muted = s.active && s.muted;
  button(c, kMic, muted ? Icon::Muted : Icon::Mic, muted ? "Mic aan" : "Demp", kButton, micInk, micInk);
  char value[8]; valueText(value, s.volume);
  button(c, kVolume, Icon::Speaker, value, volumeOpen ? 0x364C60 : kButton, kWhite, kWhite);
  const bool stop = showStop(s);
  button(c, kSession, stop ? Icon::Stop : Icon::Play, stop ? "Stop" : "Start",
         !canSession(s) ? kButton : (stop ? kStop : 0x21463E),
         canSession(s) ? kWhite : kMuted, !canSession(s) ? kMuted : (stop ? kCoral : kMint));
}

void volumePanel(Canvas& c, uint8_t volume) {
  c.fillSmoothRoundRect(56, 147, 208, 44, 10, kButton);
  char value[8]; valueText(value, volume);
  c.setTextDatum(middle_center);
  c.setTextColor(kWhite, kButton);
  c.setFont(&fonts::FreeSansBold12pt7b);
  c.drawString(value, 160, 169);
  c.drawWideLine(78, 169, 94, 169, 1.6, volume ? kWhite : kMuted);
  const uint32_t ink = volume < 100 ? kWhite : kMuted;
  c.drawWideLine(226, 169, 242, 169, 1.6, ink);
  c.drawWideLine(234, 161, 234, 177, 1.6, ink);
}

void retainPeak(std::atomic<uint32_t>& peak, uint32_t level) {
  uint32_t previous = peak.load(std::memory_order_relaxed);
  while (level > previous && !peak.compare_exchange_weak(previous, level, std::memory_order_relaxed)) {}
}

uint8_t waveLevel(uint32_t peak, bool speaking) {
  const uint32_t floor = speaking ? 160 : 95;
  if (peak <= floor) return 0;
  const float level = peak - floor;
  // Give loud microphone syllables room to grow without flattening them.
  constexpr float knee = 5200.0f;
  constexpr float micFullScale = 32768.0f - 95.0f;
  const float normalized = speaking ? std::min(1.0f, level / 4900.0f) :
      (level / (level + knee)) * ((micFullScale + knee) / micFullScale);
  return static_cast<uint8_t>(255 * std::sqrt(normalized));
}

uint32_t blend(uint32_t from, uint32_t to, unsigned opacity) {
  const auto channel = [=](unsigned shift) {
    return ((((from >> shift) & 255) * (255 - opacity) + ((to >> shift) & 255) * opacity + 127) / 255) << shift;
  };
  return channel(16) | channel(8) | channel(0);
}

uint16_t spriteColor(uint32_t rgb) {
  const uint16_t color = ((rgb >> 8) & 0xF800) | ((rgb >> 5) & 0x07E0) | ((rgb >> 3) & 0x001F);
  return (color << 8) | (color >> 8);
}

// Supersample the implicit heart outline once, before the UI starts.
template<int Size> std::array<uint8_t, Size * Size> heartMask() {
  std::array<uint8_t, Size * Size> mask{};
  constexpr int samples = 3;
  for (int row = 0; row < Size; ++row) {
    for (int col = 0; col < Size; ++col) {
      int covered = 0;
      for (int sy = 0; sy < samples; ++sy) {
        for (int sx = 0; sx < samples; ++sx) {
          const float x = (col * samples + sx + 0.5f) * (2.45f / (Size * samples)) - 1.225f;
          const float y = 1.32f - (row * samples + sy + 0.5f) * (2.45f / (Size * samples));
          const float a = x * x + y * y - 1;
          if (a * a * a <= x * x * y * y * y) ++covered;
        }
      }
      mask[row * Size + col] = covered * 255 / (samples * samples);
    }
  }
  return mask;
}

constexpr int kSmallHeart = 17, kMediumHeart = 23, kLargeHeart = 31;
const auto smallHeart = heartMask<kSmallHeart>();
const auto mediumHeart = heartMask<kMediumHeart>();
const auto largeHeart = heartMask<kLargeHeart>();

struct Heart {
  int x, size, delay, duration, drift;
  uint32_t color;
  const uint8_t* mask;
};
// The 8-pixel sway keeps the footprints in separate columns for fast compositing.
const Heart kHearts[] = {
    {154, kLargeHeart,     0, 2070,  8, 0xFF638D, largeHeart.data()},
    { 55, kMediumHeart,  400, 1880, -7, 0xFFAAAE, mediumHeart.data()},
    {251, kLargeHeart,   800, 1960, -8, 0xFF5883, largeHeart.data()},
    {108, kSmallHeart,  1200, 1680,  7, 0xFFA1BC, smallHeart.data()},
    {204, kMediumHeart, 1600, 1970,  8, 0xF785BB, mediumHeart.data()},
    {294, kSmallHeart,  2000, 1760,  7, 0xFFAC9B, smallHeart.data()},
};
constexpr size_t kHeartCount = sizeof(kHearts) / sizeof(kHearts[0]);
constexpr size_t kHeartScratchPixels = kLargeHeart * kLargeHeart;
constexpr uint32_t kHeartTouchCooldownMs = 1950;
constexpr uint32_t kHeartPulsePeriodMs = 1200;
constexpr uint32_t kHeartPulseMinMs = 10 * kHeartPulsePeriodMs;
constexpr int kHeartSpawnPeriodMs = 2 * kHeartPulsePeriodMs;
constexpr uint32_t kLedFrameMs = 20;

uint16_t heartPulseColor(uint32_t age) {
  constexpr uint32_t riseMs = 360, fadeMs = 640;
  // One gentle rise, a longer fade, and a dark pause on every beat.
  const uint32_t phase = age % kHeartPulsePeriodMs;
  const float level = phase < riseMs ? static_cast<float>(phase) / riseMs :
      std::max(0.0f, 1.0f - static_cast<float>(phase - riseMs) / fadeMs);
  const uint8_t red = std::lround(152 * level * level * (3 - 2 * level));
  return m5gfx::color565(red, 0, 0);
}

struct HeartFrame {
  Rect square, visible;
  unsigned opacity;
};

HeartFrame heartFrame(const Heart& heart, int age, uint32_t animationDuration, bool volumeOpen) {
  if (age < heart.delay) return {};
  const int elapsed = (age - heart.delay) % kHeartSpawnPeriodMs;
  if (elapsed >= heart.duration ||
      static_cast<uint32_t>(age - elapsed + heart.duration) > animationDuration) return {};
  const float t = static_cast<float>(elapsed) / heart.duration;
  const int x = heart.x - heart.size / 2 + std::lround(heart.drift * std::sin(8 * t));
  const int y = kBody.y + kBody.h -
      std::lround((kBody.h + heart.size) * t * (1.15f - 0.15f * t));
  const int top = std::max(y, kBody.y);
  const int bottom = std::min(y + heart.size, volumeOpen ? kVolumePanel.y : kBody.y + kBody.h);
  if (bottom <= top) return {};
  const unsigned opacity = std::min(255, (heart.duration - elapsed) * 255 / 180);
  return {{x, y, heart.size, heart.size}, {x, top, heart.size, bottom - top}, opacity};
}

void compositeHeart(uint16_t* canvas, uint16_t* scratch, const Heart& heart, const HeartFrame& frame) {
  const Rect& r = frame.visible;
  auto* pixels = canvas + r.y * kWidth + r.x;
  const auto* mask = heart.mask + (r.y - frame.square.y) * heart.size;
  const uint16_t solid = spriteColor(heart.color);
  for (int row = 0; row < r.h; ++row, pixels += kWidth, mask += r.w, scratch += r.w) {
    std::copy_n(pixels, r.w, scratch);
    for (int col = 0; col < r.w; ++col) {
      const unsigned alpha = mask[col] * frame.opacity / 255;
      if (alpha == 255) {
        pixels[col] = solid;
      } else if (alpha) {
        const m5gfx::swap565_t pixel{pixels[col]};
        const uint32_t background = (pixel.R8() << 16) | (pixel.G8() << 8) | pixel.B8();
        pixels[col] = spriteColor(blend(background, heart.color, alpha));
      }
    }
  }
}

void restoreHeart(uint16_t* canvas, const uint16_t* scratch, Rect r) {
  auto* pixels = canvas + r.y * kWidth + r.x;
  for (int row = 0; row < r.h; ++row, pixels += kWidth, scratch += r.w)
    std::copy_n(scratch, r.w, pixels);
}

void waveBar(Canvas& c, int x, int y, int height) {
  if (height < 2) return;
  // Seven-pixel capsule with antialiased caps. Draw directly into the RGB565
  // sprite to avoid floating-point distance calculations for every pixel.
  constexpr uint8_t cap[3][7] = {
      {0, 0, 130, 220, 130, 0, 0},
      {0, 172, 255, 255, 255, 172, 0},
      {116, 255, 255, 255, 255, 255, 116},
  };
  const int span = 2 * height;
  auto* pixels = static_cast<uint16_t*>(c.getBuffer()) + (y - height) * kWidth + x - 3;
  for (int row = 0; row <= span; ++row, pixels += kWidth) {
    const uint32_t ink = blend(0x21D3AC, 0x206383, row * 255 / span);
    const int edge = std::min(row, span - row);
    if (edge >= 3) {
      std::fill_n(pixels, 7, spriteColor(ink));
    } else {
      const uint32_t background = row == height ? 0x235364 : kBackground;
      for (int column = 0; column < 7; ++column) {
        const uint8_t coverage = cap[edge][column];
        if (coverage) pixels[column] = spriteColor(blend(background, ink, coverage));
      }
    }
  }
}

void speechWave(Canvas& c, int y, int unit, int pairs) {
  waveBar(c, kWidth / 2, y, unit);
  for (int pair = 1; pair <= pairs; ++pair) {
    const int offset = kBarPitch * pair;
    const int halfHeight = kSpeechPattern[pair] * unit;
    waveBar(c, kWidth / 2 - offset, y, halfHeight);
    waveBar(c, kWidth / 2 + offset, y, halfHeight);
  }
}

void microphoneBadge(Canvas& c, int y, bool muted) {
  constexpr int x = 281;
  const uint32_t fill = muted ? 0x303C51 : 0x281586;
  c.fillSmoothCircle(x, y, 24, fill);
  icon(c, muted ? Icon::Muted : Icon::Mic, x, y, kWhite, fill);
}

bool setupTopTouch() {
  if (M5.getBoard() != m5::board_t::board_M5StackChan) return false;
  // The StackChan BSP's Si12T defaults for the three zones.
  for (uint8_t reg = 0x0a; reg <= 0x0f; ++reg)
    if (!M5.In_I2C.writeRegister8(kTopTouchAddress, reg, 0, kTopTouchI2cHz)) return false;
  if (!M5.In_I2C.writeRegister8(kTopTouchAddress, 0x09, 0x0f, kTopTouchI2cHz) ||
      !M5.In_I2C.writeRegister8(kTopTouchAddress, 0x09, 0x03, kTopTouchI2cHz) ||
      !M5.In_I2C.writeRegister8(kTopTouchAddress, 0x08, 0x22, kTopTouchI2cHz)) return false;
  for (uint8_t reg = 0x02; reg <= 0x06; ++reg)
    if (!M5.In_I2C.writeRegister8(kTopTouchAddress, reg, 0x33, kTopTouchI2cHz)) return false;
  return true;
}
}  // namespace

bool DeviceUi::begin() {
  if (ready()) return true;
  // Never consume 154 kB of internal RAM as an implicit sprite fallback.
  if (!psramFound()) return false;
  // Keep the saved pixels for the temporary heart overlay next to the sprite.
  // The extra word preserves the sprite's trailing padding.
  pixels_ = heap_caps_calloc(kWidth * kHeight + 2 + kHeartCount * kHeartScratchPixels, sizeof(uint16_t),
                             MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!pixels_) return false;
  auto* canvas = new (std::nothrow) m5gfx::M5Canvas(&M5.Display);
  if (!canvas) {
    releaseBeforeTask();
    return false;
  }
  canvas_ = canvas;
  canvas->setColorDepth(16);
  canvas->setBuffer(pixels_, kWidth, kHeight, 16);
  canvas->setTextWrap(false);
  snapshots_ = xQueueCreate(1, sizeof(UiSnapshot));
  commands_ = xQueueCreate(8, sizeof(UiCommand));
  if (!snapshots_ || !commands_) {
    releaseBeforeTask();
    return false;
  }
  UiSnapshot initial;
  std::snprintf(initial.status, sizeof(initial.status), "Verbinding voorbereiden...");
  xQueueOverwrite(snapshots_, &initial);
  if (xTaskCreatePinnedToCore(taskEntry, "stackchan_ui", 4096, this, 1,
                              &task_, 1) != pdPASS) {
    releaseBeforeTask();
    return false;
  }
  headLedsSupported_ = M5.getBoard() == m5::board_t::board_M5StackChan;
  if (headLedsSupported_) {
    headLedRequests_ = xQueueCreate(1, sizeof(HeadLedRequest));
    if (headLedRequests_ &&
        xTaskCreatePinnedToCore([](void* context) { static_cast<DeviceUi*>(context)->runHeadLeds(); },
                                "stackchan_leds", 3072, this, 2, nullptr, 1) != pdPASS) {
      vQueueDelete(headLedRequests_);
      headLedRequests_ = nullptr;
    }
  }
  ready_.store(true, std::memory_order_release);
  return true;
}

void DeviceUi::releaseBeforeTask() {
  if (snapshots_) vQueueDelete(snapshots_);
  if (commands_) vQueueDelete(commands_);
  snapshots_ = nullptr;
  commands_ = nullptr;
  // setBuffer uses caller-owned memory; deleting the canvas does not free it.
  delete canvas_;
  canvas_ = nullptr;
  if (pixels_) heap_caps_free(pixels_);
  pixels_ = nullptr;
}

void DeviceUi::publish(const UiSnapshot& snapshot) {
  if (!ready()) return;
  UiSnapshot copy = snapshot;
  copy.status[sizeof(copy.status) - 1] = '\0';
  copy.volume = std::min<uint8_t>(100, copy.volume);
  xQueueOverwrite(snapshots_, &copy);
}

bool DeviceUi::pollCommand(UiCommand& command) {
  return ready() && xQueueReceive(commands_, &command, 0) == pdTRUE;
}

void DeviceUi::observeMic(uint32_t level) { retainPeak(micPeak_, level); }

void DeviceUi::observeSpeaker(uint32_t level) { retainPeak(speakerPeak_, level); }

void DeviceUi::taskEntry(void* context) {
  static_cast<DeviceUi*>(context)->run();
}

const char* DeviceUi::headLedState() const {
  switch (headLedMode_.load(std::memory_order_relaxed)) {
    case HeadLedMode::Off: return "off";
    case HeadLedMode::Green: return "green";
    case HeadLedMode::Orange: return "orange";
    case HeadLedMode::Pulse: return "red_pulse";
    default: return "unknown";
  }
}

bool DeviceUi::setHeadLedPower(bool on) {
  if (!on && !headLedOwnsBusPower_ && !headLedOwnsBoost_) return true;
  constexpr uint8_t address = 0x58, port0 = 0x02, port1 = 0x03;
  constexpr uint8_t busEnable = 1 << 1, usbOtg = 1 << 5, boostEnable = 1 << 7;
  auto& i2c = M5.In_I2C;
  uint8_t latch[2];
  if (!i2c.readRegister(address, port0, latch, sizeof(latch), kLedI2cHz)) return false;

  if (on) {
    if (!(latch[1] & boostEnable)) {
      headLedOwnsBoost_ = true;
      if (!i2c.writeRegister8(address, port1, latch[1] | boostEnable, kLedI2cHz)) return false;
    }
    if (latch[0] & busEnable) return true;
    // Precharge the CoreS3 BUS_OUT rail as M5GFX does at boot, so its
    // capacitive load does not pull down the supply when the switch closes.
    headLedOwnsBusPower_ = true;
    for (unsigned pulse = 0; pulse < 8; ++pulse) {
      if (!i2c.writeRegister8(address, port0, latch[0] | busEnable, kLedI2cHz)) return false;
      delayMicroseconds(pulse * 16);
      if (!i2c.writeRegister8(address, port0, latch[0], kLedI2cHz)) return false;
      delay(1);
    }
    if (!i2c.writeRegister8(address, port0, latch[0] | busEnable, kLedI2cHz)) return false;
    delay(10);  // Let the WS2812 chain settle before sending its first color.
    return true;
  }

  if (headLedOwnsBusPower_) {
    latch[0] &= ~busEnable;
    if (!i2c.bitOff(address, port0, busEnable, kLedI2cHz)) return false;
    headLedOwnsBusPower_ = false;
  }
  if (headLedOwnsBoost_ && !(latch[0] & (busEnable | usbOtg)) &&
      !i2c.writeRegister8(address, port1, latch[1] & ~boostEnable, kLedI2cHz)) return false;
  headLedOwnsBoost_ = false;
  return true;
}

void DeviceUi::requestHeadLed(HeadLedMode mode, uint32_t now) {
  if (!headLedsSupported_) return;
  const uint32_t duration = mode == HeadLedMode::Off ? 0 : heartAnimationDuration_;
  const HeadLedRequest request{mode, duration ? heartAnimationStartedAt_ : 0, duration};
  if (!headLedRequests_) {
    updateHeadLeds(request, now);
  } else if (request.mode != lastHeadLedRequest_.mode ||
             request.startedAt != lastHeadLedRequest_.startedAt || request.duration != lastHeadLedRequest_.duration) {
    lastHeadLedRequest_ = request;
    xQueueOverwrite(headLedRequests_, &request);
  }
}

void DeviceUi::runHeadLeds() {
  HeadLedRequest request{HeadLedMode::Off, 0, 0};
  for (;;) {
    updateHeadLeds(request, millis());
    const auto applied = headLedMode_.load(std::memory_order_relaxed);
    const uint32_t age = millis() - request.startedAt;
    TickType_t wait = applied == HeadLedMode::Unknown ? pdMS_TO_TICKS(250) : portMAX_DELAY;
    if (applied == HeadLedMode::Pulse || age < request.duration) {
      // Use the next point on the original clock. Display work and new
      // touch requests therefore cannot move the start of a beat.
      wait = std::max<TickType_t>(1, pdMS_TO_TICKS(kLedFrameMs - age % kLedFrameMs));
    }
    xQueueReceive(headLedRequests_, &request, wait);
  }
}

void DeviceUi::updateHeadLeds(const HeadLedRequest& request, uint32_t now) {
  HeadLedMode mode = request.mode;
  const bool active = mode != HeadLedMode::Off;
  uint16_t color = mode == HeadLedMode::Orange ? kLedOrange : active ? kLedGreen : kLedOff;
  const uint32_t age = now - request.startedAt;
  if (age < request.duration) {
    mode = HeadLedMode::Pulse;
    color = heartPulseColor(age / kLedFrameMs * kLedFrameMs);
  }
  const auto previous = headLedMode_.load(std::memory_order_relaxed);
  if ((previous == mode && headLedColor_ == color) ||
      (headLedRetryAt_ && static_cast<int32_t>(now - headLedRetryAt_) < 0)) return;

  const bool wasPowered = previous != HeadLedMode::Off && previous != HeadLedMode::Unknown;
  const bool powered = !active || wasPowered || setHeadLedPower(true);
  auto& bus = M5.In_I2C;
  if (powered && !headLedsConfigured_) {
    // The LED data output is on expander pin 13. Leave the servo registers alone.
    constexpr uint8_t pin = 1 << (13 - 8);
    headLedsConfigured_ = bus.bitOn(kLedExpander, 0x04, pin, kLedI2cHz) &&
                         bus.bitOff(kLedExpander, 0x0c, pin, kLedI2cHz) &&
                         bus.bitOn(kLedExpander, 0x0a, pin, kLedI2cHz) &&
                         bus.bitOff(kLedExpander, 0x14, pin, kLedI2cHz) &&
                         bus.writeRegister8(kLedExpander, kLedConfig, kLedCount, kLedI2cHz);
  }
  uint8_t pixels[kLedCount * 2];
  for (size_t i = 0; i < sizeof(pixels); i += 2) {
    pixels[i] = static_cast<uint8_t>(color);
    pixels[i + 1] = static_cast<uint8_t>(color >> 8);
  }
  const bool written = powered && headLedsConfigured_ &&
      bus.writeRegister(kLedExpander, kLedRam, pixels, sizeof(pixels), kLedI2cHz);
  const bool refreshed = written && bus.writeRegister8(kLedExpander, kLedConfig, kLedCount | kLedRefresh, kLedI2cHz);
  const bool powerReleased = active || setHeadLedPower(false);
  if (refreshed && powerReleased) {
    headLedColor_ = color;
    headLedMode_.store(mode, std::memory_order_relaxed);
    headLedRetryAt_ = 0;
  } else {
    headLedMode_.store(HeadLedMode::Unknown, std::memory_order_relaxed);
    headLedFailures_.fetch_add(1, std::memory_order_relaxed);
    headLedsConfigured_ = false;
    headLedRetryAt_ = now + 250;
  }
}

void DeviceUi::run() {
  while (!ready()) vTaskDelay(1);
  UiSnapshot current;
  UiSnapshot incoming;
  uint32_t lastRender = millis() - kIdleFrameMs;
  uint32_t idleSince = millis(), lastTopPoll = idleSince;
  bool previousTopSample = false, topTouch = false;
  uint8_t savedBrightness = 0;
  bool wakePending = false;
  topTouchReady_ = setupTopTouch();
  for (;;) {
    if (xQueueReceive(snapshots_, &incoming, 0) == pdTRUE) current = incoming;
    M5.update();
    const uint32_t now = millis();
    const bool busy = current.active || current.starting || current.closing || head::isAligning(current.alignment);
    const bool screenTouch = M5.Touch.getDetail().wasPressed();
    bool topPressed = false;
    if (topTouchReady() && now - lastTopPoll >= kTopTouchPollMs) {
      lastTopPoll = now;
      uint8_t levels = 0;
      if (M5.In_I2C.readRegister(kTopTouchAddress, kTopTouchOutput, &levels, 1, kTopTouchI2cHz)) {
        const bool touched = (levels & 0x3f) != 0;
        if (touched == previousTopSample && touched != topTouch) {
          topTouch = touched;
          topPressed = topTouch;
        }
        previousTopSample = touched;
      } else {
        previousTopSample = topTouch;
      }
    }
    if (busy || screenTouch || topTouch) idleSince = now;
    if (sleeping()) {
      requestHeadLed(HeadLedMode::Off, now);
      if (topPressed && !wakePending) topWakes_.fetch_add(1, std::memory_order_relaxed);
      wakePending = wakePending || screenTouch || topPressed;
      if (!busy && wakePending) {
        M5.Display.wakeup();
        sleeping_ = false;
        idleSince = now;
        const auto command = UiCommand::WakeSession;
        xQueueSend(commands_, &command, 0);
        firstPaint_ = true;
        render(current, now);
        M5.Display.setBrightness(savedBrightness);
        lastRender = millis();
      }
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    if (sleepRequested_.exchange(false) || (!busy && now - idleSince >= kStandbyTimeoutMs)) {
      heartAnimationDuration_ = 0;
      savedBrightness = M5.Display.getBrightness();
      M5.Display.setBrightness(0);
      requestHeadLed(HeadLedMode::Off, now);
      sleeping_ = true;
      M5.Display.sleep();
      wakePending = false;
      volumeOpen_ = false;
      controlsVisible_ = false;
      panelButtonMask_ = 0;
      continue;
    }
    const bool sessionActive = current.active && !current.closing;
    if (!sessionActive || now - heartAnimationStartedAt_ >= heartAnimationDuration_) heartAnimationDuration_ = 0;
    if (topPressed && sessionActive &&
        (!heartAnimationDuration_ || now - lastHeartTouchAt_ >= kHeartTouchCooldownMs)) {
      lastHeartTouchAt_ = now;
      heartBursts_.fetch_add(1, std::memory_order_relaxed);
      if (!heartAnimationDuration_) heartAnimationStartedAt_ = now;
      // Extend in whole beats so another stroke never jumps back to the start
      // of the pulse or cuts a beat short.
      const auto age = now - heartAnimationStartedAt_;
      const auto nextBeat = (age + kHeartPulsePeriodMs - 1) / kHeartPulsePeriodMs * kHeartPulsePeriodMs;
      heartAnimationDuration_ = nextBeat + kHeartPulseMinMs;
    }
    requestHeadLed(!sessionActive ? HeadLedMode::Off : current.muted ? HeadLedMode::Orange : HeadLedMode::Green, now);
    if (!firstPaint_) handleTouch(painted_);
    if (volumeOpen_ && now - volumeTouchedAt_ >= 5000) volumeOpen_ = false;
    if (now - lastRender >= (current.active ? kWaveFrameMs : kIdleFrameMs)) {
      current.speaking = current.active && current.volume &&
          static_cast<int32_t>(speakingUntil_.load() - now) > 0;
      render(current, now);
      lastRender = millis();
    }
    vTaskDelay(std::max<TickType_t>(1, pdMS_TO_TICKS(5)));
  }
}

void DeviceUi::handleTouch(const UiSnapshot& snapshot) {
  const auto touch = M5.Touch.getDetail();
  if (!touch.wasPressed()) return;
  const uint32_t now = millis();
  if (hasAcceptedTouch_ && now - lastTouchAt_ < kTouchDebounceMs) return;
  controlsTouchedAt_ = now;
  const bool aligning = head::isAligning(snapshot.alignment);
  if (!aligning && !controlsVisible()) {
    lastTouchAt_ = now;
    hasAcceptedTouch_ = true;
    return;
  }
  const int x = touch.x, y = touch.y;
  UiCommand command;
  if (aligning) {
    if (!head::canAdjust(snapshot.alignment)) return;
    if (kAlignLeft.contains(x, y)) command = UiCommand::AlignLeft;
    else if (kAlignRight.contains(x, y)) command = UiCommand::AlignRight;
    else if (kAlignSave.contains(x, y)) command = UiCommand::SaveAlignment;
    else if (kAlignCancel.contains(x, y)) command = UiCommand::CancelAlignment;
    else return;
  } else if (kVolume.contains(x, y)) {
    volumeOpen_ = !paintedVolumeOpen_;
    volumeTouchedAt_ = lastTouchAt_ = now;
    hasAcceptedTouch_ = true;
    return;
  } else if (paintedVolumeOpen_ && kMinus.contains(x, y) && snapshot.volume > 0) {
    command = UiCommand::VolumeDown;
    volumeTouchedAt_ = now;
  } else if (paintedVolumeOpen_ && kPlus.contains(x, y) && snapshot.volume < 100) {
    command = UiCommand::VolumeUp;
    volumeTouchedAt_ = now;
  } else if (kMic.contains(x, y) && canMute(snapshot)) {
    command = snapshot.muted ? UiCommand::Unmute : UiCommand::Mute;
  } else if (kSession.contains(x, y) && canSession(snapshot)) {
    command = showStop(snapshot) ? UiCommand::StopSession : UiCommand::StartSession;
    volumeOpen_ = false;
  } else {
    return;
  }
  if (uxQueueSpacesAvailable(commands_) == 0) return;
  lastTouchAt_ = now;
  hasAcceptedTouch_ = true;
  const auto immediate = immediateHandler_.load(std::memory_order_acquire);
  if (immediate) immediate(command, snapshot);
  xQueueSend(commands_, &command, 0);
}

void DeviceUi::render(const UiSnapshot& s, uint32_t now) {
  auto& c = *canvas_;
  if (head::isAligning(s.alignment)) {
    if (firstPaint_ || s.alignment != painted_.alignment || s.wifiConnected != painted_.wifiConnected ||
        s.batteryPercent != painted_.batteryPercent) {
      alignmentScreen(c, s);
      c.pushSprite(0, 0);
    }
    volumeOpen_ = false;
    controlsVisible_ = false;
    panelButtonMask_ = 0;
    paintedHeartAge_ = -1;
    waveMode_ = WaveMode::Idle;
    painted_ = s;
    firstPaint_ = false;
    return;
  }
  if (head::isAligning(painted_.alignment)) firstPaint_ = true;
  const bool first = firstPaint_;
  if (first || s.active != painted_.active) controlsTouchedAt_ = now;
  const bool visible = !s.active || now - controlsTouchedAt_ < kControlsTimeoutMs;
  const bool visibilityChanged = first || visible != controlsVisible();
  const bool statusChanged = std::strcmp(s.status, painted_.status) != 0;
  const bool header = first || s.active != painted_.active || s.wifiConnected != painted_.wifiConnected ||
      s.batteryPercent != painted_.batteryPercent ||
      (s.active && (s.muted != painted_.muted || s.mutePending != painted_.mutePending));
  const WaveMode mode = !s.active ? WaveMode::Idle : s.speaking ? WaveMode::Speaking :
      (s.muted || s.mutePending) ? WaveMode::Muted : WaveMode::Listening;
  const bool speaking = mode == WaveMode::Speaking;
  const bool modeChanged = mode != waveMode_;
  const bool volumeLayoutChanged = volumeOpen_ != paintedVolumeOpen_;
  const bool body = first || modeChanged || (s.active ? volumeLayoutChanged : statusChanged);
  const bool panel = body || volumeLayoutChanged ||
      (volumeOpen_ && s.volume != painted_.volume);
  const bool footer = visibilityChanged || volumeOpen_ != paintedVolumeOpen_ || s.volume != painted_.volume ||
      showStop(s) != showStop(painted_) || canSession(s) != canSession(painted_) ||
      canMute(s) != canMute(painted_) || (s.active && s.muted) != (painted_.active && painted_.muted);

  const uint32_t mic = micPeak_.exchange(0, std::memory_order_relaxed);
  const uint32_t speaker = speakerPeak_.exchange(0, std::memory_order_relaxed);
  if (modeChanged) {
    waveHistory_.fill(0);
    waveSampleAt_ = now;
    waveWindowPeak_ = 0;
    waveHistory_.front() = mode == WaveMode::Listening ? waveLevel(mic, false) : 0;
    speechLevel_ = 0;
    speechUpdatedAt_ = now;
  }
  if (mode == WaveMode::Listening && !modeChanged) {
    waveWindowPeak_ = std::max(waveWindowPeak_, mic);
    const auto steps = std::min<size_t>((now - waveSampleAt_) / kWaveSampleMs, waveHistory_.size());
    if (steps) {
      std::move_backward(waveHistory_.begin(), waveHistory_.end() - steps, waveHistory_.end());
      std::fill_n(waveHistory_.begin(), steps, 0);
      waveHistory_.front() = waveLevel(waveWindowPeak_, false);
      waveWindowPeak_ = 0;
      waveSampleAt_ = now - (now - waveSampleAt_) % kWaveSampleMs;
    }
  }
  int speechUnit = 0, speechPairs = 0;
  if (speaking) {
    const int fall = std::min<uint32_t>(500, now - speechUpdatedAt_) * 650 / 1000;
    speechLevel_ = std::max<int>(waveLevel(speaker, true), speechLevel_ - fall);
    speechUpdatedAt_ = now;
    constexpr int pairCount = sizeof(kSpeechPattern) / sizeof(kSpeechPattern[0]) - 1;
    speechPairs = speechLevel_ * (pairCount + 1) / 256;
    constexpr int minUnit = 6;
    const int maxUnit = volumeOpen_ ? 14 : 22;
    speechUnit = minUnit + (maxUnit - minUnit) * speechLevel_ / 255;
  }
  const int scale = volumeOpen_ ? 41 : 59;
  const int height = *std::max_element(waveHistory_.begin(), waveHistory_.end()) * scale / 255;
  const int previousHeight = listeningWaveHeight();
  const bool wave = s.active && !speaking && (body || height > 1 || previousHeight > 1);
  const bool speech = speaking &&
      (body || speechUnit != paintedSpeechUnit_ || speechPairs != paintedSpeechPairs_);
  const int heartAge = heartAnimationDuration_ ? static_cast<int>(now - heartAnimationStartedAt_) : -1;
  const bool heartsChanged = heartAge >= 0 || paintedHeartAge_ >= 0;
  if (!header && !body && !panel && !wave && !speech && !footer && !heartsChanged) return;

  const uint32_t started = millis();
  if (first) c.fillScreen(kBackground);
  if (body) c.fillRect(kBody.x, kBody.y, kBody.w, kBody.h, kBackground);
  if (header) statusLine(c, s);
  if (body && !s.active) idleStatus(c, s);
  Rect audioArea{};
  const int y = volumeOpen_ ? 92 : 116;
  if (speech) {
    const int halfWidth = kBarPitch * std::max(speechPairs, paintedSpeechPairs_) + 4;
    const int halfHeight = 3 * std::max(speechUnit, paintedSpeechUnit_) + 1;
    audioArea = {kWidth / 2 - halfWidth, y - halfHeight, 2 * halfWidth + 1, 2 * halfHeight + 1};
    if (!body) c.fillRect(audioArea.x, audioArea.y, audioArea.w, audioArea.h, kBackground);
    speechWave(c, y, speechUnit, speechPairs);
    speakingWaveFrames_.fetch_add(1, std::memory_order_relaxed);
  }
  if (wave) {
    const int radius = std::max(height, previousHeight) + 1;
    audioArea = {12, y - radius, 239, 2 * radius + 1};
    if (!body) c.fillRect(audioArea.x, audioArea.y, audioArea.w, audioArea.h, kBackground);
    const uint32_t baseline = mode == WaveMode::Muted ? 0x29344B : 0x235364;
    c.drawFastHLine(audioArea.x + 4, y, audioArea.w - 8, baseline);
    if (body) microphoneBadge(c, y, mode == WaveMode::Muted);
    if (mode == WaveMode::Listening) {
      const int travel = (now - waveSampleAt_) * kBarPitch / kWaveSampleMs;
      for (size_t index = 0; index < waveHistory_.size(); ++index) {
        const int x = 246 - static_cast<int>(index) * kBarPitch - travel;
        waveBar(c, x, y, waveHistory_[index] * scale / 255);
      }
      if (height > 1) listeningWaveFrames_.fetch_add(1, std::memory_order_relaxed);
    }
  }
  if (panel && (!s.active || volumeOpen_)) {
    c.fillRect(0, kVolumePanel.y, kWidth, kVolumePanel.h, kBackground);
    if (volumeOpen_) volumePanel(c, s.volume);
  }
  if (footer) {
    if (visible) toolbar(c, s, volumeOpen_);
    else c.fillRect(kFooter.x, kFooter.y, kFooter.w, kFooter.h, kBackground);
  }

  auto* pixels = static_cast<uint16_t*>(pixels_);
  auto* heartScratch = pixels + kWidth * kHeight + 2;
  Rect heartAreas[kHeartCount]{};
  Rect heartDirty[kHeartCount]{};
  bool heartVisible = false;
  if (heartsChanged) {
    for (size_t i = 0; i < kHeartCount; ++i) {
      const auto& heart = kHearts[i];
      const auto frame = heartFrame(heart, heartAge, heartAnimationDuration_, volumeOpen_);
      heartAreas[i] = frame.visible;
      if (!first && !body)
        heartDirty[i] = unite(frame.visible, heartFrame(heart, paintedHeartAge_,
                                                       paintedHeartDuration_, paintedVolumeOpen_).visible);
      if (frame.visible.h) {
        compositeHeart(pixels, heartScratch + i * kHeartScratchPixels, heart, frame);
        heartVisible = true;
      }
    }
  }

  if (first) c.pushSprite(0, 0);
  else {
    if (header) pushRegion(c, kHeader);
    if (body) pushRegion(c, kBody);
    else {
      if (wave || speech) pushRegion(c, audioArea);
      if (panel) pushRegion(c, kVolumePanel);
      for (const auto& dirty : heartDirty) {
        if (dirty.h && (!(wave || speech) || !audioArea.contains(dirty.x, dirty.y) ||
                        !audioArea.contains(dirty.x + dirty.w - 1, dirty.y + dirty.h - 1)))
          pushRegion(c, dirty);
      }
    }
    if (footer) {
      if (visible) pushRegion(c, kFooter);
      else M5.Display.fillRect(kFooter.x, kFooter.y, kFooter.w, kFooter.h, kBackground);
    }
  }
  if (heartVisible) {
    // Keep the underlying canvas clean for the next waveform update.
    for (size_t i = 0; i < kHeartCount; ++i) {
      if (heartAreas[i].h)
        restoreHeart(pixels, heartScratch + i * kHeartScratchPixels, heartAreas[i]);
    }
    heartFrames_.fetch_add(1, std::memory_order_relaxed);
  }
  paintedHeartAge_ = heartAge;
  paintedHeartDuration_ = heartAnimationDuration_;
  if (visibilityChanged) {
    // Read the LCD's own GRAM. Sample the solid fill of each button against
    // the gutter on the same row to accommodate both CoreS3 panel revisions.
    const int y = kFooter.y + 12;
    const auto background = M5.Display.readPixel(1, y);
    uint8_t mask = 0;
    if (M5.Display.readPixel(kMic.x + 12, y) != background) mask |= 1;
    if (M5.Display.readPixel(kVolume.x + 12, y) != background) mask |= 2;
    if (M5.Display.readPixel(kSession.x + 12, y) != background) mask |= 4;
    panelButtonMask_ = mask;
  }
  controlsVisible_ = visible;
  painted_ = s;
  paintedWaveHeight_.store(height, std::memory_order_relaxed);
  paintedSpeechUnit_ = speechUnit;
  paintedSpeechPairs_ = speechPairs;
  waveMode_ = mode;
  paintedVolumeOpen_ = volumeOpen_;
  firstPaint_ = false;
  const uint32_t elapsed = millis() - started;
  if (elapsed > maxRenderMs()) maxRenderMs_.store(elapsed, std::memory_order_relaxed);
}
