#include <WiFi.h>
#include <WebSocketsClient.h>   // "WebSockets" by Markus Sattler

// ---------- EDIT THESE 3 LINES ----------
const char* ssid = "WIFI_NAME";        // Wi-Fi / hotspot name (2.4 GHz)
const char* pass = "WIFI_PASSWORD";
const char* host = "192.168.1.10";     // IPv4 of the PC running the app (ipconfig)
// ----------------------------------------
const uint16_t port = 3000;

// Lane 0..3 = D, F, J, K
const int pins[4] = {13, 12, 14, 27};  // button -> GND (INPUT_PULLUP)

bool last[4] = {HIGH, HIGH, HIGH, HIGH};
unsigned long lastTime[4] = {0, 0, 0, 0};
WebSocketsClient ws;

void onWsEvent(WStype_t type, uint8_t* payload, size_t length) {
  if (type == WStype_CONNECTED)    Serial.println("[WS] Connected to server");
  if (type == WStype_DISCONNECTED) Serial.println("[WS] Disconnected (retrying...)");
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < 4; i++) pinMode(pins[i], INPUT_PULLUP);

  WiFi.begin(ssid, pass);
  Serial.print("Connecting WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.println("\nWiFi connected. ESP32 IP: " + WiFi.localIP().toString());

  ws.begin(host, port, "/");
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(2000);
}

void loop() {
  ws.loop();

  for (int i = 0; i < 4; i++) {
    bool s = digitalRead(pins[i]);                       // LOW = pressed
    if (s != last[i] && millis() - lastTime[i] > 60) {   // 60 ms debounce
      lastTime[i] = millis();
      last[i] = s;

      String msg = String("{\"type\":\"") + (s == LOW ? "down" : "up") +
                   "\",\"lane\":" + String(i) + "}";
      ws.sendTXT(msg);
      Serial.println(msg);
    }
  }
}
