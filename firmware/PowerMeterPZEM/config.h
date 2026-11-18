// PowerMeter PZEM (1-channel AC or DC) firmware configuration. See docs/firmware.md.
// ESP32-S3 + PZEM-004T (AC) or PZEM-017 (DC).
// The server can make this file for you: Firmware -> Generate config.h.
#pragma once

// ================================================================ [1] BOARD INFO
#define FW_VERSION "1.1.0"
#define HW_NAME "esp32s3-n16r8"
#define PROTO_VERSION 1
// One source, two firmwares: 0 = AC meter (PZEM-004T v3, TTL), 1 = DC meter (PZEM-017, RS485 module).
#define METER_DC 0
#if METER_DC
#define METER_KIND "dc"
#define SENSOR_NAME "DC Power Sensor"
#else
#define METER_KIND "ac"
#define SENSOR_NAME "AC Power Sensor"
#endif
#define FW_TYPE (METER_DC ? "pzem-dc" : "pzem-ac")  // stored on the meter: a different firmware type clears old state
#define HW_INFO SENSOR_NAME " 1Chn"  // free text shown in "Get meter info" on the server

// ================================================================ [2] ENABLE FLAGS (1 = on, 0 = off)
// A part switched off here is left out of the firmware completely (smaller binary).
#define HAS_DS3231 0        // DS3231 clock (RTC) on the I2C bus: keeps the time without Wi-Fi
#define TAMPER_ENABLE 1     // tamper / panel switch on PIN_TAMPER
#define UPS_ENABLE 0        // UPS signal input on PIN_UPS: event + notification while the UPS runs on battery
#define BATTERY_ENABLE 0    // battery voltage on PIN_BATTERY_ADC (through a resistor divider)
#define TEMP_DS18B20 0      // DS18B20 temperature sensor(s) on PIN_TEMP
#define TEMP_SHT 0          // SHT20 / SHT3x / SHT4x temperature + humidity sensor(s) on the I2C bus
#define NET_WEBSOCKET 1     // link to the server: its own WebSocket
#define NET_MQTT 1          // link to the server: through an MQTT broker (keep at least one of the two)
#define MESH_ENABLE 1       // ESP-NOW mesh: reach the server through a nearby meter when there is no Wi-Fi
#define AP_PASSWORD_IS_DEVICE_ID 1  // setup Wi-Fi "Power_<DeviceID>": 1 = password is the Device ID,
                                    // 0 = random password made on first boot (printed on serial)

// ================================================================ [3] PIN OUT (check your board)
// ESP32-S3 N16R8 (16 MB flash, octal PSRAM): GPIO 26-32 (flash) and 35-37 (PSRAM) are used internally: do not wire them.
#define PIN_PZEM_RX 5        // ESP32 RX  <- PZEM TX (DC: RS485 module RO / RXD)
#define PIN_PZEM_TX 6        // ESP32 TX  -> PZEM RX (DC: RS485 module DI / TXD)
#define PIN_RS485_DE -1      // DC only: MAX485 DE+RE pin; -1 = module with automatic direction
#define PIN_I2C_SDA 8        // I2C (optional parts): DS3231, I2C relay boards, SHT sensors
#define PIN_I2C_SCL 9
#define I2C_FREQ_HZ 100000
#define PIN_LED 48           // WS2812 RGB LED (onboard on many ESP32-S3 dev boards)
#define PIN_BOOT_BUTTON 0    // hold 10 s = factory reset
#define PIN_TAMPER 4         // NC switch to GND; open = panel open (internal pull-up)
#define TAMPER_OPEN_LEVEL HIGH
#define PIN_UPS 2            // UPS signal input
#define UPS_ACTIVE_LEVEL LOW // level that means "running on the UPS": LOW = contact to GND (pull-up),
                             // HIGH = 3.3 V logic signal (pull-down; never 5 V / 12 V directly)
#define PIN_TEMP 10           // DS18B20 1-Wire pin (4.7 k pull-up to 3.3 V)
#define PIN_BATTERY_ADC 1    // battery divider tap (ADC1; keep the pin below ~3.1 V)

