#!/usr/bin/env python3
"""End-to-end test of the MQTT link on a PC, with a real broker:

    server (app/services/mqtt_bridge.py, real paho client, MQTT over WebSocket)
        <->  broker (amqtt)  <->
    "meter" = the firmware's own src/mqttlink.cpp compiled for the PC (MQTT packets over TCP)

Checks: Test connection, login + last will, signed hello -> welcome, reading -> stored + ack, a wrong key is
ignored, the last will marks the meter offline.

    pip install amqtt paho-mqtt        (plus the server's requirements, g++ and OpenSSL headers)
    python3 firmware/tools/mqttlink_test/run.py <path to ArduinoJson/src>
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
TCP, WS, PREFIX = 18831, 18841, "pmtest"


def start_broker():
    from amqtt.broker import Broker
    cfg = {"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{TCP}"},
                         "ws": {"type": "ws", "bind": f"127.0.0.1:{WS}"}},
           "sys_interval": 0, "auth": {"allow-anonymous": True, "plugins": ["auth_anonymous"]},
           "topic-check": {"enabled": False}}
    loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(Broker(cfg, loop=loop).start())
        loop.run_forever()
    threading.Thread(target=run, daemon=True).start()
    time.sleep(1.5)


def build_meter(arduinojson):
    w = tempfile.mkdtemp()
    os.makedirs(os.path.join(w, "src"))
    os.makedirs(os.path.join(w, "inc", "mbedtls"))
    fw = os.path.join(ROOT, "firmware", "PowerMeterRBAmp", "src")
    for f in ("mqttlink.h", "mqttlink.cpp"):
        shutil.copy(os.path.join(fw, f), os.path.join(w, "src", f))
    st = os.path.join(HERE, "stubs")
    for f in ("certs.h", "storage.h", "timekeep.h"):
        shutil.copy(os.path.join(st, f), os.path.join(w, "src", f))
    for f in ("Arduino.h", "Preferences.h", "WebSocketsClient.h"):
        shutil.copy(os.path.join(st, f), os.path.join(w, "inc", f))
    shutil.copy(os.path.join(st, "mbedtls", "md.h"), os.path.join(w, "inc", "mbedtls", "md.h"))
    shutil.copy(os.path.join(st, "config.h"), os.path.join(w, "config.h"))
    shutil.copy(os.path.join(HERE, "meter.cpp"), os.path.join(w, "meter.cpp"))
    exe = os.path.join(w, "meter")
    subprocess.run(["g++", "-std=gnu++17", "-Wno-deprecated-declarations", "-I", os.path.join(w, "inc"), "-I", arduinojson,
                    os.path.join(w, "meter.cpp"), os.path.join(w, "src", "mqttlink.cpp"), "-o", exe, "-lcrypto"], check=True)
    return exe


def main():
    arduinojson = sys.argv[1]
    exe = build_meter(arduinojson)
    start_broker()
    os.environ["POWERMANAGER_INSTANCE"] = tempfile.mkdtemp()
    os.environ.setdefault("SECRET_KEY", "test")
    sys.path.insert(0, os.path.join(ROOT, "server"))
    from app import create_app
    from app.extensions import db
    from app.models import Device, Measurement, PairingCode, now
    from app.routes.device_api import pair_device
    from app.services import hub, mqtt_bridge, settings
    app = create_app()
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + name)
        ok = ok and bool(cond)

    with app.app_context():
        settings.set_many({"mqtt_enabled": "1", "mqtt_host": "127.0.0.1", "mqtt_port": str(WS), "mqtt_tls": "0",
                           "mqtt_transport": "websockets", "mqtt_path": "/", "mqtt_user": "meters", "mqtt_pass": "s3cret",
                           "mqtt_prefix": PREFIX})
        cfg = mqtt_bridge.server_config()
        check("Test connection: broker reachable over WebSocket", mqtt_bridge.test_connection(cfg)[0])
        check("Test connection: wrong port is reported", not mqtt_bridge.test_connection(dict(cfg, port=18999), timeout=3)[0])
        db.session.add(PairingCode(code="ABCD2345", expires_at=now() + 600))
        db.session.commit()
        status, body = pair_device({"code": "ABCD2345", "device_id": "MQTTEST1", "fw": "t", "proto": 1,
                                    "caps": {"channels": 1, "relays": 1}})
        check("pairing gives the broker (auto link)", status == 200 and body["mqtt"]["port"] == WS)
        key, pk = body["link_key"], Device.query.one().id
        mqtt_bridge._client = mqtt_bridge._connect(app, cfg)
    for _ in range(50):
        if mqtt_bridge.status["state"] == "connected":
            break
        time.sleep(0.1)
    check("server bridge connected and subscribed", mqtt_bridge.status["state"] == "connected")
    time.sleep(0.5)

    bad = subprocess.run([exe, str(TCP), "MQTTEST1", key, PREFIX, "tamper"], capture_output=True, text=True, timeout=30)
    check("a meter with the wrong key gets no answer", bad.returncode == 1 and "welcome=0" in bad.stdout)
    good = subprocess.run([exe, str(TCP), "MQTTEST1", key, PREFIX], capture_output=True, text=True, timeout=30)
    print(good.stdout.strip())
    check("firmware MQTT client: login, subscribe, hello -> welcome, reading -> ack", good.returncode == 0)
    time.sleep(1.5)  # the meter process is gone without a DISCONNECT: the broker sends its last will
    with app.app_context():
        m = Measurement.query.filter_by(device_fk=pk, seq=7).first()
        check("the reading is stored (288.1 W)", m is not None and m.w == 288.1)
        check("last will marked the meter offline", not hub.is_online(pk) and not db.session.get(Device, pk).online)
    print("ALL PASSED" if ok else "SOME CHECKS FAILED")
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
