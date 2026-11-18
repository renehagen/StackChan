#include <Arduino.h>
#include <ArduinoJson.h>
#include <M5Unified.h>
#include <Preferences.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <atomic>
#include <ctime>
#include "AudioDuplex.h"
#include "AudioFrame.h"
#include "DeviceUi.h"
#include "HeadMotion.h"
#include "VoiceTest.h"
#include <esp_heap_caps.h>
#include <lwip/sockets.h>

namespace {
struct Config {
  String ssid, password, host, token, ca;
  uint16_t port = 8765;
} cfg;
Preferences preferences;
AudioDuplex audio;
class AudioWebSocket : public WebSocketsClient {
 public:
  void abortTransport() { if (_client.tcp) _client.tcp->stop(); }
  bool hasPendingInput() { return _client.tcp && _client.tcp->available() > 0; }
  bool writeWouldBlock() const {
    const int fd = _client.ssl ? _client.ssl->fd() : -1;
    if (fd < 0) return false; // Let the WebSocket report a closed connection.
    fd_set writable;
    FD_ZERO(&writable);
    FD_SET(fd, &writable);
    timeval poll{};
    return select(fd + 1, nullptr, &writable, nullptr, &poll) == 0;
  }
};
AudioWebSocket audioSocket;
DeviceUi ui;
QueueHandle_t captureQueue, playbackQueue;
constexpr unsigned kCaptureFrames = 240, kPlaybackFrames = 200;
StaticQueue_t captureControl{}, playbackControl{};
bool pairedPcm = false;
std::atomic<unsigned> prebufferFrames{20};
std::atomic<uint32_t> voicedGapMs{0};
bool testStallUsed = false;
uint32_t testResumeAt = 0;
uint32_t testTxResumeAt = 0;
bool testTxStallUsed = false;
std::atomic<bool> active{false}, locallyMuted{false};
std::atomic<bool> localInputHold{false}, localOutputHold{false};
enum AudioFault : uint32_t { RxStall = 1, TxFailure = 2 };
std::atomic<uint32_t> audioFaults{0};
std::atomic<uint32_t> echoUntil{0};
std::atomic<uint32_t> rxSamples{0}, txSamples{0}, audioFailures{0};
std::atomic<uint32_t> captureOverflows{0}, playbackOverflows{0}, playbackUnderruns{0}, readTimeouts{0};
std::atomic<uint32_t> captureQueueHigh{0}, playbackQueueHigh{0};
std::atomic<uint16_t> mouthLevel{0};
std::atomic<uint8_t> volume{90};
uint32_t netInputFrames = 0, netOutputFrames = 0, loopMaxMs = 0, sendMaxMs = 0;
uint32_t sendStalls = 0, recvServiceMaxMs = 0;
uint32_t txWaitSince = 0, txWaitMaxMs = 0, txWaitEvents = 0, failedSendMs = 0;
uint32_t volumeChangedAt = 0;
bool volumeDirty = false;
String lastStop = "none", lastBridgeError;
bool configured = false, audioReady = false, socketStarted = false;
bool connected = false, starting = false, closing = false, mutePending = false;
bool requestedMuteState = false;
bool protocolFault = false;
uint32_t operationSince = 0, muteSince = 0, wifiSince = 0, clockSince = 0;
String authHeader, serialLine, status = "Instellen via USB";
constexpr uint32_t kBackground = 0x0D1825;
constexpr uint32_t kOperationTimeout = 20000;

void updateHigh(std::atomic<uint32_t>& value, uint32_t sample) {
  uint32_t previous = value.load();
  while (sample > previous && !value.compare_exchange_weak(previous, sample)) {}
}

bool safeString(const String& s) {
  for (size_t i = 0; i < s.length(); ++i) {
    if (static_cast<uint8_t>(s[i]) < 32 || s[i] == 127) return false;
  }
  return true;
}

bool parseConfig(const String& source, Config& result) {
  JsonDocument doc;
  if (deserializeJson(doc, source)) return false;
  result.ssid = doc["wifi_ssid"].as<String>();
  result.password = doc["wifi_password"].as<String>();
  result.host = doc["bridge_host"].as<String>();
  result.token = doc["device_token"].as<String>();
  result.ca = doc["ca_pem"].as<String>();
  const int port = doc["bridge_port"] | 8765;
  if (result.ssid.isEmpty() || result.ssid.length() > 32 ||
      result.password.length() > 63 || result.host.isEmpty() ||
      result.host.length() > 253 || !safeString(result.host) ||
      result.host.indexOf('/') >= 0 || result.host.indexOf(':') >= 0 ||
      result.token.length() < 32 || result.token.length() > 256 ||
      !safeString(result.token) || port < 1 || port > 65535 ||
      result.ca.length() > 4096 ||
      !result.ca.startsWith("-----BEGIN CERTIFICATE-----") ||
      result.ca.indexOf("-----END CERTIFICATE-----") < 0) return false;
  result.port = static_cast<uint16_t>(port);
  return true;
}

// Only the audio tasks own the I2S API. The network/UI task uses bounded queues.
void captureTask(void*) {
  uint32_t lastSample = millis();
  for (;;) {
    AudioFrame frame;
    while (frame.count < kAudioSamples) {
      const size_t n = audio.read(frame.samples + frame.count,
                                 kAudioSamples - frame.count, 50);
      if (n == 0) {
        readTimeouts.fetch_add(1);
        if (millis() - lastSample > 500) audioFaults.fetch_or(RxStall);
        vTaskDelay(pdMS_TO_TICKS(2));
        continue;
      }
      lastSample = millis();
      rxSamples.fetch_add(n);
      frame.count += n;
    }
    if (!active.load()) continue;
    frame.capturedMs = millis();
    const bool echoGuard = static_cast<int32_t>(echoUntil.load() - millis()) > 0;
    if (voiceTest::enabled()) voiceTest::measureMic(frame, echoGuard);
    // Continuous, paced silence preserves the Live audio clock while muted.
    if (locallyMuted.load() || localInputHold.load() || echoGuard) memset(frame.samples, 0, sizeof(frame.samples));
    if (xQueueSend(captureQueue, &frame, 0) != pdTRUE) {
      AudioFrame stale;
      xQueueReceive(captureQueue, &stale, 0);
      xQueueSend(captureQueue, &frame, 0);
      captureOverflows.fetch_add(1);
    }
    updateHigh(captureQueueHigh, uxQueueMessagesWaiting(captureQueue));
  }
}

void playbackTask(void*) {
  AudioFrame frame;
  bool playing = false;
  uint32_t firstQueuedAt = 0;
  uint32_t lastVoiced = 0, gapSince = 0;
  unsigned resumedFrames = 0;
  for (;;) {
    const uint32_t queued = uxQueueMessagesWaiting(playbackQueue);
    updateHigh(playbackQueueHigh, queued);
    if (!active.load() || localOutputHold.load()) {
      playing = false;
      firstQueuedAt = lastVoiced = gapSince = resumedFrames = 0;
    }
    if (active.load() && !localOutputHold.load() && !playing && queued) {
      if (!firstQueuedAt) firstQueuedAt = millis();
      const unsigned target = prebufferFrames.load();
      if (queued >= target || millis() - firstQueuedAt >= target*20 + 60) playing = true;
    }
    const bool gotFrame = playing && xQueueReceive(playbackQueue, &frame, 0) == pdTRUE;
    if (!gotFrame || !active.load() || localOutputHold.load()) {
      if (playing && active.load()) {
        playbackUnderruns.fetch_add(1);
        if (lastVoiced && millis()-lastVoiced < 80) { gapSince = millis(); resumedFrames = 0; }
      }
      playing = false;
      if (!queued) firstQueuedAt = 0;
      memset(frame.samples, 0, sizeof(frame.samples));
      frame.count = kAudioSamples;
    }
    uint32_t magnitude = 0;
    for (size_t i = 0; i < frame.count; ++i) magnitude += abs(static_cast<int32_t>(frame.samples[i]));
    const uint16_t level = frame.count ? magnitude / frame.count : 0;
    if (gotFrame && gapSince) {
      if (level > 180) {
        voicedGapMs += millis()-gapSince;
        gapSince = 0;
      } else if (++resumedFrames >= 3) gapSince = 0;
    }
    if (level > 180) lastVoiced = millis();
    mouthLevel = volume.load() ? level : 0;
    if (level > 180 && volume.load()) echoUntil = millis() + 260;
    // The fixture is acoustic input. Let the real mic hear it. Ordinary
    // assistant playback still receives the existing echo guard above.
    if (active.load() && !localOutputHold.load() && voiceTest::enabled()) {
      if (level > 180) voiceTest::markVoicedReply();
      else voiceTest::playPrompt(frame);
    }
    if (!audio.write(frame.samples, frame.count, 60)) {
      audioFailures.fetch_add(1);
      audioFaults.fetch_or(TxFailure);
      vTaskDelay(pdMS_TO_TICKS(5));
    } else txSamples.fetch_add(frame.count);
  }
}

void clearAudio() {
  active = false;
  mouthLevel = 0;
  if (captureQueue) xQueueReset(captureQueue);
  if (playbackQueue) xQueueReset(playbackQueue);
}

void sendControl(const char* type) {
  JsonDocument doc;
  doc["type"] = type;
  String json;
  serializeJson(doc, json);
  audioSocket.sendTXT(json);
}

void stopConversation(const char* message, const char* reason = "user_stop") {
  voiceTest::stop();
  const bool wasSession = active.load() || starting;
  clearAudio();
  starting = false;
  mutePending = false;
  status = message;
  if (wasSession) lastStop = reason;
  if (connected && wasSession && !closing) {
    closing = true;
    operationSince = millis();
    JsonDocument doc;
    doc["type"] = "stop";
    doc["reason"] = reason;
    String payload;
    serializeJson(doc, payload);
    audioSocket.sendTXT(payload);
  }
}

void onSocket(WStype_t event, uint8_t* data, size_t length) {
  switch (event) {
    case WStype_CONNECTED:
      pairedPcm = false;
      connected = true;
      starting = closing = false;
      protocolFault = false;
      status = (lastStop == "audio_send_failed" || lastStop == "bridge_error" || lastStop == "wifi_lost")
          ? "Verbinding hersteld - tik Start" : "Tik op Start";
      break;
    case WStype_DISCONNECTED:
      voiceTest::stop();
      if (active.load() || starting) lastStop = WiFi.status() == WL_CONNECTED ? "bridge_error" : "wifi_lost";
      connected = false;
      starting = closing = mutePending = false;
      clearAudio();
      status = "Bridge niet verbonden";
      break;
    case WStype_BIN: {
      if (!active.load()) break;
      const size_t blockBytes = sizeof(AudioFrame::samples);
      const bool silence = pairedPcm && length == 1 && (data[0] == 1 || data[0] == 2);
      if (silence) length = data[0]*blockBytes;
      if (length == 0 || length > blockBytes*(pairedPcm ? 2 : 1) || length % 2) {
        protocolFault = true;
        break;
      }
      if (localOutputHold.load()) break;
      for (size_t offset = 0; offset < length; offset += blockBytes) {
        AudioFrame frame;
        frame.count = std::min(blockBytes, length-offset) / 2;
        frame.capturedMs = millis();
        if (!silence) memcpy(frame.samples, data+offset, frame.count*2);
        ++netOutputFrames;
        if (xQueueSend(playbackQueue, &frame, 0) != pdTRUE) {
          AudioFrame stale;
          xQueueReceive(playbackQueue, &stale, 0);
          xQueueSend(playbackQueue, &frame, 0);
          playbackOverflows.fetch_add(1);
        }
      }
      break;
    }
    case WStype_TEXT: {
      if (length > 2048) { protocolFault = true; break; }
      JsonDocument doc;
      if (deserializeJson(doc, data, length)) { protocolFault = true; break; }
      const String type = doc["type"] | "";
      if (type == "connected") {
        pairedPcm = doc["audio"].as<String>() == "pcm-pair-v1";
      } else if (type == "ready") {
        if (closing) break; // stop may cross an already queued startup ACK
        if (!starting || (doc["rate"] | 0) != 16000) {
          protocolFault = true;
          break;
        }
        clearAudio();
        locallyMuted = false;
        localInputHold = localOutputHold = false;
        echoUntil = 0;
        starting = false;
        prebufferFrames = WiFi.RSSI() <= -67 ? 105 : 90;
        active = true;
        loopMaxMs = sendMaxMs = recvServiceMaxMs = sendStalls = 0;
        txWaitSince = txWaitMaxMs = txWaitEvents = failedSendMs = 0;
        status = "Ik luister";
      } else if (type == "closed") {
        voiceTest::stop();
        clearAudio();
        starting = closing = mutePending = false;
        const String reason = doc["reason"] | "stopped";
        if (lastStop == "none") lastStop = reason.substring(0, 64);
        if (status == "Gesprek stoppen..." || lastStop == "user_stop") status = "Gesprek gestopt";
        else if (reason == "time_limit") status = "10 minuten - start opnieuw";
        else if (status == "Ik luister") status = "Verbinding gestopt";
      } else if (type == "error") {
        const String code = doc["code"] | "unknown";
        lastBridgeError = safeString(code) ? code.substring(0, 64) : "invalid_error";
        stopConversation("Verbindingsfout - probeer opnieuw", "bridge_error");
      } else if (type == "muted") {
        const bool muted = doc["muted"] | true;
        if (!active.load() || !mutePending || muted != requestedMuteState) break;
        locallyMuted = muted;
        localInputHold = muted;
        mutePending = false;
        xQueueReset(captureQueue);
        status = muted ? "Microfoon gedempt" : "Ik luister";
      }
      break;
    }
    // The bridge emits complete, small frames; reject unexpected fragmentation.
    case WStype_FRAGMENT_BIN_START:
    case WStype_FRAGMENT_TEXT_START:
    case WStype_FRAGMENT:
    case WStype_FRAGMENT_FIN:
      protocolFault = true;
      break;
    default: break;
  }
}

void immediateUiCommand(UiCommand command, const UiSnapshot& snapshot) {
  // These atomics affect the audio workers immediately even if TLS is blocked.
  if (command == UiCommand::StopSession) {
    localInputHold = true;
    localOutputHold = true;
  } else if (command == UiCommand::Mute) {
    localInputHold = true;
  }
}

bool startConversation() {
  if (active.load() || starting || closing || !audioReady || !connected || protocolFault) return false;
  clearAudio();
  lastStop = "none";
  lastBridgeError = "";
  starting = true;
  operationSince = millis();
  status = "Verbinding met OpenAI...";
  sendControl("start");
  return true;
}

void handleUiCommands() {
  UiCommand command;
  while (ui.pollCommand(command)) {
    if (command == UiCommand::VolumeDown || command == UiCommand::VolumeUp) {
      int value = volume.load() + (command == UiCommand::VolumeUp ? 10 : -10);
      value = std::max(0, std::min(100, value));
      volume = value;
      audio.setVolume(value);
      volumeChangedAt = millis();
      volumeDirty = true;
      continue;
    }
    if (command == UiCommand::StopSession) {
      if (!closing && (active.load() || starting)) {
        stopConversation("Gesprek stoppen...", "user_stop");
      }
    } else if (command == UiCommand::StartSession) {
      startConversation();
    } else if ((command == UiCommand::Mute || command == UiCommand::Unmute) && active.load() && !mutePending) {
      const bool requestedMute = command == UiCommand::Mute;
      if (locallyMuted.load() == requestedMute) continue;
      if (requestedMute) locallyMuted = true;
      requestedMuteState = requestedMute;
      mutePending = true;
      muteSince = millis();
      xQueueReset(captureQueue);
      JsonDocument doc;
      doc["type"] = "mute";
      doc["muted"] = requestedMute;
      String json;
      serializeJson(doc, json);
      audioSocket.sendTXT(json);
      status = requestedMute ? "Microfoon gedempt" : "Microfoon inschakelen...";
    }
  }
  // Avoid flash writes while the audio stream is active.
  if (volumeDirty && !active.load() && !starting && !closing && millis() - volumeChangedAt > 2000) {
    preferences.begin("stackchan", false);
    preferences.putUChar("volume", volume.load());
    preferences.end();
    volumeDirty = false;
  }
}

void publishUi() {
  static uint32_t last = 0;
  if (millis() - last < 40) return;
  last = millis();
  UiSnapshot snapshot{};
  snapshot.active = active.load();
  snapshot.muted = locallyMuted.load();
  snapshot.mutePending = mutePending;
  snapshot.starting = starting;
  snapshot.closing = closing;
  snapshot.connected = connected;
  snapshot.wifiConnected = WiFi.status() == WL_CONNECTED;
  snapshot.volume = volume.load();
  snapshot.mouthLevel = mouthLevel.load();
  snapshot.speaking = static_cast<int32_t>(echoUntil.load() - millis()) > 0;
  String visible = status;
  if (snapshot.active && !snapshot.muted && !mutePending)
    visible = snapshot.speaking ? "Ik praat" : "Ik luister";
  visible.toCharArray(snapshot.status, sizeof(snapshot.status));
  ui.publish(snapshot);
}

void addStats(JsonObject stats) {
  stats["uptime_ms"] = millis();
  stats["active"] = active.load();
  stats["rx_samples"] = rxSamples.load();
  stats["tx_samples"] = txSamples.load();
  stats["net_input_frames"] = netInputFrames;
  stats["net_output_frames"] = netOutputFrames;
  stats["capture_overflows"] = captureOverflows.load();
  stats["playback_overflows"] = playbackOverflows.load();
  stats["playback_underruns"] = playbackUnderruns.load();
  stats["audio_write_errors"] = audioFailures.load();
  stats["read_timeouts"] = readTimeouts.load();
  stats["capture_queue_high"] = captureQueueHigh.load();
  stats["playback_queue_high"] = playbackQueueHigh.load();
  stats["loop_max_ms"] = loopMaxMs;
  stats["render_max_ms"] = ui.maxRenderMs();
  stats["controls_visible"] = ui.controlsVisible();
  stats["panel_button_mask"] = ui.panelButtonMask();
  stats["send_max_ms"] = sendMaxMs;
  stats["send_stalls"] = sendStalls;
  stats["tx_wait_events"] = txWaitEvents;
  stats["tx_wait_max_ms"] = txWaitMaxMs;
  stats["failed_send_ms"] = failedSendMs;
  stats["voiced_gap_ms"] = voicedGapMs.load();
  stats["recv_service_max_ms"] = recvServiceMaxMs;
  stats["wifi_rssi"] = WiFi.isConnected() ? WiFi.RSSI() : -127;
  stats["free_heap"] = ESP.getFreeHeap();
  stats["psram_bytes"] = ESP.getPsramSize();
  stats["volume"] = volume.load();
  // Bridge accepts only its fixed reason enum. Arbitrary remote codes stay USB-local.
  const char* known[] = {"none","stopped","user_stop","capture_overflow","playback_overflow","audio_io","wifi_lost","protocol_error","bridge_error","operation_timeout","mute_timeout","audio_send_failed"};
  for (const char* code : known) if (lastStop == code) { stats["last_stop"] = code; break; }
}

void sendDiagnostics() {
  static uint32_t last = 0;
  if (!connected || millis() - last < 5000) return;
  last = millis();
  JsonDocument doc;
  doc["type"] = "diagnostics";
  addStats(doc["stats"].to<JsonObject>());
  String payload;
  serializeJson(doc, payload);
  audioSocket.sendTXT(payload);
}

void handleSerial() {
  static bool discarding = false;
  for (int n = 0; n < 256 && Serial.available(); ++n) {
    const char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (discarding) {
        discarding = false;
        Serial.println("CONFIG_ERROR length");
      } else if (serialLine == "STATUS") {
        Serial.println(active.load() ? "STATUS active" : "STATUS idle");
      } else if (serialLine.startsWith("TEST.")) {
        const bool idle = !active.load() && !starting && !closing;
        bool ok = false;
        if (serialLine == "TEST.RESET" && idle) ok = voiceTest::reset();
        else if (serialLine.startsWith("TEST.DATA ") && idle)
          ok = voiceTest::append(serialLine.c_str()+10);
        else if (serialLine == "TEST.START" && idle && voiceTest::arm()) {
          testStallUsed = false;
          testTxStallUsed = false;
          ok = startConversation();
          if (!ok) voiceTest::stop();
        } else if (serialLine == "TEST.SAY" && voiceTest::enabled() && active.load() &&
                   !localOutputHold.load() && !locallyMuted.load() && volume.load() &&
                   static_cast<int32_t>(echoUntil.load()-millis()) <= 0)
          ok = voiceTest::say();
        else if ((serialLine == "TEST.JITTER" || serialLine == "TEST.JITTER1550") &&
                 voiceTest::enabled() && active.load() && !testStallUsed) {
          testStallUsed = true;
          testResumeAt = millis() + (serialLine == "TEST.JITTER" ? 650 : 1550);
          ok = true;
        } else if (serialLine == "TEST.TXBUSY" && voiceTest::enabled() && active.load() && !testTxStallUsed) {
          testTxStallUsed = true;
          testTxResumeAt = millis() + 3200;
          ok = true;
        } else if (serialLine == "TEST.STOP" && voiceTest::enabled()) {
          stopConversation("Gesprek stoppen...");
          ok = true;
        }
        Serial.println(ok ? "TEST OK" : "TEST ERROR");
      } else if (serialLine == "MOTION_TEST") {
        const bool accepted = !active.load() && !starting && !closing && head::requestTest();
        Serial.println(accepted ? "MOTION_TEST queued" : "MOTION_TEST busy_or_unavailable");
      } else if (serialLine == "DIAG") {
        JsonDocument diag;
        addStats(diag.to<JsonObject>());
        diag["audio_ready"] = audioReady;
        diag["configured"] = configured;
        diag["board"] = static_cast<int>(M5.getBoard());
        diag["display_board"] = static_cast<int>(M5.Display.getBoard());
        diag["display_width"] = M5.Display.width();
        diag["display_height"] = M5.Display.height();
        diag["brightness"] = M5.Display.getBrightness();
        diag["display_count"] = M5.getDisplayCount();
        diag["backlight_enable_register"] = M5.In_I2C.readRegister8(0x34, 0x90, 400000);
        diag["backlight_voltage_register"] = M5.In_I2C.readRegister8(0x34, 0x99, 400000);
        diag["rx_samples"] = rxSamples.load();
        diag["tx_samples"] = txSamples.load();
        diag["audio_write_errors"] = audioFailures.load();
        diag["wifi_connected"] = WiFi.status() == WL_CONNECTED;
        diag["bridge_connected"] = connected;
        diag["active"] = active.load();
        diag["free_heap"] = ESP.getFreeHeap();
        diag["ui_ready"] = ui.ready();
        head::diagnostics(diag["head"].to<JsonObject>());
        voiceTest::diagnostics(diag["voice_test"].to<JsonObject>());
        diag["paired_pcm"] = pairedPcm;
        diag["prebuffer_frames"] = prebufferFrames.load();
        diag["speaking"] = static_cast<int32_t>(echoUntil.load()-millis()) > 0;
        diag["session_closing"] = closing;
        diag["free_psram"] = ESP.getFreePsram();
        diag["last_stop"] = lastStop;
        diag["last_bridge_error"] = lastBridgeError;
        diag["state"] = status;
        Serial.print("DIAG ");
        serializeJson(diag, Serial);
        Serial.println();
      } else if (serialLine.startsWith("CONFIG ") && !active.load() && !starting && !closing) {
        Config candidate;
        if (parseConfig(serialLine.substring(7), candidate)) {
          const String json = serialLine.substring(7);
          preferences.begin("stackchan", false);
          const size_t saved = preferences.putString("config", json);
          preferences.end();
          if (saved == json.length()) {
            Serial.println("CONFIG_OK");
            Serial.flush();
            delay(200);
            ESP.restart();
          } else Serial.println("CONFIG_ERROR storage");
        } else Serial.println("CONFIG_ERROR invalid");
      } else Serial.println("CONFIG_ERROR command_or_busy");
      serialLine = "";
    } else if (!discarding) {
      if (serialLine.length() >= 8192) { serialLine = ""; discarding = true; }
      else serialLine += c;
    }
  }
}

