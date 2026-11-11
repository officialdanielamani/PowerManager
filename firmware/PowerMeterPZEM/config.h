// PowerMeter firmware configuration. See docs/firmware.md.
#pragma once

#define FW_VERSION "0.7.1"
#define HW_NAME "esp32s3-supermini"
#define PROTO_VERSION 1

// ---------------------------------------------------------------- meter type
// One source, two firmwares: build with 0 for the AC meter, 1 for the DC meter.
//   AC meter: PZEM-004T v3 (TTL, 9600 8N1), V / A / W / kWh / PF / Hz
//   DC meter: PZEM-017 (RS485, 9600 8N2) through an RS485-TTL module, V / A / W / kWh
// The meter tells the server its type when pairing; the server picks it up automatically.
#define METER_DC 0

#if METER_DC
#define METER_KIND "dc"
#define SENSOR_NAME "DC Power Sensor"
#else
#define METER_KIND "ac"
#define SENSOR_NAME "AC Power Sensor"
#endif
#define HW_INFO SENSOR_NAME " 1Chn"  // free text shown in "Get meter info" on the server
#define FW_TYPE (METER_DC ? "pzem-dc" : "pzem-ac")  // stored on the meter: a different firmware type clears old state

// ---------------------------------------------------------------- pins (check your board)
#define PIN_PZEM_RX 5       // ESP32 RX  <- PZEM TX (DC: RS485 module RO / RXD)
#define PIN_PZEM_TX 6       // ESP32 TX  -> PZEM RX (DC: RS485 module DI / TXD)
#define PIN_RS485_DE -1     // DC only: MAX485 DE+RE pin; -1 = module with automatic direction
#define PIN_TAMPER 4        // NC switch to GND; open = panel open (INPUT_PULLUP)
#define PIN_LED 48          // WS2812 RGB LED (onboard on most Super Mini boards)
#define PIN_BOOT_BUTTON 0   // hold 10 s = factory reset
#define PIN_I2C_SDA 8       // I2C (optional): DS3231, PCF8574 / XL9535 relay boards, SHT sensors
#define PIN_I2C_SCL 9
#define I2C_FREQ_HZ 100000

// ---------------------------------------------------------------- relay outputs
// One entry per relay board / pin group: { driver, I2C address, relays used, active high }.
//   RELAY_GPIO     ESP32 pins listed in RELAY_GPIO_PINS (address 0)      <- default: SSR on GPIO 7
//   RELAY_XL9535   XL9535 / TCA9535 relay board, up to 16 outputs (0x20-0x27); active high 1
//   RELAY_PCF8574  PCF8574 (0x20-0x27) / PCF8574A (0x38-0x3F), up to 8 outputs; with low-side SSRs usually 0
// "relays used" counts from output 0. Relays are numbered across the entries (entry 1 = relays 1..n, ...).
// CHANNEL_RELAY switches the measured line; every other relay is an AUX relay (manual, schedule or event).
// Examples:  { {RELAY_GPIO, 0, 1, 1} } + RELAY_GPIO_PINS { 7 }              one SSR on GPIO 7 (default)
//            { {RELAY_XL9535, 0x20, 4, 1} }                                  4-relay board: relay 1 + 3 AUX
//            { {RELAY_GPIO, 0, 1, 1}, {RELAY_PCF8574, 0x20, 6, 0} }          SSR on GPIO 7 + 6 AUX outputs
#define RELAY_BOARDS { {RELAY_GPIO, 0, 1, 1} }
#define RELAY_GPIO_PINS { 7 }       // pins for RELAY_GPIO entries, in order
#define CHANNEL_RELAY 1             // relay number of the measured line (0 = no relay)
#define TAMPER_OPEN_LEVEL HIGH   // with NC switch to GND + pull-up, HIGH = open

// ---------------------------------------------------------------- optional parts (0 = not fitted)
// A part switched off here is left out of the firmware completely (smaller binary).
#define HAS_DS3231 0        // DS3231 RTC module on PIN_I2C_SDA / PIN_I2C_SCL (keeps the time without Wi-Fi)
#define TAMPER_ENABLE 1     // tamper / panel switch on PIN_TAMPER
#define TEMP_DS18B20 0      // DS18B20 temperature sensor(s) on PIN_TEMP (see "temperature" below)
#define TEMP_SHT 0          // SHT20 / SHT3x / SHT4x temperature + humidity sensor(s) on the I2C bus
// (BATTERY_ENABLE and MESH_ENABLE are in their own sections below.)

// ---------------------------------------------------------------- temperature / humidity
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
// on the same wire). PIN_TEMP is not used when TEMP_DS18B20 is 0.
#define PIN_TEMP 10
#define TEMP_MAX_SENSORS 4        // DS18B20s at most (SHT sensors: as many as listed above)
#define TEMP_READ_MS 10000        // read (and record) the sensors (SHT and DS18B20) this often
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

// ---------------------------------------------------------------- sensor
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

// ---------------------------------------------------------------- LED
#define LED_BRIGHTNESS 40         // 0-255, kept low on purpose

// ---------------------------------------------------------------- timing
#define STACK_NET 20480           // bytes; TLS handshake + JSON + mesh need room
#define STACK_WEB 10240
#define STACK_CONTROL 6144
#define STACK_LED 3072
#define CONTROL_TICK_MS 250       // tamper/relay loop; sensor read every 4th tick (1 s)
#define TRIP_CONFIRM_READS 2      // over-current must be seen this many reads in a row
#define RELAY_MIN_OFF_MS 2000     // anti-chatter: after switching OFF, stay OFF at least this long
                                  // (OFF is always immediate; only the next ON waits)
#define RELAY_CHATTER_SWITCHES 6  // more relay changes than this within 60 s = warning on serial + event
#define TAMPER_DEBOUNCE_TICKS 4   // tamper input must be stable this many ticks (x 250 ms) to count
#define VOLT_OUT_READS 3          // operating voltage: out of range for 3 reads (~3 s) to trigger
#define VOLT_OK_READS 10          // ...and back in range for 10 reads (~10 s) to clear
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
#define OUTBOX_RAM_ITEMS 120       // readings wait in RAM until acknowledged; flash only when offline
#define OUTBOX_RAM_MAX_AGE_MS 30000  // ...or still unsent after this long
#define OUTBOX_BATCH_MAX 50       // items per "tel" message

// ---------------------------------------------------------------- defaults
#define DEFAULT_REPORT_S 60       // send to the server every ... s
#define DEFAULT_READ_S 60         // record a reading every ... s (sent in batches every report_s)
