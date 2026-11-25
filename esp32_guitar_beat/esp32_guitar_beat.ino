#include <WiFi.h>
#include <WebSocketsClient.h>   // "WebSockets" by Markus Sattler

// ---- EDIT THESE ----
const char* ssid = "DITO_E9043_2.4";
const char* pass = "Ab002958";
const char* host = "192.168.8.125";   // IP of the PC running server.js (use ipconfig)
const uint16_t port = 3000;
// --------------------

const int pins[4]    = {13, 12, 14, 27};   // buttons -> GND (INPUT_PULLUP)
const int ledPins[4] = {26, 25, 33, 32};   // optional LEDs (with 220 ohm resistor)
const int buzzerPin  = 15;                 // optional buzzer

bool last[4] = {HIGH, HIGH, HIGH, HIGH};
unsigned long lastTime[4] = {0, 0, 0, 0};
WebSocketsClient ws;

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < 4; i++) {
    pinMode(pins[i], INPUT_PULLUP);
    pinMode(ledPins[i], OUTPUT);
  }
  pinMode(buzzerPin, OUTPUT);

  WiFi.begin(ssid, pass);
  Serial.print("Connecting WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.println("\nConnected: " + WiFi.localIP().toString());

  ws.begin(host, port, "/");
  ws.setReconnectInterval(2000);
}

void loop() {
  ws.loop();
  for (int i = 0; i < 4; i++) {
    bool s = digitalRead(pins[i]);
    if (s != last[i] && millis() - lastTime[i] > 20) {   // debounce
      lastTime[i] = millis();
      last[i] = s;
      digitalWrite(ledPins[i], s == LOW);
      if (s == LOW) {
        ws.sendTXT("{\"type\":\"down\",\"lane\":" + String(i) + "}");
        tone(buzzerPin, 400 + i * 150, 60);
      } else {
        ws.sendTXT("{\"type\":\"up\",\"lane\":" + String(i) + "}");
      }
    }
  }
}
