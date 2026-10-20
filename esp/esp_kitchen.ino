// ============================================================
// KumaFlow ESP-колонка (S3-Zero + PCM5100A + PAM8403)
// MVP: Wave -> Navidrome stream -> I2S
// Библиотеки (Library Manager):
//   - ESP32-audioI2S by schreibfaul1 (Audio.h)
//   - ArduinoJson by Benoit Blanchon (v7)
// Ядро: esp32 by Espressif 3.x, плата ESP32S3 Dev Module
// Настройки платы: USB CDC On Boot: Enabled, PSRAM: OPI PSRAM
// Подробности: см. esp/README.md (что к чему паять)
// ============================================================
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <MD5Builder.h>
#include "Audio.h"

// ---------- 1. ВСТАВЬ СВОЁ: WiFi ----------
const char* WIFI_SSID = "___SSID___";
const char* WIFI_PASS = "___PASS___";

// ---------- 2. ВСТАВЬ СВОЁ: Brain (https через KeenDNS + токен) ----------
// Пример: "https://brain.xxxxx.keenetic.pro"  (БЕЗ слэша в конце)
const char* BRAIN_URL = "___https://BRAIN-DOMAIN___";
// Токен из веб: Настройки -> Токены (скоуп wave). Если auth выкл — оставь "".
const char* BRAIN_TOKEN = "___BRAIN_TOKEN___";
// user_id мозга: uuid из POST /api/users/by-credentials (один раз создать).
const char* USER_ID = "___USER_UUID___";
// Стабильное имя слота колонки (латиница, цифры, - _). Придумал раз — не меняй.
const char* DEVICE_ID = "esp_kitchen";

// ---------- 3. ВСТАВЬ СВОЁ: Navidrome (https через KeenDNS + логин/пароль) ----------
// Пример: "https://music.xxxxx.keenetic.pro" (БЕЗ /rest, БЕЗ слэша в конце)
const char* NAVI_URL = "___https://NAVI-DOMAIN___";
const char* NAVI_USER = "___NAVI_LOGIN___";
const char* NAVI_PASS = "___NAVI_PASSWORD___";

// ---------- 4. Железо (у тебя так, не трогай) ----------
// PCM5100A: BCK->7, DIN->9, LRCK->8, SCK->GND, VCC->3V3 (см. README.md)
#define I2S_BCLK 7
#define I2S_LRCK 8
#define I2S_DOUT 9
// true = только логи, без звука (первый прогон на голой S3).
// false = играть в PCM5100A.
#define DEBUG_NO_AUDIO true

Audio audio;
String queueExt[20];
int queueLen = 0;
int queuePos = 0;

void setInsecure(WiFiClientSecure &c) { c.setInsecure(); }

String md5hex(const String &s) {
  MD5Builder md5;
  md5.begin();
  md5.add(s);
  md5.calculate();
  return md5.toString();
}

// Subsonic auth: t = md5(password + salt)
String streamUrlFor(const String &songId) {
  String salt = String((uint32_t)esp_random(), HEX);
  String token = md5hex(String(NAVI_PASS) + salt);
  String url = String(NAVI_URL) + "/rest/stream.view?u=" + String(NAVI_USER)
    + "&s=" + salt + "&t=" + token
    + "&v=1.16.1&c=esp-kitchen&f=json&id=" + songId;
  return url;
}

bool brainPOST(const String &path, const String &body, String &out) {
  WiFiClientSecure cli;
  setInsecure(cli);
  HTTPClient http;
  String url = String(BRAIN_URL) + path;
  http.begin(cli, url);
  http.addHeader("Content-Type", "application/json");
  if (strlen(BRAIN_TOKEN) > 8) http.addHeader("Authorization", String("Bearer ") + BRAIN_TOKEN);
  http.setTimeout(15000);
  int code = http.POST(body);
  out = http.getString();
  Serial.printf("[BRAIN] POST %s -> %d\n%s\n", path.c_str(), code, out.substring(0, 500).c_str());
  http.end();
  return (code >= 200 && code < 300);
}

