// AtomS3R web image server — a 4-slot image kiosk with single-button gestures.
//
// Holds 4 128x128 RGB565 images in flash. The displayed slot is switched with a
// simple HTTP GET (GET /show?slot=N). The AtomS3R has one button, so each slot
// maps three button gestures to HTTP GET actions: short click (<500 ms), long
// click (>1500 ms), and double click (two shorts within 2000 ms). A gesture URL
// of "2" shows slot 2 here; "/show?slot=1" is also local; "http://other/…" drives
// another unit. Still speaks atom-manager's /frame (?mid), /state (markerId,
// battery) so the host keeps working — it just pushes to the current slot.
//
// Serial console (115200, USB-CDC): wifi <ssid>:<password> / ip / status / ap.

#include <M5Unified.h>
#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <LittleFS.h>
#include <math.h>

#include "names_discovery.h"
#include "index_html.h"

static const uint16_t FRAME_W = 128;
static const uint16_t FRAME_H = 128;
static const size_t   FRAME_BYTES = (size_t)FRAME_W * FRAME_H * 2;  // 32768

static const int   NUM_SLOTS = 4;
static const int   GESTURES  = 3;            // short / long / double per slot
static const char* BUTTONS_PATH = "/buttons.txt";
static String slotPath(int n) { return "/" + String(n) + ".bin"; }

// Single-button gesture thresholds.
static const uint32_t SHORT_MAX_MS = 500;    // release before this = short click
static const uint32_t LONG_MIN_MS  = 700;    // held at least this  = long click
static const uint32_t DOUBLE_MS    = 1500;   // 2nd short within this of 1st = double

// Battery sense ADC. AtomS3R / AtomS3 read the pack through a 2:1 divider on
// GPIO 8 (the older Atom series uses GPIO 33). analogReadMilliVolts() already
// applies the eFuse calibration, so we just double it back to pack volts.
static const int      BAT_ADC_PIN = 8;
static const int      BAT_FULL_MV = 4200;  // 1S LiPo at 100%
static const int      BAT_EMPTY_MV = 3300; // treat as 0%

// SoftAP fallback credentials. Password must be >= 8 chars, or "" for open.
static const char* AP_SSID = "AtomFramer";
static const char* AP_PASS = "atomframer";

static const uint32_t STA_TIMEOUT_MS = 20000;  // give up on a join after this

WebServer server(80);
Preferences prefs;

static uint8_t frameBuf[FRAME_BYTES];
static size_t  frameLen = 0;     // bytes received so far in the current upload
static bool    frameOk  = false; // a full frame is currently stored/displayed
static int     markerId = -1;    // ArUco id of the current slot (-1 = unknown)
static int     curSlot  = 0;
static bool    slotFilled[NUM_SLOTS] = { false };

static String  lineBuf;          // serial line accumulator
static String  pendingSsid;      // ssid we're currently trying to join
static bool    staConnecting = false;
static uint32_t staDeadline = 0;

// Accelerometer drop/impact detector. A real drop first produces near-zero
// acceleration (free fall), followed by a sharp acceleration spike when the
// enclosure hits a hard surface. Requiring that sequence avoids treating
// ordinary handling or vibration as a dropped tag.
static bool     accelAvailable = false;
static bool     accelEnabled = true;
static bool     accelRequireDrop = true;
static float    accelSensitivity = 0.85f; // max g considered free fall
static float    accelHitThreshold = 1.05f; // landing impact threshold in g
static uint32_t accelDropConfirmMs = 10;  // legacy API value; shape has no minimum gap
static uint16_t accelSampleRateHz = 0;
static uint8_t  accelRangeG = 0;
static float    accelX = 0.0f;
static float    accelY = 0.0f;
static float    accelZ = 0.0f;
static float    accelMagnitude = 0.0f;
static bool     dropArmed = false;
static bool     impactAboveThreshold = false;
static uint32_t lastImpactMs = 0;
static float    lastImpactG = 0.0f;
static uint32_t impactCount = 0;
static bool     lastShapeValid = false;
static float    lastShapeDropG = 0.0f;
static float    lastShapeDeltaG = 0.0f;
static uint32_t lastShapeRiseMs = 0;

static const uint32_t IMPACT_RECENT_MS = 3000;
static const uint32_t IMPACT_COOLDOWN_MS = 300;
static const uint32_t ACCEL_BUFFER_US = 3000000; // 3-second display history
static const uint32_t SHAPE_RISE_WINDOW_US = 62500; // 1/16-second hit matcher
static const float    SHAPE_MIN_RISE_G = 0.20f;
static const uint32_t ACCEL_BUFFER_MIN_INTERVAL_US = 625;
static const size_t   ACCEL_BUFFER_CAPACITY = 4800; // 1600 Hz * 3 seconds
static const size_t   ACCEL_DISPLAY_SAMPLES = 480;
static const size_t   ACCEL_DISPLAY_DECIMATION = 10; // 160 displayed samples/s
static uint8_t        bmi270I2cAddr = 0x69;
static const uint8_t  BMI270_ACC_CONF = 0x40;
static const uint8_t  BMI270_ACC_RANGE = 0x41;
static const uint8_t  BMI270_GYR_CONF = 0x42;
static const uint8_t  BMI270_ACC_DATA = 0x0C;
static const uint8_t  BMI270_ACC_ODR_1600HZ = 0x0C;
static const uint8_t  BMI270_GYR_ODR_1600HZ = 0x0C;
static const uint8_t  BMI270_ACC_RANGE_2G = 0x00;
// M5Unified converts BMI270 raw acceleration with its default +/-8 g scale.
// Correct those values after explicitly selecting the more sensitive +/-2 g range.
static const float    BMI270_ACCEL_SCALE_CORRECTION = 2.0f / 8.0f;
static const uint32_t BMI270_I2C_HZ = 400000;

