// PowerMeter rbAmp (multi-channel AC) firmware configuration. See docs/multichannel.md.
// ESP32-S3 + rbAmp BASIC (SCT-013 clamps, I2C) + I2C relay board.
// The server can make this file: Firmware -> Generate config.h. What each setting means: README.md, section "config.h".
#pragma once

// ================================================================ [1] BOARD INFO
#define FW_VERSION "1.2.0"
#define HW_NAME "esp32s3-n16r8"
#define PROTO_VERSION 1
#define METER_KIND "ac"
#define SENSOR_NAME "AC Power Sensor"
#define MC_MAX_CH 16              // channels the firmware can hold (the CHANNELS table sets how many are used)
#define FW_TYPE "rbamp-mc"        // stored on the meter: a different firmware type clears old state
#define HW_INFO "AC Power Sensor (multi-channel)"  // free text shown in "Get meter info" on the server

// ================================================================ [2] ENABLE FLAGS (1 = on, 0 = off)
#define HAS_DS3231 1        // DS3231 clock (RTC)
#define TAMPER_ENABLE 1     // tamper switch
#define UPS_ENABLE 0        // UPS signal input
#define BATTERY_ENABLE 0    // battery %
#define TEMP_DS18B20 0      // DS18B20 temperature
#define TEMP_SHT 0          // SHT temperature + humidity
#define NET_WEBSOCKET 1     // link: WebSocket
#define NET_MQTT 1          // link: MQTT broker (keep one of the two)
#define MESH_ENABLE 1       // ESP-NOW mesh
#define AP_PASSWORD_IS_DEVICE_ID 1  // setup Wi-Fi password: 1 = Device ID, 0 = random

// ================================================================ [3] PIN OUT (check your board)
// ESP32-S3 N16R8: do not use GPIO 26-32 and 35-37
#define PIN_I2C_SDA 8        // shared I2C bus: rbAmp, DS3231, relay board, SHT sensors
#define PIN_I2C_SCL 9
#define I2C_FREQ_HZ 50000    // rbAmp recommendation for ESP32 hosts (do not use 400 kHz)
#define PIN_DRDY 5           // rbAmp DRDY of board 1 (open-drain, internal pull-up; 10 k to 3.3 V for long wires)
                             // -1 = not wired: Normal mode only (the module is read once a second)
#define PIN_LED 48           // WS2812 RGB LED (onboard on many ESP32-S3 dev boards)
#define PIN_BOOT_BUTTON 0    // hold 10 s = factory reset
#define PIN_TAMPER 4         // tamper switch (NC to GND)
#define TAMPER_OPEN_LEVEL HIGH
#define PIN_UPS 2            // UPS signal input
#define UPS_ACTIVE_LEVEL LOW // level = "on UPS" (LOW: contact to GND, HIGH: 3.3 V signal)
#define PIN_TEMP 6           // DS18B20 1-Wire pin
#define PIN_BATTERY_ADC 1    // battery divider tap (ADC1)

// ================================================================ [4] OPTIONS / CONFIGURATION
// ---- power sensor boards: rbAmp BASIC
// One I2C address per rbAmp module (board 1 = first). New modules: README, "Giving a module its own address".
#define RBAMP_BOARDS { 0x50 }
#define SUPPLY_LOST_VOLTAGE 50.0f          // below this = AC lost

// Clamp models (written to the module at start-up); CT-series modules with factory-paired clamps: SCT_KEEP
#define SCT_KEEP 0
#define SCT_5A 1      // SCT-013-005
#define SCT_10A 2     // SCT-013-010
#define SCT_20A 6     // SCT-013-020
#define SCT_30A 3     // SCT-013-030
#define SCT_50A 4     // SCT-013-050
#define SCT_60A 7     // SCT-013-060
#define SCT_100A 5    // SCT-013-100

// ---- relay outputs
// One entry per relay board: { driver, I2C address, relays used, active high }. Drivers: RELAY_MCP23017, RELAY_XL9535, RELAY_PCF8574, RELAY_GPIO
#define RELAY_BOARDS { {RELAY_XL9535, 0x20, 2, 1} }
#define RELAY_GPIO_PINS { -1 }      // pins for RELAY_GPIO entries

// ---- channels: what is wired where
// One line per channel: rbAmp board, input 1..7, clamp, relay board (1 = A), output (0, 0 = no relay). Unused outputs = AUX relays.
#define CHANNELS { \
  /* board  input  clamp     relay board  output */ \
  {  1,     1,     SCT_30A,  1,           1 },   /* CH1 */ \
  {  1,     2,     SCT_30A,  1,           2 },   /* CH2 */ \
}