bool waveContinue() {
  JsonDocument doc;
  doc["user_id"] = USER_ID;
  JsonArray q = doc["queue"].to<JsonArray>();
  for (int i = 0; i < queueLen; i++) q.add(queueExt[i]);
  doc["count"] = 20;
  String body;
  serializeJson(doc, body);

  String resp;
  if (!brainPOST("/api/wave/continue", body, resp)) return false;

  JsonDocument r;
  if (deserializeJson(r, resp)) { Serial.println("JSON parse fail"); return false; }
  JsonArray tracks = r["tracks"].as<JsonArray>();
  if (tracks.size() == 0) { Serial.println("wave пустая"); return false; }

  queueLen = 0; queuePos = 0;
  for (JsonObject t : tracks) {
    if (queueLen >= 20) break;
    // external_id = Navidrome song id (играбельный), track_id = uuid мозга
    String ext = t["external_id"] | t["track_id"] | "";
    if (ext.length()) queueExt[queueLen++] = ext;
    Serial.printf("  + %s - %s [%s]\n",
      (t["artist_name"] | "?").as<const char*>(),
      (t["title"] | "?").as<const char*>(),
      ext.c_str());
  }
  return queueLen > 0;
}

void wavePublish(const String &currentExt) {
  JsonDocument doc;
  doc["user_id"] = USER_ID;
  doc["device"] = DEVICE_ID;
  JsonArray q = doc["queue"].to<JsonArray>();
  for (int i = 0; i < queueLen; i++) q.add(queueExt[i]);
  doc["current_track_id"] = currentExt;
  String body; serializeJson(doc, body);
  String out;
  brainPOST("/api/wave/publish", body, out);
}

void waveEvent(const String &extId, const char* action) {
  String path = String("/api/users/") + USER_ID + "/events";
  JsonDocument doc;
  JsonArray ev = doc["events"].to<JsonArray>();
  JsonObject e = ev.add<JsonObject>();
  e["track_id"] = extId;
  e["action"] = action;
  String body; serializeJson(doc, body);
  String out;
  brainPOST(path, body, out);
}

void playCurrent() {
  if (queuePos >= queueLen) return;
  String ext = queueExt[queuePos];
  String url = streamUrlFor(ext);
  Serial.println("[PLAY] " + ext);
  Serial.println("       " + url.substring(0, 160) + "...");
  wavePublish(ext);
  waveEvent(ext, "play");
#if !DEBUG_NO_AUDIO
  audio.stopSong();
  audio.connecttohost(url.c_str());
#else
  Serial.println("(DEBUG_NO_AUDIO=true — звук пропущен, только логи)");
#endif
}

void setup() {
  Serial.begin(115200);
  delay(800);
  Serial.println("\n=== KumaFlow ESP start ===");
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi...");
  for (int i = 0; i < 60 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print("."); }
  Serial.println(WiFi.status() == WL_CONNECTED ? "\nWiFi OK: " + WiFi.localIP().toString() : "\nWiFi FAIL");

  // Время нужно для HTTPS (KeenDNS + LetsEncrypt), иначе TLS не встанет
  configTzTime("MSK-3", "pool.ntp.org", "time.google.com");
  Serial.print("NTP...");
  for (int i = 0; i < 20; i++) { time_t t = time(nullptr); if (t > 1700000000) break; delay(500); Serial.print("."); }
  Serial.println(" done");

#if !DEBUG_NO_AUDIO
  audio.setPinout(I2S_LRCK, I2S_BCLK, I2S_DOUT);
  audio.setVolume(5); // 0..21, стартуем тихо! PAM без крутилки
#endif

  if (waveContinue()) playCurrent();
  else Serial.println("waveContinue не дал треков — проверь USER_ID/токен/BRAIN_URL");
}

void loop() {
#if !DEBUG_NO_AUDIO
  audio.loop();
  if (!audio.isRunning()) {
    // трек кончился (или не стартовал) — complete + следующий
    if (queuePos < queueLen) waveEvent(queueExt[queuePos], "complete");
    queuePos++;
    if (queueLen - queuePos <= 2) waveContinue();  // рефилл
    else playCurrent();
    delay(500);
  }
#else
  // Без аудио: просто держим очередь живой, чтобы веб /wave видел слот
  static unsigned long last = 0;
  if (millis() - last > 30000 && queueLen > 0) {
    last = millis();
    wavePublish(queueExt[queuePos]);
    Serial.println("[DEBUG] publish heartbeat ok, слот жив 10 мин (TTL)");
  }
  delay(1000);
#endif
}

// Кнопка Next (потом): вызови nextTrack() по клику
void nextTrack() {
  if (queuePos < queueLen) waveEvent(queueExt[queuePos], "skip");
  queuePos++;
  if (queuePos >= queueLen) waveContinue();
  else playCurrent();
}