// ================================================================ [4] OPTIONS / CONFIGURATION
// ---- sensor
#define PZEM_ADDRESS 0xF8         // general address, works with a single PZEM on the bus
#define PZEM_TIMEOUT_MS 250
#define SENSOR_CHANNELS 1
#if METER_DC
// PZEM-017 shunt: 50, 100, 200 or 300 (A). Written to the PZEM at start-up; 0 = leave as it is.
#define PZEM017_SHUNT_A 100
#define SENSOR_AMP_RATING (PZEM017_SHUNT_A ? (float)PZEM017_SHUNT_A : 300.0f)
#define SUPPLY_LOST_VOLTAGE 5.0f  // below this (or no reply x3) = no DC supply (PZEM-017 needs >= 7 V)
#else
#define SENSOR_AMP_RATING 100.0f  // PZEM-004T 100 A version (with CT)
#define SUPPLY_LOST_VOLTAGE 50.0f // below this (or no reply x3) = AC lost
#endif

// ---- relay outputs
// One entry per relay board / pin group: { driver, I2C address, relays used, active high }.
//   RELAY_GPIO     ESP32 pins listed in RELAY_GPIO_PINS (address 0)      <- default: SSR on GPIO 7
//   RELAY_MCP23017 MCP23017 I/O expander, 16 push-pull outputs GPA0-7 = 0-7, GPB0-7 = 8-15 (0x20-0x27); active high 1
//                  (recommended for SSRs: each output sources / sinks up to 25 mA, 125 mA for the whole chip)
//   RELAY_XL9535   XL9535 / TCA9535 relay board, up to 16 outputs (0x20-0x27); active high 1
//   RELAY_PCF8574  PCF8574 (0x20-0x27) / PCF8574A (0x38-0x3F), up to 8 outputs; with low-side SSRs usually 0
// "relays used" counts from output 0. Relays are numbered across the entries (entry 1 = relays 1..n, ...).
// CHANNEL_RELAY switches the measured line; every other relay is an AUX relay (manual, schedule or event).
// Examples:  { {RELAY_GPIO, 0, 1, 1} } + RELAY_GPIO_PINS { 7 }              one SSR on GPIO 7 (default)
//            { {RELAY_XL9535, 0x20, 4, 1} }                                  4-relay board: relay 1 + 3 AUX
//            { {RELAY_GPIO, 0, 1, 1}, {RELAY_PCF8574, 0x20, 6, 0} }          SSR on GPIO 7 + 6 AUX outputs
//            { {RELAY_MCP23017, 0x20, 8, 1} }                                MCP23017 GPA0-7: relay 1 + 7 AUX
#define RELAY_BOARDS { {RELAY_GPIO, 0, 1, 1} }
#define RELAY_GPIO_PINS { 7 }       // pins for RELAY_GPIO entries, in order
#define CHANNEL_RELAY 1             // relay number of the measured line (0 = no relay)

// ---- temperature / humidity
// Numbering: T1, T2 ... = first the SHT sensors (in the order of SHT_SENSORS), then the DS18B20s sorted by
// serial number (a sensor keeps its number over a restart). Both kinds can be used together.
//
// SHT sensors (TEMP_SHT 1): on the I2C bus (PIN_I2C_SDA / PIN_I2C_SCL, shared with the other I2C parts),
// no extra pin. One entry per sensor, { type, I2C address }:
//   SHT_20  SHT20 / HTU21D          0x40
//   SHT_3X  SHT30 / SHT31 / SHT35   0x44 (ADDR pin to GND) or 0x45 (ADDR to VCC)
//   SHT_4X  SHT40 / SHT41 / SHT45   0x44 (0x45 / 0x46 variants)
// e.g. one SHT30:  { {SHT_3X, 0x44} }     two:  { {SHT_3X, 0x44}, {SHT_3X, 0x45} }
#define SHT_SENSORS { {SHT_4X, 0x44} }
//
// DS18B20 (TEMP_DS18B20 1): on their own 1-Wire pin PIN_TEMP with a 4.7 k pull-up to 3.3 V (several sensors
// on the same wire).
#define TEMP_MAX_SENSORS 4        // DS18B20s at most (SHT sensors: as many as listed above)
#define TEMP_READ_MS 10000        // read (and record) the sensors (SHT and DS18B20) this often
#define TEMP_HYST 5.0f            // over-temperature clears this many degC below the limit

// ---- battery (BATTERY_ENABLE): battery + -- R1 --+-- R2 -- GND, tap to PIN_BATTERY_ADC
#define BATTERY_DIVIDER 2.0f       // (R1 + R2) / R2
#define BATTERY_V_EMPTY 3.30f      // 0 %   (1S Li-ion; e.g. 11.0 / 12.6 for a 3S pack)
#define BATTERY_V_FULL 4.20f       // 100 %

