// PowerMeter MC (multi-channel AC) firmware configuration. See docs/multichannel.md.
// ESP32-S3 Super Mini + rbAmp BASIC (SCT-013 clamps, I2C) + DS3231 + tamper switch + I2C relay board.
#pragma once

#define FW_VERSION "0.7.2"
#define HW_NAME "esp32s3-supermini"
#define PROTO_VERSION 1

#define METER_KIND "ac"
#define SENSOR_NAME "AC Power Sensor"
#define MC_MAX_CH 7               // rbAmp BASIC: up to 7 current channels (UI7)
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

// Set to 1 if a DS3231 RTC module is fitted (used in this project).
#define HAS_DS3231 1

// ---------------------------------------------------------------- power sensor: rbAmp BASIC
// The variant (UI1/UI2/UI3/UI5/UI7 or I1/I2/I3 = channels, with or without voltage) is read from
// the module. Set the SCT-013 model of the clamp on each channel (written to the module at start-up):
//   1 = SCT-013-005 (5 A)    2 = SCT-013-010 (10 A)   6 = SCT-013-020 (20 A)   3 = SCT-013-030 (30 A)
//   4 = SCT-013-050 (50 A)   7 = SCT-013-060 (60 A)   5 = SCT-013-100 (100 A)  0 = leave as it is
// (CT-series modules with factory-paired clamps: use 0 for every channel.)
#define RBAMP_ADDRESS 0x50
#define RBAMP_CT_MODELS {3, 3, 3, 3, 3, 3, 3}   // channel 1..7
#define SUPPLY_LOST_VOLTAGE 50.0f // below this = AC lost (the module keeps answering: it has its own 5 V)

// ---------------------------------------------------------------- relay board (I2C)
// One relay per measuring channel: relay 1 = channel 1, ... Extra relays stay off.
#define RELAY_PCF8574 1     // PCF8574 / PCF8574A I/O expander driving SSRs / a relay module
#define RELAY_XL9535 2      // XL9535 (TCA9535-compatible) relay board, 1/2/4/8 relays
#define RELAY_DRIVER RELAY_XL9535
#define RELAY_ADDRESS 0x20  // PCF8574 0x20-0x27, PCF8574A 0x38-0x3F, XL9535 0x20-0x27 (A0-A2 jumpers)
#define RELAY_COUNT 2       // relays on the board (1, 2, 4 or 8)
#define RELAY_ACTIVE_HIGH 1 // XL9535 boards: 1. PCF8574 with low-side SSR / relay modules: usually 0

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
