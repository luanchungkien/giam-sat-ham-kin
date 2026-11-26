#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <time.h>
#include <Update.h>
#include <Preferences.h>
#include <ModbusMaster.h>

// ===== Chân kết nối (phải khớp với dây thực tế) =====
constexpr uint8_t MQ2_PIN       = 35;   // ADC1, chỉ đầu vào
constexpr uint8_t DUST_VO_PIN   = 34;   // ADC1, chỉ đầu vào
constexpr uint8_t DUST_LED_PIN  = 25;
constexpr uint8_t RS485_RO_PIN  = 16;   // RX2
constexpr uint8_t RS485_DI_PIN  = 17;   // TX2
constexpr uint8_t RS485_DE_RE   = 13;
constexpr uint8_t ALARM_LED_PIN = 26;   // LED cảnh báo (qua điện trở 220 ohm xuống GND)

// ===== Mạch chia áp 10k (trên) + 20k (dưới) =====
constexpr float DIVIDER_RATIO = 1.0f;

// ===== Cảm biến bụi GP2Y1010AU0F =====
constexpr uint32_t DUST_SAMPLE_AT_US = 280;
constexpr uint32_t DUST_PULSE_US     = 320;
constexpr uint32_t DUST_PERIOD_US    = 10000;
constexpr uint8_t  DUST_SAMPLES      = 20;     // tối đa 64
constexpr float    DUST_V0_DEFAULT   = 0.9f;
constexpr float    DUST_UG_PER_V     = 200.0f; // 0.5V ứng với 0.1 mg/m3
constexpr bool     DUST_AUTO_CALIB   = true;
constexpr float    DUST_WARN_UG      = 500.0f;
constexpr float    DUST_SPREAD_WARN  = 0.30f;

// ===== MQ-2 =====
constexpr float    MQ2_WARN_MV       = 1800.0f;
constexpr float    MQ2_SPREAD_WARN   = 300.0f;
constexpr uint32_t MQ2_WARMUP_MS     = 60000;

// ===== RS485 / Modbus (KTH2-R) =====
constexpr uint32_t MODBUS_BAUD   = 9600;
constexpr uint8_t  MODBUS_ID     = 1;
constexpr uint8_t  MODBUS_RETRY  = 2;
constexpr uint16_t REG_TH_START  = 0x0300;   // 0x0300 nhiệt độ, 0x0301 độ ẩm
constexpr float    TEMP_SCALE    = 0.1f;
constexpr float    HUM_SCALE     = 0.1f;
constexpr float    HUM_MIN_VALID = 0.0f;

// ===== Chu kỳ =====
constexpr uint32_t LOOP_MS           = 2000;    // chu kỳ đo
constexpr uint32_t LATEST_PERIOD_MS  = 10000;   // ghi /latest
constexpr uint32_t HISTORY_PERIOD_MS = 60000;   // ghi /history (trung bình 1 phút)

// ===== WiFi / Firebase =====
constexpr uint32_t WIFI_CONNECT_TIMEOUT_MS = 15000;
constexpr uint32_t WIFI_RETRY_MS           = 20000;
constexpr uint32_t AUTH_RETRY_MS           = 30000;
constexpr time_t   TIME_VALID_AFTER        = 1700000000;   // epoch hợp lệ (sau tháng 11/2023)

// ===== Cập nhật firmware qua mạng (OTA) =====
#define FW_VERSION "1.0.0"   // ĐỔI số này mỗi khi phát hành bản mới
constexpr const char *OTA_MANIFEST_URL = "https://luanchungkien.github.io/giam-sat-ham-kin/firmware/version.json";
constexpr uint32_t OTA_CHECK_MS = 6UL * 3600UL * 1000UL;   // kiểm tra mỗi 6 giờ
uint32_t lastOtaCheck = 0;
bool otaBootChecked = false;
bool otaForce = false;

// ===== Cấu hình lưu trong bộ nhớ NVS của thiết bị (không nằm trong file firmware) =====
String cfgSsid, cfgWifiPass, cfgApiKey, cfgDbUrl, cfgEmail, cfgPass;
Preferences prefs;

ModbusMaster node;
float dustV0 = DUST_V0_DEFAULT;

void preTransmission()  { digitalWrite(RS485_DE_RE, HIGH); }
void postTransmission() { digitalWrite(RS485_DE_RE, LOW); }

