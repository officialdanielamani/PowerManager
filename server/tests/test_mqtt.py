"""MQTT link: broker settings, auto link at pairing, signed envelope both ways, Wi-Fi / broker change."""
import json

import pytest

from app.extensions import db
from app.models import Command, Device, Measurement, now
from app.services import hub, mesh, mqtt_bridge, settings

from .test_pairing import _code, _pair


class FakeClient:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, qos=0):
        self.sent.append((topic, json.loads(payload)))

    def inner(self, t):
        return [json.loads(p["m"]) for _topic, p in self.sent if json.loads(p["m"]).get("t") == t]


@pytest.fixture()
def broker(app, admin, monkeypatch):
    r = admin.post("/settings/mqtt", data={"mqtt_enabled": "on", "mqtt_host": "wss://mqtt.example.com/", "mqtt_tls": "on",
                                           "mqtt_transport": "websockets", "mqtt_path": "/", "mqtt_user": "meters",
                                           "mqtt_pass": "s3cret", "mqtt_prefix": "pm"})
    assert r.status_code == 302
    fake = FakeClient()
    monkeypatch.setattr(mqtt_bridge, "_client", fake)
    mqtt_bridge._conns.clear()
    yield fake
    mqtt_bridge._conns.clear()


def _up(dev_id, key, n, msg):
    m = json.dumps(msg, separators=(",", ":"))
    return json.dumps({"n": n, "h": mesh.sign(key, dev_id, n, m), "m": m}).encode()


def test_settings_and_auto_link_at_pairing(app, admin, broker):
    with app.app_context():
        assert settings.get("mqtt_host") == "mqtt.example.com"
        assert mqtt_bridge.meter_config() == {"enabled": True, "host": "mqtt.example.com", "port": 443, "tls": True,
                                              "path": "/", "user": "meters", "pass": "s3cret", "prefix": "pm"}
    body = _pair(admin, _code(admin)).get_json()
    assert body["mqtt"]["host"] == "mqtt.example.com" and body["mqtt"]["prefix"] == "pm"
    page = admin.get("/settings/?tab=mqtt").get_data(as_text=True)
    assert "s3cret" not in page and "MQTT broker" in page  # the password is never sent back
    # server on the LAN (TCP), meters through Cloudflare
    admin.post("/settings/mqtt", data={"mqtt_enabled": "on", "mqtt_host": "10.0.0.5", "mqtt_transport": "tcp",
                                       "mqtt_prefix": "pm", "mqtt_pub_host": "mqtt.example.com", "mqtt_pub_tls": "on"})
    with app.app_context():
        assert settings.get("mqtt_pass") == "s3cret"  # empty password field keeps the saved one
        assert mqtt_bridge.server_config()["port"] == 1883
        mc = mqtt_bridge.meter_config()
        assert (mc["host"], mc["port"], mc["tls"]) == ("mqtt.example.com", 443, True)


