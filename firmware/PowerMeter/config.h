// PowerMeter firmware configuration. See docs/firmware.md.
#pragma once

#define FW_VERSION "0.3.0"
#define HW_NAME "esp32s3-supermini"
#define HW_INFO "PZEM-004T 1Chn"   // free text shown in "Get meter info" on the server
#define PROTO_VERSION 1

// ---------------------------------------------------------------- pins (check your board)
#define PIN_PZEM_RX 5       // ESP32 RX  <- PZEM TX
#define PIN_PZEM_TX 6       // ESP32 TX  -> PZEM RX
#define PIN_RELAY 7         // SSR control, active HIGH
#define PIN_TAMPER 4        // NC switch to GND; open = panel open (INPUT_PULLUP)
#define PIN_LED 48          // WS2812 RGB LED (onboard on most Super Mini boards)
#define PIN_BOOT_BUTTON 0   // hold 10 s = factory reset
#define PIN_I2C_SDA 8       // DS3231 (optional)
#define PIN_I2C_SCL 9

#define RELAY_ACTIVE_HIGH 1
#define TAMPER_OPEN_LEVEL HIGH   // with NC switch to GND + pull-up, HIGH = open

// Set to 1 if a DS3231 RTC module is fitted.
#define HAS_DS3231 0

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
#define SENSOR_AMP_RATING 100.0f  // PZEM-004T 100 A version (with CT)
#define AC_LOST_VOLTAGE 50.0f     // below this (or no reply x3) = AC lost

// ---------------------------------------------------------------- LED
#define LED_BRIGHTNESS 40         // 0-255, kept low on purpose

// ---------------------------------------------------------------- timing
#define CONTROL_TICK_MS 250       // tamper/relay loop; sensor read every 4th tick (1 s)
#define TRIP_CONFIRM_READS 2      // over-current must be seen this many reads in a row
#define VOLT_OUT_READS 3          // operating voltage: out of range for 3 reads (~3 s) to trigger
#define VOLT_OK_READS 10          // ...and back in range for 10 reads (~10 s) to clear
#define WS_PING_INTERVAL_MS 30000
#define WS_PONG_TIMEOUT_MS 10000
#define ACK_TIMEOUT_MS 5000       // resend unacked outbox items after this
#define WIFI_FALLBACK_AP_MS (3UL * 60UL * 1000UL)   // open setup AP if Wi-Fi fails this long
#define AP_CLOSE_AFTER_STA_MS (5UL * 60UL * 1000UL) // close fallback AP after STA is back
#define FACTORY_RESET_HOLD_MS 10000
#define WEB_SESSION_TTL_S (12UL * 3600UL)

// ---------------------------------------------------------------- outbox (offline buffer)
// Stored in the LittleFS partition: choose Tools > Partition Scheme >
// "Minimal SPIFFS (1.9MB APP with OTA/128KB SPIFFS)".
#define OUTBOX_SEGMENT_ITEMS 50   // items per segment file (~8 KB)
#define OUTBOX_SEGMENTS 10        // max segments -> keeps the newest ~500 items (min 30 required)
#define OUTBOX_BATCH_MAX 50       // items per "tel" message

// ---------------------------------------------------------------- defaults
#define DEFAULT_REPORT_S 60