// ===== Bộ cộng dồn để lấy trung bình mỗi phút =====
struct Acc {
  float sum = 0;
  uint16_t n = 0;
  void add(float v) { if (!isnan(v)) { sum += v; n++; } }
  float avg() const { return n ? sum / n : NAN; }
  void reset() { sum = 0; n = 0; }
};
Acc accTemp, accHum, accMq2, accDust;
bool histAlarm = false;
bool histWarmup = false;

// ===== Mẫu đo mới nhất =====
float gTemp = NAN, gHum = NAN, gMq2 = NAN, gDust = NAN;
bool gSmoke = false, gDustAlarm = false, gDustNoise = false, gMq2Noise = false;
bool gModbusOk = false, gWarmup = true;

// ===== Trạng thái Firebase =====
String idToken, refreshToken;
uint32_t tokenExpireAt = 0;
uint32_t authRetryAt = 0;
bool authBackoff = false;
bool ntpStarted = false;
uint32_t lastLatest = 0, lastHistory = 0, lastWifiTry = 0;

// ================= Cảm biến =================

static float trimmedMean(uint16_t *a, uint8_t n, uint8_t trim) {
  for (uint8_t i = 1; i < n; i++) {
    uint16_t k = a[i];
    int j = i - 1;
    while (j >= 0 && a[j] > k) { a[j + 1] = a[j]; j--; }
    a[j + 1] = k;
  }
  uint32_t s = 0;
  for (uint8_t i = trim; i < n - trim; i++) s += a[i];
  return (float)s / (n - 2 * trim);
}

static float readDustVolts(uint8_t n, float *spread = nullptr) {
  uint16_t mv[64];
  if (n > 64) n = 64;
  for (uint8_t i = 0; i < n; i++) {
    uint32_t t0 = micros();
    digitalWrite(DUST_LED_PIN, LOW);                 // LOW = bật LED hồng ngoại
    while (micros() - t0 < DUST_SAMPLE_AT_US) {}
    mv[i] = analogReadMilliVolts(DUST_VO_PIN);
    while (micros() - t0 < DUST_PULSE_US) {}
    digitalWrite(DUST_LED_PIN, HIGH);                // HIGH = tắt LED
    delayMicroseconds(DUST_PERIOD_US - DUST_PULSE_US);
  }
  float m = trimmedMean(mv, n, n / 5);
  if (spread) *spread = (mv[n - 1] - mv[0]) / 1000.0f * DIVIDER_RATIO;
  if (m < 10.0f) return NAN;
  return m / 1000.0f * DIVIDER_RATIO;
}

static float readMQ2_mV(float *spread = nullptr) {
  uint32_t s = 0;
  uint16_t lo = 65535, hi = 0;
  for (uint8_t i = 0; i < 16; i++) {
    uint16_t v = analogReadMilliVolts(MQ2_PIN);
    s += v;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
    delayMicroseconds(200);
  }
  if (spread) *spread = (hi - lo) * DIVIDER_RATIO;
  return s / 16.0f * DIVIDER_RATIO;
}

struct HumTemp {
  uint8_t err = 0xE2;
  bool tempOk = false, humOk = false;
  float temp = NAN, hum = NAN;
  int16_t humRaw = 0;
};
HumTemp ht;

static void readHumTemp() {
  uint8_t lastErr = 0xE2;
  ht = HumTemp();
  for (uint8_t i = 0; i < MODBUS_RETRY; i++) {
    while (Serial2.available()) Serial2.read();
    uint8_t r = node.readHoldingRegisters(REG_TH_START, 2);
    lastErr = r;
    if (r == node.ku8MBSuccess) {
      int16_t t = (int16_t)node.getResponseBuffer(0);
      int16_t h = (int16_t)node.getResponseBuffer(1);
      ht.err    = r;
      ht.temp   = t * TEMP_SCALE;
      ht.humRaw = h;
      ht.hum    = h * HUM_SCALE;
      ht.tempOk = (ht.temp > -40.0f && ht.temp < 85.0f);
      ht.humOk  = (ht.hum >= HUM_MIN_VALID && ht.hum <= 100.0f);
      return;
    }
    delay(50);
  }
  ht.err = lastErr;
}

