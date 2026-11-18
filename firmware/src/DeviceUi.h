#pragma once

#include <array>
#include <atomic>
#include <cstdint>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

enum class UiCommand : uint8_t {
  StartSession,
  StopSession,
  Mute,
  Unmute,
  VolumeDown,
  VolumeUp,
};

struct UiSnapshot {
  bool active = false;
  bool muted = false;
  bool mutePending = false;
  bool starting = false;
  bool closing = false;
  bool connected = false;
  bool wifiConnected = false;
  uint8_t volume = 90;
  bool speaking = false;
  char status[80]{};
};

namespace m5gfx { class M5Canvas; }

// Lifetime: keep this object alive for the lifetime of the firmware.
// After begin(), only its UI task may call M5.update(), M5.Touch, or Display.
// Main/network code communicates by snapshots and commands, never UI globals.
class DeviceUi {
 public:
  using ImmediateHandler = void (*)(UiCommand, const UiSnapshot&);

  explicit DeviceUi(const std::atomic<uint32_t>& speakingUntil) : speakingUntil_(speakingUntil) {}
  DeviceUi(const DeviceUi&) = delete;
  DeviceUi& operator=(const DeviceUi&) = delete;

  bool begin();
  void publish(const UiSnapshot& snapshot);
  bool pollCommand(UiCommand& command);
  void observeMic(uint32_t level);
  void observeSpeaker(uint32_t level);
  uint32_t listeningWaveFrames() const { return listeningWaveFrames_.load(std::memory_order_relaxed); }
  int listeningWaveHeight() const { return paintedWaveHeight_.load(std::memory_order_relaxed); }
  uint32_t speakingWaveFrames() const { return speakingWaveFrames_.load(std::memory_order_relaxed); }
  bool controlsVisible() const { return controlsVisible_.load(); }
  uint8_t panelButtonMask() const { return panelButtonMask_.load(); }
  bool ready() const { return ready_.load(std::memory_order_acquire); }
  uint32_t maxRenderMs() const {
    return maxRenderMs_.load(std::memory_order_relaxed);
  }
  // Handler runs on the UI task before enqueue. Keep it nonblocking and use
  // only atomics for immediate local stop/mute; no network/state-machine work.
  void setImmediateHandler(ImmediateHandler handler) {
    immediateHandler_.store(handler, std::memory_order_release);
  }

 private:
  static void taskEntry(void* context);
  void run();
  void handleTouch(const UiSnapshot& snapshot);
  void render(const UiSnapshot& snapshot, uint32_t now);
  void releaseBeforeTask();

  enum class WaveMode : uint8_t { Idle, Listening, Speaking, Muted };
  const std::atomic<uint32_t>& speakingUntil_;
  std::atomic<uint32_t> micPeak_{0}, speakerPeak_{0};
  std::atomic<uint32_t> listeningWaveFrames_{0}, speakingWaveFrames_{0};
  std::array<uint8_t, 23> waveHistory_{};
  WaveMode waveMode_ = WaveMode::Idle;
  uint32_t waveSampleAt_ = 0;
  uint32_t waveWindowPeak_ = 0;
  std::atomic<int> paintedWaveHeight_{0};
  int speechLevel_ = 0;
  uint32_t speechUpdatedAt_ = 0;
  int paintedSpeechUnit_ = 0, paintedSpeechPairs_ = 0;

  m5gfx::M5Canvas* canvas_ = nullptr;
  void* pixels_ = nullptr;
  QueueHandle_t snapshots_ = nullptr;
  QueueHandle_t commands_ = nullptr;
  TaskHandle_t task_ = nullptr;
  std::atomic<bool> ready_{false};
  std::atomic<uint32_t> maxRenderMs_{0};
  std::atomic<ImmediateHandler> immediateHandler_{nullptr};
  // Render history and volume panel belong exclusively to the display task.
  UiSnapshot painted_{};
  bool firstPaint_ = true;
  std::atomic<bool> controlsVisible_{true};
  std::atomic<uint8_t> panelButtonMask_{0};
  uint32_t controlsTouchedAt_ = 0;
  bool volumeOpen_ = false;
  bool paintedVolumeOpen_ = false;
  uint32_t volumeTouchedAt_ = 0;
  uint32_t lastTouchAt_ = 0;
  bool hasAcceptedTouch_ = false;
};
