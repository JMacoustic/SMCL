/*
  Closed-loop impeller RPM controller for Arduino UNO.

  Pin plan:
    Encoder channel A -> D2 / INT0
    Encoder channel B -> D3 / INT1
    Timer1 OC1A PWM pulse output -> D9

  Serial control at 115200 baud:
    T<rpm>  Set integer target RPM. T0 stops the motor.
    O<pct>  Hold fixed safe output strength for calibration. O0 stops.
    R<ms>   Set the status line interval (100-5000 ms, default 100).
    P<kp>   Set PID proportional gain.
    I<ki>   Set PID integral gain.
    D<kd>   Set PID derivative gain.
    S0      Stop the motor.
    ?       Print current status.

  Status lines start with "OK t_ms=<millis> rpm_t_ms=<millis of last RPM sample>"
  so a PC logger can timestamp samples with the controller's own clock.

  The PWM safety boundary is enforced on Arduino:
    - High pulse width is clamped between MIN_PULSE_WIDTH_NS and MAX_PULSE_WIDTH_NS.
    - Effective duty is clamped to the measured original impeller range.
*/

#include <util/atomic.h>

const uint8_t ENCODER_A_PIN = 2;
const uint8_t ENCODER_B_PIN = 3;
const uint8_t MOTOR_PWM_PIN = 9;  // Arduino UNO PB1 / OC1A

const float COUNTS_PER_REVOLUTION = 500.0f;

// After running calibration.py, copy the generated calibration constants here before uploading.
const uint8_t RPM_CALIB_POINT_COUNT = 6;
const float RPM_CALIB_MEASURED[RPM_CALIB_POINT_COUNT] = {
  234.615f, 319.800f, 386.857f,
  476.400f, 558.800f, 656.200f
};
const float RPM_CALIB_REFERENCE[RPM_CALIB_POINT_COUNT] = {
  158.000f, 215.000f, 261.000f,
  320.000f, 376.000f, 440.000f
};

const uint16_t MAX_TARGET_RPM = 700;

const unsigned long RPM_SAMPLE_INTERVAL_MS = 100UL;
const unsigned long RPM_CALC_WINDOW_MS = 1000UL;
// Must hold at least RPM_CALC_WINDOW_MS / RPM_SAMPLE_INTERVAL_MS + 1 samples.
const uint8_t RPM_HISTORY_SIZE = 12;
const float RPM_EMA_ALPHA = 0.65f;
const unsigned long PID_INTERVAL_MS = 100UL;
const unsigned long DEFAULT_STATUS_INTERVAL_MS = 100UL;
// Not below RPM_SAMPLE_INTERVAL_MS: faster reports would only repeat the same RPM value.
const unsigned long MIN_STATUS_INTERVAL_MS = RPM_SAMPLE_INTERVAL_MS;
const unsigned long MAX_STATUS_INTERVAL_MS = 5000UL;
const unsigned long SERIAL_COMMAND_TIMEOUT_MS = 2500UL;

const float DEFAULT_PID_KP = 0.055f;
const float DEFAULT_PID_KI = 0.035f;
const float DEFAULT_PID_KD = 0.000f;
const float MAX_PID_KP = 1.0f;
const float MAX_PID_KI = 1.0f;
const float MAX_PID_KD = 1.0f;
const uint8_t FEEDFORWARD_POINT_COUNT = 10;
const float FEEDFORWARD_RPM[FEEDFORWARD_POINT_COUNT] = {
  270.0f, 290.0f, 310.0f, 330.0f, 350.0f,
  370.0f, 390.0f, 410.0f, 430.0f, 450.0f
};
const float FEEDFORWARD_OUTPUT[FEEDFORWARD_POINT_COUNT] = {
  21.400f, 23.565f, 25.715f, 27.827f, 29.835f,
  31.619f, 33.402f, 35.110f, 36.814f, 38.773f
};
const float FEEDFORWARD_CONTROL_RATIO = 0.92f;
const float FEEDFORWARD_PRECHARGE_RATIO = 0.85f;
const float PID_D_FILTER_ALPHA = 0.35f;
const float PID_CORRECTION_MIN_OUTPUT = -10.0f;
const float PID_CORRECTION_MAX_OUTPUT = 6.0f;
const float PID_INTEGRAL_LIMIT = 300.0f;
const float PID_INTEGRATION_BAND_RPM = 25.0f;
const float PID_OUTPUT_RISE_LIMIT_PER_SECOND = 16.0f;
const float PID_SETPOINT_RISE_RPM_PER_SECOND = 220.0f;
const float PID_SETPOINT_FALL_RPM_PER_SECOND = 180.0f;
const float PID_HOLD_BAND_RPM = 3.0f;
const float PID_RESUME_BAND_RPM = 5.0f;
const unsigned long PID_HOLD_CONFIRM_MS = 1500UL;
const float RPM_OVERSPEED_STOP_RPM = 750.0f;

