// EXAMPLE config.h for PowerMeterRBAmp: copy it over PowerMeterRBAmp/config.h (keep your pins / Wi-Fi settings).
//   2 x rbAmp UI5 (5 current inputs each, SCT-013-030 30 A / 1 V clamps)  -> 10 channels
//   2 x PCF8574 (16 outputs): P0-P4 of each drive the 10 SSRs, P5-P7 of each = 6 AUX relays
//   DS3231 RTC, 2 x DS18B20 (T1, T2: meter-wide, not tied to a channel), tamper limit switch
// PowerMeter MC (multi-channel AC) firmware configuration. See docs/multichannel.md.
// ESP32-S3 Super Mini + rbAmp BASIC (SCT-013 clamps, I2C) + DS3231 + tamper switch + I2C relay board.
#pragma once

#define FW_VERSION "0.9.1"
#define HW_NAME "esp32s3-supermini"
#define PROTO_VERSION 1

#define METER_KIND "ac"
#define SENSOR_NAME "AC Power Sensor"
#define MC_MAX_CH 16              // channels the firmware can hold (the CHANNELS table below sets how many are used)
#define FW_TYPE "rbamp-mc"        // stored on the meter: a different firmware type clears old state

// ---------------------------------------------------------------- pins (check your board)
#define PIN_I2C_SDA 8       // shared I2C bus: rbAmp, DS3231, relay board
#define PIN_I2C_SCL 9
#define I2C_FREQ_HZ 50000   // rbAmp recommendation for ESP32 hosts (do not use 400 kHz)
#define PIN_DRDY 5           // rbAmp DRDY (open-drain, internal pull-up used; add 10 k to 3.3 V for long wires)
#define PIN_TAMPER 4        // NC switch to GND; open = panel open (INPUT_PULLUP)
#define PIN_LED 48          // WS2812 RGB LED (onboard on most Super Mini boards)
#define PIN_BOOT_BUTTON 0   // hold 10 s = factory reset

#define TAMPER_OPEN_LEVEL HIGH   // with NC switch to GND + pull-up, HIGH = open

// ---------------------------------------------------------------- optional parts (0 = not fitted)
// A part switched off here is left out of the firmware completely (smaller binary).
#define HAS_DS3231 1        // DS3231 RTC module on the I2C bus (keeps the time without Wi-Fi)
#define TAMPER_ENABLE 1     // tamper / panel switch on PIN_TAMPER
#define TEMP_DS18B20 1      // DS18B20 temperature sensor(s) on PIN_TEMP (see "temperature" below)
#define TEMP_SHT 0          // SHT20 / SHT3x / SHT4x temperature + humidity sensor(s) on the I2C bus
// (BATTERY_ENABLE and MESH_ENABLE are in their own sections below.)

// ---------------------------------------------------------------- power sensor boards: rbAmp BASIC
// One entry per rbAmp module on the I2C bus: its I2C address. Board 1 = first entry, board 2 = second...
// New modules all answer at 0x50: give every extra module its own address ONCE, with only that module
// connected, by typing in the Serial Monitor:   rbamp addr 0x50 0x51
// ("i2c scan" lists what answers on the bus.) DRDY is taken from board 1.
#define RBAMP_BOARDS { 0x50, 0x51 }        // module 2: type "rbamp addr 0x50 0x51" once, with only that module connected
#define SUPPLY_LOST_VOLTAGE 50.0f          // below this = AC lost (the module keeps answering: it has its own 5 V)

// Clamp models (written to the module at start-up). CT-series modules with factory-paired clamps: SCT_KEEP.
#define SCT_KEEP 0
#define SCT_5A 1      // SCT-013-005
#define SCT_10A 2     // SCT-013-010
#define SCT_20A 6     // SCT-013-020
#define SCT_30A 3     // SCT-013-030
#define SCT_50A 4     // SCT-013-050
#define SCT_60A 7     // SCT-013-060
#define SCT_100A 5    // SCT-013-100

// ---------------------------------------------------------------- relay outputs
// One entry per relay board / pin group: { driver, I2C address, relays used, active high }.
//   RELAY_XL9535   XL9535 / TCA9535 relay board, up to 16 outputs (0x20-0x27, A0-A2 jumpers); active high 1
//   RELAY_PCF8574  PCF8574 (0x20-0x27) / PCF8574A (0x38-0x3F), up to 8 outputs; with low-side SSRs usually 0
//   RELAY_GPIO     ESP32 pins listed in RELAY_GPIO_PINS (address 0)
// "relays used" counts from output 0 (P0): 5 = outputs 0-4. Relays are numbered across the entries:
// entry 1 = relays 1..n, entry 2 continues with n+1 ... Examples:
//   { {RELAY_PCF8574, 0x20, 5, 0}, {RELAY_PCF8574, 0x21, 7, 0} }   PCF8574 (0-4) + PCF8574 (0-6) = 12 relays
//   { {RELAY_XL9535, 0x20, 2, 1}, {RELAY_XL9535, 0x21, 8, 1} }     XL9535 2-relay + 8-relay board = 10 relays
//   { {RELAY_GPIO, 0, 2, 1} } with RELAY_GPIO_PINS { 10, 11 }      two SSRs on GPIO 10 / 11
// Both PCF8574 with all 8 outputs: relays 1-8 = board 0x20 P0-P7, relays 9-16 = board 0x21 P0-P7.
// Active low (0) for SSRs / relay modules switched to GND by the PCF8574 (it can sink, not source, current).
#define RELAY_BOARDS { {RELAY_PCF8574, 0x20, 8, 0}, {RELAY_PCF8574, 0x21, 8, 0} }
#define RELAY_GPIO_PINS { -1 }      // pins for RELAY_GPIO entries, in order (-1 = none)