struct AccelHistorySample {
  uint32_t us;
  float magnitude;
};
struct __attribute__((packed)) AccelCaptureSample {
  uint32_t us;
  float magnitude;
  float accelX;
  float accelY;
  float accelZ;
  float gyroX;
  float gyroY;
  float gyroZ;
  uint8_t detectorHit;
};
static_assert(sizeof(AccelCaptureSample) == 33, "capture wire record must stay packed");
static AccelHistorySample accelBuffer[ACCEL_BUFFER_CAPACITY];
static const size_t ACCEL_CAPTURE_SAMPLES = 1600;
static AccelCaptureSample accelCaptureBuffer[ACCEL_CAPTURE_SAMPLES];
static size_t accelCaptureHead = 0;
static size_t accelCaptureCount = 0;
static uint32_t accelCaptureTotal = 0;
static uint16_t accelHistoryPayload[ACCEL_DISPLAY_SAMPLES];
static size_t accelBufferHead = 0;
static size_t accelBufferCount = 0;
static uint32_t lastBufferedUs = 0;
static uint32_t lastLowSampleUs = 0;

static bool apActive() {
  auto m = WiFi.getMode();
  return m == WIFI_AP || m == WIFI_AP_STA;
}

// ---- frame display + persistence -------------------------------------------
// Push frameBuf to the panel. The page packs RGB565 big-endian (high byte
// first); typing the source as swap565_t lets M5GFX convert from that byte
// order to the panel's native order itself (reading it as a plain uint16
// rotates the channels: red->blue, green->red, blue->green).
static void pushFrame() {
  M5.Display.startWrite();
  M5.Display.pushImage(0, 0, FRAME_W, FRAME_H, (const m5gfx::swap565_t*)frameBuf);
  M5.Display.endWrite();
}

static String midKey(int n) { return "mid" + String(n); }

static void refreshFilled() {
  for (int i = 0; i < NUM_SLOTS; i++) slotFilled[i] = LittleFS.exists(slotPath(i));
}

// Write frameBuf (and the current marker id) to slot n.
static void saveSlot(int n) {
  File f = LittleFS.open(slotPath(n), "w");
  if (!f) { Serial.println("save: open failed"); return; }
  size_t w = f.write(frameBuf, FRAME_BYTES);
  f.close();
  prefs.putInt(midKey(n).c_str(), markerId);
  if (w != FRAME_BYTES) Serial.printf("save: short write %u/%u\n", (unsigned)w, (unsigned)FRAME_BYTES);
  else { slotFilled[n] = true; }
}

// Load slot n into frameBuf, display it, and make it the current slot.
static bool loadSlot(int n) {
  if (n < 0 || n >= NUM_SLOTS || !LittleFS.exists(slotPath(n))) return false;
  File f = LittleFS.open(slotPath(n), "r");
  if (!f) return false;
  size_t r = f.read(frameBuf, FRAME_BYTES);
  f.close();
  if (r != FRAME_BYTES) return false;
  markerId = prefs.getInt(midKey(n).c_str(), -1);
  curSlot = n;
  frameOk = true;
  prefs.putInt("slot", curSlot);
  pushFrame();
  return true;
}

// ---- hotspot / gesture URL table -------------------------------------------
// /buttons.txt is NUM_SLOTS*GESTURES newline-separated lines (slot*GESTURES + g,
// g = 0 short / 1 long / 2 double); an empty line means that gesture has no
// action. Read all GESTURES urls for a slot.
static void readSlotUrls(int slot, String out[GESTURES]) {
  for (int i = 0; i < GESTURES; i++) out[i] = "";
  File f = LittleFS.open(BUTTONS_PATH, "r");
  if (!f) return;
  int base = slot * GESTURES;
  int line = 0;
  while (f.available() && line < base + GESTURES) {
    String l = f.readStringUntil('\n');
    if (line >= base) { l.trim(); out[line - base] = l; }
    line++;
  }
  f.close();
}

// Fire an outbound HTTP GET to another unit. Blocking, but only for a button
// press. Never aim this at our own IP — the single-threaded WebServer would
// deadlock on a self-request; self actions are handled locally in doAction().
static void fireGet(const String& url) {
  Serial.println("GET " + url);
  HTTPClient http;
  http.setConnectTimeout(2000);
  http.setTimeout(3000);
  if (http.begin(url)) {
    int code = http.GET();
    Serial.printf("  -> %d\n", code);
    http.end();
  } else {
    Serial.println("  begin failed");
  }
}

// Run a gesture action. Self page-switches are done locally (no self HTTP):
//   "2"            -> show slot 2 here (bare number 1..NUM_SLOTS)
//   "/show?slot=1" -> show slot 1 here (0-based, self)
//   "http://ip/…"  -> GET another unit
static void doAction(String url) {
  url.trim();
  if (!url.length()) return;
  bool numeric = true;
  for (size_t i = 0; i < url.length(); i++) if (!isDigit(url[i])) { numeric = false; break; }
  if (numeric) {
    int n = url.toInt();
    if (n >= 1 && n <= NUM_SLOTS) { if (!loadSlot(n - 1)) Serial.printf("slot %d empty\n", n); }
    return;
  }
  if (url.startsWith("/show?slot=")) {
    if (!loadSlot(url.substring(11).toInt())) Serial.println("self slot empty");
    return;
  }
  if (url.startsWith("http://") || url.startsWith("https://")) { fireGet(url); return; }
  // "name:slot" — resolve a fleet name to its IP (slot is 1-based, like the UI).
  int colon = url.indexOf(':');
  if (colon > 0) {
    String nm = url.substring(0, colon);
    String rest = url.substring(colon + 1); rest.trim();
    bool restNum = rest.length() > 0;
    for (size_t i = 0; i < rest.length(); i++) if (!isDigit(rest[i])) restNum = false;
    if (restNum) {
      int sl = rest.toInt();
      if (nm == disco::name()) { if (!loadSlot(sl - 1)) Serial.println("self slot empty"); return; }
      IPAddress ip;
      if (disco::lookup(nm, ip)) fireGet("http://" + ip.toString() + "/show?slot=" + String(sl - 1));
      else Serial.println("action: name not found: " + nm);
      return;
    }
  }
  Serial.println("action: unsupported target: " + url);
}

// ---- battery ---------------------------------------------------------------
static int batteryMilliVolts() {
  return (int)analogReadMilliVolts(BAT_ADC_PIN) * 2;  // undo the 2:1 divider
}

static int batteryPercent(int mv) {
  int pct = (mv - BAT_EMPTY_MV) * 100 / (BAT_FULL_MV - BAT_EMPTY_MV);
  return pct < 0 ? 0 : (pct > 100 ? 100 : pct);
}