const bool USE_FIXED_TARGET_OUTPUT_TABLE = false;
const uint8_t FIXED_TARGET_OUTPUT_POINT_COUNT = 2;
const float FIXED_TARGET_RPM[FIXED_TARGET_OUTPUT_POINT_COUNT] = {
  430.0f, 450.0f
};
const float FIXED_TARGET_OUTPUT[FIXED_TARGET_OUTPUT_POINT_COUNT] = {
  37.7f, 40.4f
};

const uint16_t MIN_PULSE_WIDTH_NS = 300;
const uint16_t MAX_PULSE_WIDTH_NS = 3500;
const uint32_t NOMINAL_PWM_PERIOD_NS = 50000UL;

// Update this after measuring the original impeller at its minimum RPM.
// The default makes 1% output roughly 1% of the safe maximum with a 300 ns pulse.
const uint32_t REST_US_AT_STRENGTH_1 = 8000UL;

const uint32_t MIN_EFFECTIVE_DUTY_PPM = 6000UL;
const uint32_t MAX_EFFECTIVE_DUTY_PPM = 70000UL;
const uint32_t STRENGTH_SCALE = 100000UL;  // 100.000%

const uint16_t MIN_PULSE_TICKS = (uint16_t)(((uint32_t)MIN_PULSE_WIDTH_NS * 2UL + 124UL) / 125UL);
const uint16_t MAX_PULSE_TICKS = (uint16_t)(((uint32_t)MAX_PULSE_WIDTH_NS * 2UL + 124UL) / 125UL);

volatile long encoderCount = 0;
volatile uint8_t lastEncoderState = 0;

unsigned long lastRpmSampleMs = 0;
unsigned long lastPidMs = 0;
unsigned long lastStatusMs = 0;
unsigned long statusIntervalMs = DEFAULT_STATUS_INTERVAL_MS;
unsigned long lastSerialCommandMs = 0;

unsigned long countSampleTimes[RPM_HISTORY_SIZE];
long countSamples[RPM_HISTORY_SIZE];
uint8_t countSampleIndex = 0;
uint8_t countSampleCount = 0;

float rpmEncoder = 0.0f;
float rpmRaw = 0.0f;
float rpmMeasured = 0.0f;
bool rpmMeasuredReady = false;
uint16_t targetRpm = 0;
float pidIntegral = 0.0f;
float lastError = 0.0f;
float pidOutput = 0.0f;
float pidKp = DEFAULT_PID_KP;
float pidKi = DEFAULT_PID_KI;
float pidKd = DEFAULT_PID_KD;
float pidSetpointRpm = 0.0f;
float lastControlRpm = 0.0f;
float filteredControlDerivative = 0.0f;
bool controlDerivativeReady = false;
bool pidOutputHold = false;
unsigned long pidHoldBandSinceMs = 0;
bool openLoopMode = false;

uint32_t pwmStrengthMilliPercent = 0;
uint16_t configuredPulseTicks = MIN_PULSE_TICKS;
uint16_t configuredPeriodTicks = 0;
uint32_t configuredPulseNs = 0;
uint32_t configuredRestUs = REST_US_AT_STRENGTH_1;
uint32_t configuredPeriodUs = REST_US_AT_STRENGTH_1;
uint32_t configuredDutyPpm = 0;
bool pwmRunning = false;

char commandBuffer[24];
uint8_t commandLength = 0;

void setup() {
  pinMode(ENCODER_A_PIN, INPUT_PULLUP);
  pinMode(ENCODER_B_PIN, INPUT_PULLUP);
  pinMode(MOTOR_PWM_PIN, OUTPUT);
  stopMotor();

  Serial.begin(115200);
  while (!Serial) {
    ;  // UNO continues immediately; native USB boards wait for serial.
  }

  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    lastEncoderState = readEncoderState();
  }

  attachInterrupt(digitalPinToInterrupt(ENCODER_A_PIN), handleEncoderChange, CHANGE);
  attachInterrupt(digitalPinToInterrupt(ENCODER_B_PIN), handleEncoderChange, CHANGE);

  lastRpmSampleMs = millis();
  recordCountSample(lastRpmSampleMs, 0);
  lastPidMs = millis();
  lastStatusMs = millis();
  lastSerialCommandMs = millis();

  Serial.println("Impeller PID controller ready.");
  Serial.println("Encoder: A->D2, B->D3. PWM: D9 / OC1A.");
  printStatus();
}

void loop() {
  readSerialCommands();
  enforceSerialCommandTimeout();
  updateRpm();
  updatePid();
  printPeriodicStatus();
}