// ================= WiFi và thời gian =================

static void wifiBegin() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(cfgSsid.c_str(), cfgWifiPass.c_str());
}

static bool timeSynced() {
  return time(nullptr) > TIME_VALID_AFTER;
}

static void startNtp() {
  configTime(0, 0, "pool.ntp.org", "time.google.com");   // lưu epoch UTC
  ntpStarted = true;
}

// ================= Firebase (REST) =================

static int httpsRequest(const char *method, const String &url, const String &body,
                        const char *contentType, String &response) {
  WiFiClientSecure client;
  client.setInsecure();                 // bỏ qua kiểm tra chứng chỉ, đơn giản cho đồ án
  HTTPClient http;
  http.setTimeout(10000);
  if (!http.begin(client, url)) return -100;   // URL không hợp lệ
  http.addHeader("Content-Type", contentType);
  int code = http.sendRequest(method, body);
  response = (code > 0) ? http.getString() : String();
  http.end();
  return code;
}

static String jsonField(const String &json, const char *key) {
  String pat = String("\"") + key + "\"";
  int i = json.indexOf(pat);
  if (i < 0) return "";
  i = json.indexOf(':', i + pat.length());
  if (i < 0) return "";
  i = json.indexOf('"', i);
  if (i < 0) return "";
  int j = json.indexOf('"', i + 1);
  if (j < 0) return "";
  return json.substring(i + 1, j);
}

static bool firebaseSignIn() {
  String url = String("https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=") + cfgApiKey;
  String body = String("{\"email\":\"") + cfgEmail +
                "\",\"password\":\"" + cfgPass +
                "\",\"returnSecureToken\":true}";
  String resp;
  int code = httpsRequest("POST", url, body, "application/json", resp);
  if (code != 200) {
    Serial.printf("[FB] Dang nhap that bai, HTTP %d: %s\n", code, resp.c_str());
    return false;
  }
  String t = jsonField(resp, "idToken");
  String r = jsonField(resp, "refreshToken");
  uint32_t exp = jsonField(resp, "expiresIn").toInt();
  if (t.length() == 0) return false;
  idToken = t;
  refreshToken = r;
  if (exp < 300) exp = 3600;
  tokenExpireAt = millis() + (exp - 120) * 1000UL;   // gia hạn sớm 2 phút
  authBackoff = false;
  Serial.println("[FB] Dang nhap thanh cong");
  return true;
}

static bool firebaseRefresh() {
  String url = String("https://securetoken.googleapis.com/v1/token?key=") + cfgApiKey;
  String body = String("grant_type=refresh_token&refresh_token=") + refreshToken;
  String resp;
  int code = httpsRequest("POST", url, body, "application/x-www-form-urlencoded", resp);
  if (code != 200) {
    Serial.printf("[FB] Gia han token that bai, HTTP %d\n", code);
    return false;
  }
  String t = jsonField(resp, "id_token");
  String r = jsonField(resp, "refresh_token");
  uint32_t exp = jsonField(resp, "expires_in").toInt();
  if (t.length() == 0) return false;
  idToken = t;
  if (r.length()) refreshToken = r;
  if (exp < 300) exp = 3600;
  tokenExpireAt = millis() + (exp - 120) * 1000UL;
  authBackoff = false;
  return true;
}

static bool ensureAuth() {
  if (idToken.length() && (int32_t)(tokenExpireAt - millis()) > 0) return true;
  if (authBackoff && (int32_t)(authRetryAt - millis()) > 0) return false;
  bool ok = false;
  if (refreshToken.length()) ok = firebaseRefresh();
  if (!ok) ok = firebaseSignIn();
  if (!ok) {
    idToken = "";
    authBackoff = true;
    authRetryAt = millis() + AUTH_RETRY_MS;
  }
  return ok;
}

static bool dbPut(const String &path, const String &json) {
  String url = cfgDbUrl + path + ".json?auth=" + idToken;
  String resp;
  int code = httpsRequest("PUT", url, json, "application/json", resp);
  if (code == 200) return true;
  Serial.printf("[FB] PUT %s%s that bai, HTTP %d (%s) %s\n", cfgDbUrl.c_str(), path.c_str(),
                code, HTTPClient::errorToString(code).c_str(), resp.c_str());
  if (code == 401) idToken = "";        // token không hợp lệ, lần sau gia hạn hoặc đăng nhập lại
  return false;
}