// ---------------------------------------------------------------- channels: what is wired where
// One line per measured channel, in order: the first line is CH1, the second CH2 ...
//   board: 1 = first rbAmp in RBAMP_BOARDS      input: current input on that board (1..7)
//   clamp: SCT_* model of the clamp on it       relay: relay number switching it (0 = no relay)
// Relays that no channel uses are AUX relays (manual ON / OFF from the server, not metered).
// Voltage comes from the first board that has a voltage input (UI* variants).
#define CHANNELS { \
  /* board  input  clamp     relay */ \
  {  1,     1,     SCT_30A,  1 },   /* CH1  = module 1 input 1, SSR on PCF8574 0x20 P0 */ \
  {  1,     2,     SCT_30A,  2 },   /* CH2 */ \
  {  1,     3,     SCT_30A,  3 },   /* CH3 */ \
  {  1,     4,     SCT_30A,  4 },   /* CH4 */ \
  {  1,     5,     SCT_30A,  5 },   /* CH5  = 0x20 P4 */ \
  {  2,     1,     SCT_30A,  9 },   /* CH6  = module 2 input 1, SSR on PCF8574 0x21 P0 */ \
  {  2,     2,     SCT_30A,  10 },  /* CH7 */ \
  {  2,     3,     SCT_30A,  11 },  /* CH8 */ \
  {  2,     4,     SCT_30A,  12 },  /* CH9 */ \
  {  2,     5,     SCT_30A,  13 },  /* CH10 = 0x21 P4 */ \
}
// -> AUX relays (automation on the server): AUX1-3 = relays 6-8 (0x20 P5-P7), AUX4-6 = relays 14-16 (0x21 P5-P7)
// Example, rbAmp UI5 (0x50) + rbAmp I7 (0x51), 8 channels, two XL9535 8-relay boards (0x20, 0x21):
//   #define RBAMP_BOARDS { 0x50, 0x51 }
//   #define RELAY_BOARDS { {RELAY_XL9535, 0x20, 8, 1}, {RELAY_XL9535, 0x21, 8, 1} }
//   #define CHANNELS { {1, 1, SCT_100A, 1}, {1, 2, SCT_30A, 2}, {1, 3, SCT_30A, 3}, {1, 4, SCT_30A, 4},
//                      {1, 5, SCT_30A, 5}, {2, 1, SCT_20A, 9}, {2, 2, SCT_20A, 10}, {2, 3, SCT_20A, 11} }
//   -> relays 6, 7, 8 and 12..16 are AUX relays.

// ---------------------------------------------------------------- temperature / humidity
// Sensors are numbered T1, T2 ...: first the SHT sensors in the order below, then the DS18B20s sorted by
// serial number (a sensor keeps its number over a restart). Both kinds can be used together.
// SHT (TEMP_SHT 1): one entry per sensor, { type, I2C address }:
//   SHT_20 (SHT20 / HTU21D, 0x40)   SHT_3X (SHT30/31/35, 0x44 or 0x45)   SHT_4X (SHT40/41/45, 0x44 / 0x45 / 0x46)
#define SHT_SENSORS { {SHT_4X, 0x44} }
// DS18B20 (TEMP_DS18B20 1): on one 1-Wire pin with a 4.7 k pull-up to 3.3 V, up to TEMP_MAX_SENSORS.
#define PIN_TEMP 6
#define TEMP_MAX_SENSORS 4
#define TEMP_READ_MS 10000        // read (and record) the temperatures this often
#define TEMP_HYST 5.0f            // over-temperature clears this many degC below the limit

// ---------------------------------------------------------------- battery (optional)
// Measures the DC UPS / backup battery through a resistor divider into an ADC pin:
//   battery + ── R1 ──┬── R2 ── GND        BATTERY_DIVIDER = (R1 + R2) / R2
//                     └── PIN_BATTERY_ADC   (keep the pin below ~3.1 V)
// Example: 1S Li-ion (4.2 V max) with R1 = R2 = 100 k  ->  divider 2.0.
#define BATTERY_ENABLE 0
#define PIN_BATTERY_ADC 1          // GPIO1 = ADC1 channel 0 (ADC1 works while Wi-Fi is on)
#define BATTERY_DIVIDER 2.0f
#define BATTERY_V_EMPTY 3.30f      // 0 %   (1S Li-ion; e.g. 11.0 / 12.6 for a 3S pack)
#define BATTERY_V_FULL 4.20f       // 100 %