void updateRpm() {
  const unsigned long now = millis();
  if (now - lastRpmSampleMs < RPM_SAMPLE_INTERVAL_MS) {
    return;
  }

  long countSnapshot;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    countSnapshot = encoderCount;
  }

  recordCountSample(now, countSnapshot);

  unsigned long elapsedMs = 0;
  long delta = 0;
  calculateWindowDelta(now, countSnapshot, &elapsedMs, &delta);

  const float countsPerSecond = elapsedMs > 0 ? (delta * 1000.0f) / elapsedMs : 0.0f;
  float unscaledRpm = (countsPerSecond * 60.0f) / COUNTS_PER_REVOLUTION;

  if (unscaledRpm < 0.0f) {
    unscaledRpm = -unscaledRpm;
  }

  rpmRaw = unscaledRpm > 0.0f ? calibrateRpm(unscaledRpm) : 0.0f;
  rpmEncoder = unscaledRpm;
  rpmMeasured = smoothRpm(rpmRaw);

  lastRpmSampleMs = now;
}

void recordCountSample(unsigned long sampleMs, long count) {
  countSampleTimes[countSampleIndex] = sampleMs;
  countSamples[countSampleIndex] = count;
  countSampleIndex = (countSampleIndex + 1) % RPM_HISTORY_SIZE;

  if (countSampleCount < RPM_HISTORY_SIZE) {
    countSampleCount++;
  }
}

void calculateWindowDelta(unsigned long now, long currentCount, unsigned long *elapsedMs, long *delta) {
  if (countSampleCount < 2) {
    *elapsedMs = 0;
    *delta = 0;
    return;
  }

  uint8_t newestIndex = (countSampleIndex + RPM_HISTORY_SIZE - 1U) % RPM_HISTORY_SIZE;
  uint8_t selectedIndex = newestIndex;
  unsigned long selectedAgeMs = 0;

  for (uint8_t i = 0; i < countSampleCount; i++) {
    const uint8_t index = (countSampleIndex + RPM_HISTORY_SIZE - 1U - i) % RPM_HISTORY_SIZE;
    const unsigned long ageMs = now - countSampleTimes[index];

    if (ageMs <= RPM_CALC_WINDOW_MS && ageMs >= selectedAgeMs) {
      selectedAgeMs = ageMs;
      selectedIndex = index;
    }
  }

  if (selectedIndex == newestIndex || selectedAgeMs == 0) {
    *elapsedMs = 0;
    *delta = 0;
    return;
  }

  *elapsedMs = selectedAgeMs;
  *delta = currentCount - countSamples[selectedIndex];
}

float smoothRpm(float rpm) {
  if (!rpmMeasuredReady) {
    rpmMeasuredReady = true;
    return rpm;
  }

  return rpmMeasured + (RPM_EMA_ALPHA * (rpm - rpmMeasured));
}

float calibrateRpm(float measuredRpm) {
  if (RPM_CALIB_POINT_COUNT == 0) {
    return measuredRpm;
  }

  if (RPM_CALIB_POINT_COUNT == 1) {
    if (RPM_CALIB_MEASURED[0] <= 0.0f) {
      return measuredRpm;
    }

    return measuredRpm * (RPM_CALIB_REFERENCE[0] / RPM_CALIB_MEASURED[0]);
  }

  if (measuredRpm <= RPM_CALIB_MEASURED[0]) {
    return interpolateRpm(
      measuredRpm,
      RPM_CALIB_MEASURED[0],
      RPM_CALIB_REFERENCE[0],
      RPM_CALIB_MEASURED[1],
      RPM_CALIB_REFERENCE[1]
    );
  }

  for (uint8_t i = 1; i < RPM_CALIB_POINT_COUNT; i++) {
    if (measuredRpm <= RPM_CALIB_MEASURED[i]) {
      return interpolateRpm(
        measuredRpm,
        RPM_CALIB_MEASURED[i - 1],
        RPM_CALIB_REFERENCE[i - 1],
        RPM_CALIB_MEASURED[i],
        RPM_CALIB_REFERENCE[i]
      );
    }
  }

  const uint8_t last = RPM_CALIB_POINT_COUNT - 1;
  return interpolateRpm(
    measuredRpm,
    RPM_CALIB_MEASURED[last - 1],
    RPM_CALIB_REFERENCE[last - 1],
    RPM_CALIB_MEASURED[last],
    RPM_CALIB_REFERENCE[last]
  );
}

float interpolateRpm(float measuredRpm, float measuredLow, float referenceLow, float measuredHigh, float referenceHigh) {
  return interpolateFloat(measuredRpm, measuredLow, referenceLow, measuredHigh, referenceHigh);
}

float interpolateFloat(float x, float x0, float y0, float x1, float y1) {
  const float span = x1 - x0;
  if (span == 0.0f) {
    return y0;
  }

  const float position = (x - x0) / span;
  return y0 + (position * (y1 - y0));
}