// ---- accelerometer ---------------------------------------------------------
static void configureAccelerometer() {
  accelSampleRateHz = 0;
  accelRangeG = 0;
  if (!accelAvailable || M5.Imu.getType() != m5::imu_bmi270) return;

  // M5Unified probes both valid BMI270 addresses. Use the one actually found
  // instead of assuming every AtomS3R revision is wired at 0x69.
  if (m5::In_I2C.readRegister8(0x69, 0x00, BMI270_I2C_HZ) == 0x24)
    bmi270I2cAddr = 0x69;
  else if (m5::In_I2C.readRegister8(0x68, 0x00, BMI270_I2C_HZ) == 0x24)
    bmi270I2cAddr = 0x68;
  else
    return;

  // Use the BMI270's most sensitive range. ACC_RANGE 0x00 is +/-2 g.
  m5::In_I2C.writeRegister8(
      bmi270I2cAddr, BMI270_ACC_RANGE, BMI270_ACC_RANGE_2G, BMI270_I2C_HZ);
  uint8_t range = m5::In_I2C.readRegister8(
      bmi270I2cAddr, BMI270_ACC_RANGE, BMI270_I2C_HZ);
  if ((range & 0x03) == BMI270_ACC_RANGE_2G) accelRangeG = 2;

  // M5Unified leaves ACC_CONF at the BMI270 reset/default value (100 Hz).
  // Preserve its performance/filter bits and select the sensor's maximum
  // accelerometer ODR, 1600 Hz, in the low nibble.
  uint8_t conf = m5::In_I2C.readRegister8(
      bmi270I2cAddr, BMI270_ACC_CONF, BMI270_I2C_HZ);
  uint8_t maxRateConf = (conf & 0xF0) | BMI270_ACC_ODR_1600HZ;
  m5::In_I2C.writeRegister8(
      bmi270I2cAddr, BMI270_ACC_CONF, maxRateConf, BMI270_I2C_HZ);
  uint8_t verified = m5::In_I2C.readRegister8(
      bmi270I2cAddr, BMI270_ACC_CONF, BMI270_I2C_HZ);
  if ((verified & 0x0F) == BMI270_ACC_ODR_1600HZ) accelSampleRateHz = 1600;

  // Capture reads accel and gyro together, so run both sensors at 1600 Hz.
  uint8_t gyroConf = m5::In_I2C.readRegister8(
      bmi270I2cAddr, BMI270_GYR_CONF, BMI270_I2C_HZ);
  m5::In_I2C.writeRegister8(
      bmi270I2cAddr, BMI270_GYR_CONF,
      (gyroConf & 0xF0) | BMI270_GYR_ODR_1600HZ, BMI270_I2C_HZ);
}

static bool bufferAccelerometerSample(
    uint32_t nowUs, float magnitude, float x, float y, float z,
    float gx, float gy, float gz) {
  // Do not fill the ring with duplicate host reads faster than the BMI270 ODR.
  // At 1600 Hz a fresh sample can arrive every 625 us.
  if (lastBufferedUs &&
      (uint32_t)(nowUs - lastBufferedUs) < ACCEL_BUFFER_MIN_INTERVAL_US) {
    return false;
  }
  lastBufferedUs = nowUs;
  accelBuffer[accelBufferHead] = {nowUs, magnitude};
  accelBufferHead = (accelBufferHead + 1) % ACCEL_BUFFER_CAPACITY;
  if (accelBufferCount < ACCEL_BUFFER_CAPACITY) accelBufferCount++;
  accelCaptureBuffer[accelCaptureHead] = {
      nowUs, magnitude, x, y, z, gx, gy, gz, 0};
  accelCaptureHead = (accelCaptureHead + 1) % ACCEL_CAPTURE_SAMPLES;
  if (accelCaptureCount < ACCEL_CAPTURE_SAMPLES) accelCaptureCount++;
  accelCaptureTotal++;
  return true;
}

static size_t recentAccelerometerSampleCount(uint32_t nowUs) {
  size_t recent = 0;
  for (size_t n = 0; n < accelBufferCount; n++) {
    size_t idx =
        (accelBufferHead + ACCEL_BUFFER_CAPACITY - 1 - n) % ACCEL_BUFFER_CAPACITY;
    if ((uint32_t)(nowUs - accelBuffer[idx].us) > ACCEL_BUFFER_US) break;
    recent++;
  }
  return recent;
}

// Find a low-G point in the most recent 1/16 second of the rolling buffer,
// followed by the current upward crossing with a rise greater than 0.20 g.
static bool findDropImpactShape(
    uint32_t nowUs, float& minimumG, float& riseG, uint32_t& lowToHitMs) {
  bool foundLow = false;
  uint32_t newestLowAgeUs = UINT32_MAX;
  minimumG = accelMagnitude;

  for (size_t n = 1; n < accelBufferCount; n++) { // skip the current hit sample
    size_t idx =
        (accelBufferHead + ACCEL_BUFFER_CAPACITY - 1 - n) % ACCEL_BUFFER_CAPACITY;
    uint32_t ageUs = nowUs - accelBuffer[idx].us;
    if (ageUs > SHAPE_RISE_WINDOW_US) break;
    if (ageUs <= SHAPE_RISE_WINDOW_US &&
        accelBuffer[idx].magnitude <= accelSensitivity) {
      foundLow = true;
      if (ageUs < newestLowAgeUs) newestLowAgeUs = ageUs;
      if (accelBuffer[idx].magnitude < minimumG) {
        minimumG = accelBuffer[idx].magnitude;
      }
    }
  }

  if (!foundLow || newestLowAgeUs > SHAPE_RISE_WINDOW_US) return false;
  riseG = accelMagnitude - minimumG;
  if (riseG <= SHAPE_MIN_RISE_G) return false;
  lowToHitMs = newestLowAgeUs / 1000U;
  return true;
}

static void registerImpact(
    uint32_t now, bool shapeValid, float shapeDropG, float shapeDeltaG,
    uint32_t shapeRiseMs) {
  lastImpactMs = now;
  lastImpactG = accelMagnitude;
  impactCount++;
  dropArmed = false;
  lastShapeValid = shapeValid;
  lastShapeDropG = shapeDropG;
  lastShapeDeltaG = shapeDeltaG;
  lastShapeRiseMs = shapeRiseMs;
}

