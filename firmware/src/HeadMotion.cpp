#include "HeadMotion.h"

#include <Arduino.h>
#include <M5Unified.h>
#include <Preferences.h>
#include <algorithm>
#include <atomic>
#include <driver/uart.h>

namespace head {
namespace {
// Official StackChan-BSP: PY32 pin 0 switches VM, UART1 TX6/RX7 drives SCS servos.
// https://github.com/m5stack/StackChan-BSP/blob/e2f3b02/src/M5StackChan.cpp
constexpr uint8_t kExpander = 0x6f;
constexpr int kI2cHz = 100000;
constexpr uart_port_t kPort = UART_NUM_1;
constexpr int kLegMs = 470;
constexpr uint32_t kBetweenGesturesMs = 7800;
constexpr uint8_t kTorque = 40, kGoal = 42, kFeedback = 56;
struct Axis {
  uint8_t id;
  int low, high, excursion;
  int measured, rawLow, rawHigh;
};
Axis yaw{1, 0, 0, 16, -1, -1, -1}, pitch{2, 0, 0, 12, -1, -1, -1};
StateReader readState;
std::atomic<const char*> status{"off"};
std::atomic<bool> ready{false}, testPending{false};
std::atomic<unsigned> gestures{0}, failures{0};
std::atomic<int> lastAxis{0}, lastStart{-1}, lastPeak{-1}, lastEnd{-1};
std::atomic<int> lastRxBytes{0}, lastServoStatus{0};
std::atomic<int> vmConfig{-1}, vmOut{-1}, vmIn{-1}, batteryMv{-1};

bool setupBus() {
  auto& i2c = M5.In_I2C;
  uint8_t version = 0;
  for (unsigned attempt = 0; attempt < 5; ++attempt) {
    if (i2c.readRegister(kExpander, 0x02, &version, 1, kI2cHz) && version && version != 255) break;
    version = 0;
    vTaskDelay(pdMS_TO_TICKS(100));
  }
  if (!version) return false;
  uart_config_t cfg{};
  cfg.baud_rate = 1000000;
  cfg.data_bits = UART_DATA_8_BITS;
  cfg.parity = UART_PARITY_DISABLE;
  cfg.stop_bits = UART_STOP_BITS_1;
  cfg.source_clk = UART_SCLK_APB;
  if (uart_driver_install(kPort, 256, 0, 0, nullptr, 0) != ESP_OK) return false;
  if (uart_param_config(kPort, &cfg) != ESP_OK ||
      uart_set_pin(kPort, 6, 7, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE) != ESP_OK) return false;
  return i2c.bitOn(kExpander, 0x03, 1, kI2cHz) && i2c.bitOff(kExpander, 0x0b, 1, kI2cHz) &&
         i2c.bitOn(kExpander, 0x09, 1, kI2cHz) && i2c.bitOn(kExpander, 0x05, 1, kI2cHz);
}

bool sendPacket(uint8_t id, uint8_t op, const uint8_t* args, size_t count) {
  uint8_t bytes[18] = {0xff, 0xff, id, static_cast<uint8_t>(count+2), op};
  if (count > sizeof(bytes)-6) return false;
  memcpy(bytes+5, args, count);
  uint8_t sum = 0;
  for (size_t i = 2; i < 5+count; ++i) sum += bytes[i];
  bytes[5+count] = ~sum;
  return uart_write_bytes(kPort, bytes, 6+count) == static_cast<int>(6+count);
}

bool readRegs(uint8_t id, uint8_t reg, uint8_t* dest, uint8_t size) {
  uint8_t response[10];
  if (size > sizeof(response)-6) return false;
  const uint8_t args[] = {reg, size};
  uart_flush_input(kPort);
  if (!sendPacket(id, 2, args, sizeof(args))) return false;
  const int n = uart_read_bytes(kPort, response, size+6, pdMS_TO_TICKS(30));
  lastRxBytes = n;
  lastServoStatus = n >= 5 ? response[4] : -1;
  if (n != size+6 || response[0] != 255 || response[1] != 255 ||
      response[2] != id || response[3] != size+2 || response[4] != 0) return false;
  uint8_t sum = 0;
  for (int i = 2; i < n; ++i) sum += response[i];
  if (sum != 255) return false;
  memcpy(dest, response+5, size);
  return true;
}

// SCS SYNC_WRITE addresses only the listed servo. It generates no status packet
// that could be confused with the next measured position.
bool torque(uint8_t id, bool enabled) {
  const uint8_t args[] = {kTorque, 1, id, static_cast<uint8_t>(enabled)};
  return sendPacket(0xfe, 0x83, args, sizeof(args));
}

bool goal(uint8_t id, int position) {
  const uint8_t args[] = {kGoal, 6, id, static_cast<uint8_t>(position >> 8),
      static_cast<uint8_t>(position), static_cast<uint8_t>(kLegMs >> 8),
      static_cast<uint8_t>(kLegMs & 255), 0, 0};
  return sendPacket(0xfe, 0x83, args, sizeof(args));
}

int word(const uint8_t* p) { return (p[0] << 8) | p[1]; }

int position(uint8_t id) {
  uint8_t p[2];
  return readRegs(id, kFeedback, p, 2) ? word(p) : -1;
}

bool configureAxis(Axis& axis, int zero, int minOffset, int maxOffset) {
  uint8_t bounds[4];
  axis.measured = position(axis.id);
  if (!readRegs(axis.id, 9, bounds, 4)) return false;
  // A zero/zero angle range is continuous-rotation mode. Never change EEPROM.
  axis.rawLow = word(bounds);
  axis.rawHigh = word(bounds+2);
  axis.low = std::max({4, axis.rawLow, zero + minOffset});
  axis.high = std::min({996, axis.rawHigh, zero + maxOffset});
  return axis.high - axis.low >= 2*axis.excursion && axis.measured >= 0;
}

int zeroPosition(Preferences& p, const char* key, int fallback) {
  int value = p.getInt(key, fallback);
  return value >= 0 && value <= 1000 ? value : fallback;
}

bool release(uint8_t id) {
  uint8_t value;
  return torque(id, false) && readRegs(id, kTorque, &value, 1) && value == 0;
}

void disable(const char* reason) {
  ready = false;
  status = reason;
  failures++;
  M5.In_I2C.bitOff(kExpander, 0x05, 1, kI2cHz);
}

bool keepGoing(bool test) {
  return (readState() == State::Idle) == test;
}

bool waitForServo(bool test) {
  const uint32_t since = millis();
  do {
    vTaskDelay(pdMS_TO_TICKS(20));
    if (!keepGoing(test)) return false;
  } while (millis()-since < kLegMs+150);
  return true;
}

enum class Result { Done, Cancelled, Outside, Fault };

Result gesture(const Axis& a, int sign, bool test) {
  const int base = position(a.id);
  if (base < 0 || base > 1023) return Result::Fault;
  if (base < a.low || base > a.high) return Result::Outside;
  int delta = sign*a.excursion;
  if (base+delta < a.low || base+delta > a.high) delta = -delta;
  if (!keepGoing(test)) return Result::Cancelled;

  lastAxis = a.id; lastStart = base; lastPeak = lastEnd = -1;
  status = "moving";
  // Set the nearby target before enabling torque. Never energize a stale goal.
  if (!goal(a.id, base+delta) || !torque(a.id, true)) return Result::Fault;
  if (!waitForServo(test)) return Result::Cancelled;
  const int peak = position(a.id);
  lastPeak = peak;
  if (peak < 0 || abs(peak-base) > a.excursion+7) return Result::Fault;
  if (!keepGoing(test)) return Result::Cancelled;
  if (!goal(a.id, base)) return Result::Fault;
  if (!waitForServo(test)) return Result::Cancelled;
  const int end = position(a.id);
  lastEnd = end;
  if (end < 0 || abs(end-base) > 6 || abs(peak-base) < a.excursion/2 ||
      (peak-base)*delta <= 0) return Result::Fault;
  return Result::Done;
}

void run(void*) {
  status = "initializing";
  if (!setupBus()) { disable("power_or_uart"); vTaskDelete(nullptr); return; }
  vTaskDelay(pdMS_TO_TICKS(350));
  Preferences calibration;
  const bool saved = calibration.begin("servo", true);
  const int zeroYaw = saved ? zeroPosition(calibration, "zero_pos_1", 460) : 460;
  const int zeroPitch = saved ? zeroPosition(calibration, "zero_pos_2", 620) : 620;
  if (saved) calibration.end();
  bool yawOk = false, pitchOk = false;
  for (int attempt = 0; attempt < 6; ++attempt) {
    if (!yawOk) yawOk = configureAxis(yaw, zeroYaw, -409, 409);
    if (!pitchOk) pitchOk = configureAxis(pitch, zeroPitch, 16, 272);  // 5..85 degrees
    if (yawOk && pitchOk) break;
    vTaskDelay(pdMS_TO_TICKS(250));
  }
  vmConfig = M5.In_I2C.readRegister8(kExpander, 0x03, kI2cHz);
  vmOut = M5.In_I2C.readRegister8(kExpander, 0x05, kI2cHz);
  vmIn = M5.In_I2C.readRegister8(kExpander, 0x07, kI2cHz);
  uint8_t volts[2];
  if (M5.In_I2C.readRegister(0x41, 2, volts, 2, kI2cHz)) batteryMv = word(volts)*5/4;
  if (!yawOk || !pitchOk) {
    disable(!yawOk ? "yaw_init" : "pitch_init"); vTaskDelete(nullptr); return;
  }
  if (!release(yaw.id) || !release(pitch.id)) {
    disable("torque_release"); vTaskDelete(nullptr); return;
  }
  status = "ready";
  ready = true;
  unsigned phase = 0;
  uint32_t last = millis()-kBetweenGesturesMs;
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(40));
    const bool test = testPending.exchange(false);
    const auto state = readState();
    if (test ? state != State::Idle :
        state != State::Speaking || millis()-last < kBetweenGesturesMs) continue;
    last = millis();
    const Axis* axis = phase%2 ? &pitch : &yaw;
    const int sign = phase%4 < 2 ? 1 : -1;
    ++phase;
    Result result = gesture(*axis, sign, test);
    if (result == Result::Outside) {
      axis = axis == &yaw ? &pitch : &yaw;
      result = gesture(*axis, sign, test);
    }
    if (result == Result::Outside) { status = "outside_range"; continue; }
    if (!release(axis->id) || result == Result::Fault) {
      disable("feedback_or_release"); vTaskDelete(nullptr); return;
    }
    if (result == Result::Done) ++gestures;
    status = "ready";
  }
}
}  // namespace