void updatePid() {
  const unsigned long now = millis();
  if (now - lastPidMs < PID_INTERVAL_MS) {
    return;
  }

  const float dt = (now - lastPidMs) / 1000.0f;
  lastPidMs = now;

  if (targetRpm == 0 && !openLoopMode) {
    pidIntegral = 0.0f;
    lastError = 0.0f;
    resetControlDerivative(0.0f);
    pidOutput = 0.0f;
    pidSetpointRpm = 0.0f;
    pidOutputHold = false;
    pidHoldBandSinceMs = 0;
    applyPwmStrength(0);
    return;
  }

  if (rpmMeasured > RPM_OVERSPEED_STOP_RPM || rpmRaw > RPM_OVERSPEED_STOP_RPM) {
    Serial.println("ERR overspeed; stopped motor");
    setTargetRpm(0);
    printStatus();
    return;
  }

  if (openLoopMode) {
    applyPwmStrength((uint32_t)(pidOutput * 1000.0f + 0.5f));
    return;
  }

  if (USE_FIXED_TARGET_OUTPUT_TABLE) {
    pidSetpointRpm = (float)targetRpm;
    pidOutput = fixedOutputForRpm((float)targetRpm);
    applyPwmStrength((uint32_t)(pidOutput * 1000.0f + 0.5f));
    return;
  }

  updatePidSetpoint(dt);

  const float controlRpm = maxFloat(rpmMeasured, rpmRaw);
  const float error = pidSetpointRpm - controlRpm;
  const float absError = absFloat(error);
  const float targetError = maxFloat(
    absFloat((float)targetRpm - rpmMeasured),
    absFloat((float)targetRpm - rpmRaw)
  );
  const bool finalSetpointReached = absFloat(pidSetpointRpm - (float)targetRpm) < 0.5f;

  if (pidOutputHold) {
    lastError = error;

    if (targetError <= PID_RESUME_BAND_RPM) {
      applyPwmStrength((uint32_t)(pidOutput * 1000.0f + 0.5f));
      return;
    }

    pidOutputHold = false;
    pidHoldBandSinceMs = 0;
  }

  if (finalSetpointReached && targetError <= PID_HOLD_BAND_RPM) {
    if (pidHoldBandSinceMs == 0) {
      pidHoldBandSinceMs = now;
    } else if (now - pidHoldBandSinceMs >= PID_HOLD_CONFIRM_MS) {
      pidOutputHold = true;
      lastError = error;
      applyPwmStrength((uint32_t)(pidOutput * 1000.0f + 0.5f));
      return;
    }
  } else {
    pidHoldBandSinceMs = 0;
  }

  const bool shouldIntegrate =
    absError <= PID_INTEGRATION_BAND_RPM ||
    (pidIntegral > 0.0f && error < 0.0f) ||
    (pidIntegral < 0.0f && error > 0.0f);

  if (shouldIntegrate) {
    pidIntegral += error * dt;
    pidIntegral = constrainFloat(pidIntegral, -PID_INTEGRAL_LIMIT, PID_INTEGRAL_LIMIT);
  }

  const float controlDerivative = updateControlDerivative(controlRpm, dt);
  lastError = error;

  const float feedforwardOutput = controlFeedforwardForRpm((float)targetRpm);
  const float pidCorrection = constrainFloat(
    (pidKp * error) + (pidKi * pidIntegral) - (pidKd * controlDerivative),
    PID_CORRECTION_MIN_OUTPUT,
    PID_CORRECTION_MAX_OUTPUT
  );
  const float requestedOutput = constrainFloat(
    feedforwardOutput + pidCorrection,
    0.0f,
    100.0f
  );

  const float maxOutputRise = PID_OUTPUT_RISE_LIMIT_PER_SECOND * dt;
  if (requestedOutput > pidOutput + maxOutputRise) {
    pidOutput += maxOutputRise;
  } else {
    pidOutput = requestedOutput;
  }

  applyPwmStrength((uint32_t)(pidOutput * 1000.0f + 0.5f));
}

float feedforwardForRpm(float rpm) {
  if (FEEDFORWARD_POINT_COUNT == 0 || rpm <= 0.0f) {
    return 0.0f;
  }

  if (FEEDFORWARD_POINT_COUNT == 1) {
    return FEEDFORWARD_OUTPUT[0];
  }

  if (rpm <= FEEDFORWARD_RPM[0]) {
    return interpolateFloat(
      rpm,
      FEEDFORWARD_RPM[0],
      FEEDFORWARD_OUTPUT[0],
      FEEDFORWARD_RPM[1],
      FEEDFORWARD_OUTPUT[1]
    );
  }

  for (uint8_t i = 1; i < FEEDFORWARD_POINT_COUNT; i++) {
    if (rpm <= FEEDFORWARD_RPM[i]) {
      return interpolateFloat(
        rpm,
        FEEDFORWARD_RPM[i - 1],
        FEEDFORWARD_OUTPUT[i - 1],
        FEEDFORWARD_RPM[i],
        FEEDFORWARD_OUTPUT[i]
      );
    }
  }

  const uint8_t last = FEEDFORWARD_POINT_COUNT - 1;
  return interpolateFloat(
    rpm,
    FEEDFORWARD_RPM[last - 1],
    FEEDFORWARD_OUTPUT[last - 1],
    FEEDFORWARD_RPM[last],
    FEEDFORWARD_OUTPUT[last]
  );
}

