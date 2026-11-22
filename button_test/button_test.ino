// GUITAR-BEAT: 4-button test (Serial Monitor only, no Wi-Fi)
// Wiring: GPIO 13, 12, 14, 27 -> button -> GND (INPUT_PULLUP, no resistor)

const int NUM_BUTTONS = 4;
const int pins[NUM_BUTTONS]    = {13, 12, 14, 27};
const char* names[NUM_BUTTONS] = {"D", "F", "J", "K"};

bool lastState[NUM_BUTTONS]           = {HIGH, HIGH, HIGH, HIGH};
unsigned long lastChange[NUM_BUTTONS] = {0, 0, 0, 0};
unsigned long pressStart[NUM_BUTTONS] = {0, 0, 0, 0};
unsigned long pressCount[NUM_BUTTONS] = {0, 0, 0, 0};

const unsigned long DEBOUNCE_MS = 20;

void printAll() {
  Serial.print("   Now held -> ");
  for (int i = 0; i < NUM_BUTTONS; i++) {
    Serial.print(names[i]);
    Serial.print(":");
    Serial.print(lastState[i] == LOW ? 1 : 0);
    Serial.print("  ");
  }
  Serial.println();
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < NUM_BUTTONS; i++) pinMode(pins[i], INPUT_PULLUP);
  Serial.println("\n=== GUITAR-BEAT button test ===");
  Serial.println("Press each button. 1 = pressed, 0 = released.");
}

void loop() {
  for (int i = 0; i < NUM_BUTTONS; i++) {          // each button is checked independently
    bool now = digitalRead(pins[i]);
    if (now != lastState[i] && millis() - lastChange[i] > DEBOUNCE_MS) {
      lastChange[i] = millis();
      lastState[i] = now;

      if (now == LOW) {                             // pressed
        pressStart[i] = millis();
        pressCount[i]++;
        Serial.print("Button ");  Serial.print(i + 1);
        Serial.print(" (");       Serial.print(names[i]);
        Serial.print(", GPIO ");  Serial.print(pins[i]);
        Serial.print(") PRESSED  | total presses: ");
        Serial.println(pressCount[i]);
      } else {                                      // released
        Serial.print("Button ");  Serial.print(i + 1);
        Serial.print(" (");       Serial.print(names[i]);
        Serial.print(") RELEASED | held for ");
        Serial.print(millis() - pressStart[i]);
        Serial.println(" ms");
      }
      printAll();
    }
  }
}
