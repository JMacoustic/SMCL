#include <Arduino.h>
#line 1 "C:\\Users\\juhye\\Desktop\\CurrentFiles\\2025project\\impeller\\encoder\\encoder.ino"
/*
  Custom disk encoder RPM calibration reader for Arduino UNO.

  Pin plan:
    Encoder channel A -> D2 / INT0
    Encoder channel B -> D3 / INT1

  The sketch prints CSV at 115200 baud:
    count,delta,counts_per_second,rpm_raw,rpm_smooth,A,B
*/

#include <util/atomic.h>

const uint8_t ENCODER_A_PIN = 2;
const uint8_t ENCODER_B_PIN = 3;

const unsigned long SAMPLE_INTERVAL_MS = 500UL;
const unsigned long RPM_CALC_WINDOW_MS = 3000UL;
const float COUNTS_PER_REVOLUTION = 500.0f;  // 20-slot quadrature disk * 4 transitions.
const uint8_t RPM_HISTORY_SIZE = 8;
const float RPM_EMA_ALPHA = 0.25f;

volatile long encoderCount = 0;
volatile uint8_t lastEncoderState = 0;

unsigned long lastSampleMs = 0;

unsigned long countSampleTimes[RPM_HISTORY_SIZE];
long countSamples[RPM_HISTORY_SIZE];
uint8_t countSampleIndex = 0;
uint8_t countSampleCount = 0;

float rpmSmooth = 0.0f;
bool rpmSmoothReady = false;

#line 36 "C:\\Users\\juhye\\Desktop\\CurrentFiles\\2025project\\impeller\\encoder\\encoder.ino"
void setup();
#line 60 "C:\\Users\\juhye\\Desktop\\CurrentFiles\\2025project\\impeller\\encoder\\encoder.ino"
void loop();
#line 109 "C:\\Users\\juhye\\Desktop\\CurrentFiles\\2025project\\impeller\\encoder\\encoder.ino"
void recordCountSample(unsigned long sampleMs, long count);
#line 119 "C:\\Users\\juhye\\Desktop\\CurrentFiles\\2025project\\impeller\\encoder\\encoder.ino"
void calculateWindowDelta(unsigned long now, long currentCount, unsigned long *elapsedMs, long *delta);
#line 150 "C:\\Users\\juhye\\Desktop\\CurrentFiles\\2025project\\impeller\\encoder\\encoder.ino"
float smoothRpm(float rpm);
#line 159 "C:\\Users\\juhye\\Desktop\\CurrentFiles\\2025project\\impeller\\encoder\\encoder.ino"
uint32_t roundPositiveFloat(float value);
#line 167 "C:\\Users\\juhye\\Desktop\\CurrentFiles\\2025project\\impeller\\encoder\\encoder.ino"
void handleEncoderChange();
#line 182 "C:\\Users\\juhye\\Desktop\\CurrentFiles\\2025project\\impeller\\encoder\\encoder.ino"
uint8_t readEncoderState();
#line 36 "C:\\Users\\juhye\\Desktop\\CurrentFiles\\2025project\\impeller\\encoder\\encoder.ino"
void setup() {
  pinMode(ENCODER_A_PIN, INPUT_PULLUP);
  pinMode(ENCODER_B_PIN, INPUT_PULLUP);

  Serial.begin(115200);
  while (!Serial) {
    ;  // UNO continues immediately; native USB boards wait for serial.
  }

  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    lastEncoderState = readEncoderState();
  }

  attachInterrupt(digitalPinToInterrupt(ENCODER_A_PIN), handleEncoderChange, CHANGE);
  attachInterrupt(digitalPinToInterrupt(ENCODER_B_PIN), handleEncoderChange, CHANGE);

  lastSampleMs = millis();
  recordCountSample(lastSampleMs, 0);

  Serial.println("Encoder RPM calibration reader ready.");
  Serial.println("Wiring: A->D2, B->D3, +->5V, -->GND");
  Serial.println("count,delta,counts_per_second,rpm_raw,rpm_smooth,A,B");
}

void loop() {
  const unsigned long now = millis();

  if (now - lastSampleMs < SAMPLE_INTERVAL_MS) {
    return;
  }

  long countSnapshot;
  uint8_t stateSnapshot;

  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    countSnapshot = encoderCount;
    stateSnapshot = lastEncoderState;
  }

  recordCountSample(now, countSnapshot);

  unsigned long elapsedMs = 0;
  long delta = 0;
  calculateWindowDelta(now, countSnapshot, &elapsedMs, &delta);

  const float countsPerSecond = elapsedMs > 0 ? (delta * 1000.0f) / elapsedMs : 0.0f;
  float rpmRaw = (countsPerSecond * 60.0f) / COUNTS_PER_REVOLUTION;

  if (rpmRaw < 0.0f) {
    rpmRaw = -rpmRaw;
  }

  rpmSmooth = smoothRpm(rpmRaw);
  const uint32_t rpmRawRounded = roundPositiveFloat(rpmRaw);
  const uint32_t rpmSmoothRounded = roundPositiveFloat(rpmSmooth);

  Serial.print(countSnapshot);
  Serial.print(',');
  Serial.print(delta);
  Serial.print(',');
  Serial.print(countsPerSecond, 2);
  Serial.print(',');
  Serial.print(rpmRawRounded);
  Serial.print(',');
  Serial.print(rpmSmoothRounded);
  Serial.print(',');
  Serial.print((stateSnapshot >> 1) & 0x01);
  Serial.print(',');
  Serial.println(stateSnapshot & 0x01);

  lastSampleMs = now;
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
  if (!rpmSmoothReady) {
    rpmSmoothReady = true;
    return rpm;
  }

  return rpmSmooth + (RPM_EMA_ALPHA * (rpm - rpmSmooth));
}

uint32_t roundPositiveFloat(float value) {
  if (value <= 0.0f) {
    return 0;
  }

  return (uint32_t)(value + 0.5f);
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