float controlFeedforwardForRpm(float rpm) {
  return feedforwardForRpm(rpm) * FEEDFORWARD_CONTROL_RATIO;
}

float fixedOutputForRpm(float rpm) {
  if (FIXED_TARGET_OUTPUT_POINT_COUNT == 0 || rpm <= 0.0f) {
    return 0.0f;
  }

  if (FIXED_TARGET_OUTPUT_POINT_COUNT == 1) {
    return FIXED_TARGET_OUTPUT[0];
  }

  if (rpm <= FIXED_TARGET_RPM[0]) {
    return FIXED_TARGET_OUTPUT[0];
  }

  for (uint8_t i = 1; i < FIXED_TARGET_OUTPUT_POINT_COUNT; i++) {
    if (rpm <= FIXED_TARGET_RPM[i]) {
      return interpolateFloat(
        rpm,
        FIXED_TARGET_RPM[i - 1],
        FIXED_TARGET_OUTPUT[i - 1],
        FIXED_TARGET_RPM[i],
        FIXED_TARGET_OUTPUT[i]
      );
    }
  }

  return FIXED_TARGET_OUTPUT[FIXED_TARGET_OUTPUT_POINT_COUNT - 1];
}

float updateControlDerivative(float controlRpm, float dt) {
  if (!controlDerivativeReady || dt <= 0.0f) {
    resetControlDerivative(controlRpm);
    return 0.0f;
  }

  const float measuredDerivative = (controlRpm - lastControlRpm) / dt;
  filteredControlDerivative += PID_D_FILTER_ALPHA * (measuredDerivative - filteredControlDerivative);
  lastControlRpm = controlRpm;
  return filteredControlDerivative;
}

void resetControlDerivative(float controlRpm) {
  lastControlRpm = controlRpm;
  filteredControlDerivative = 0.0f;
  controlDerivativeReady = true;
}

void updatePidSetpoint(float dt) {
  const float finalTarget = (float)targetRpm;

  if (pidSetpointRpm < finalTarget) {
    pidSetpointRpm += PID_SETPOINT_RISE_RPM_PER_SECOND * dt;
    if (pidSetpointRpm > finalTarget) {
      pidSetpointRpm = finalTarget;
    }
  } else if (pidSetpointRpm > finalTarget) {
    pidSetpointRpm -= PID_SETPOINT_FALL_RPM_PER_SECOND * dt;
    if (pidSetpointRpm < finalTarget) {
      pidSetpointRpm = finalTarget;
    }
  }
}

void readSerialCommands() {
  while (Serial.available() > 0) {
    char c = Serial.read();

    if (c == '\n' || c == '\r') {
      if (commandLength > 0) {
        commandBuffer[commandLength] = '\0';
        processCommand(commandBuffer);
        commandLength = 0;
      }
    } else if (commandLength < sizeof(commandBuffer) - 1) {
      commandBuffer[commandLength++] = c;
    } else {
      commandLength = 0;
      Serial.println("ERR command too long");
    }
  }
}

void processCommand(char *command) {
  if (command[0] == '?') {
    markValidSerialCommand();
    printStatus();
    return;
  }

  if (command[0] == 'T' || command[0] == 't') {
    uint16_t value;
    if (!parseNonNegativeInteger(command + 1, &value) || value > MAX_TARGET_RPM) {
      Serial.print("ERR use T0-T");
      Serial.println(MAX_TARGET_RPM);
      return;
    }

    markValidSerialCommand();
    setTargetRpm(value);
    printStatus();
    return;
  }

  if (command[0] == 'R' || command[0] == 'r') {
    uint16_t value;
    if (!parseNonNegativeInteger(command + 1, &value) ||
        value < MIN_STATUS_INTERVAL_MS || value > MAX_STATUS_INTERVAL_MS) {
      Serial.print("ERR status interval must be R");
      Serial.print(MIN_STATUS_INTERVAL_MS);
      Serial.print("-R");
      Serial.println(MAX_STATUS_INTERVAL_MS);
      return;
    }

    markValidSerialCommand();
    statusIntervalMs = value;
    printStatus();
    return;
  }

  if (command[0] == 'O' || command[0] == 'o') {
    float value;
    if (!parseNonNegativeFloat(command + 1, &value) || value > 100.0f) {
      Serial.println("ERR open-loop output must be O0-O100");
      return;
    }

    markValidSerialCommand();
    setOpenLoopOutput(value);
    printStatus();
    return;
  }

  if (command[0] == 'P' || command[0] == 'p') {
    processPidGainCommand(command + 1, 'P', MAX_PID_KP);
    return;
  }

  if (command[0] == 'I' || command[0] == 'i') {
    processPidGainCommand(command + 1, 'I', MAX_PID_KI);
    return;
  }

  if (command[0] == 'D' || command[0] == 'd') {
    processPidGainCommand(command + 1, 'D', MAX_PID_KD);
    return;
  }

  if (command[0] == 'S' || command[0] == 's') {
    if (command[1] == '0' && command[2] == '\0') {
      markValidSerialCommand();
      setTargetRpm(0);
      printStatus();
    } else {
      Serial.println("ERR PID sketch only accepts S0 stop; use T<rpm> or O<pct>");
    }
    return;
  }

  Serial.println("ERR use T<rpm>, O<pct>, R<ms>, P<kp>, I<ki>, D<kd>, S0, or ?");
}

