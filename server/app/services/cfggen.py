"""config.h generator (Firmware page): fills the firmware's own config.h with the chosen hardware.

The templates in app/firmware_cfg/ are copies of firmware/PowerMeterPZEM/config.h and
firmware/PowerMeterRBAmp/config.h (tests/test_cfggen.py fails when they differ), so the generated file
always matches the firmware version shipped with this server.
"""
import os
import re

DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "firmware_cfg")

METERS = {"rbamp": "Multi-channel AC (rbAmp + SCT-013 clamps) - sketch PowerMeterRBAmp",
          "pzem_ac": "1-channel AC (PZEM-004T) - sketch PowerMeterPZEM",
          "pzem_dc": "1-channel DC (PZEM-017) - sketch PowerMeterPZEM"}
DRIVERS = {"RELAY_GPIO": ("ESP32 pins (GPIO)", 32), "RELAY_MCP23017": ("MCP23017 (I2C, 16 outputs)", 16),
           "RELAY_XL9535": ("XL9535 / TCA9535 relay board (I2C, 16 outputs)", 16),
           "RELAY_PCF8574": ("PCF8574 (I2C, 8 outputs)", 8)}
CLAMPS = {"SCT_5A": "SCT-013-005 (5 A)", "SCT_10A": "SCT-013-010 (10 A)", "SCT_20A": "SCT-013-020 (20 A)",
          "SCT_30A": "SCT-013-030 (30 A)", "SCT_50A": "SCT-013-050 (50 A)", "SCT_60A": "SCT-013-060 (60 A)",
          "SCT_100A": "SCT-013-100 (100 A)", "SCT_KEEP": "Keep the module's setting (CT series)"}
SHT = {"SHT_20": "SHT20 / HTU21D", "SHT_3X": "SHT30 / SHT31 / SHT35", "SHT_4X": "SHT40 / SHT41 / SHT45"}
SHUNTS = (50, 100, 200, 300)


def template(meter):
    with open(os.path.join(DIR, "rbamp_config.h" if meter == "rbamp" else "pzem_config.h"), encoding="utf-8") as fh:
        return fh.read()


def version(meter):
    m = re.search(r'#define FW_VERSION "([^"]+)"', template(meter))
    return m.group(1) if m else "?"


def _set(text, name, value):
    """Replace the value of `#define NAME ...` (first one), keeping the comment behind it."""
    pat = re.compile(r"^(#define %s[ \t]+)(.*?)([ \t]*//.*)?$" % re.escape(name), re.M)
    if not pat.search(text):
        raise KeyError(name)
    return pat.sub(lambda m: m.group(1) + str(value) + (m.group(3) or ""), text, count=1)


def _int(raw, lo, hi, label, errors, default=None):
    """Whole number, decimal or 0x.. hex."""
    try:
        v = int(str(raw).strip(), 0)
        if lo <= v <= hi:
            return v
    except (TypeError, ValueError):
        pass
    rng = f"{lo:#04x} to {hi:#04x}" if "address" in label else f"{lo} to {hi}"
    errors.append(f"{label}: enter a value from {rng}")
    return default if default is not None else lo


def _num(raw, lo, hi, label, errors, default):
    try:
        v = float(str(raw).strip())
        if lo <= v <= hi:
            return v
    except (TypeError, ValueError):
        pass
    errors.append(f"{label}: enter a number from {lo} to {hi}")
    return default


def _on(o, key, default=False):
    v = o.get(key, default)
    return v in (True, "on", "1", 1, "true")