static String jn(float v, unsigned int decimals) {
  return isnan(v) ? String("null") : String(v, decimals);
}

static const char *jb(bool b) { return b ? "true" : "false"; }

static String buildLatest() {
  String j = "{\"ts\":{\".sv\":\"timestamp\"}";
  j += ",\"temp\":" + jn(gTemp, 1);
  j += ",\"hum\":" + jn(gHum, 1);
  j += ",\"mq2\":" + jn(gMq2, 0);
  j += ",\"dust\":" + jn(gDust, 0);
  j += String(",\"smoke\":") + jb(gSmoke);
  j += String(",\"dustAlarm\":") + jb(gDustAlarm);
  j += String(",\"alarm\":") + jb(gSmoke || gDustAlarm);
  j += String(",\"warmup\":") + jb(gWarmup);
  j += String(",\"noiseDust\":") + jb(gDustNoise);
  j += String(",\"noiseMq2\":") + jb(gMq2Noise);
  j += String(",\"modbusOk\":") + jb(gModbusOk);
  j += String(",\"fw\":\"") + FW_VERSION + "\"";
  j += ",\"rssi\":" + String(WiFi.RSSI());
  j += ",\"uptime\":" + String((uint32_t)(millis() / 1000));
  j += "}";
  return j;
}

static String buildHistory() {
  String j = "{\"ts\":{\".sv\":\"timestamp\"}";
  j += ",\"t\":" + jn(accTemp.avg(), 1);
  j += ",\"h\":" + jn(accHum.avg(), 1);
  j += ",\"mq2\":" + jn(accMq2.avg(), 0);
  j += ",\"dust\":" + jn(accDust.avg(), 0);
  j += String(",\"al\":") + (histAlarm ? "1" : "0");
  j += String(",\"wu\":") + (histWarmup ? "1" : "0");
  j += "}";
  return j;
}

// ================= Cấu hình qua Serial (lưu trong NVS) =================

static void loadConfig() {
  prefs.begin("cfg", false);
  cfgSsid     = prefs.getString("ssid", "");
  cfgWifiPass = prefs.getString("wpass", "");
  cfgApiKey   = prefs.getString("apikey", "");
  cfgDbUrl    = prefs.getString("dburl", "");
  cfgEmail    = prefs.getString("email", "");
  cfgPass     = prefs.getString("fpass", "");
  prefs.end();
  while (cfgDbUrl.endsWith("/")) cfgDbUrl.remove(cfgDbUrl.length() - 1);
}

static bool configComplete() {
  return cfgSsid.length() && cfgApiKey.length() && cfgDbUrl.length() && cfgEmail.length() && cfgPass.length();
}

static void printConfigHelp() {
  Serial.println("--- CAU HINH (luu trong bo nho thiet bi, khong nam trong file firmware) ---");
  Serial.println("Serial Monitor: chon Newline, gui tung dong:");
  Serial.println("  SET ssid=TenWiFi");
  Serial.println("  SET wpass=MatKhauWiFi");
  Serial.println("  SET apikey=WebAPIKey");
  Serial.println("  SET dburl=https://ten-project-default-rtdb....firebasedatabase.app");
  Serial.println("  SET email=email_tai_khoan_esp32");
  Serial.println("  SET fpass=mat_khau_tai_khoan_esp32");
  Serial.println("  SHOW (xem cau hinh) | OTA (kiem tra cap nhat ngay) | RESTART | HELP");
}

static String maskStr(const String &s) {
  return s.length() ? String("(") + s.length() + " ky tu)" : String("(trong)");
}

static void showConfig() {
  String ak = cfgApiKey.length() > 8 ? cfgApiKey.substring(0, 6) + "..." : maskStr(cfgApiKey);
  Serial.printf("ssid   : %s\n", cfgSsid.c_str());
  Serial.printf("wpass  : %s\n", maskStr(cfgWifiPass).c_str());
  Serial.printf("apikey : %s\n", ak.c_str());
  Serial.printf("dburl  : %s\n", cfgDbUrl.c_str());
  Serial.printf("email  : %s\n", cfgEmail.c_str());
  Serial.printf("fpass  : %s\n", maskStr(cfgPass).c_str());
  Serial.printf("Firmware: %s\n", FW_VERSION);
}