void markValidSerialCommand() {
  lastSerialCommandMs = millis();
}

void enforceSerialCommandTimeout() {
  if ((targetRpm == 0 && !openLoopMode) || SERIAL_COMMAND_TIMEOUT_MS == 0UL) {
    return;
  }

  if (millis() - lastSerialCommandMs <= SERIAL_COMMAND_TIMEOUT_MS) {
    return;
  }

  Serial.println("ERR command timeout; stopped motor");
  openLoopMode = false;
  setTargetRpm(0);
  printStatus();
}

bool parseNonNegativeInteger(char *text, uint16_t *value) {
  if (text[0] == '\0') {
    return false;
  }

  uint32_t parsed = 0;

  for (uint8_t i = 0; text[i] != '\0'; i++) {
    if (text[i] < '0' || text[i] > '9') {
      return false;
    }

    parsed = (parsed * 10UL) + (uint32_t)(text[i] - '0');
    if (parsed > 65535UL) {
      return false;
    }
  }

  *value = (uint16_t)parsed;
  return true;
}

void processPidGainCommand(char *text, char gainName, float maxValue) {
  float value;
  if (!parseNonNegativeFloat(text, &value) || value > maxValue) {
    Serial.println("ERR PID gain out of range");
    return;
  }

  markValidSerialCommand();
  setPidGain(gainName, value);
  printStatus();
}

bool parseNonNegativeFloat(char *text, float *value) {
  if (text[0] == '\0') {
    return false;
  }

  bool seenDigit = false;
  bool seenDot = false;

  for (uint8_t i = 0; text[i] != '\0'; i++) {
    if (text[i] >= '0' && text[i] <= '9') {
      seenDigit = true;
    } else if (text[i] == '.' && !seenDot) {
      seenDot = true;
    } else {
      return false;
    }
  }

  if (!seenDigit) {
    return false;
  }

  *value = atof(text);
  return true;
}

void setTargetRpm(uint16_t value) {
  openLoopMode = false;
  targetRpm = value;
  pidIntegral = 0.0f;
  pidSetpointRpm = rpmMeasured;
  const float controlRpm = maxFloat(rpmMeasured, rpmRaw);
  lastError = pidSetpointRpm - controlRpm;
  resetControlDerivative(controlRpm);
  pidOutputHold = false;
  pidHoldBandSinceMs = 0;

  if (targetRpm == 0) {
    pidOutput = 0.0f;
    pidSetpointRpm = 0.0f;
    applyPwmStrength(0);
  } else {
    const float prechargeOutput = feedforwardForRpm((float)targetRpm) * FEEDFORWARD_PRECHARGE_RATIO;
    if (prechargeOutput > pidOutput) {
      pidOutput = constrainFloat(prechargeOutput, 0.0f, 100.0f);
      applyPwmStrength((uint32_t)(pidOutput * 1000.0f + 0.5f));
    }
  }
}

void setOpenLoopOutput(float value) {
  targetRpm = 0;
  pidIntegral = 0.0f;
  lastError = 0.0f;
  pidSetpointRpm = 0.0f;
  resetControlDerivative(0.0f);
  pidOutputHold = false;
  pidHoldBandSinceMs = 0;

  if (value <= 0.0f) {
    openLoopMode = false;
    pidOutput = 0.0f;
    applyPwmStrength(0);
    return;
  }

  openLoopMode = true;
  pidOutput = constrainFloat(value, 0.0f, 100.0f);
  applyPwmStrength((uint32_t)(pidOutput * 1000.0f + 0.5f));
}