static void pumpAccelerometer() {
  if (!accelAvailable || !accelEnabled || !M5.Imu.update()) return;

  auto data = M5.Imu.getImuData();
  const float accelScale = accelRangeG == 2
      ? BMI270_ACCEL_SCALE_CORRECTION : 1.0f;
  accelX = data.accel.x * accelScale;
  accelY = data.accel.y * accelScale;
  accelZ = data.accel.z * accelScale;
  accelMagnitude = sqrtf(accelX * accelX + accelY * accelY + accelZ * accelZ);
  const float gyroX = data.gyro.x;
  const float gyroY = data.gyro.y;
  const float gyroZ = data.gyro.z;
  uint32_t now = millis();
  uint32_t nowUs = micros();
  if (!bufferAccelerometerSample(
          nowUs, accelMagnitude, accelX, accelY, accelZ, gyroX, gyroY, gyroZ)) return;

  // Optional impact-only mode for extremely short drops. Hysteresis makes this
  // edge-triggered: a sustained high reading produces one hit, and the latch
  // resets only after acceleration has fallen clearly below the threshold.
  if (!accelRequireDrop) {
    const float resetThreshold = accelHitThreshold - 0.03f;
    if (!impactAboveThreshold && accelMagnitude >= accelHitThreshold) {
      impactAboveThreshold = true;
      if (!impactCount || now - lastImpactMs >= IMPACT_COOLDOWN_MS) {
        registerImpact(now, false, 0.0f, 0.0f, 0);
        accelCaptureBuffer[(accelCaptureHead + ACCEL_CAPTURE_SAMPLES - 1) %
                           ACCEL_CAPTURE_SAMPLES].detectorHit = 1;
      }
    } else if (impactAboveThreshold && accelMagnitude < resetThreshold) {
      impactAboveThreshold = false;
    }
    return;
  }

  // Show the drop-armed state while a recent qualifying low-G sample remains
  // in the abrupt-rise window. The actual decision below scans the full buffer.
  if (accelMagnitude <= accelSensitivity) {
    dropArmed = true;
    lastLowSampleUs = nowUs;
  } else if (dropArmed && lastLowSampleUs &&
             (uint32_t)(nowUs - lastLowSampleUs) > SHAPE_RISE_WINDOW_US) {
    dropArmed = false;
  }

  const float resetThreshold = accelHitThreshold - 0.03f;
  if (!impactAboveThreshold && accelMagnitude >= accelHitThreshold) {
    impactAboveThreshold = true;
    if (!impactCount || now - lastImpactMs >= IMPACT_COOLDOWN_MS) {
      float shapeDropG = 0.0f;
      float shapeDeltaG = 0.0f;
      uint32_t shapeRiseMs = 0;
      if (findDropImpactShape(nowUs, shapeDropG, shapeDeltaG, shapeRiseMs)) {
        registerImpact(now, true, shapeDropG, shapeDeltaG, shapeRiseMs);
        accelCaptureBuffer[(accelCaptureHead + ACCEL_CAPTURE_SAMPLES - 1) %
                           ACCEL_CAPTURE_SAMPLES].detectorHit = 1;
      }
    }
  } else if (impactAboveThreshold && accelMagnitude < resetThreshold) {
    impactAboveThreshold = false;
  }
}

// ---- on-screen status ------------------------------------------------------
static void showStatus() {
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setTextColor(TFT_GREEN, TFT_BLACK);
  M5.Display.setTextSize(1);
  M5.Display.setCursor(3, 3);
  M5.Display.println(disco::name());
  M5.Display.setTextColor(0x8410, TFT_BLACK);  // dim grey
  if (WiFi.status() == WL_CONNECTED) {
    M5.Display.println("STA " + WiFi.SSID());
    M5.Display.setTextColor(TFT_GREEN, TFT_BLACK);
    M5.Display.println(WiFi.localIP().toString());
  } else if (staConnecting) {
    M5.Display.println("joining");
    M5.Display.println(pendingSsid);
  } else if (apActive()) {
    M5.Display.printf("AP %s\n", AP_SSID);
    if (AP_PASS[0]) M5.Display.printf("pw %s\n", AP_PASS);
    M5.Display.setTextColor(TFT_GREEN, TFT_BLACK);
    M5.Display.println(WiFi.softAPIP().toString());
  } else {
    M5.Display.println("offline");
  }
}

// ---- serial reporting ------------------------------------------------------
static void reportIP() {
  if (WiFi.status() == WL_CONNECTED) {
    String ip = WiFi.localIP().toString();
    Serial.printf("IP %s  http://%s/  (STA \"%s\", RSSI %d)\n",
                  ip.c_str(), ip.c_str(), WiFi.SSID().c_str(), WiFi.RSSI());
  } else if (staConnecting) {
    Serial.println("joining... (no IP from DHCP yet — try 'ip' again)");
  } else if (apActive()) {
    String ip = WiFi.softAPIP().toString();
    Serial.printf("IP %s  http://%s/  (SoftAP \"%s\")\n", ip.c_str(), ip.c_str(), AP_SSID);
  } else {
    Serial.println("not connected");
  }
}

static void printHelp() {
  Serial.println("commands:");
  Serial.println("  wifi <ssid>:<password>  - join a network (saved, auto-reconnects)");
  Serial.println("  ip                      - show current IP");
  Serial.println("  status                  - mode / ssid / ip / rssi");
  Serial.println("  ap                      - forget wifi, start SoftAP");
  Serial.println("  help");
}

// ---- WiFi mode control -----------------------------------------------------
static void startAP() {
  staConnecting = false;
  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASS);
  Serial.printf("SoftAP \"%s\"  http://%s/\n", AP_SSID, WiFi.softAPIP().toString().c_str());
  if (!frameOk) showStatus();  // keep a restored frame on screen; status is on the button
}

static void startSTA(const String& ssid, const String& pass, bool save) {
  if (save) {
    prefs.putString("ssid", ssid);
    prefs.putString("pass", pass);
  }
  pendingSsid = ssid;
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);    // rejoin automatically after a WiFi-infra swap
  WiFi.begin(ssid.c_str(), pass.c_str());
  staConnecting = true;
  staDeadline = millis() + STA_TIMEOUT_MS;
  Serial.printf("joining \"%s\"... (waiting for DHCP)\n", ssid.c_str());
  if (!frameOk) showStatus();  // keep a restored frame on screen; status is on the button
}

