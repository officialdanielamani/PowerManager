// PowerMeter PZEM (1-channel AC or DC) firmware configuration. See docs/firmware.md.
// ESP32-S3 + PZEM-004T (AC) or PZEM-017 (DC).
// The server can make this file: Firmware -> Generate config.h. What each setting means: README.md, section "config.h".
#pragma once

// ================================================================ [1] BOARD INFO
#define FW_VERSION "1.1.0"
#define HW_NAME "esp32s3-n16r8"
#define PROTO_VERSION 1
// 0 = AC meter (PZEM-004T), 1 = DC meter (PZEM-017)
#define METER_DC 0
#if METER_DC
#define METER_KIND "dc"
#define SENSOR_NAME "DC Power Sensor"
#else
#define METER_KIND "ac"
#define SENSOR_NAME "AC Power Sensor"
#endif
#if METER_DC
#define FW_TYPE "pzem-dc"  // firmware type (OTA tag)
#else
#define FW_TYPE "pzem-ac"
#endif
#define HW_INFO SENSOR_NAME " 1Chn"  // free text shown in "Get meter info" on the server

// ================================================================ [2] ENABLE FLAGS (1 = on, 0 = off)
#define HAS_DS3231 0        // DS3231 clock (RTC)
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
#define PIN_PZEM_RX 5        // ESP32 RX  <- PZEM TX (DC: RS485 module RO / RXD)
#define PIN_PZEM_TX 6        // ESP32 TX  -> PZEM RX (DC: RS485 module DI / TXD)
#define PIN_RS485_DE -1      // DC only: MAX485 DE+RE pin; -1 = module with automatic direction
#define PIN_I2C_SDA 8        // I2C (optional parts): DS3231, I2C relay boards, SHT sensors
#define PIN_I2C_SCL 9
#define I2C_FREQ_HZ 100000
#define PIN_LED 48           // WS2812 RGB LED (onboard on many ESP32-S3 dev boards)
#define PIN_BOOT_BUTTON 0    // hold 10 s = factory reset
#define PIN_TAMPER 4         // tamper switch (NC to GND)
#define TAMPER_OPEN_LEVEL HIGH
#define PIN_UPS 2            // UPS signal input
#define UPS_ACTIVE_LEVEL LOW // level = "on UPS" (LOW: contact to GND, HIGH: 3.3 V signal)
#define PIN_TEMP 10           // DS18B20 1-Wire pin
#define PIN_BATTERY_ADC 1    // battery divider tap (ADC1)

// ================================================================ [4] OPTIONS / CONFIGURATION
// ---- sensor
#define PZEM_ADDRESS 0xF8         // general address, works with a single PZEM on the bus
#define PZEM_TIMEOUT_MS 250
#define SENSOR_CHANNELS 1
#if METER_DC
// PZEM-017 shunt: 50, 100, 200 or 300 A (0 = leave as it is)
#define PZEM017_SHUNT_A 100
#define SENSOR_AMP_RATING (PZEM017_SHUNT_A ? (float)PZEM017_SHUNT_A : 300.0f)
#define SUPPLY_LOST_VOLTAGE 5.0f  // below this = no DC supply
#else
#define SENSOR_AMP_RATING 100.0f  // PZEM-004T 100 A version (with CT)
#define SUPPLY_LOST_VOLTAGE 50.0f // below this = AC lost
#endif

// ---- relay outputs
// One entry per relay board: { driver, I2C address, relays used, active high }. Drivers: RELAY_GPIO, RELAY_MCP23017, RELAY_XL9535, RELAY_PCF8574
// CHANNEL_RELAY = relay of the measured line, the others are AUX relays.
#define RELAY_BOARDS { {RELAY_GPIO, 0, 1, 1} }
#define RELAY_GPIO_PINS { 7 }       // pins for RELAY_GPIO entries, in order
#define CHANNEL_RELAY 1             // relay number of the measured line (0 = no relay)

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
#define STACK_CONTROL 6144
#define STACK_LED 3072
#define CONTROL_TICK_MS 250       // tamper/relay loop; sensor read every 4th tick (1 s)
#define TRIP_CONFIRM_READS 2      // reads in a row to trip
#define RELAY_MIN_OFF_MS 2000     // minimum OFF time
#define RELAY_CHATTER_SWITCHES 6  // switches per 60 s = chatter warning
#define TAMPER_DEBOUNCE_TICKS 4   // tamper input must be stable this many ticks (x 250 ms) to count
#define UPS_DEBOUNCE_MS 2000      // UPS input must be stable this long to count
#define VOLT_OUT_READS 3          // operating voltage: out of range for 3 reads (~3 s) to trigger
#define VOLT_OK_READS 10          // ...and back in range for 10 reads (~10 s) to clear

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
#define OUTBOX_RAM_ITEMS 120       // kept in RAM until acknowledged
#define OUTBOX_RAM_MAX_AGE_MS 30000  // ...or this long
#define OUTBOX_BATCH_MAX 50       // items per message