// ---------------------------------------------------------------- setup Wi-Fi (access point)
// SSID is always "Power_<DeviceID>".
// 1 = password is the same Device ID (easy: SSID Power_K7P29QXM, password K7P29QXM).
//     Anyone who can see the SSID can join, so finish setup (create the admin) promptly.
// 0 = random 8-character password made on first boot (printed on serial; write it on a label).
#define AP_PASSWORD_IS_DEVICE_ID 1

// ---------------------------------------------------------------- LED
#define LED_BRIGHTNESS 40         // 0-255, kept low on purpose

// ---------------------------------------------------------------- timing
#define STACK_NET 20480           // bytes; TLS handshake + JSON + mesh need room
#define STACK_WEB 10240
#define STACK_CONTROL 8192
#define STACK_LED 3072
#define CONTROL_TICK_MS 250       // control loop without DRDY (with DRDY it runs on every 100 ms window)
#define SENSOR_READ_MS 1000       // without DRDY: read the module this often
#define ENERGY_PERIOD_MS 1000     // close an rbAmp period (energy) this often
#define ENERGY_SAVE_MS 300000     // save the kWh counters to NVS this often (and on session changes)
#define TRIP_CONFIRM_READS 2      // over-current must be seen this many reads in a row
#define RELAY_MIN_OFF_MS 2000     // anti-chatter: after switching OFF, stay OFF at least this long
                                  // (OFF is always immediate; only the next ON waits)
#define RELAY_CHATTER_SWITCHES 6  // more relay changes than this within 60 s = warning on serial + event
#define TAMPER_DEBOUNCE_MS 1000   // tamper input must be stable this long to count
#define VOLT_OUT_MS 3000          // operating voltage: out of range this long to trigger
#define VOLT_OK_MS 10000          // ...and back in range this long to clear
#define WS_PING_INTERVAL_MS 30000
#define WS_PONG_TIMEOUT_MS 10000
#define ACK_TIMEOUT_MS 5000       // resend unacked outbox items after this
#define WIFI_FALLBACK_AP_MS (3UL * 60UL * 1000UL)   // open setup AP if Wi-Fi fails this long
#define AP_CLOSE_AFTER_STA_MS (5UL * 60UL * 1000UL) // close fallback AP after STA is back
#define FACTORY_RESET_HOLD_MS 10000
#define WEB_SESSION_TTL_S (12UL * 3600UL)

// ---------------------------------------------------------------- mesh (ESP-NOW)
// Auto role: Wi-Fi first; if Wi-Fi is down for MESH_LEAF_AFTER_MS the meter becomes a "leaf" and
// reaches the server through a nearby "gateway" meter (any Auto meter that is online on Wi-Fi).
// One hop only. See docs/mesh.md.
#define MESH_ENABLE 1                                 // 0 = no ESP-NOW at all (behaves like firmware 0.3)
#define MESH_LEAF_AFTER_MS (90UL * 1000UL)            // Wi-Fi down this long -> leaf
#define MESH_WIFI_RETRY_MS (10UL * 60UL * 1000UL)     // leaf with a working mesh link: try Wi-Fi this often
#define MESH_NOLINK_RETRY_MS (2UL * 60UL * 1000UL)    // leaf without a gateway this long -> try Wi-Fi
#define MESH_WIFI_TRY_MS (30UL * 1000UL)              // Wi-Fi retry from leaf: back to mesh after this
#define MESH_BEACON_MS 2000                            // gateway beacon interval
#define MESH_GATEWAY_LOST_MS 10000                     // leaf: no beacon this long -> find another
#define MESH_MAX_LEAVES 10                             // per gateway
#define MESH_SCAN_DWELL_MS 300                         // leaf: listen time per channel when searching
#define MESH_BATCH_MAX 8                               // telemetry items per message on the mesh

// ---------------------------------------------------------------- outbox (offline buffer)
// Stored in the LittleFS partition: choose Tools > Partition Scheme >
// "Minimal SPIFFS (1.9MB APP with OTA/128KB SPIFFS)".
#define OUTBOX_SEGMENT_ITEMS 50   // items per segment file (~8 KB)
#define OUTBOX_SEGMENTS 10        // max segments -> keeps the newest ~500 items (min 30 required)
#define OUTBOX_RAM_ITEMS 200       // readings wait in RAM until acknowledged; flash only when offline
#define OUTBOX_RAM_BYTES 64000   // ...and at most this much (DRDY: ~90 s of 2-channel windows)
#define OUTBOX_RAM_MAX_AGE_MS 120000 // ...or still unsent after this long (DRDY send interval up to 90 s)
#define OUTBOX_BATCH_MAX 50       // items per "tel" message

// ---------------------------------------------------------------- defaults
#define DEFAULT_REPORT_S 60       // send to the server every ... s
#define DEFAULT_READ_S 60         // record a reading every ... s (sent in batches every report_s)
#define HW_INFO "AC Power Sensor (multi-channel)"  // free text shown in "Get meter info" on the server