void sendCapturedAudio() {
  const unsigned group = pairedPcm ? 2 : 1;
  static uint8_t payload[2 * sizeof(AudioFrame::samples)];
  static const uint8_t silence[sizeof(payload)]{};
  for (unsigned batch = 0; batch < 3 && active.load() && connected &&
       uxQueueMessagesWaiting(captureQueue) >= group; ++batch) {
    // Leave complete unsent frames queued while TCP is congested. Returning
    // to loop() services incoming speech and UI instead of blocking in TLS.
    const uint32_t now = millis();
    const bool testBusy = voiceTest::enabled() && testTxStallUsed && static_cast<int32_t>(testTxResumeAt - now) > 0;
    if (testBusy || audioSocket.writeWouldBlock()) {
      if (!txWaitSince) { txWaitSince = now; ++txWaitEvents; }
      txWaitMaxMs = std::max(txWaitMaxMs, now - txWaitSince);
      return;
    }
    txWaitSince = 0;
    unsigned frames = 0;
    for (unsigned i = 0; i < group; ++i) {
      AudioFrame frame;
      if (!xQueueReceive(captureQueue, &frame, 0)) break;
      if (millis() - frame.capturedMs > kCaptureFrames * 20) {
        ++captureOverflows;
        continue;
      }
      memcpy(payload + frames * sizeof(frame.samples), frame.samples, sizeof(frame.samples));
      ++frames;
    }
    if (!frames) continue;
    size_t bytes = frames * sizeof(AudioFrame::samples);
    if (locallyMuted.load() || localInputHold.load()) memset(payload, 0, bytes);
    if (pairedPcm && memcmp(payload, silence, bytes) == 0) {
      payload[0] = frames;
      bytes = 1;
    }
    const uint32_t began = millis();
    const bool sent = audioSocket.sendBIN(payload, bytes);
    const uint32_t elapsed = millis() - began;
    sendMaxMs = std::max(sendMaxMs, elapsed);
    if (elapsed > 20) ++sendStalls;
    if (!sent) {
      failedSendMs = elapsed;
      // The peer may have received a partial frame. No further WebSocket
      // command (including Stop) is valid on this stream.
      voiceTest::stop();
      clearAudio();
      lastStop = "audio_send_failed";
      status = "Bridge onbereikbaar";
      audioSocket.abortTransport();
      return;
    }
    netInputFrames += frames;
    if (elapsed > 20 || audioSocket.hasPendingInput()) return;
  }
}