// ---- temperature / humidity: SHT sensors { type, I2C address } (SHT_20, SHT_3X, SHT_4X); DS18B20 on PIN_TEMP
#define SHT_SENSORS { {SHT_4X, 0x44} }
#define TEMP_MAX_SENSORS 4        // DS18B20s at most
#define TEMP_READ_MS 10000        // read the sensors this often
#define TEMP_HYST 5.0f            // over-temperature clears this far below the limit

// ---- battery (BATTERY_ENABLE)
#define BATTERY_DIVIDER 2.0f       // (R1 + R2) / R2 of the divider
#define BATTERY_V_EMPTY 3.30f      // 0 %
#define BATTERY_V_FULL 4.20f       // 100 %

// ---- LED and defaults
#define LED_BRIGHTNESS 40         // 0-255, kept low on purpose
#define DEFAULT_REPORT_S 60       // send to the server every ... s
#define DEFAULT_READ_S 60         // record a reading every ... s (sent in batches every report_s)

// ---- timing
#define STACK_NET 20480           // bytes
#define STACK_WEB 10240
#define STACK_CONTROL 8192
#define STACK_LED 3072
#define CONTROL_TICK_MS 250       // control loop
#define SENSOR_READ_MS 1000       // read the module this often
#define ENERGY_PERIOD_MS 1000     // energy period
#define ENERGY_SAVE_MS 300000     // save kWh counters this often
#define TRIP_CONFIRM_READS 2      // reads in a row to trip
#define RELAY_MIN_OFF_MS 2000     // minimum OFF time
#define HW_CONFIRM_MS 60000UL     // new hardware profile must answer within this, else rollback
#define RELAY_CHATTER_SWITCHES 6  // switches per 60 s = chatter warning
#define TAMPER_DEBOUNCE_MS 1000   // tamper input must be stable this long to count
#define UPS_DEBOUNCE_MS 2000      // UPS input must be stable this long to count
#define VOLT_OUT_MS 3000          // operating voltage: out of range this long to trigger
#define VOLT_OK_MS 10000          // ...and back in range this long to clear

// ---- link to the server
#define MQTT_FALLBACK_MS (5UL * 60UL * 1000UL)   // broker unreachable this long -> use the WebSocket
#define MQTT_RETRY_MS (30UL * 60UL * 1000UL)     // ...and try the broker again after this
#define WS_PING_INTERVAL_MS 30000
#define WS_PONG_TIMEOUT_MS 10000
#define ACK_TIMEOUT_MS 5000       // resend unacked items after this
#define WIFI_FALLBACK_AP_MS (3UL * 60UL * 1000UL)   // open setup AP if Wi-Fi fails this long
#define WIFI_MAIN_CHECK_MS (5UL * 60UL * 1000UL)    // backup Wi-Fi: look for the main one
#define AP_CLOSE_AFTER_STA_MS (5UL * 60UL * 1000UL) // close fallback AP after Wi-Fi is back
#define FACTORY_RESET_HOLD_MS 10000
#define WEB_SESSION_TTL_S (12UL * 3600UL)

// ---- mesh (MESH_ENABLE), docs/mesh.md
#define MESH_LEAF_AFTER_MS (90UL * 1000UL)            // Wi-Fi down this long -> leaf
#define MESH_WIFI_RETRY_MS (10UL * 60UL * 1000UL)     // leaf with a working mesh link: try Wi-Fi this often
#define MESH_NOLINK_RETRY_MS (2UL * 60UL * 1000UL)    // leaf without a gateway this long -> try Wi-Fi
#define MESH_WIFI_TRY_MS (30UL * 1000UL)              // Wi-Fi retry from leaf: back to mesh after this
#define MESH_BEACON_MS 2000                            // gateway beacon interval
#define MESH_GATEWAY_LOST_MS 10000                     // leaf: no beacon this long -> find another
#define MESH_MAX_LEAVES 10                             // per gateway
#define MESH_SCAN_DWELL_MS 300                         // leaf: listen time per channel when searching
#define MESH_BATCH_MAX 8                               // telemetry items per message on the mesh

// ---- outbox (offline buffer on LittleFS; oldest data is dropped when full)
#define OUTBOX_SEGMENT_ITEMS 50   // items per file
#define OUTBOX_SEGMENTS 100       // max files: newest ~5000 items are kept
#define OUTBOX_RAM_ITEMS 200       // kept in RAM until acknowledged
#define OUTBOX_RAM_BYTES 64000   // ...at most this many bytes
#define OUTBOX_RAM_MAX_AGE_MS 120000 // ...or this long
#define OUTBOX_BATCH_MAX 50       // items per message