static void handleSerialConfig() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;
  String up = line;
  up.toUpperCase();
  if (up == "SHOW")    { showConfig(); return; }
  if (up == "HELP")    { printConfigHelp(); return; }
  if (up == "OTA")     { otaForce = true; Serial.println("[OTA] Se kiem tra cap nhat ngay"); return; }
  if (up == "RESTART") { Serial.println("Khoi dong lai..."); delay(300); ESP.restart(); }
  if (up.startsWith("SET ")) {
    int eq = line.indexOf('=');
    if (eq < 5) { Serial.println("Sai cu phap. Vi du: SET ssid=TenWiFi"); return; }
    String key = line.substring(4, eq);
    key.trim();
    key.toLowerCase();
    String val = line.substring(eq + 1);
    val.trim();
    if (key != "ssid" && key != "wpass" && key != "apikey" && key != "dburl" && key != "email" && key != "fpass") {
      Serial.println("Khoa khong hop le. Dung: ssid, wpass, apikey, dburl, email, fpass");
      return;
    }
    bool wasComplete = configComplete();
    prefs.begin("cfg", false);
    prefs.putString(key.c_str(), val);
    prefs.end();
    loadConfig();
    Serial.printf("Da luu %s\n", key.c_str());
    if (!wasComplete && configComplete()) {
      Serial.println("Da du thong tin, khoi dong lai...");
      delay(500);
      ESP.restart();
    }
    return;
  }
  Serial.println("Lenh khong hop le. Go HELP de xem huong dan");
}

// ================= Cập nhật firmware qua mạng (OTA) =================

static bool otaFetchManifest(String &ver, String &url, String &md5) {
  String resp;
  int code = httpsRequest("GET", String(OTA_MANIFEST_URL), "", "application/json", resp);
  if (code != 200) {
    Serial.printf("[OTA] Khong doc duoc version.json, HTTP %d\n", code);
    return false;
  }
  ver = jsonField(resp, "version");
  url = jsonField(resp, "url");
  md5 = jsonField(resp, "md5");
  md5.toLowerCase();
  return ver.length() > 0 && url.length() > 0;
}

static bool otaApply(const String &url, const String &md5) {
  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  http.setTimeout(20000);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  if (!http.begin(client, url)) return false;
  int code = http.GET();
  if (code != 200) {
    Serial.printf("[OTA] Tai file that bai, HTTP %d\n", code);
    http.end();
    return false;
  }
  int len = http.getSize();
  bool known = len > 0;
  if (!Update.begin(known ? (size_t)len : UPDATE_SIZE_UNKNOWN)) {
    Serial.printf("[OTA] Khong du bo nho de cap nhat: %s\n", Update.errorString());
    http.end();
    return false;
  }
  if (md5.length() == 32) Update.setMD5(md5.c_str());
  Serial.printf("[OTA] Dang ghi firmware (%d byte)...\n", len);
  size_t written = Update.writeStream(*http.getStreamPtr());
  bool ok = (!known || written == (size_t)len) && Update.end() && Update.isFinished();
  if (!ok) Serial.printf("[OTA] Loi: %s\n", Update.errorString());
  http.end();
  return ok;
}

static void otaCheck() {
  String ver, url, md5;
  if (!otaFetchManifest(ver, url, md5)) return;
  Serial.printf("[OTA] Dang chay %s, ban moi nhat %s\n", FW_VERSION, ver.c_str());
  if (ver == FW_VERSION) return;

  prefs.begin("ota", false);
  String tried = prefs.getString("tried", "");
  prefs.end();
  if (tried == ver) {
    Serial.println("[OTA] Da cap nhat len ban nay roi, bo qua de tranh lap (hay doi FW_VERSION trong ban moi)");
    return;
  }

  Serial.printf("[OTA] Tai ban %s\n", ver.c_str());
  if (otaApply(url, md5)) {
    prefs.begin("ota", false);
    prefs.putString("tried", ver);
    prefs.end();
    Serial.println("[OTA] Thanh cong, khoi dong lai");
    delay(500);
    ESP.restart();
  } else {
    Serial.println("[OTA] That bai, giu nguyen ban hien tai");
  }
}