// ---- LED and defaults
#define LED_BRIGHTNESS 40         // 0-255, kept low on purpose
#define DEFAULT_REPORT_S 60       // send to the server every ... s
#define DEFAULT_READ_S 60         // record a reading every ... s (sent in batches every report_s)

// ---- timing
#define STACK_NET 20480           // bytes; TLS handshake + JSON + mesh need room
#define STACK_WEB 10240
#define STACK_CONTROL 6144
#define STACK_LED 3072
#define CONTROL_TICK_MS 250       // tamper/relay loop; sensor read every 4th tick (1 s)
#define TRIP_CONFIRM_READS 2      // over-current must be seen this many reads in a row
#define RELAY_MIN_OFF_MS 2000     // after switching OFF, stay OFF at least this long (OFF is always immediate)
#define RELAY_CHATTER_SWITCHES 6  // more relay changes than this within 60 s = warning on serial + event
#define TAMPER_DEBOUNCE_TICKS 4   // tamper input must be stable this many ticks (x 250 ms) to count
#define UPS_DEBOUNCE_MS 2000      // UPS input must be stable this long to count
#define VOLT_OUT_READS 3          // operating voltage: out of range for 3 reads (~3 s) to trigger
#define VOLT_OK_READS 10          // ...and back in range for 10 reads (~10 s) to clear

// ---- link to the server (NET_WEBSOCKET / NET_MQTT). With both, MQTT is used when the server has a broker
// and the WebSocket otherwise (and as a fallback). Pairing always uses HTTPS.
#define MQTT_FALLBACK_MS (5UL * 60UL * 1000UL)   // broker unreachable this long -> use the WebSocket
#define MQTT_RETRY_MS (30UL * 60UL * 1000UL)     // ...and try the broker again after this
#define WS_PING_INTERVAL_MS 30000
#define WS_PONG_TIMEOUT_MS 10000
#define ACK_TIMEOUT_MS 5000       // resend unacked outbox items after this
#define WIFI_FALLBACK_AP_MS (3UL * 60UL * 1000UL)   // open setup AP if Wi-Fi fails this long
#define WIFI_MAIN_CHECK_MS (5UL * 60UL * 1000UL)    // on the backup Wi-Fi: look for the main one this often
#define AP_CLOSE_AFTER_STA_MS (5UL * 60UL * 1000UL) // close fallback AP after STA is back
#define FACTORY_RESET_HOLD_MS 10000
#define WEB_SESSION_TTL_S (12UL * 3600UL)

// ---- mesh (MESH_ENABLE): Wi-Fi first; without Wi-Fi the meter talks through a nearby online meter. docs/mesh.md
#define MESH_LEAF_AFTER_MS (90UL * 1000UL)            // Wi-Fi down this long -> leaf
#define MESH_WIFI_RETRY_MS (10UL * 60UL * 1000UL)     // leaf with a working mesh link: try Wi-Fi this often
#define MESH_NOLINK_RETRY_MS (2UL * 60UL * 1000UL)    // leaf without a gateway this long -> try Wi-Fi
#define MESH_WIFI_TRY_MS (30UL * 1000UL)              // Wi-Fi retry from leaf: back to mesh after this
#define MESH_BEACON_MS 2000                            // gateway beacon interval
#define MESH_GATEWAY_LOST_MS 10000                     // leaf: no beacon this long -> find another
#define MESH_MAX_LEAVES 10                             // per gateway
#define MESH_SCAN_DWELL_MS 300                         // leaf: listen time per channel when searching
#define MESH_BATCH_MAX 8                               // telemetry items per message on the mesh

// ---- outbox (offline buffer)
// Stored in the LittleFS ("spiffs") partition of partitions.csv (16 MB flash, ~3.9 MB for this):
// choose Tools > Flash Size > 16MB and Tools > Partition Scheme > "Custom".
#define OUTBOX_SEGMENT_ITEMS 50   // items per segment file (~8 KB)
#define OUTBOX_SEGMENTS 100       // max segments -> keeps the newest ~5000 items (~800 KB of the 3.9 MB LittleFS)
#define OUTBOX_RAM_ITEMS 120       // readings wait in RAM until acknowledged; flash only when offline
#define OUTBOX_RAM_MAX_AGE_MS 30000  // ...or still unsent after this long
#define OUTBOX_BATCH_MAX 50       // items per "tel" message
