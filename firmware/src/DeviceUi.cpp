#include "DeviceUi.h"
#include "FaceTiles.h"

#include <Arduino.h>
#include <M5Unified.h>
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <esp_heap_caps.h>
#include <new>

extern "C" const unsigned char stackchan_face[] asm("_binary_art_face_rgb565_start");

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
constexpr uint32_t kFrameIntervalMs = 100, kTouchDebounceMs = 180;
constexpr uint32_t kControlsTimeoutMs = 10000;

using Canvas = m5gfx::M5Canvas;
struct Rect {
  int x, y, w, h;
  bool contains(int px, int py) const {
    return px >= x && py >= y && px < x+w && py < y+h;
  }
};
constexpr Rect kHeader{0, 0, 320, 40};
constexpr Rect kFooter{0, 193, 320, 47};
constexpr Rect kLowerFace{0, 145, 320, 48};
constexpr Rect kLeftEye{70, 69, 57, 70}, kRightEye{194, 75, 57, 70};
constexpr Rect kMouth{132, 146, 56, 34};
constexpr Rect kMic{6, 196, 98, 39}, kVolume{111, 196, 98, 39}, kSession{216, 196, 98, 39};
constexpr Rect kMinus{58, 148, 56, 42}, kPlus{206, 148, 56, 42};