// DNS hợp lệ khi phân giải được và địa chỉ trả về khác 0.0.0.0
static bool dnsWorks() {
  IPAddress ip;
  return WiFi.hostByName("identitytoolkit.googleapis.com", ip) && (uint32_t)ip != 0;
}

// ================= setup / loop =================

void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(ALARM_LED_PIN, OUTPUT);
  digitalWrite(ALARM_LED_PIN, LOW);

  pinMode(DUST_LED_PIN, OUTPUT);
  digitalWrite(DUST_LED_PIN, HIGH);

  analogReadResolution(12);
  analogSetPinAttenuation(MQ2_PIN, ADC_11db);
  analogSetPinAttenuation(DUST_VO_PIN, ADC_11db);

  pinMode(RS485_DE_RE, OUTPUT);
  digitalWrite(RS485_DE_RE, LOW);
  Serial2.begin(MODBUS_BAUD, SERIAL_8N1, RS485_RO_PIN, RS485_DI_PIN);
  node.begin(MODBUS_ID, Serial2);
  node.preTransmission(preTransmission);
  node.postTransmission(postTransmission);

  Serial.println("=== HE THONG GIAM SAT HAM KIN v4 (Firebase + OTA) ===");
  Serial.printf("Firmware %s\n", FW_VERSION);
  loadConfig();
  if (!configComplete()) printConfigHelp();

  if (DUST_AUTO_CALIB) {
    float sp = 0;
    float v = readDustVolts(50, &sp);
    if (!isnan(v) && v >= 0.4f && v <= 1.5f && sp < DUST_SPREAD_WARN) {
      dustV0 = v;
      Serial.printf("Hieu chuan bui: V0 = %.3f V\n", dustV0);
    } else {
      Serial.printf("Hieu chuan bui khong dat (V=%.2f, nhieu=%.2f), dung V0 mac dinh %.2f V\n",
                    v, sp, dustV0);
    }
  }

  if (configComplete()) {
  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  wifiBegin();
  Serial.print("Ket noi WiFi");
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < WIFI_CONNECT_TIMEOUT_MS) {
    delay(500);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf(" OK, IP %s\n", WiFi.localIP().toString().c_str());
    Serial.printf("Gateway: %s | DNS: %s\n", WiFi.gatewayIP().toString().c_str(), WiFi.dnsIP().toString().c_str());
    bool dnsOk = dnsWorks();
    if (!dnsOk) {
      Serial.println("DNS cua router khong hop le (0.0.0.0 hoac loi), chuyen sang DNS 8.8.8.8 / 1.1.1.1");
      WiFi.config(WiFi.localIP(), WiFi.gatewayIP(), WiFi.subnetMask(),
                  IPAddress(8, 8, 8, 8), IPAddress(1, 1, 1, 1));
      delay(3000);                       // chờ bộ nhớ đệm DNS cũ hết hạn
      dnsOk = dnsWorks();
    }
    Serial.println(dnsOk ? "DNS OK" : "DNS van loi: mang nay chan Google, hay doi WiFi khac hoac dung hotspot dien thoai");
    startNtp();
    t0 = millis();
    while (!timeSynced() && millis() - t0 < 10000) delay(200);
    Serial.println(timeSynced() ? "Dong bo gio NTP thanh cong" : "Chua dong bo duoc gio NTP, se thu lai sau");
  } else {
    Serial.println(" that bai, van do cam bien va se thu ket noi lai");
  }
  } else {
    Serial.println("Chua cau hinh WiFi/Firebase: thiet bi chi do cam bien. Go HELP de xem cach cau hinh.");
  }
  Serial.printf("MQ-2 lam nong, bo qua canh bao %lu giay\n", MQ2_WARMUP_MS / 1000);
}