void maintainNetwork() {
  if (!configured || !audioReady) return;
  if (WiFi.status() != WL_CONNECTED) {
    if (active.load() || starting) stopConversation("Wifi verbroken", "wifi_lost");
    if (millis() - wifiSince > 30000) status = "Controleer wifi-instellingen";
    return;
  }
  if (!clockSince) {
    configTime(0, 0, "pool.ntp.org", "time.cloudflare.com");
    clockSince = millis();
    status = "Klok synchroniseren...";
  }
  if (time(nullptr) < 1700000000) {
    if (millis() - clockSince > 20000) status = "Geen tijdserver bereikbaar";
    return; // certificate validation is never disabled
  }
  if (!socketStarted) {
    authHeader = "Authorization: Bearer " + cfg.token + "\r\nX-StackChan-Audio: pcm-pair-v1";
    audioSocket.beginSslWithCA(cfg.host.c_str(), cfg.port, "/device", cfg.ca.c_str(), "");
    audioSocket.setExtraHeaders(authHeader.c_str());
    audioSocket.onEvent(onSocket);
    audioSocket.setReconnectInterval(10000);
    // The bridge already pings every 20 s. Avoid a second periodic client
    // ping/write path competing with continuous bidirectional audio.
    audioSocket.disableHeartbeat();
    socketStarted = true;
    status = "Bridge verbinden...";
  }
  const uint32_t serviceStarted = millis();
  audioSocket.loop();
  // Drain a received TCP burst before sending microphone backlog. A single
  // read per loop can leave speech/control events behind outgoing TLS writes.
  const uint32_t drainStarted = millis();
  for (int i = 0; i < 24 && audioSocket.hasPendingInput() && millis() - drainStarted < 8; ++i)
    audioSocket.loop();
  recvServiceMaxMs = std::max<uint32_t>(recvServiceMaxMs, millis() - serviceStarted);
}
} // namespace