// Watch a pending STA join; report the IP once DHCP lands, or fall back to AP.
static void pollSTA() {
  if (!staConnecting) return;
  if (WiFi.status() == WL_CONNECTED) {
    staConnecting = false;
    Serial.println("connected.");
    reportIP();
    if (frameOk) pushFrame(); else showStatus();  // don't wipe a restored frame
  } else if ((int32_t)(millis() - staDeadline) >= 0) {
    staConnecting = false;
    Serial.println("join timed out — starting SoftAP.");
    startAP();
  }
}

// ---- serial command parsing ------------------------------------------------
static void handleSerialLine(String line) {
  line.trim();
  if (!line.length()) return;
  if (line.equalsIgnoreCase("ip"))     { reportIP();  return; }
  if (line.equalsIgnoreCase("status")) { reportIP();  return; }
  if (line.equalsIgnoreCase("help"))   { printHelp(); return; }
  if (line.equalsIgnoreCase("name"))   { Serial.println("name: " + disco::name()); return; }
  if (line.length() > 5 && line.substring(0, 5).equalsIgnoreCase("name ")) {
    String nm = line.substring(5); nm.trim();
    if (nm.length()) { disco::setName(nm); Serial.println("renamed: " + disco::name()); }
    return;
  }
  if (line.equalsIgnoreCase("ap")) {
    prefs.remove("ssid");
    prefs.remove("pass");
    Serial.println("forgot saved wifi.");
    startAP();
    return;
  }

  // "wifi ssid:pass", or a bare "ssid:pass" line.
  String creds;
  if (line.length() > 5 && line.substring(0, 5).equalsIgnoreCase("wifi ")) {
    creds = line.substring(5);
  } else if (line.indexOf(':') >= 0) {
    creds = line;
  } else {
    Serial.println("unknown command — type 'help'");
    return;
  }
  creds.trim();
  int colon = creds.indexOf(':');               // split on the FIRST colon
  if (colon < 0) { Serial.println("usage: wifi <ssid>:<password>"); return; }
  String ssid = creds.substring(0, colon); ssid.trim();
  String pass = creds.substring(colon + 1);      // password may contain colons
  if (!ssid.length()) { Serial.println("empty ssid"); return; }
  startSTA(ssid, pass, true);
}

static void pumpSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') { handleSerialLine(lineBuf); lineBuf = ""; }
    else if (lineBuf.length() < 160) lineBuf += c;
  }
}

// ---- single-button gestures ------------------------------------------------
static uint32_t pressStart   = 0;
static bool     shortPending = false;
static uint32_t firstShortAt = 0;

// Run the current slot's action for gesture g (0 short / 1 long / 2 double).
// An unconfigured long-press falls back to showing WiFi/IP status on the LCD.
static void runGesture(int g) {
  String urls[GESTURES];
  readSlotUrls(curSlot, urls);
  String url = urls[g];
  Serial.printf("gesture %d -> \"%s\"\n", g, url.c_str());
  if (url.length()) doAction(url);
  else if (g == 1) showStatus();
}

// Classify BtnA press/release timing into short / long / double and fire it. A
// short release is held back until the double-click window passes (so it can
// become a double instead); a 500–1500 ms release is in neither band, ignored.
static void pumpButton() {
  if (M5.BtnA.wasPressed())  pressStart = millis();
  if (M5.BtnA.wasReleased()) {
    uint32_t dur = millis() - pressStart;
    if (dur >= LONG_MIN_MS) {
      shortPending = false;
      runGesture(1);                                   // long
    } else if (dur < SHORT_MAX_MS) {
      if (shortPending && (millis() - firstShortAt) <= DOUBLE_MS) {
        shortPending = false;
        runGesture(2);                                 // double
      } else {
        shortPending = true;
        firstShortAt = millis();
      }
    }
  }
  if (shortPending && (millis() - firstShortAt) > DOUBLE_MS) {
    shortPending = false;
    runGesture(0);                                     // single short
  }
}

// ---- HTTP handlers ---------------------------------------------------------
static void handleRoot() {
  server.send_P(200, "text/html", INDEX_HTML);
}

// Multipart file upload: binary-safe, streamed in chunks by the core WebServer.
static void handleFrameUpload() {
  HTTPUpload& up = server.upload();
  if (up.status == UPLOAD_FILE_START) {
    frameLen = 0;
    frameOk = false;
  } else if (up.status == UPLOAD_FILE_WRITE) {
    size_t n = up.currentSize;
    if (frameLen + n > FRAME_BYTES) n = FRAME_BYTES - frameLen;  // clamp overflow
    memcpy(frameBuf + frameLen, up.buf, n);
    frameLen += n;
  } else if (up.status == UPLOAD_FILE_END) {
    frameOk = (frameLen == FRAME_BYTES);
  }
}

// Runs after the upload completes; stores the frame into the target slot
// (?slot=N, default = current) and displays it. ?mid=<n> records the ArUco id.
static void handleFrameDone() {
  if (!frameOk) {
    server.send(400, "text/plain", "expected 32768 bytes of RGB565");
    return;
  }
  int slot = server.hasArg("slot") ? server.arg("slot").toInt() : curSlot;
  if (slot < 0 || slot >= NUM_SLOTS) slot = curSlot;
  markerId = server.hasArg("mid") ? server.arg("mid").toInt() : -1;
  saveSlot(slot);
  curSlot = slot;
  prefs.putInt("slot", curSlot);
  pushFrame();
  server.send(200, "text/plain", "OK slot " + String(slot));
}

// GET /frame?slot=N — stream a slot's raw 32768-byte RGB565 back (read-back).
static void handleFrameGet() {
  int slot = server.hasArg("slot") ? server.arg("slot").toInt() : curSlot;
  if (slot < 0 || slot >= NUM_SLOTS) { server.send(400, "text/plain", "slot 0..3"); return; }
  if (!LittleFS.exists(slotPath(slot))) { server.send(404, "text/plain", "slot empty"); return; }
  File f = LittleFS.open(slotPath(slot), "r");
  if (!f) { server.send(500, "text/plain", "open failed"); return; }
  server.streamFile(f, "application/octet-stream");
  f.close();
}