void loop() {
  handleSerialConfig();
  static uint32_t last = 0;
  static float dustUg = NAN;
  if (millis() - last < LOOP_MS) return;
  last = millis();

  // ---- Đo ----
  float mq2Spread, dustSpread = 0;
  float mq2mV  = readMQ2_mV(&mq2Spread);
  float dustV  = readDustVolts(DUST_SAMPLES, &dustSpread);
  if (!isnan(dustV)) {
    float ug = (dustV - dustV0) * DUST_UG_PER_V;
    if (ug < 0) ug = 0;
    dustUg = isnan(dustUg) ? ug : 0.3f * ug + 0.7f * dustUg;
  }
  readHumTemp();

  // ---- In ra Serial ----
  Serial.printf("MQ2=%.0f mV", mq2mV);
  if (!isnan(dustUg)) Serial.printf(" | Bui=%.0f ug/m3 (Vo=%.2f V)", dustUg, dustV);
  else                Serial.print(" | Bui=LOI ADC");

  if (ht.err != node.ku8MBSuccess) {
    Serial.printf(" | Modbus LOI 0x%02X", ht.err);
  } else {
    if (ht.tempOk) Serial.printf(" | Nhiet do=%.1f C", ht.temp);
    else           Serial.print(" | Nhiet do=KHONG HOP LE");
    if (ht.humOk)  Serial.printf(" | Do am=%.1f %%RH", ht.hum);
    else           Serial.printf(" | Do am=KHONG HOP LE (raw=%d)", ht.humRaw);
  }

  bool smokeAlarm = false, dustAlarm = false;
  bool warmup = millis() < MQ2_WARMUP_MS;
  if (warmup)                                       Serial.print("  [MQ2 WARM-UP]");
  else if (mq2mV > MQ2_WARN_MV)                   { Serial.print("  [CANH BAO KHOI]"); smokeAlarm = true; }
  if (!isnan(dustUg) && dustUg > DUST_WARN_UG)    { Serial.print("  [CANH BAO BUI]");  dustAlarm = true; }
  bool dustNoise = dustSpread > DUST_SPREAD_WARN;
  bool mq2Noise  = mq2Spread > MQ2_SPREAD_WARN;
  if (dustNoise) Serial.print("  [VO NHIEU]");
  if (mq2Noise)  Serial.print("  [AO NHIEU]");
  Serial.println();

  digitalWrite(ALARM_LED_PIN, (smokeAlarm || dustAlarm) ? HIGH : LOW);

  // ---- Cập nhật mẫu mới nhất và bộ cộng dồn ----
  gTemp = ht.tempOk ? ht.temp : NAN;
  gHum  = ht.humOk ? ht.hum : NAN;
  gMq2  = mq2mV;
  gDust = dustUg;
  gSmoke = smokeAlarm;
  gDustAlarm = dustAlarm;
  gDustNoise = dustNoise;
  gMq2Noise = mq2Noise;
  gModbusOk = (ht.err == node.ku8MBSuccess);
  gWarmup = warmup;

  accTemp.add(gTemp);
  accHum.add(gHum);
  accMq2.add(gMq2);
  accDust.add(gDust);
  if (smokeAlarm || dustAlarm) histAlarm = true;
  if (warmup) histWarmup = true;

  // ---- WiFi và gửi Firebase ----
  if (!configComplete()) return;
  if (WiFi.status() != WL_CONNECTED) {
    if (millis() - lastWifiTry >= WIFI_RETRY_MS) {
      lastWifiTry = millis();
      Serial.println("[WiFi] Mat ket noi, thu ket noi lai...");
      WiFi.disconnect();
      wifiBegin();
    }
    return;
  }

  if (!ntpStarted) startNtp();

  uint32_t now = millis();

  if (otaForce || (now > 30000 && (!otaBootChecked || now - lastOtaCheck >= OTA_CHECK_MS))) {
    otaForce = false;
    otaBootChecked = true;
    lastOtaCheck = now;
    otaCheck();
  }

  if (now - lastLatest >= LATEST_PERIOD_MS) {
    lastLatest = now;
    if (ensureAuth()) {
      bool ok = dbPut("/latest", buildLatest());
      Serial.println(ok ? "[FB] /latest OK" : "[FB] /latest LOI");
    }
  }

  if (now - lastHistory >= HISTORY_PERIOD_MS && timeSynced()) {
    lastHistory = now;
    if (ensureAuth()) {
      String path = "/history/" + String((uint32_t)time(nullptr));
      bool ok = dbPut(path, buildHistory());
      Serial.println(ok ? "[FB] /history OK" : "[FB] /history LOI");
      if (ok) {
        accTemp.reset(); accHum.reset(); accMq2.reset(); accDust.reset();
        histAlarm = false;
        histWarmup = false;
      }
    }
  }
}