def generate(o):
    """o: options from the Firmware page (JSON). Returns (config.h text or None, errors, notes)."""
    errors, notes = [], []
    meter = o.get("meter")
    if meter not in METERS:
        return None, ["Choose a meter type"], []
    rb = meter == "rbamp"
    t = template(meter)
    i2c = {}  # address -> what uses it

    def claim(addr, what):
        if addr in i2c:
            errors.append(f"I2C address {addr:#04x} is used twice: {i2c[addr]} and {what}")
        i2c[addr] = what

    # ---- pins
    pins = {}

    def pin(key, name, label, lo=0):
        v = _int(o.get(key, ""), lo, 48, label, errors)
        if v >= 0:
            if v in pins:
                errors.append(f"GPIO {v} is used twice: {pins[v]} and {label}")
            pins[v] = label
        return v

    sda, scl = pin("pin_sda", "PIN_I2C_SDA", "I2C SDA pin"), pin("pin_scl", "PIN_I2C_SCL", "I2C SCL pin")
    t = _set(_set(t, "PIN_I2C_SDA", sda), "PIN_I2C_SCL", scl)
    t = _set(t, "PIN_LED", pin("pin_led", "PIN_LED", "LED pin"))

    # ---- relay outputs
    rows, gpio_needed, total = [], 0, 0
    for k, r in enumerate(o.get("relays") or []):
        drv = r.get("driver")
        if drv not in DRIVERS:
            continue
        label = f"Relay board {k + 1}"
        n = _int(r.get("n", ""), 1, DRIVERS[drv][1], f"{label}: relays used", errors, 1)
        high = 1 if _on(r, "high", True) else 0
        addr = 0
        if drv == "RELAY_GPIO":
            gpio_needed += n
        else:
            lo, hi = (0x20, 0x3F) if drv == "RELAY_PCF8574" else (0x20, 0x27)
            addr = _int(r.get("addr", ""), lo, hi, f"{label}: I2C address", errors, lo)
            claim(addr, f"{label} ({DRIVERS[drv][0].split(' (')[0]})")
        rows.append("{%s, %s, %d, %d}" % (drv, f"0x{addr:02X}" if addr else "0", n, high))
        total += n
    if total > 32:
        errors.append("At most 32 relays in total")
    gpio_pins = [p for p in re.split(r"[,\s]+", str(o.get("gpio_pins", "")).strip()) if p]
    gp = [_int(p, 0, 48, "Relay GPIO pins", errors) for p in gpio_pins][:32]
    if gpio_needed > len(gp):
        errors.append(f"Relay GPIO pins: list {gpio_needed} pin(s), one for every GPIO relay (e.g. 10, 11)")
    for p in gp[:gpio_needed]:
        if p in pins:
            errors.append(f"GPIO {p} is used twice: {pins[p]} and a relay")
        pins[p] = "a relay"
    t = _set(t, "RELAY_BOARDS", "{ " + ", ".join(rows) + " }" if rows else "{ {RELAY_GPIO, 0, 0, 1} }")
    t = _set(t, "RELAY_GPIO_PINS", "{ " + ", ".join(str(p) for p in gp[:gpio_needed]) + " }" if gpio_needed and gp else "{ -1 }")

    # ---- power sensor
    used = {}
    if rb:
        boards = [b for b in (o.get("boards") or []) if str(b).strip()]
        addrs = [_int(b, 0x08, 0x77, f"rbAmp board {k + 1}: I2C address", errors, 0x50) for k, b in enumerate(boards)][:8]
        if not addrs:
            errors.append("Add at least one rbAmp board")
        for k, a in enumerate(addrs):
            claim(a, f"rbAmp board {k + 1}")
        t = _set(t, "RBAMP_BOARDS", "{ " + ", ".join(f"0x{a:02X}" for a in addrs) + " }")
        t = _set(t, "PIN_DRDY", pin("pin_drdy", "PIN_DRDY", "rbAmp DRDY pin", -1) if _on(o, "drdy", True) else -1)
        chans, seen = [], set()
        for k, c in enumerate(o.get("channels") or []):
            label = f"CH{k + 1}"
            b = _int(c.get("board", ""), 1, max(len(addrs), 1), f"{label}: board", errors, 1)
            inp = _int(c.get("input", ""), 1, 7, f"{label}: input", errors, 1)
            clamp = c.get("clamp") if c.get("clamp") in CLAMPS else "SCT_30A"
            relay = _int(c.get("relay", "0") or "0", 0, max(total, 0), f"{label}: relay", errors, 0)
            if (b, inp) in seen:
                errors.append(f"{label}: board {b} input {inp} is already used by another channel")
            seen.add((b, inp))
            if relay:
                if relay in used:
                    errors.append(f"{label}: relay {relay} is already used by {used[relay]}")
                used[relay] = label
            chans.append((b, inp, clamp, relay))
        if not chans:
            errors.append("Add at least one channel")
        if len(chans) > 16:
            errors.append("At most 16 channels")
        body = "#define CHANNELS { \\\n  /* board  input  clamp     relay */ \\\n"
        for k, (b, inp, clamp, relay) in enumerate(chans):
            body += "  {  %d,     %d,     %-9s %d },   /* CH%d */ \\\n" % (b, inp, clamp + ",", relay, k + 1)
        body += "}"
        pat = re.compile(r"^#define CHANNELS \{ \\\n(?:.*\\\n)*\}", re.M)
        if not pat.search(t):
            raise KeyError("CHANNELS")
        t = pat.sub(lambda m: body, t, count=1)
    else:
        dc = meter == "pzem_dc"
        t = _set(t, "METER_DC", 1 if dc else 0)
        t = _set(t, "PIN_PZEM_RX", pin("pin_pzem_rx", "PIN_PZEM_RX", "PZEM RX pin"))
        t = _set(t, "PIN_PZEM_TX", pin("pin_pzem_tx", "PIN_PZEM_TX", "PZEM TX pin"))
        if dc:
            t = _set(t, "PIN_RS485_DE", pin("pin_de", "PIN_RS485_DE", "RS485 DE pin", -1))
            shunt = _int(o.get("shunt", 100), 0, 300, "Shunt", errors, 100)
            if shunt not in SHUNTS and shunt != 0:
                errors.append("Shunt: 50, 100, 200 or 300 A (0 = leave the PZEM as it is)")
            t = _set(t, "PZEM017_SHUNT_A", shunt)
        relay = _int(o.get("channel_relay", "0") or "0", 0, max(total, 0), "Relay of the measured line", errors, 0)
        if relay:
            used[relay] = "the measured line"
        t = _set(t, "CHANNEL_RELAY", relay)

    # ---- optional parts
    rtc = _on(o, "rtc")
    t = _set(t, "HAS_DS3231", int(rtc))
    if rtc:
        claim(0x68, "DS3231 clock")
    tamper = _on(o, "tamper")
    t = _set(t, "TAMPER_ENABLE", int(tamper))
    if tamper:
        t = _set(t, "PIN_TAMPER", pin("pin_tamper", "PIN_TAMPER", "Tamper switch pin"))
    ds = _on(o, "ds18b20")
    t = _set(t, "TEMP_DS18B20", int(ds))
    if ds:
        t = _set(t, "PIN_TEMP", pin("pin_temp", "PIN_TEMP", "DS18B20 pin"))
        t = _set(t, "TEMP_MAX_SENSORS", _int(o.get("ds_max", 4), 1, 8, "DS18B20 sensors", errors, 4))
    sht = []
    for k, s in enumerate(o.get("sht") or []):
        if s.get("type") not in SHT:
            continue
        a = _int(s.get("addr", ""), 0x40, 0x46, f"SHT sensor {k + 1}: I2C address", errors, 0x44)
        claim(a, f"SHT sensor {k + 1}")
        sht.append("{%s, 0x%02X}" % (s["type"], a))
    t = _set(t, "TEMP_SHT", int(bool(sht)))
    if sht:
        t = _set(t, "SHT_SENSORS", "{ " + ", ".join(sht) + " }")
    bat = _on(o, "battery")
    t = _set(t, "BATTERY_ENABLE", int(bat))
    if bat:
        t = _set(t, "PIN_BATTERY_ADC", pin("pin_battery", "PIN_BATTERY_ADC", "Battery ADC pin"))
        t = _set(t, "BATTERY_DIVIDER", "%.2ff" % _num(o.get("bat_div", 2), 1, 100, "Battery divider", errors, 2.0))
        lo_v = _num(o.get("bat_empty", 3.3), 0.5, 100, "Battery empty voltage", errors, 3.3)
        hi_v = _num(o.get("bat_full", 4.2), 0.5, 100, "Battery full voltage", errors, 4.2)
        if hi_v <= lo_v:
            errors.append("Battery: the full voltage must be higher than the empty voltage")
        t = _set(_set(t, "BATTERY_V_EMPTY", "%.2ff" % lo_v), "BATTERY_V_FULL", "%.2ff" % hi_v)

    # ---- defaults
    ws, mqtt = _on(o, "net_ws", True), _on(o, "net_mqtt", True)
    if not ws and not mqtt:
        errors.append("Link to the server: keep WebSocket or MQTT (or both)")
    t = _set(_set(t, "NET_WEBSOCKET", int(ws)), "NET_MQTT", int(mqtt))
    t = _set(t, "MESH_ENABLE", int(_on(o, "mesh", True)))
    t = _set(t, "AP_PASSWORD_IS_DEVICE_ID", int(_on(o, "ap_pass_id", True)))
    t = _set(t, "LED_BRIGHTNESS", _int(o.get("led", 40), 0, 255, "LED brightness", errors, 40))
    read_s = _int(o.get("read_s", 60), 1, 3600, "Record a reading every", errors, 60)
    report_s = _int(o.get("report_s", 60), 5, 3600, "Send to the server every", errors, 60)
    t = _set(_set(t, "DEFAULT_REPORT_S", report_s), "DEFAULT_READ_S", read_s)
    info = re.sub(r'[^\w .,()+/-]', "", str(o.get("hw_info", "")).strip())[:60]
    if info:
        t = _set(t, "HW_INFO", '"%s"' % info)
    if errors:
        return None, errors, []

    aux = [r for r in range(1, total + 1) if r not in used]
    notes.append(f"Sketch: {'PowerMeterRBAmp' if rb else 'PowerMeterPZEM'} (firmware {version(meter)}). "
                 "Replace the whole config.h of that sketch with this text.")
    if total:
        notes.append(f"{total} relay(s): " + (", ".join(f"relay {r} = {w}" for r, w in sorted(used.items())) or "none switches a measured line")
                     + (f"; AUX relays: {', '.join(map(str, aux))}" if aux else ""))
    else:
        notes.append("No relays: the meter only measures.")
    if i2c:
        notes.append("I2C bus: " + ", ".join(f"{a:#04x} {w}" for a, w in sorted(i2c.items())))
    if rb and len(i2c) and any(a != 0x50 for a, w in i2c.items() if w.startswith("rbAmp")):
        notes.append('New rbAmp modules all answer at 0x50: give each extra module its address once with '
                     '"rbamp addr 0x50 0x51" in the Serial Monitor (only that module connected).')
    return t, [], notes