def test_signed_messages_through_the_broker(app, admin, broker):
    body = _pair(admin, _code(admin)).get_json()
    key = body["link_key"]
    with app.app_context():
        d = Device.query.one()
        pk, dev = d.id, d.device_id
        t = now()
        hello = {"t": "hello", "fw": "0.7.2", "proto": 1, "up": 10, "boot": 1, "tv": 1, "ts": t, "cfg_ver": 0,
                 "caps": {"kind": "ac", "channels": 1, "relays": 1, "mqtt": True, "ws": True}}
        mqtt_bridge.handle_message(f"pm/{dev}/up", _up(dev, key, 1, hello))
        assert hub.is_online(pk) and db.session.get(Device, pk).online
        topic, env = broker.sent[0]
        assert topic == f"pm/{dev}/down" and env["h"] == mesh.sign(key, dev, env["n"], env["m"])  # signed downlink
        assert broker.inner("welcome")
        tel = {"t": "tel", "items": [{"seq": 5, "ts": t, "tv": 1, "ch": [{"i": 0, "v": 230, "a": 1, "w": 230, "e": 1.5}]}]}
        mqtt_bridge.handle_message(f"pm/{dev}/up", _up(dev, key, 2, tel))
        assert Measurement.query.filter_by(device_fk=pk, seq=5).count() == 1 and broker.inner("ack")
        # replay and forgery are dropped
        n_before = len(broker.sent)
        mqtt_bridge.handle_message(f"pm/{dev}/up", _up(dev, key, 2, tel))
        bad = json.loads(_up(dev, key, 9, tel))
        bad["m"] = bad["m"].replace("230", "999")
        mqtt_bridge.handle_message(f"pm/{dev}/up", json.dumps(bad).encode())
        mqtt_bridge.handle_message(f"pm/{dev}/up", _up(dev, "wrong-key", 10, tel))
        assert len(broker.sent) == n_before and db.session.get(Device, pk).link_up == 2
        # a command goes out through the broker
        admin_cmd = admin.post(f"/meters/{pk}/action", json={"action": "renew"}).get_json()
        assert admin_cmd["ok"] and broker.inner("cmd")[-1]["c"] == "renew"
        # last will: offline
        mqtt_bridge.handle_message(f"pm/{dev}/status", b"offline")
        assert not hub.is_online(pk) and not db.session.get(Device, pk).online
        # the server restarted while the meter stayed on the broker: the next message gets a "rehello"
        mqtt_bridge.handle_message(f"pm/{dev}/up", _up(dev, key, 3, {"t": "ping"}))
        assert hub.is_online(pk) and broker.inner("rehello")


def test_change_wifi_and_broker_from_the_server(app, admin, broker):
    body = _pair(admin, _code(admin)).get_json()
    with app.app_context():
        d = Device.query.one()
        d.caps = dict(d.caps, mqtt=True)
        db.session.commit()
        pk, dev, key = d.id, d.device_id, body["link_key"]
        mqtt_bridge.handle_message(f"pm/{dev}/up", _up(dev, key, 1, {"t": "hello", "up": 1, "boot": 1, "cfg_ver": 0}))
    assert admin.post(f"/meters/{pk}/connection", data={"what": "wifi", "ssid": "Workshop", "wifi_pass": "12345678"}).status_code == 302
    with app.app_context():
        cmd = Command.query.filter_by(device_fk=pk, c="set_wifi").one()
        assert cmd.params == {"ssid": "Workshop", "pass": "12345678"} and broker.inner("cmd")[-1]["ssid"] == "Workshop"
        mqtt_bridge.handle_message(f"pm/{dev}/up", _up(dev, key, 2, {"t": "cmd_ack", "ref": cmd.id, "ok": True}))
        assert db.session.get(Command, cmd.id).params["pass"] == ""  # wiped once acknowledged
    assert admin.post(f"/meters/{pk}/connection", data={"what": "wifi", "ssid": "x", "wifi_pass": "short"}).status_code == 302
    with app.app_context():
        assert Command.query.filter_by(device_fk=pk, c="set_wifi").count() == 1  # bad password: nothing sent
    # this meter: WebSocket only, then its own broker address
    admin.post(f"/meters/{pk}/connection", data={"what": "mqtt", "mqtt_mode": "off"})
    assert broker.inner("cmd")[-1] == {"t": "cmd", "c": "set_mqtt", "ref": broker.inner("cmd")[-1]["ref"], "enabled": False,
                                       "id": broker.inner("cmd")[-1]["id"]}
    admin.post(f"/meters/{pk}/connection", data={"what": "mqtt", "mqtt_mode": "custom", "host": "lan-broker.local",
                                                 "port": "9001", "path": "/mqtt", "user": "u", "pass": "p"})
    last = broker.inner("cmd")[-1]
    assert (last["host"], last["port"], last["tls"], last["path"], last["prefix"]) == ("lan-broker.local", 9001, False, "/mqtt", "pm")
    page = admin.get(f"/meters/{pk}/configure").get_data(as_text=True)
    assert 'id="connection"' in page and "lan-broker.local" in page