void setup() {
  auto m5cfg = M5.config();
  m5cfg.internal_mic = false;
  m5cfg.internal_spk = false;
  m5cfg.external_spk = false;
  M5.begin(m5cfg);
  // A USB configuration line includes a PEM certificate and can exceed 2 KB.
  // HWCDC's default 256-byte queue can lose that burst while the display draws.
  Serial.setRxBufferSize(9216);
  Serial.begin(115200);
  M5.Display.setRotation(1);
  M5.Display.fillScreen(kBackground);
  M5.Display.setTextColor(TFT_WHITE, kBackground);
  M5.Display.setTextDatum(middle_center);
  M5.Display.drawString("Even opstarten...", 160, 120);
  auto makeQueue = [](unsigned frames, StaticQueue_t& control) -> QueueHandle_t {
    auto* data = static_cast<uint8_t*>(heap_caps_malloc(frames*sizeof(AudioFrame), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    return data ? xQueueCreateStatic(frames, sizeof(AudioFrame), data, &control) :
                  xQueueCreate(frames, sizeof(AudioFrame));
  };
  captureQueue = makeQueue(kCaptureFrames, captureControl);
  playbackQueue = makeQueue(kPlaybackFrames, playbackControl);
  audioReady = captureQueue && playbackQueue && audio.begin();
  if (audioReady) {
    const bool rx = xTaskCreatePinnedToCore(captureTask, "capture", 4096, nullptr, 5, nullptr, 0) == pdPASS;
    const bool tx = xTaskCreatePinnedToCore(playbackTask, "playback", 4096, nullptr, 5, nullptr, 0) == pdPASS;
    audioReady = rx && tx;
  }
  // Apply backlight after the official StackChan's internal I2C bus has been
  // restored by AudioDuplex. A cached brightness alone doesn't prove it is on.
  M5.Display.wakeup();
  M5.Display.setBrightness(160);
  preferences.begin("stackchan", true);
  configured = parseConfig(preferences.getString("config", ""), cfg);
  volume = std::min<uint8_t>(100, preferences.getUChar("volume", 90));
  preferences.end();
  audio.setVolume(volume.load());
  if (!audioReady) status = "Audiofout - controleer apparaat";
  else if (configured) {
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.begin(cfg.ssid.c_str(), cfg.password.c_str());
    wifiSince = millis();
    status = "Wifi verbinden...";
  }
  ui.setImmediateHandler(immediateUiCommand);
  if (!ui.begin()) {
    status = "Schermbuffer niet beschikbaar";
    M5.Display.setTextSize(1);
    M5.Display.drawString("Schermbuffer/PSRAM fout", 160, 120);
  }
  // Network owns session state. UI runs separately at priority 1; audio at 5.
  vTaskPrioritySet(nullptr, 3);
  head::begin([] {
    if (!active.load() || localOutputHold.load()) return head::State::Idle;
    if (!volume.load() || locallyMuted.load() || localInputHold.load()) return head::State::Listening;
    return static_cast<int32_t>(echoUntil.load() - millis()) > 0 ?
        head::State::Speaking : head::State::Listening;
  });
  Serial.println("STACKCHAN_READY v9");
}

void loop() {
  const uint32_t began = millis();
  handleSerial();
  if (voiceTest::expired()) stopConversation("Spraaktest afgerond");
  // Only an explicit USB hardware test can inject this single bounded stall.
  const bool paused = voiceTest::enabled() && testStallUsed && static_cast<int32_t>(testResumeAt-millis()) > 0;
  if (!paused) maintainNetwork();
  handleUiCommands();
  const uint32_t faults = audioFaults.exchange(0);
  if (faults && (active.load() || starting)) {
    stopConversation("Audio onderbroken - start opnieuw", "audio_io");
  }
  if (protocolFault && connected) {
    stopConversation("Ongeldig bridgebericht", "protocol_error");
    audioSocket.disconnect();
  }
  if ((starting || closing) && millis() - operationSince > kOperationTimeout) {
    clearAudio();
    starting = closing = false;
    status = "Verbinding verlopen";
    if (lastStop == "none") lastStop = "operation_timeout";
    audioSocket.disconnect();
  }
  if (mutePending && millis() - muteSince > 5000) {
    stopConversation("Microfoonstatus niet bevestigd", "mute_timeout");
  }
  if (!paused) {
    sendCapturedAudio();
    if (!active.load() || !txWaitSince) sendDiagnostics();
  }
  publishUi();
  loopMaxMs = std::max<uint32_t>(loopMaxMs, millis() - began);
  delay(1);
}
