from app.models import User
from app.services import validation


def test_password_rules():
    assert validation.password("short1!A") is not None
    assert validation.password("alllowercase1!") is not None
    assert validation.password("NoDigitsHere!!") is not None
    assert validation.password("NoSymbols12345") is not None
    assert validation.password("Str0ng!Passw0rd", "different") == "Passwords do not match"
    assert validation.password("Str0ng!Passw0rd", "Str0ng!Passw0rd") is None


def test_phone_and_username():
    assert validation.phone("+60 12-345 6789") == ("+60123456789", None)
    assert validation.phone("0123456789")[1] is not None
    assert validation.username("  Admin_1 ") == ("admin_1", None)
    assert validation.username("ab")[1] is not None


def test_setup_redirect_and_create(app, client):
    r = client.get("/")
    assert r.status_code == 302 and "/setup" in r.location
    r = client.post("/setup", data={"username": "Boss", "password": "Str0ng!Passw0rd",
                                    "confirm": "Str0ng!Passw0rd", "phone": "+60123456789",
                                    "email": "boss@example.com"})
    assert r.status_code == 302
    with app.app_context():
        assert User.query.filter_by(username="boss", role="admin").count() == 1
    assert client.get("/setup").status_code == 404  # only once


def test_setup_requires_all_fields(client):
    r = client.post("/setup", data={"username": "boss", "password": "Str0ng!Passw0rd",
                                    "confirm": "Str0ng!Passw0rd", "phone": "", "email": ""})
    assert r.status_code == 200
    assert b"Phone number is required" in r.data and b"Email is required" in r.data


def test_login_case_insensitive(admin):
    assert admin.get("/").status_code == 200


def test_viewer_cannot_change_things(app, admin):
    r = admin.post("/settings/users", data={"what": "add", "username": "Viewer1",
                                            "password": "Vi3wer!Passw0rd", "confirm": "Vi3wer!Passw0rd"})
    assert r.status_code == 302
    admin.post("/logout")
    admin.post("/login", data={"username": "viewer1", "password": "Vi3wer!Passw0rd"})
    assert admin.get("/").status_code == 200
    assert admin.get("/meters/add").status_code == 403
    assert admin.post("/meters/1/action", json={"action": "relay_on"}).status_code == 403
    assert admin.post("/settings/server", data={}).status_code == 403


def test_password_change_logs_out_other_sessions(app, admin):
    other = app.test_client()
    other.post("/login", data={"username": "admin", "password": "Str0ng!Passw0rd"})
    assert other.get("/").status_code == 200
    r = admin.post("/settings/account", data={"what": "password", "current_password": "Str0ng!Passw0rd",
                                              "password": "N3w!Passw0rdXX", "confirm": "N3w!Passw0rdXX"})
    assert r.status_code == 302
    assert admin.get("/").status_code == 200       # this session stays logged in
    assert other.get("/").status_code == 302       # other session is logged out