void tile(Canvas& c, int x, int y, const face::Tile& t) {
  // Assets and the fixed 320-wide canvas use the same SPI-order RGB565 bytes.
  // The linker may place the embedded asset at an odd flash address.
  const auto* src = stackchan_face + t.offset;
  auto* dst = static_cast<uint8_t*>(c.getBuffer()) + 2 * (y * kWidth + x);
  const size_t rowBytes = t.w * 2;
  for (int row = 0; row < t.h; ++row) {
    std::memcpy(dst, src, rowBytes);
    src += rowBytes;
    dst += 2 * kWidth;
  }
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

void statusLine(Canvas& c, const UiSnapshot& s) {
  c.fillRect(0, 0, kWidth, kHeader.h, kBackground);
  const uint32_t dot = s.active && !s.muted && !s.mutePending ?
      (s.speaking ? kPeach : kMint) : kMuted;
  c.fillSmoothCircle(13, 17, 5, dot);
  c.setTextSize(1);
  c.setFont(&fonts::FreeSansBold12pt7b);
  c.setTextColor(kWhite, kBackground);
  c.setTextDatum(middle_left);
  constexpr int available = 263;
  if (c.textWidth(s.status) > available) c.setFont(&fonts::FreeSansBold9pt7b);
  if (c.textWidth(s.status) <= available) {
    c.drawString(s.status, 26, 17);
  } else {
    // Long errors keep both their text and the connection icon.
    c.setFont(&fonts::Font2);
    char line[sizeof(s.status)];
    std::snprintf(line, sizeof(line), "%s", s.status);
    char* split = std::strrchr(line, ' ');
    if (split) {
      *split = 0;
      c.drawString(line, 26, 9);
      c.drawString(split + 1, 26, 26);
    } else c.drawString(line, 26, 17);
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

void valueText(char (&text)[8], uint8_t volume) {
  std::snprintf(text, sizeof(text), "%u%%", static_cast<unsigned>(volume));
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
}  // namespace

bool DeviceUi::begin() {
  if (ready()) return true;
  // Never consume 154 kB of internal RAM as an implicit sprite fallback.
  if (!psramFound()) return false;
  pixels_ = heap_caps_calloc(1, kWidth * kHeight * sizeof(uint16_t) + 4,
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

void DeviceUi::taskEntry(void* context) {
  static_cast<DeviceUi*>(context)->run();
}

void DeviceUi::run() {
  UiSnapshot current;
  UiSnapshot incoming;
  uint32_t lastRender = millis() - kFrameIntervalMs;
  for (;;) {
    if (xQueueReceive(snapshots_, &incoming, 0) == pdTRUE) current = incoming;
    M5.update();
    if (!firstPaint_) handleTouch(painted_);
    const uint32_t now = millis();
    if (volumeOpen_ && now - volumeTouchedAt_ >= 5000) volumeOpen_ = false;
    if (now - lastRender >= kFrameIntervalMs) {
      const uint32_t phase = now % 4700;
      const uint8_t eye = phase < 4390 ? 0 : (phase >= 4490 && phase < 4600 ? 2 : 1);
      const uint8_t mouth = current.active && current.speaking ?
          std::min<unsigned>(5, (current.mouthLevel + 90) / 280) : 0;
      render(current, eye, mouth);
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
  if (!controlsVisible()) {
    lastTouchAt_ = now;
    hasAcceptedTouch_ = true;
    return;
  }
  const int x = touch.x, y = touch.y;
  UiCommand command;
  if (kVolume.contains(x, y)) {
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

void DeviceUi::render(const UiSnapshot& s, uint8_t eye, uint8_t mouth) {
  auto& c = *canvas_;
  const bool first = firstPaint_;
  const uint32_t now = millis();
  if (first || s.active != painted_.active) controlsTouchedAt_ = now;
  const bool visible = !s.active || now - controlsTouchedAt_ < kControlsTimeoutMs;
  const bool visibilityChanged = first || visible != controlsVisible();
  const bool header = first || s.active != painted_.active || s.speaking != painted_.speaking ||
      s.muted != painted_.muted || s.mutePending != painted_.mutePending ||
      s.wifiConnected != painted_.wifiConnected || std::strcmp(s.status, painted_.status) != 0;
  const bool eyes = first || eye != paintedEye_;
  const bool panel = first || volumeOpen_ != paintedVolumeOpen_ ||
      (volumeOpen_ && s.volume != painted_.volume);
  const bool lips = !volumeOpen_ && (panel || mouth != paintedMouth_);
  const bool footer = visibilityChanged || volumeOpen_ != paintedVolumeOpen_ || s.volume != painted_.volume ||
      showStop(s) != showStop(painted_) || canSession(s) != canSession(painted_) ||
      canMute(s) != canMute(painted_) || (s.active && s.muted) != (painted_.active && painted_.muted);
  if (!header && !eyes && !panel && !lips && !footer) return;

  const uint32_t started = millis();
  if (first) {
    c.fillScreen(kBackground);
    tile(c, 59, 39, face::kLeftBrow);
    tile(c, 207, 51, face::kRightBrow);
  }
  if (header) statusLine(c, s);
  if (eyes) {
    tile(c, kLeftEye.x, kLeftEye.y, face::eyes[eye]);
    tile(c, kRightEye.x, kRightEye.y, face::eyes[eye]);
  }
  if (panel) {
    c.fillRect(0, kLowerFace.y, kWidth, kLowerFace.h, kBackground);
    if (volumeOpen_) volumePanel(c, s.volume);
    else {
      tile(c, 61, 145, face::kCheek);
      tile(c, 223, 145, face::kCheek);
    }
  }
  if (lips) tile(c, kMouth.x, kMouth.y, face::mouths[mouth]);
  if (footer) {
    if (visible) toolbar(c, s, volumeOpen_);
    else c.fillRect(kFooter.x, kFooter.y, kFooter.w, kFooter.h, kBackground);
  }

  if (first) c.pushSprite(0, 0);
  else {
    if (header) pushRegion(c, kHeader);
    if (eyes) { pushRegion(c, kLeftEye); pushRegion(c, kRightEye); }
    if (panel) pushRegion(c, kLowerFace);
    else if (lips) pushRegion(c, kMouth);
    if (footer) {
      if (visible) pushRegion(c, kFooter);
      else M5.Display.fillRect(kFooter.x, kFooter.y, kFooter.w, kFooter.h, kBackground);
    }
  }
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
  paintedEye_ = eye;
  paintedMouth_ = mouth;
  paintedVolumeOpen_ = volumeOpen_;
  firstPaint_ = false;
  const uint32_t elapsed = millis() - started;
  if (elapsed > maxRenderMs()) maxRenderMs_.store(elapsed, std::memory_order_relaxed);
}
