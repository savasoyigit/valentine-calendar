"""İlk açılış kurulum sihirbazı.

Ayarlar eksikken app.py bu küçük uygulamayı çalıştırır. Kurulum ekranı,
sunucu konsoluna yazdırılan tek kullanımlık kurulum koduyla korunur;
böylece internete açık bir sunucuda başkası kurulumu senden önce yapamaz.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import smtplib
import ssl
import time
import unicodedata
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config_store

HERE = Path(__file__).parent
PBKDF2_ITERATIONS = 310000
MAX_PHOTO_BYTES = 3 * 1024 * 1024

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

import security  # noqa: E402

security.add_security_headers(app)

_fails = {}


# ---------- yardımcılar ----------

def check_token(token: str, request: Request):
    """Kurulum kodunu doğrular; 5 hatalı denemede 15 dakika kilitler."""
    ip = request.client.host if request.client else "?"
    now = time.time()
    count, until = _fails.get(ip, (0, 0))
    if now < until:
        raise HTTPException(429, "Çok fazla hatalı deneme. 15 dakika sonra tekrar dene.")
    expected = config_store.get_setup_token()
    given = (token or "").strip().upper()
    if not hmac.compare_digest(given.encode(), expected.encode()):
        count += 1
        _fails[ip] = (count, now + 900 if count >= 5 else 0)
        raise HTTPException(401, "Kurulum kodu hatalı.")
    _fails.pop(ip, None)


def profile_slug(name: str) -> str:
    """index.html'deki profileImageSlug() ile aynı sonucu üretir."""
    s = name.strip().replace("I", "ı").replace("İ", "i").lower().replace("ı", "i")
    s = "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256:{PBKDF2_ITERATIONS}:{salt.hex()}:{digest.hex()}"


def generate_vapid_keys():
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from py_vapid import Vapid02, b64urlencode

    v = Vapid02()
    v.generate_keys()
    private = b64urlencode(v.private_key.private_numbers().private_value.to_bytes(32, "big"))
    public = b64urlencode(v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))
    return public, private


def clean_url(url: str, field: str) -> str:
    url = (url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, f"{field} http:// veya https:// ile başlayan geçerli bir adres olmalı.")
    return url


EMAIL_RE = re.compile(r"^[^@\s:,]+@[^@\s:,]+\.[^@\s:,]+$")


# ---------- modeller ----------

class TokenOnly(BaseModel):
    token: str


class ImmichSettings(BaseModel):
    url: str = ""
    api_key: str = ""
    album_name: str = "Aşkımız ❤️"
    instant_album_name: str = "Aşkımız-Gelenler"


class SmtpSettings(BaseModel):
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""


class ImmichTest(TokenOnly, ImmichSettings):
    pass


class SmtpTest(TokenOnly, SmtpSettings):
    test_to: str = ""


class Person(BaseModel):
    name: str
    email: str = ""
    password: str
    photo: str = ""  # data:image/png;base64,...


class SetupPayload(TokenOnly):
    base_url: str
    relationship_start: str = ""
    people: list[Person]
    immich: ImmichSettings | None = None
    smtp: SmtpSettings | None = None


# ---------- bağlantı testleri ----------

def immich_check(url: str, api_key: str):
    url = clean_url(url, "Immich adresi")
    try:
        req = UrlRequest(f"{url}/api/users/me", headers={"x-api-key": api_key, "Accept": "application/json"})
        with urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
        return data.get("name") or data.get("email") or "Immich kullanıcısı"
    except HTTPError as ex:
        if ex.code in (401, 403):
            raise HTTPException(400, "Immich'e ulaşıldı ama API anahtarı reddedildi.")
        raise HTTPException(400, f"Immich hata döndürdü (HTTP {ex.code}).")
    except (URLError, TimeoutError, OSError) as ex:
        raise HTTPException(400, f"Immich'e ulaşılamadı: {getattr(ex, 'reason', ex)}")


def smtp_check(s: SmtpSettings, send_to: str = ""):
    try:
        with smtplib.SMTP(s.host.strip(), int(s.port), timeout=12) as conn:
            conn.starttls(context=ssl.create_default_context())
            conn.login(s.user.strip(), s.password)
            if send_to:
                from email.message import EmailMessage
                m = EmailMessage()
                m["From"], m["To"], m["Subject"] = s.user.strip(), send_to, "Aşkımız kurulum testi 💌"
                m.set_content("E-posta ayarların çalışıyor. Bu mesajı kurulum sihirbazı gönderdi.")
                conn.send_message(m)
    except smtplib.SMTPAuthenticationError:
        raise HTTPException(400, "E-posta sunucusu kullanıcı adı/şifreyi reddetti. Gmail kullanıyorsan uygulama şifresi gerekir.")
    except (smtplib.SMTPException, OSError) as ex:
        raise HTTPException(400, f"E-posta sunucusuna bağlanılamadı: {ex}")


# ---------- uç noktalar ----------

@app.post("/api/setup/verify")
def verify(b: TokenOnly, request: Request):
    check_token(b.token, request)
    return {"ok": True}