// GET /show?slot=N — display a stored slot. This is the gesture GET target.
static void handleShow() {
  int slot = server.hasArg("slot") ? server.arg("slot").toInt() : -1;
  if (slot < 0 || slot >= NUM_SLOTS) { server.send(400, "text/plain", "slot 0..3"); return; }
  if (!loadSlot(slot)) { server.send(404, "text/plain", "slot empty"); return; }
  server.send(200, "text/plain", "OK slot " + String(slot));
}

static void handleButtonsGet() {
  String body;
  File f = LittleFS.open(BUTTONS_PATH, "r");
  if (f) { body = f.readString(); f.close(); }
  server.send(200, "text/plain", body);
}

static void handleButtonsPost() {
  String body = server.arg("plain");
  File f = LittleFS.open(BUTTONS_PATH, "w");
  if (!f) { server.send(500, "text/plain", "open failed"); return; }
  f.print(body);
  f.close();
  server.send(200, "text/plain", "OK");
}

// GET/POST /accelerometer. Settings are persisted on the physical tag.
// POST query args: enabled=0|1, requireDrop=0|1, sensitivity=0.01..0.95 g
// (maximum acceleration considered free fall), and hitThreshold=1.05..8.0 g.
// dropConfirmMs=5..250 is accepted for compatibility but no longer gates hits.
static void handleAccelerometer() {
  if (server.method() == HTTP_POST) {
    if (server.hasArg("enabled")) {
      String v = server.arg("enabled");
      accelEnabled = !(v == "0" || v.equalsIgnoreCase("false") || v.equalsIgnoreCase("off"));
      prefs.putBool("accelOn", accelEnabled);
      if (!accelEnabled) {
        dropArmed = false;
      }
    }
    if (server.hasArg("requireDrop")) {
      String v = server.arg("requireDrop");
      accelRequireDrop =
          !(v == "0" || v.equalsIgnoreCase("false") || v.equalsIgnoreCase("off"));
      prefs.putBool("accelReqDrop", accelRequireDrop);
      dropArmed = false;
      lastLowSampleUs = 0;
      impactAboveThreshold = false;
    }
    if (server.hasArg("sensitivity")) {
      float v = server.arg("sensitivity").toFloat();
      if (v < 0.01f || v > 0.95f) {
        server.send(400, "application/json",
                    "{\"ok\":false,\"error\":\"drop sensitivity must be 0.01..0.95 g\"}");
        return;
      }
      accelSensitivity = v;
      prefs.putFloat("accelSens", accelSensitivity);
    }
    if (server.hasArg("dropConfirmMs")) {
      int v = server.arg("dropConfirmMs").toInt();
      if (v < 5 || v > 250) {
        server.send(400, "application/json",
                    "{\"ok\":false,\"error\":\"drop duration must be 5..250 ms\"}");
        return;
      }
      accelDropConfirmMs = (uint32_t)v;
      prefs.putUInt("accelDropMs", accelDropConfirmMs);
    }
    if (server.hasArg("hitThreshold")) {
      float v = server.arg("hitThreshold").toFloat();
      if (v < 1.05f || v > 8.0f) {
        server.send(400, "application/json",
                    "{\"ok\":false,\"error\":\"hit threshold must be 1.05..8.0 g\"}");
        return;
      }
      accelHitThreshold = v;
      prefs.putFloat("accelThr", accelHitThreshold);
    }
  }

  uint32_t impactAge = impactCount ? millis() - lastImpactMs : 0;
  bool hardSurfaceHit = impactCount && impactAge <= IMPACT_RECENT_MS;
  size_t bufferSamples = recentAccelerometerSampleCount(micros());
  String s = "{\"ok\":true"
           + String(",\"available\":") + (accelAvailable ? "true" : "false")
           + ",\"enabled\":" + (accelEnabled ? "true" : "false")
           + ",\"requireDrop\":" + (accelRequireDrop ? "true" : "false")
           + ",\"sampleRateHz\":" + String(accelSampleRateHz)
           + ",\"rangeG\":" + String(accelRangeG)
           + ",\"bufferWindowMs\":3000"
           + ",\"bufferSamples\":" + String(bufferSamples)
           + ",\"shapeRiseWindowMs\":62.5"
           + ",\"shapeMinRiseG\":" + String(SHAPE_MIN_RISE_G, 2)
           + ",\"sensitivity\":" + String(accelSensitivity, 2)
           + ",\"hitThreshold\":" + String(accelHitThreshold, 2)
           + ",\"effectiveThreshold\":" + String(accelHitThreshold, 3)
           + ",\"dropConfirmMs\":" + String(accelDropConfirmMs)
           + ",\"x\":" + String(accelX, 3)
           + ",\"y\":" + String(accelY, 3)
           + ",\"z\":" + String(accelZ, 3)
           + ",\"magnitude\":" + String(accelMagnitude, 3)
           + ",\"dropArmed\":" + (dropArmed ? "true" : "false")
           + ",\"hardSurfaceHit\":" + (hardSurfaceHit ? "true" : "false")
           + ",\"lastImpactG\":" + String(lastImpactG, 3)
           + ",\"lastShapeDropG\":" +
               (lastShapeValid ? String(lastShapeDropG, 3) : "null")
           + ",\"lastShapeDeltaG\":" +
               (lastShapeValid ? String(lastShapeDeltaG, 3) : "null")
           + ",\"lastShapeRiseMs\":" +
               (lastShapeValid ? String(lastShapeRiseMs) : "null")
           + ",\"impactCount\":" + String(impactCount)
           + ",\"lastImpactAgoMs\":" + (impactCount ? String(impactAge) : "null")
           + "}";
  server.send(200, "application/json", s);
}