void begin(StateReader state) {
  readState = state;
  if (M5.getBoard() == m5::board_t::board_M5StackChan &&
      xTaskCreatePinnedToCore(run, "head_motion", 4096, nullptr, 1, nullptr, 1) != pdPASS)
    status = "task_failed";
}

bool requestTest() {
  return ready && strcmp(status.load(), "moving") != 0 && !testPending.exchange(true);
}

void diagnostics(JsonObject out) {
  out["status"] = status.load();
  out["count"] = gestures.load();
  out["faults"] = failures.load();
  out["axis"] = lastAxis.load();
  out["from"] = lastStart.load();
  out["peak"] = lastPeak.load();
  out["returned"] = lastEnd.load();
  out["rx_bytes"] = lastRxBytes.load();
  out["servo_status"] = lastServoStatus.load();
  out["vm_output_config"] = vmConfig.load();
  out["vm_latch"] = vmOut.load();
  out["vm_pins"] = vmIn.load();
  out["battery_mv"] = batteryMv.load();
  // Startup probe fields are immutable after init publishes ready or a fault.
  if (ready.load() || failures.load()) {
    for (const Axis* axis : {&yaw, &pitch}) {
      auto a = out[axis->id == yaw.id ? "yaw" : "pitch"].to<JsonArray>();
      for (int v : {axis->measured, axis->rawLow, axis->rawHigh, axis->low, axis->high}) a.add(v);
    }
  }
}
}