void setPidGain(char gainName, float value) {
  if (gainName == 'P') {
    pidKp = value;
  } else if (gainName == 'I') {
    pidKi = value;
  } else if (gainName == 'D') {
    pidKd = value;
  }

  pidIntegral = 0.0f;
  const float controlRpm = maxFloat(rpmMeasured, rpmRaw);
  lastError = pidSetpointRpm - controlRpm;
  resetControlDerivative(controlRpm);
  pidOutputHold = false;
  pidHoldBandSinceMs = 0;
}

void applyPwmStrength(uint32_t value) {
  const uint32_t nextStrengthMilliPercent = constrainStrength(value);

  if (nextStrengthMilliPercent == 0) {
    pwmStrengthMilliPercent = 0;
    stopMotor();
    return;
  }

  const uint16_t nextPulseTicks = pulseTicksForStrength(nextStrengthMilliPercent);
  const uint32_t nextDutyPpm = dutyPpmForStrength(nextStrengthMilliPercent);
  const uint16_t nextPeriodTicks = periodTicksForDuty(nextPulseTicks, nextDutyPpm);
  const bool timingChanged = nextPulseTicks != configuredPulseTicks || nextPeriodTicks != configuredPeriodTicks;

  pwmStrengthMilliPercent = nextStrengthMilliPercent;
  configuredPulseTicks = nextPulseTicks;
  configuredPulseNs = ticksToNanoseconds(nextPulseTicks);
  configuredDutyPpm = nextDutyPpm;
  configuredPeriodTicks = nextPeriodTicks;
  configuredPeriodUs = ticksToMicroseconds(nextPeriodTicks);
  configuredRestUs = ticksToMicroseconds(nextPeriodTicks - nextPulseTicks);

  if (!pwmRunning) {
    startFastPwm(nextPulseTicks, nextPeriodTicks);
  } else if (timingChanged) {
    updateFastPwm(nextPulseTicks, nextPeriodTicks);
  }
}

uint16_t pulseTicksForStrength(uint32_t value) {
  if (value <= 1000UL) {
    return MIN_PULSE_TICKS;
  }

  if (value >= STRENGTH_SCALE) {
    return MAX_PULSE_TICKS;
  }

  return pulseTicksForDuty(dutyPpmForStrength(value));
}

uint16_t pulseTicksForDuty(uint32_t dutyPpm) {
  const uint64_t numerator = (uint64_t)NOMINAL_PWM_PERIOD_NS * dutyPpm * 2ULL;
  const uint64_t denominator = 1000000ULL * 125ULL;
  uint16_t ticks = (uint16_t)((numerator + denominator - 1ULL) / denominator);

  if (ticks < MIN_PULSE_TICKS) {
    return MIN_PULSE_TICKS;
  }

  if (ticks > MAX_PULSE_TICKS) {
    return MAX_PULSE_TICKS;
  }

  return ticks;
}

uint32_t dutyPpmForStrength(uint32_t value) {
  if (value == 0) {
    return 0;
  }

  if (value <= 1000UL) {
    return MIN_EFFECTIVE_DUTY_PPM;
  }

  if (value >= STRENGTH_SCALE) {
    return MAX_EFFECTIVE_DUTY_PPM;
  }

  return MIN_EFFECTIVE_DUTY_PPM +
    (uint32_t)(((uint64_t)(MAX_EFFECTIVE_DUTY_PPM - MIN_EFFECTIVE_DUTY_PPM) * (value - 1000UL)) / 99000ULL);
}

uint16_t periodTicksForDuty(uint16_t pulseTicks, uint32_t dutyPpm) {
  if (dutyPpm == 0) {
    return pulseTicks + 1;
  }

  uint32_t periodTicks = (uint32_t)((((uint64_t)pulseTicks * 1000000ULL) + dutyPpm - 1ULL) / dutyPpm);

  if (periodTicks <= pulseTicks) {
    periodTicks = pulseTicks + 1;
  }

  if (periodTicks > 65535UL) {
    return 65535;
  }

  return (uint16_t)periodTicks;
}

uint32_t ticksToNanoseconds(uint16_t ticks) {
  return ((uint32_t)ticks * 125UL + 1UL) / 2UL;
}

uint32_t ticksToMicroseconds(uint16_t ticks) {
  return (((uint32_t)ticks * 125UL) + 1999UL) / 2000UL;
}

uint32_t constrainStrength(uint32_t value) {
  if (value > STRENGTH_SCALE) {
    return STRENGTH_SCALE;
  }

  return value;
}

void printMilliPercent(uint32_t value) {
  Serial.print(value / 1000UL);
  Serial.print('.');

  const uint16_t fractional = (uint16_t)(value % 1000UL);
  if (fractional < 100U) {
    Serial.print('0');
  }
  if (fractional < 10U) {
    Serial.print('0');
  }
  Serial.print(fractional);
}

void printRoundedRpm(float rpm) {
  if (rpm <= 0.0f) {
    Serial.print(0);
    return;
  }

  Serial.print((uint32_t)(rpm + 0.5f));
}

