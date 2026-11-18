"""Uçtan uca duman testleri: kurulum sihirbazı → giriş → özellikler → ayarlar.

Uygulama modül düzeyinde durum tuttuğu için testler tek süreçte, sırayla çalışır.
"""
import base64
import importlib
import json
import os
import stat
import struct
import sys
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
CONFIG_KEYS = (
    "SITE_SECRET", "BASE_URL", "DB_PATH", "RELATIONSHIP_START", "LOGIN_CREDENTIALS", "PEOPLE",
    "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT", "IMMICH_URL", "IMMICH_API_KEY",
    "IMMICH_ALBUM_NAME", "INSTANT_ALBUM_NAME", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS",
)


def tiny_png():
    raw = b"".join(b"\x00" + b"\xd9\x46\x6f" * 4 for _ in range(4))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode()


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("data")
    saved = {k: os.environ.pop(k, None) for k in CONFIG_KEYS + ("DATA_DIR",)}
    os.environ["DATA_DIR"] = str(data_dir)
    sys.path.insert(0, str(ROOT))
    for name in ("app", "core", "config_store", "setup_wizard", "settings_api"):
        sys.modules.pop(name, None)
    import app as app_module
    client = TestClient(app_module.app)
    yield {"client": client, "data": data_dir, "app": app_module}
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def token(env):
    return (env["data"] / ".setup-token").read_text().strip()


def test_setup_page_shown_when_unconfigured(env):
    c = env["client"]
    r = c.get("/")
    assert r.status_code == 200 and "Kurulum kodu" in r.text
    assert c.post("/api/login", json={"username": "a", "password": "b"}).status_code in (404, 405)


def test_wrong_setup_token_rejected(env):
    r = env["client"].post("/api/setup/verify", json={"token": "YANLIS"})
    assert r.status_code == 401