@app.post("/api/setup/test-immich")
def test_immich(b: ImmichTest, request: Request):
    check_token(b.token, request)
    who = immich_check(b.url, b.api_key.strip())
    return {"ok": True, "message": f"Bağlantı başarılı: {who}"}


@app.post("/api/setup/test-smtp")
def test_smtp(b: SmtpTest, request: Request):
    check_token(b.token, request)
    to = b.test_to.strip()
    if to and not EMAIL_RE.match(to):
        raise HTTPException(400, "Test e-posta adresi geçersiz.")
    smtp_check(b, to)
    return {"ok": True, "message": "Test e-postası gönderildi." if to else "Giriş başarılı."}


@app.post("/api/setup/complete")
def complete(b: SetupPayload, request: Request):
    check_token(b.token, request)
    if config_store.is_configured():
        raise HTTPException(409, "Kurulum zaten tamamlanmış.")

    base_url = clean_url(b.base_url, "Uygulama adresi")

    since = b.relationship_start.strip()
    if since:
        try:
            if date.fromisoformat(since) > date.today():
                raise ValueError
        except ValueError:
            raise HTTPException(400, "Başlangıç tarihi geçersiz.")

    if len(b.people) != 2:
        raise HTTPException(400, "Tam olarak iki kişi tanımlanmalı.")

    people_entries, login_entries, photos = [], [], []
    seen = set()
    for idx, p in enumerate(b.people, start=1):
        name = p.name.strip()
        label = f"{idx}. kişi"
        if not 1 <= len(name) <= 40 or any(ch in name for ch in ":,\n\r"):
            raise HTTPException(400, f"{label}: ad 1-40 karakter olmalı ve ':' ya da ',' içermemeli.")
        username = name.lower()
        if username in seen:
            raise HTTPException(400, "İki kişinin adı birbirinden farklı olmalı.")
        seen.add(username)
        email = p.email.strip()
        if email and not EMAIL_RE.match(email):
            raise HTTPException(400, f"{label}: e-posta adresi geçersiz.")
        if len(p.password) < 8:
            raise HTTPException(400, f"{label}: şifre en az 8 karakter olmalı.")

        # Üçüncü alan oturum imzasında kullanılan kişiye özel rastgele değerdir.
        people_entries.append(f"{name}:{email}:{secrets.token_hex(16)}")
        login_entries.append(f"{username}:{hash_password(p.password)}")

        if p.photo:
            prefix = "data:image/png;base64,"
            if not p.photo.startswith(prefix):
                raise HTTPException(400, f"{label}: profil fotoğrafı okunamadı.")
            try:
                raw = base64.b64decode(p.photo[len(prefix):], validate=True)
            except ValueError:
                raise HTTPException(400, f"{label}: profil fotoğrafı okunamadı.")
            if len(raw) > MAX_PHOTO_BYTES or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise HTTPException(400, f"{label}: profil fotoğrafı geçersiz ya da çok büyük.")
            slug = profile_slug(name)
            if slug:
                photos.append((slug, raw))

    cfg = {
        "SITE_SECRET": secrets.token_hex(32),
        "BASE_URL": base_url,
        "RELATIONSHIP_START": since,
        "DB_PATH": str(config_store.DATA_DIR / "askimiz.db"),
        "PEOPLE": ",".join(people_entries),
        "LOGIN_CREDENTIALS": ",".join(login_entries),
    }

    vapid_public, vapid_private = generate_vapid_keys()
    first_email = next((p.email.strip() for p in b.people if p.email.strip()), "")
    cfg.update({
        "VAPID_PUBLIC_KEY": vapid_public,
        "VAPID_PRIVATE_KEY": vapid_private,
        "VAPID_SUBJECT": f"mailto:{first_email}" if first_email
        else (base_url if base_url.startswith("https://") else "mailto:noreply@example.com"),
    })

    if b.immich and b.immich.url.strip() and b.immich.api_key.strip():
        cfg.update({
            "IMMICH_URL": clean_url(b.immich.url, "Immich adresi"),
            "IMMICH_API_KEY": b.immich.api_key.strip(),
            "IMMICH_ALBUM_NAME": b.immich.album_name.strip() or "Aşkımız ❤️",
            "INSTANT_ALBUM_NAME": b.immich.instant_album_name.strip() or "Aşkımız-Gelenler",
        })

    if b.smtp and b.smtp.host.strip():
        cfg.update({
            "SMTP_HOST": b.smtp.host.strip(),
            "SMTP_PORT": int(b.smtp.port or 587),
            "SMTP_USER": b.smtp.user.strip(),
            "SMTP_PASS": b.smtp.password,
        })

    config_store.ensure_data_dir()
    if photos:
        config_store.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        for slug, raw in photos:
            (config_store.PROFILE_DIR / f"{slug}.png").write_bytes(raw)

    config_store.save_config(cfg)
    return {"ok": True, "usernames": [e.split(":", 1)[0] for e in login_entries]}


@app.get("/icons/{name}")
def setup_icon(name: str):
    if name not in ("icon.svg", "icon-192.png", "apple-touch-icon.png"):
        raise HTTPException(404)
    return FileResponse(HERE / "icons" / name)


@app.get("/{path:path}")
def setup_page(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(HERE / "setup.html", headers={"Cache-Control": "no-store"})
