#pragma once

#include "HeadMotion.h"
#include <array>
#include <atomic>
#include <cstdint>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

enum class UiCommand : uint8_t {
  StartSession,
  WakeSession,
  StopSession,
  Mute,
  Unmute,
  VolumeDown,
  VolumeUp,
  AlignLeft,
  AlignRight,
  SaveAlignment,
  CancelAlignment,
};

struct UiSnapshot {
  bool active = false;
  bool muted = false;
  bool mutePending = false;
  bool starting = false;
  bool closing = false;
  bool voiceWaiting = false;
  bool wakeWordWaiting = false;
  bool voiceCostVisible = false;
  uint32_t voiceCostCents = 0;
  bool voiceTotalCostVisible = false;
  uint32_t voiceTotalCostCents = 0;
  bool toolTotalCostVisible = false;
  uint32_t toolTotalCostCents = 0;
  bool connected = false;
  bool wifiConnected = false;
  int8_t batteryPercent = -1;
  uint8_t volume = 90;
  bool speaking = false;
  head::Alignment alignment = head::Alignment::Off;
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
  bool sleeping() const { return sleeping_.load(); }
  bool headLedsIdle() const {
    return !headLedsSupported_ || headLedMode_.load(std::memory_order_relaxed) == HeadLedMode::Off;
  }
  void requestSleep() { sleepRequested_ = true; }
  bool topTouchReady() const { return topTouchReady_.load(); }
  uint32_t topWakes() const { return topWakes_.load(std::memory_order_relaxed); }
  uint32_t heartBursts() const { return heartBursts_.load(std::memory_order_relaxed); }
  uint32_t heartFrames() const { return heartFrames_.load(std::memory_order_relaxed); }
  const char* headLedState() const;
  uint32_t headLedFailures() const { return headLedFailures_.load(std::memory_order_relaxed); }
  uint8_t panelButtonMask() const { return panelButtonMask_.load(); }
  int wakeBannerPosition() const { return wakeBannerPosition_.load(std::memory_order_relaxed); }
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
  bool setHeadLedPower(bool on);
  void render(const UiSnapshot& snapshot, uint32_t now);
  void releaseBeforeTask();

  enum class WaveMode : uint8_t { Idle, Listening, Speaking, Muted };
  enum class HeadLedMode : uint8_t { Unknown, Off, Green, Orange, DimOrange, RedPulse, GreenPulse };
  struct HeadLedRequest {
    HeadLedMode mode;
    uint32_t startedAt, duration;
  };
  void requestHeadLed(HeadLedMode mode, uint32_t now);
  void updateHeadLeds(const HeadLedRequest& request, uint32_t now);
  void runHeadLeds();
  const std::atomic<uint32_t>& speakingUntil_;
  std::atomic<uint32_t> micPeak_{0}, speakerPeak_{0};
  std::atomic<uint32_t> listeningWaveFrames_{0}, speakingWaveFrames_{0};
  std::array<uint8_t, 23> waveHistory_{};
  WaveMode waveMode_ = WaveMode::Idle;
  uint32_t waveSampleAt_ = 0;
  uint32_t waveWindowPeak_ = 0;
  std::atomic<int> paintedWaveHeight_{0};
  std::atomic<int> wakeBannerPosition_{-1};
  uint32_t wakeBannerStartedAt_ = 0;
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
  std::atomic<bool> sleeping_{false}, sleepRequested_{false}, topTouchReady_{false};
  std::atomic<uint32_t> topWakes_{0};
  std::atomic<uint32_t> heartBursts_{0}, heartFrames_{0};
  QueueHandle_t headLedRequests_ = nullptr;
  bool headLedsSupported_ = false;
  HeadLedRequest lastHeadLedRequest_{HeadLedMode::Off, 0, 0};
  std::atomic<HeadLedMode> headLedMode_{HeadLedMode::Unknown};
  std::atomic<uint32_t> headLedFailures_{0};
  uint16_t headLedColor_ = 0;
  uint8_t headLedBrighterPixels_ = 0;
  uint32_t headLedRetryAt_ = 0;
  bool headLedsConfigured_ = false;
  bool headLedOwnsBusPower_ = false, headLedOwnsBoost_ = false;
  uint32_t heartAnimationStartedAt_ = 0, heartAnimationDuration_ = 0, lastHeartTouchAt_ = 0;
  int paintedHeartAge_ = -1;
  uint32_t paintedHeartDuration_ = 0;
  std::atomic<bool> controlsVisible_{true};
  std::atomic<uint8_t> panelButtonMask_{0};
  uint32_t controlsTouchedAt_ = 0;
  bool volumeOpen_ = false;
  bool paintedVolumeOpen_ = false;
  uint32_t volumeTouchedAt_ = 0;
  uint32_t lastTouchAt_ = 0;
  bool hasAcceptedTouch_ = false;
};