def test_complete_setup(env):
    c = env["client"]
    body = {
        "token": token(env).lower(),
        "base_url": "http://localhost:8000/",
        "relationship_start": "2022-11-22",
        "people": [
            {"name": "Ada", "email": "ada@example.com", "password": "parola-ada", "photo": tiny_png()},
            {"name": "Deniz", "email": "", "password": "parola-deniz"},
        ],
        "immich": {"url": "", "api_key": ""},
        "smtp": {"host": ""},
    }
    bad = dict(body, people=[body["people"][0], dict(body["people"][1], name="ADA")])
    assert c.post("/api/setup/complete", json=bad).status_code == 400

    r = c.post("/api/setup/complete", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["usernames"] == ["ada", "deniz"]

    cfg_path = env["data"] / "config.json"
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600
    cfg = json.loads(cfg_path.read_text())
    assert cfg["BASE_URL"] == "http://localhost:8000"
    assert cfg["VAPID_PUBLIC_KEY"] and cfg["VAPID_PRIVATE_KEY"]
    assert "parola-ada" not in cfg_path.read_text()
    assert (env["data"] / "profile-images" / "ada.png").is_file()


def test_setup_closed_after_install(env):
    c = env["client"]
    assert c.post("/api/setup/verify", json={"token": "x"}).status_code == 404
    assert not (env["data"] / ".setup-token").exists()
    assert "Kurulum kodu" not in c.get("/").text


def test_login_over_plain_http(env):
    c = env["client"]
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/login", json={"username": "ada", "password": "yanlis"}).status_code == 401
    r = c.post("/api/login", json={"username": "ADA", "password": "parola-ada"})
    assert r.status_code == 200
    assert "secure" not in r.headers["set-cookie"].lower()
    me = c.get("/api/me").json()
    assert me == {"ok": True, "name": "Ada", "since": "2022-11-22"}


@pytest.mark.parametrize("post_url,body,get_url", [
    ("/api/events", {"title": "Akşam yemeği", "date": "2030-01-01", "proposed_by": "Ada"}, "/api/events"),
    ("/api/notes", {"date": "2030-01-01", "text": "Merhaba"}, "/api/notes"),
    ("/api/shared-notes", {"title": "Liste", "text": "Not"}, "/api/shared-notes"),
    ("/api/shopping", {"title": "Market"}, "/api/shopping"),
    ("/api/wishlist", {"title": "Kitap"}, "/api/wishlist"),
    ("/api/special-days", {"title": "Yıldönümü", "date": "2022-11-22"}, "/api/special-days"),
    ("/api/watchlist", {"title": "Film"}, "/api/watchlist"),
    ("/api/memories", {"title": "İlk kahve", "date": "2023-01-01"}, "/api/memories"),
])
def test_features_work_on_fresh_database(env, post_url, body, get_url):
    c = env["client"]
    assert c.post(post_url, json=body).status_code == 200
    r = c.get(get_url)
    assert r.status_code == 200 and r.json()


def test_static_assets(env):
    c = env["client"]
    assert c.get("/manifest.json").json()["icons"][0]["src"] == "/icons/icon-192.png"
    assert c.get("/icons/icon-192.png").headers["content-type"] == "image/png"
    assert c.get("/icons/../core.py").status_code == 404
    assert c.get("/profile-images/ada.png").status_code == 200
    assert c.get("/api/push/public-key").json()["publicKey"]


def test_settings_never_expose_secrets(env):
    s = env["client"].get("/api/settings").json()
    flat = json.dumps(s)
    cfg = json.loads((env["data"] / "config.json").read_text())
    for key in ("SITE_SECRET", "LOGIN_CREDENTIALS", "VAPID_PRIVATE_KEY"):
        assert cfg[key] not in flat
    assert s["me"] == {"name": "Ada", "has_photo": True}
    assert s["locked"] == []


def test_change_general_settings(env):
    c = env["client"]
    assert c.put("/api/settings/general", json={"base_url": "ftp://x", "relationship_start": ""}).status_code == 400
    r = c.put("/api/settings/general", json={"base_url": "https://takvim.example.com", "relationship_start": "2021-05-01"})
    assert r.status_code == 200
    assert c.get("/api/me").json()["since"] == "2021-05-01"
    assert json.loads((env["data"] / "config.json").read_text())["BASE_URL"] == "https://takvim.example.com"


def test_smtp_and_immich_toggle(env):
    c = env["client"]
    assert c.put("/api/settings/smtp", json={"host": "smtp.example.com", "user": "a@example.com"}).status_code == 400
    assert c.put("/api/settings/smtp", json={"host": "smtp.example.com", "user": "a@example.com", "password": "x"}).status_code == 200
    s = c.get("/api/settings").json()
    assert s["smtp"]["enabled"] and s["smtp"]["has_password"] and "password" not in s["smtp"]
    assert c.put("/api/settings/smtp", json={"host": ""}).status_code == 200
    assert not c.get("/api/settings").json()["smtp"]["enabled"]

    assert c.put("/api/settings/immich", json={"url": "http://immich.local:2283", "api_key": "k"}).status_code == 200
    assert c.get("/api/settings").json()["immich"]["enabled"]
    assert c.put("/api/settings/immich", json={"disable": True}).status_code == 200
    assert not c.get("/api/settings").json()["immich"]["enabled"]


def test_profile_photo(env):
    c = env["client"]
    assert c.delete("/api/settings/photo").json()["removed"] is True
    assert c.get("/profile-images/ada.png").status_code == 404
    assert c.post("/api/settings/photo", json={"photo": "data:image/png;base64,AAAA"}).status_code == 400
    assert c.post("/api/settings/photo", json={"photo": tiny_png()}).status_code == 200
    assert c.get("/profile-images/ada.png").status_code == 200


def test_change_password(env):
    c = env["client"]
    assert c.post("/api/settings/password", json={"current": "yanlis", "new": "yeni-parola-1"}).status_code == 400
    assert c.post("/api/settings/password", json={"current": "parola-ada", "new": "kisa"}).status_code == 400
    assert c.post("/api/settings/password", json={"current": "parola-ada", "new": "yeni-parola-1"}).status_code == 200
    c.post("/api/logout")
    c.cookies.clear()
    assert c.post("/api/login", json={"username": "ada", "password": "parola-ada"}).status_code == 401
    assert c.post("/api/login", json={"username": "ada", "password": "yeni-parola-1"}).status_code == 200
    # Diğer kişinin şifresi etkilenmemeli
    assert c.post("/api/login", json={"username": "deniz", "password": "parola-deniz"}).status_code == 200


def test_places(env):
    c = env["client"]
    c.cookies.clear()
    assert c.post("/api/login", json={"username": "ada", "password": "yeni-parola-1"}).status_code == 200
    assert c.post("/api/places", json={"name": " "}).status_code == 400
    assert c.post("/api/places", json={"name": "X", "rating": 6}).status_code == 400
    assert c.post("/api/places", json={"name": "X", "url": "javascript:alert(1)"}).status_code == 400

    r = c.post("/api/places", json={
        "name": "Moda Kahvecisi", "rating": 4, "address": "Moda, Kadıköy",
        "url": "instagram.com/moda", "note": "Filtre kahvesi efsane", "status": "visited",
    })
    assert r.status_code == 200
    cafe = r.json()["id"]
    wish = c.post("/api/places", json={"name": "Yeni Bistro", "rating": 5, "status": "wishlist"}).json()["id"]

    places = {p["id"]: p for p in c.get("/api/places").json()}
    assert places[cafe]["url"] == "https://instagram.com/moda"
    assert places[cafe]["ratings"] == {"Ada": 4} and places[cafe]["avg"] == 4
    assert places[wish]["ratings"] == {}  # gidilecek mekana puan yazılmaz

    # Diğer kişi puanlar; iki puan ayrı tutulur ve bildirim gider
    c.cookies.clear()
    c.post("/api/login", json={"username": "deniz", "password": "parola-deniz"})
    assert c.put(f"/api/places/{cafe}/rating", json={"rating": 5}).status_code == 200
    p = {x["id"]: x for x in c.get("/api/places").json()}[cafe]
    assert p["ratings"] == {"Ada": 4, "Deniz": 5} and p["avg"] == 4.5
    # Gidilecek mekana puan vermek onu "gittik" yapar
    c.put(f"/api/places/{wish}/rating", json={"rating": 3})
    assert {x["id"]: x for x in c.get("/api/places").json()}[wish]["status"] == "visited"

    c.cookies.clear()
    c.post("/api/login", json={"username": "ada", "password": "yeni-parola-1"})
    notes = c.get("/api/notifications").json()
    assert any(n["target"] == f"place:{cafe}" for n in notes)

    # Düzenleme kendi puanını değiştirir, diğerininkine dokunmaz
    assert c.put(f"/api/places/{cafe}", json={"name": "Moda Kahvecisi ☕", "rating": 2, "status": "visited"}).status_code == 200
    p = {x["id"]: x for x in c.get("/api/places").json()}[cafe]
    assert p["name"] == "Moda Kahvecisi ☕" and p["ratings"] == {"Ada": 2, "Deniz": 5}

    assert c.delete(f"/api/places/{cafe}").status_code == 200
    assert c.delete(f"/api/places/{cafe}").status_code == 404
    assert cafe not in {x["id"] for x in c.get("/api/places").json()}


def test_pending_plans_and_in_app_accept(env):
    c = env["client"]
    c.cookies.clear()
    c.post("/api/login", json={"username": "ada", "password": "yeni-parola-1"})
    eid = c.post("/api/events", json={"title": "Sinema", "date": "2031-02-14", "proposed_by": "Ada"}).json()["id"]
    d = c.get("/api/dashboard").json()
    assert eid in [e["id"] for e in d["pending_out"]] and not any(e["id"] == eid for e in d["pending_in"])
    # Kendi önerine cevap verilemez
    assert c.post(f"/api/events/{eid}/accept").status_code == 403
    assert c.get(f"/api/events/{eid}/respond").status_code == 403

    c.cookies.clear()
    c.post("/api/login", json={"username": "deniz", "password": "parola-deniz"})
    d = c.get("/api/dashboard").json()
    assert eid in [e["id"] for e in d["pending_in"]]
    link = c.get(f"/api/events/{eid}/respond").json()["url"]
    assert link.startswith("/r/") and "Kabul ediyorum" in c.get(link).text
    assert c.post(f"/api/events/{eid}/accept").status_code == 200
    assert c.post(f"/api/events/{eid}/accept").status_code == 404  # ikinci kez olmaz
    assert c.get(link).text.count("geçerli değil") == 1

    c.cookies.clear()
    c.post("/api/login", json={"username": "ada", "password": "yeni-parola-1"})
    notes = c.get("/api/notifications").json()
    assert any(n["type"] == "accepted" and n["target"] == f"calendar:{eid}" for n in notes)
    assert c.get("/api/dashboard").json()["pending_out"] == [] or all(
        e["id"] != eid for e in c.get("/api/dashboard").json()["pending_out"])


def test_ares_icon_served(env):
    r = env["client"].get("/icons/ares.svg")
    assert r.status_code == 200 and "<svg" in r.text
