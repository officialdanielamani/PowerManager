"""config.h generator (Firmware page)."""
import os

import pytest

from app.services import cfggen

FW = os.path.join(os.path.dirname(__file__), "..", "..", "firmware")
BASE = {"pin_sda": 8, "pin_scl": 9, "pin_led": 48}


@pytest.mark.parametrize("name,sketch", [("pzem_config.h", "PowerMeterPZEM"), ("rbamp_config.h", "PowerMeterRBAmp")])
def test_templates_match_the_firmware(name, sketch):
    src = os.path.join(FW, sketch, "config.h")
    if not os.path.exists(src):
        pytest.skip("firmware folder not here (Docker image)")
    with open(src, encoding="utf-8") as a, open(os.path.join(cfggen.DIR, name), encoding="utf-8") as b:
        assert a.read() == b.read(), f"copy firmware/{sketch}/config.h to server/app/firmware_cfg/{name}"


def test_generate_rbamp():
    text, errors, notes = cfggen.generate(dict(BASE, **{
        "meter": "rbamp", "boards": ["0x50", "0x51"], "drdy": False,
        "relays": [{"driver": "RELAY_MCP23017", "addr": "0x20", "n": "10", "high": True}],
        "channels": [{"board": 1, "input": i, "clamp": "SCT_30A", "relay_board": 1, "relay_out": i} for i in range(1, 6)]
        + [{"board": 2, "input": 1, "clamp": "SCT_100A", "relay_board": 0, "relay_out": 0}],
        "rtc": True, "tamper": False, "ds18b20": True, "pin_temp": 6, "ds_max": 2,
        "sht": [{"type": "SHT_3X", "addr": "0x44"}], "mesh": False, "net_ws": True, "net_mqtt": False}))
    assert not errors
    for line in ("#define RBAMP_BOARDS { 0x50, 0x51 }", "#define RELAY_BOARDS { {RELAY_MCP23017, 0x20, 10, 1} }",
                 "#define PIN_DRDY -1", "#define HAS_DS3231 1", "#define TAMPER_ENABLE 0", "#define TEMP_DS18B20 1",
                 "#define TEMP_SHT 1", "#define SHT_SENSORS { {SHT_3X, 0x44} }", "#define MESH_ENABLE 0",
                 "#define NET_MQTT 0", "  {  2,     1,     SCT_100A, 0,           0 },   /* CH6 */ \\"):
        assert line in text, line
    assert any("AUX relays: 6, 7, 8, 9, 10" in n for n in notes)


def test_generate_pzem_and_errors():
    text, errors, _ = cfggen.generate(dict(BASE, **{
        "meter": "pzem_dc", "pin_pzem_rx": 5, "pin_pzem_tx": 6, "pin_de": -1, "shunt": 200, "channel_relay": 1,
        "relays": [{"driver": "RELAY_GPIO", "n": 1, "high": True}], "gpio_pins": "7", "ap_pass_id": False}))
    assert not errors
    assert "#define METER_DC 1" in text and "#define PZEM017_SHUNT_A 200" in text
    assert "#define RELAY_GPIO_PINS { 7 }" in text and "#define AP_PASSWORD_IS_DEVICE_ID 0" in text
    # wrong input: address clash, relay used twice, relay that does not exist, no link
    text, errors, _ = cfggen.generate(dict(BASE, **{
        "meter": "rbamp", "boards": ["0x50"], "relays": [{"driver": "RELAY_PCF8574", "addr": "0x20", "n": 2, "high": False}],
        "channels": [{"board": 1, "input": 1, "clamp": "SCT_30A", "relay_board": 1, "relay_out": 1},
                     {"board": 1, "input": 2, "clamp": "SCT_30A", "relay_board": 1, "relay_out": 1},
                     {"board": 1, "input": 3, "clamp": "SCT_30A", "relay_board": 1, "relay_out": 5}],
        "sht": [{"type": "SHT_20", "addr": "0x40"}, {"type": "SHT_3X", "addr": "0x40"}], "net_ws": False, "net_mqtt": False}))
    assert text is None
    joined = " | ".join(errors)
    for part in ("0x40 is used twice", "relay board A output 1 is already used by another channel", "CH3: relay output", "keep WebSocket or MQTT"):
        assert part in joined, joined


def test_firmware_page(admin):
    page = admin.get("/firmware").get_data(as_text=True)
    assert "List of firmware" in page and 'href="/firmware/config"' in page and "Add New Firmware" in page
    page = admin.get("/firmware/config?meter=pzem_dc").get_data(as_text=True)
    assert "Generate config.h" in page and '<option value="pzem_dc" selected>' in page
    r = admin.post("/firmware/config", json=dict(BASE, meter="pzem_ac", pin_pzem_rx=5, pin_pzem_tx=6, channel_relay=0)).get_json()
    assert r["ok"] and "#define CHANNEL_RELAY 0" in r["text"]