// GET /accelerometer/history — three seconds of full-rate measurements,
// decimated 10:1 for a 160 samples/s live display. The body is little-endian
// uint16 milli-g values, so a full response is 480 points / 960 bytes.
static void handleAccelerometerHistory() {
  uint32_t nowUs = micros();
  size_t recent = recentAccelerometerSampleCount(nowUs);
  size_t first =
      (accelBufferHead + ACCEL_BUFFER_CAPACITY - recent) % ACCEL_BUFFER_CAPACITY;
  size_t outputCount = 0;

  for (size_t n = 0;
       n < recent && outputCount < ACCEL_DISPLAY_SAMPLES;
       n += ACCEL_DISPLAY_DECIMATION) {
    size_t idx = (first + n) % ACCEL_BUFFER_CAPACITY;
    long milliG = lroundf(accelBuffer[idx].magnitude * 1000.0f);
    if (milliG < 0) milliG = 0;
    if (milliG > 65535) milliG = 65535;
    accelHistoryPayload[outputCount++] = (uint16_t)milliG;
  }

  uint32_t durationUs = 0;
  if (recent > 1) {
    size_t newest =
        (accelBufferHead + ACCEL_BUFFER_CAPACITY - 1) % ACCEL_BUFFER_CAPACITY;
    durationUs = accelBuffer[newest].us - accelBuffer[first].us;
  }
  server.sendHeader("X-Window-Ms", "3000");
  server.sendHeader("X-Source-Rate-Hz", String(accelSampleRateHz));
  server.sendHeader("X-Source-Samples", String(recent));
  server.sendHeader("X-Display-Decimation", String(ACCEL_DISPLAY_DECIMATION));
  server.sendHeader("X-Duration-Us", String(durationUs));
  server.send_P(200, "application/octet-stream",
                reinterpret_cast<const char*>(accelHistoryPayload),
                outputCount * sizeof(uint16_t));
}

// Read one BMI270 sample without M5Unified's interrupt-status gate. That gate
// yields only ~160 readings/s on AtomS3R even when ACC_CONF is set to 1600 Hz.
static bool readRawCaptureSample(AccelCaptureSample& sample, uint32_t timestampUs) {
  int16_t raw[6];
  if (!m5::In_I2C.readRegister(
          bmi270I2cAddr, BMI270_ACC_DATA,
          reinterpret_cast<uint8_t*>(raw), sizeof(raw), BMI270_I2C_HZ)) {
    return false;
  }
  // AtomS3R's M5Unified orientation is Y, -X, Z for accel and gyro.
  static const float ACCEL_RES = 2.0f / 32768.0f;
  static const float GYRO_RES = 2000.0f / 32768.0f;
  float x = raw[1] * ACCEL_RES;
  float y = -raw[0] * ACCEL_RES;
  float z = raw[2] * ACCEL_RES;
  sample = {timestampUs, sqrtf(x * x + y * y + z * z),
            x, y, z,
            raw[4] * GYRO_RES, -raw[3] * GYRO_RES, raw[5] * GYRO_RES, 0};
  return true;
}

// GET /accelerometer/capture — collect the next exact second at 1600 Hz.
// Each packed little-endian record is: uint32 timestamp_us, seven float32
// values (magnitude, accel xyz, gyro xyz), then uint8 detector_hit.
static void handleAccelerometerCapture() {
  if (!accelAvailable || !accelEnabled ||
      M5.Imu.getType() != m5::imu_bmi270 || accelSampleRateHz != 1600) {
    server.send(409, "application/json",
                "{\"ok\":false,\"error\":\"1600 Hz accelerometer is unavailable or disabled\"}");
    return;
  }

  uint32_t startUs = micros();
  size_t count = 0;
  bool aboveThreshold = false;
  for (size_t i = 0; i < ACCEL_CAPTURE_SAMPLES; i++) {
    uint32_t targetUs = startUs + i * ACCEL_BUFFER_MIN_INTERVAL_US;
    while ((int32_t)(targetUs - micros()) > 50) delayMicroseconds(25);
    while ((int32_t)(targetUs - micros()) > 0) {}
    if (!readRawCaptureSample(accelCaptureBuffer[i], micros())) break;

    const float magnitude = accelCaptureBuffer[i].magnitude;
    bool shapeMatch = !accelRequireDrop;
    if (accelRequireDrop && magnitude >= accelHitThreshold) {
      size_t oldest = i > 100 ? i - 100 : 0; // previous 62.5 ms at 1600 Hz
      float minimumG = magnitude;
      for (size_t j = oldest; j < i; j++) {
        if (accelCaptureBuffer[j].magnitude < minimumG)
          minimumG = accelCaptureBuffer[j].magnitude;
      }
      shapeMatch = minimumG <= accelSensitivity &&
                   magnitude - minimumG > SHAPE_MIN_RISE_G;
    }
    if (!aboveThreshold && magnitude >= accelHitThreshold && shapeMatch) {
      accelCaptureBuffer[i].detectorHit = 1;
    }
    if (magnitude >= accelHitThreshold) aboveThreshold = true;
    else if (magnitude < accelHitThreshold - 0.03f) aboveThreshold = false;
    count++;
  }
  server.setContentLength(count * sizeof(AccelCaptureSample));
  server.send(200, "application/octet-stream", "");
  WiFiClient client = server.client();
  for (size_t n = 0; n < count; n++) {
    client.write(reinterpret_cast<const uint8_t*>(&accelCaptureBuffer[n]),
                 sizeof(AccelCaptureSample));
  }
}