void startFastPwm(uint16_t highTicks, uint16_t periodTicks) {
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    TCCR1A = 0;
    TCCR1B = 0;
    TCNT1 = 0;
    ICR1 = periodTicks - 1U;
    OCR1A = highTicks;
    TIFR1 = _BV(TOV1) | _BV(OCF1A);
    TIMSK1 &= ~_BV(TOIE1);
    TCCR1A = _BV(COM1A1) | _BV(WGM11);
    TCCR1B = _BV(WGM13) | _BV(WGM12) | _BV(CS10);
    pwmRunning = true;
  }
}

void updateFastPwm(uint16_t highTicks, uint16_t periodTicks) {
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    const uint16_t topTicks = periodTicks - 1U;

    if (TCNT1 >= topTicks) {
      TCNT1 = 0;
      TIFR1 = _BV(TOV1) | _BV(OCF1A);
    }

    ICR1 = topTicks;
    OCR1A = highTicks;
  }
}

void stopMotor() {
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    TCCR1A = 0;
    TCCR1B = 0;
    TIMSK1 &= ~_BV(TOIE1);
    PORTB &= ~_BV(PB1);
    pwmRunning = false;
  }
}

void handleEncoderChange() {
  const uint8_t currentState = readEncoderState();
  const uint8_t transition = (lastEncoderState << 2) | currentState;

  static const int8_t quadratureDelta[16] = {
    0, -1, 1, 0,
    1, 0, 0, -1,
    -1, 0, 0, 1,
    0, 1, -1, 0
  };

  encoderCount += quadratureDelta[transition];
  lastEncoderState = currentState;
}

uint8_t readEncoderState() {
  const uint8_t portValue = PIND;
  const uint8_t channelA = (portValue & _BV(PD2)) ? 1 : 0;
  const uint8_t channelB = (portValue & _BV(PD3)) ? 1 : 0;

  return (channelA << 1) | channelB;
}

float constrainFloat(float value, float low, float high) {
  if (value < low) {
    return low;
  }

  if (value > high) {
    return high;
  }

  return value;
}

float absFloat(float value) {
  return value < 0.0f ? -value : value;
}

float maxFloat(float a, float b) {
  return a > b ? a : b;
}

void printPeriodicStatus() {
  const unsigned long now = millis();
  if (now - lastStatusMs < statusIntervalMs) {
    return;
  }

  lastStatusMs = now;
  printStatus();
}

void printStatus() {
  // t_ms: controller clock when this line was printed; rpm_t_ms: clock of the
  // last RPM calculation (RPM values only change every RPM_SAMPLE_INTERVAL_MS).
  Serial.print("OK t_ms=");
  Serial.print(millis());
  Serial.print(" rpm_t_ms=");
  Serial.print(lastRpmSampleMs);
  Serial.print(" target_rpm=");
  Serial.print(targetRpm);
  Serial.print(" setpoint_rpm=");
  printRoundedRpm(pidSetpointRpm);
  Serial.print(" measured_rpm=");
  printRoundedRpm(rpmMeasured);
  Serial.print(" raw_rpm=");
  printRoundedRpm(rpmRaw);
  Serial.print(" encoder_rpm=");
  printRoundedRpm(rpmEncoder);
  Serial.print(" output=");
  Serial.print(pidOutput, 2);
  Serial.print(" hold=");
  Serial.print(pidOutputHold ? 1 : 0);
  Serial.print(" open_loop=");
  Serial.print(openLoopMode ? 1 : 0);
  Serial.print(" kp=");
  Serial.print(pidKp, 4);
  Serial.print(" ki=");
  Serial.print(pidKi, 4);
  Serial.print(" kd=");
  Serial.print(pidKd, 4);
  Serial.print(" d_rpm_s=");
  Serial.print(filteredControlDerivative, 2);
  Serial.print(" ff=");
  Serial.print(controlFeedforwardForRpm((float)targetRpm), 2);
  Serial.print(" strength=");
  printMilliPercent(pwmStrengthMilliPercent);
  Serial.print(" status_ms=");
  Serial.print(statusIntervalMs);
  Serial.print(" calib_points=");
  Serial.print(RPM_CALIB_POINT_COUNT);
  Serial.print(" pulse_ticks=");
  Serial.print(configuredPulseTicks);
  Serial.print(" period_ticks=");
  Serial.print(configuredPeriodTicks);
  Serial.print(" pulse_ns=");
  Serial.print(configuredPulseNs);
  Serial.print(" rest_us=");
  Serial.print(configuredRestUs);
  Serial.print(" period_us=");
  Serial.print(configuredPeriodUs);
  Serial.print(" duty_ppm=");
  Serial.print(configuredDutyPpm);
  Serial.print(" max_duty_ppm=");
  Serial.println(MAX_EFFECTIVE_DUTY_PPM);
}
