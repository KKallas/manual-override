// AtomS3R web image server — displays a 128x128 RGB565 bitmap pushed over HTTP.
//
// WiFi: boots into SoftAP unless STA credentials were saved, then auto-joins
// that network. A serial console (115200, USB-CDC) lets you switch at runtime:
//
//   wifi <ssid>:<password>   join a network (saved, auto-reconnects on boot)
//   ip                       print the current IP (DHCP-assigned in STA mode)
//   status                   mode / ssid / ip / rssi
//   ap                       forget wifi + start SoftAP
//   help
//
// HTTP: GET / serves the "ATOM FRAMER" UI; POST /frame takes a 32768-byte
// RGB565 frame (big-endian, as the page packs it) and pushImage()s it.

#include <M5Unified.h>
#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <LittleFS.h>

#include "index_html.h"

static const uint16_t FRAME_W = 128;
static const uint16_t FRAME_H = 128;
static const size_t   FRAME_BYTES = (size_t)FRAME_W * FRAME_H * 2;  // 32768

// The last pushed frame is mirrored to flash so the panel restores it on boot.
static const char* FRAME_PATH = "/frame.bin";

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
static int     markerId = -1;    // ArUco id of the stored frame (-1 = unknown)

static String  lineBuf;          // serial line accumulator
static String  pendingSsid;      // ssid we're currently trying to join
static bool    staConnecting = false;
static uint32_t staDeadline = 0;

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

// Mirror the current frame (and its marker id) to flash so a reboot restores it.
static void saveFrame() {
  File f = LittleFS.open(FRAME_PATH, "w");
  if (!f) { Serial.println("save: open failed"); return; }
  size_t n = f.write(frameBuf, FRAME_BYTES);
  f.close();
  prefs.putInt("mid", markerId);
  if (n != FRAME_BYTES) Serial.printf("save: short write %u/%u\n", (unsigned)n, (unsigned)FRAME_BYTES);
}

// Load a previously saved frame into frameBuf. Returns true if a full frame was
// restored; the caller pushes it to the panel.
static bool loadFrame() {
  if (!LittleFS.exists(FRAME_PATH)) return false;
  File f = LittleFS.open(FRAME_PATH, "r");
  if (!f) return false;
  size_t n = f.read(frameBuf, FRAME_BYTES);
  f.close();
  if (n != FRAME_BYTES) return false;
  markerId = prefs.getInt("mid", -1);
  return true;
}

// ---- battery ---------------------------------------------------------------
static int batteryMilliVolts() {
  return (int)analogReadMilliVolts(BAT_ADC_PIN) * 2;  // undo the 2:1 divider
}

static int batteryPercent(int mv) {
  int pct = (mv - BAT_EMPTY_MV) * 100 / (BAT_FULL_MV - BAT_EMPTY_MV);
  return pct < 0 ? 0 : (pct > 100 ? 100 : pct);
}

// ---- on-screen status ------------------------------------------------------
static void showStatus() {
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setTextColor(TFT_GREEN, TFT_BLACK);
  M5.Display.setTextSize(1);
  M5.Display.setCursor(3, 3);
  M5.Display.println("ATOM FRAMER");
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

// Runs after the upload completes; pushes the frame if it was the right size,
// then mirrors it to flash so it survives a reboot. An optional ?mid=<n> query
// arg records which ArUco marker this frame is, so GET /state can report it.
static void handleFrameDone() {
  if (!frameOk) {
    server.send(400, "text/plain", "expected 32768 bytes of RGB565");
    return;
  }
  markerId = server.hasArg("mid") ? server.arg("mid").toInt() : -1;
  pushFrame();
  saveFrame();
  server.send(200, "text/plain", "OK");
}

// GET /state: current device state for the host poller / local GUI.
static void handleState() {
  int mv = batteryMilliVolts();
  String s = "{\"markerId\":" + String(markerId)
           + ",\"hasFrame\":" + (frameOk ? "true" : "false")
           + ",\"battery\":{\"mv\":" + String(mv)
           + ",\"pct\":" + String(batteryPercent(mv)) + "}}";
  server.send(200, "application/json", s);
}

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Display.setRotation(0);

  // Battery ADC: 12-bit, 2:1 divider on GPIO 8 (see batteryMilliVolts()).
  pinMode(BAT_ADC_PIN, INPUT);
  analogReadResolution(12);

  Serial.begin(115200);
  prefs.begin("wifi", false);

  // Restore the last image from flash (if any) and paint it immediately, so the
  // panel comes straight up showing the frame — no boot animation, no status
  // screen, no WiFi wait. Format on first mount.
  if (!LittleFS.begin(true)) Serial.println("LittleFS mount failed — frame won't persist");
  if (loadFrame()) { frameOk = true; pushFrame(); }

  // Bring WiFi up FIRST — this initialises the lwIP/TCP-IP stack. Starting the
  // web server before any WiFi.mode() call asserts "Invalid mbox" in lwIP.
  String savedSsid = prefs.getString("ssid", "");
  if (savedSsid.length()) startSTA(savedSsid, prefs.getString("pass", ""), false);
  else startAP();

  server.on("/", HTTP_GET, handleRoot);
  server.on("/state", HTTP_GET, handleState);
  // POST /frame: (responder, upload-handler) — the upload handler fires first.
  server.on("/frame", HTTP_POST, handleFrameDone, handleFrameUpload);
  server.begin();

  Serial.println();
  Serial.println("ATOM FRAMER ready.");
  printHelp();
}

void loop() {
  M5.update();
  server.handleClient();
  pumpSerial();
  pollSTA();
  if (M5.BtnA.wasPressed()) showStatus();  // tap the screen-button to recall status
}