// GET /state: current device state. Keeps markerId + battery for atom-manager,
// adds the current slot and which slots are filled.
static void handleState() {
  int mv = batteryMilliVolts();
  String filled = "[";
  for (int i = 0; i < NUM_SLOTS; i++) {
    filled += slotFilled[i] ? "true" : "false";
    if (i < NUM_SLOTS - 1) filled += ",";
  }
  filled += "]";
  uint32_t impactAge = impactCount ? millis() - lastImpactMs : 0;
  bool hardSurfaceHit = impactCount && impactAge <= IMPACT_RECENT_MS;
  size_t bufferSamples = recentAccelerometerSampleCount(micros());
  String s = "{\"name\":\"" + disco::name() + "\""
           + ",\"slot\":" + String(curSlot)
           + ",\"slots\":" + String(NUM_SLOTS)
           + ",\"filled\":" + filled
           + ",\"markerId\":" + String(markerId)
           + ",\"hasFrame\":" + (frameOk ? "true" : "false")
           + ",\"battery\":{\"mv\":" + String(mv)
           + ",\"pct\":" + String(batteryPercent(mv)) + "}"
           + ",\"accelerometer\":{\"available\":" + (accelAvailable ? "true" : "false")
           + ",\"enabled\":" + (accelEnabled ? "true" : "false")
           + ",\"requireDrop\":" + (accelRequireDrop ? "true" : "false")
           + ",\"sampleRateHz\":" + String(accelSampleRateHz)
           + ",\"rangeG\":" + String(accelRangeG)
           + ",\"bufferWindowMs\":3000"
           + ",\"bufferSamples\":" + String(bufferSamples)
           + ",\"shapeRiseWindowMs\":62.5"
           + ",\"shapeMinRiseG\":" + String(SHAPE_MIN_RISE_G, 2)
           + ",\"sensitivity\":" + String(accelSensitivity, 2)
           + ",\"hitThreshold\":" + String(accelHitThreshold, 2)
           + ",\"effectiveThreshold\":" + String(accelHitThreshold, 3)
           + ",\"dropConfirmMs\":" + String(accelDropConfirmMs)
           + ",\"x\":" + String(accelX, 3)
           + ",\"y\":" + String(accelY, 3)
           + ",\"z\":" + String(accelZ, 3)
           + ",\"magnitude\":" + String(accelMagnitude, 3)
           + ",\"dropArmed\":" + (dropArmed ? "true" : "false")
           + ",\"hardSurfaceHit\":" + (hardSurfaceHit ? "true" : "false")
           + ",\"lastImpactG\":" + String(lastImpactG, 3)
           + ",\"lastShapeDropG\":" +
               (lastShapeValid ? String(lastShapeDropG, 3) : "null")
           + ",\"lastShapeDeltaG\":" +
               (lastShapeValid ? String(lastShapeDeltaG, 3) : "null")
           + ",\"lastShapeRiseMs\":" +
               (lastShapeValid ? String(lastShapeRiseMs) : "null")
           + ",\"impactCount\":" + String(impactCount)
           + ",\"lastImpactAgoMs\":" + (impactCount ? String(impactAge) : "null")
           + "}}";
  server.send(200, "application/json", s);
}

static void handlePeers() { server.send(200, "application/json", disco::peersJson()); }

static void handleNameGet() { server.send(200, "text/plain", disco::name()); }

static void handleNamePost() {
  String nm = server.arg("plain"); nm.trim();
  if (!nm.length()) { server.send(400, "text/plain", "empty name"); return; }
  disco::setName(nm);
  server.send(200, "text/plain", disco::name());
}

void setup() {
  auto cfg = M5.config();
  cfg.internal_imu = true;
  M5.begin(cfg);
  M5.Display.setRotation(0);
  accelAvailable = M5.Imu.getType() != m5::imu_none;
  configureAccelerometer();

  // Battery ADC: 12-bit, 2:1 divider on GPIO 8 (see batteryMilliVolts()).
  pinMode(BAT_ADC_PIN, INPUT);
  analogReadResolution(12);

  Serial.begin(115200);
  prefs.begin("wifi", false);
  accelEnabled = prefs.getBool("accelOn", true);
  accelRequireDrop = prefs.getBool("accelReqDrop", true);
  // Firmware before split controls stored its combined value in accelThr.
  // Preserve it as the landing threshold when valid. Old impact-gain values
  // in accelSens are outside the new drop-sensitivity range and migrate to
  // the 0.85 g short-drop default.
  float legacyAccelValue = prefs.getFloat("accelThr", 3.0f);
  accelSensitivity = prefs.getFloat("accelSens", 0.85f);
  accelDropConfirmMs = prefs.getUInt("accelDropMs", 10);
  accelHitThreshold = legacyAccelValue;
  if (accelHitThreshold < 1.05f) accelHitThreshold = 1.05f;
  if (accelHitThreshold > 8.0f) accelHitThreshold = 3.0f;
  if (accelSensitivity < 0.01f || accelSensitivity > 0.95f) accelSensitivity = 0.85f;
  if (accelDropConfirmMs < 5 || accelDropConfirmMs > 250) accelDropConfirmMs = 10;

  // Mount flash and restore the last-shown slot immediately, so the panel comes
  // straight up on its page — no boot animation, no status screen, no WiFi wait.
  if (!LittleFS.begin(true)) Serial.println("LittleFS mount failed — slots won't persist");
  refreshFilled();
  curSlot = prefs.getInt("slot", 0);
  if (curSlot < 0 || curSlot >= NUM_SLOTS) curSlot = 0;
  if (slotFilled[curSlot]) loadSlot(curSlot);   // displays it and sets frameOk

  // Bring WiFi up FIRST — this initialises the lwIP/TCP-IP stack. Starting the
  // web server before any WiFi.mode() call asserts "Invalid mbox" in lwIP.
  String savedSsid = prefs.getString("ssid", "");
  if (savedSsid.length()) startSTA(savedSsid, prefs.getString("pass", ""), false);
  else startAP();

  disco::begin();   // random name (persisted) + UDP discovery, after WiFi is up

  server.on("/", HTTP_GET, handleRoot);
  server.on("/state", HTTP_GET, handleState);
  server.on("/peers", HTTP_GET, handlePeers);
  server.on("/name", HTTP_GET, handleNameGet);
  server.on("/name", HTTP_POST, handleNamePost);
  server.on("/show", HTTP_GET, handleShow);
  server.on("/buttons", HTTP_GET, handleButtonsGet);
  server.on("/buttons", HTTP_POST, handleButtonsPost);
  server.on("/accelerometer", HTTP_GET, handleAccelerometer);
  server.on("/accelerometer", HTTP_POST, handleAccelerometer);
  server.on("/accelerometer/history", HTTP_GET, handleAccelerometerHistory);
  server.on("/accelerometer/capture", HTTP_GET, handleAccelerometerCapture);
  server.on("/frame", HTTP_GET, handleFrameGet);
  // POST /frame: (responder, upload-handler) — the upload handler fires first.
  server.on("/frame", HTTP_POST, handleFrameDone, handleFrameUpload);
  server.begin();

  Serial.println();
  Serial.printf("ATOM FRAMER ready — name \"%s\".\n", disco::name().c_str());
  printHelp();
}

void loop() {
  M5.update();
  server.handleClient();
  pumpSerial();
  pollSTA();
  disco::loop();   // UDP announce + peer table upkeep
  pumpButton();    // short / long / double click -> the current slot's actions
  pumpAccelerometer();
}
