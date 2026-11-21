"""Uygulama içi Ayarlar sayfasının API'si.

Değişiklikler data/config.json'a yazılır ve çalışan uygulamaya anında
uygulanır; yeniden başlatma gerekmez. Ortam değişkeniyle (.env, Docker)
verilmiş ayarlar burada salt okunurdur, çünkü her açılışta yine onlar geçerli olur.
"""
import base64
import hashlib
import hmac
from datetime import date

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel

import config_store
import setup_wizard


class GeneralSettings(BaseModel):
    base_url: str
    relationship_start: str = ""


class ImmichSettings(BaseModel):
    url: str = ""
    api_key: str = ""          # boş = mevcut anahtarı koru
    album_name: str = ""
    instant_album_name: str = ""
    disable: bool = False


class SmtpSettings(BaseModel):
    host: str = ""             # boş = e-postayı kapat
    port: int = 587
    user: str = ""
    password: str = ""         # boş = mevcut şifreyi koru


class SmtpTest(SmtpSettings):
    test_to: str = ""


class PasswordChange(BaseModel):
    current: str
    new: str


class PhotoUpload(BaseModel):
    photo: str


def register(app, core):
    """Ayarlar uç noktalarını core uygulamasına ekler."""

    def locked():
        return config_store.env_locked_keys()

    def ensure_unlocked(*keys):
        hit = [k for k in keys if k in locked()]
        if hit:
            raise HTTPException(
                409,
                "Bu ayar sunucuda ortam değişkeniyle (.env / Docker) verilmiş; "
                f"oradan değiştirilmeli: {', '.join(hit)}",
            )

    def apply(changes: dict):
        """config.json'u güncelle ve çalışan uygulamadaki değerleri değiştir."""
        config_store.update_config(changes)
        for key, value in changes.items():
            if key == "SMTP_PORT":
                value = int(value or 587)
            elif key in ("BASE_URL", "IMMICH_URL"):
                value = (value or "").rstrip("/")
            setattr(core, key, value if value is not None else "")

    @app.get("/api/settings")
    def get_settings(r: Request):
        who = core.auth(r)
        slug = setup_wizard.profile_slug(who)
        has_photo = any(
            (d / f"{slug}.png").is_file()
            for d in (config_store.PROFILE_DIR, core.HERE / "profile-images")
        )
        return {
            "me": {"name": who, "has_photo": has_photo},
            "general": {"base_url": core.BASE_URL, "relationship_start": core.RELATIONSHIP_START},
            "immich": {
                "enabled": bool(core.IMMICH_API_KEY),
                "url": core.IMMICH_URL if core.IMMICH_API_KEY else "",
                "has_key": bool(core.IMMICH_API_KEY),
                "album_name": core.IMMICH_ALBUM_NAME,
                "instant_album_name": core.INSTANT_ALBUM_NAME,
            },
            "smtp": {
                "enabled": bool(core.SMTP_HOST),
                "host": core.SMTP_HOST,
                "port": core.SMTP_PORT,
                "user": core.SMTP_USER,
                "has_password": bool(core.SMTP_PASS),
            },
            "push": {"enabled": bool(core.VAPID_PUBLIC_KEY and core.VAPID_PRIVATE_KEY)},
            "locked": sorted(locked()),
        }

    @app.put("/api/settings/general")
    def put_general(b: GeneralSettings, r: Request):
        core.auth(r)
        base_url = setup_wizard.clean_url(b.base_url, "Uygulama adresi")
        since = b.relationship_start.strip()
        if since:
            try:
                if date.fromisoformat(since) > date.today():
                    raise ValueError
            except ValueError:
                raise HTTPException(400, "Başlangıç tarihi geçersiz.")
        changes = {}
        if base_url != core.BASE_URL:
            ensure_unlocked("BASE_URL")
            changes["BASE_URL"] = base_url
        if since != core.RELATIONSHIP_START:
            ensure_unlocked("RELATIONSHIP_START")
            changes["RELATIONSHIP_START"] = since
        if changes:
            apply(changes)
        return {"ok": True}

    @app.put("/api/settings/immich")
    def put_immich(b: ImmichSettings, r: Request):
        core.auth(r)
        keys = ("IMMICH_URL", "IMMICH_API_KEY", "IMMICH_ALBUM_NAME", "INSTANT_ALBUM_NAME")
        ensure_unlocked(*keys)
        if b.disable:
            apply({k: "" for k in ("IMMICH_URL", "IMMICH_API_KEY")})
            return {"ok": True}
        url = setup_wizard.clean_url(b.url, "Immich adresi")
        api_key = b.api_key.strip() or core.IMMICH_API_KEY
        if not api_key:
            raise HTTPException(400, "Immich API anahtarı gerekli.")
        apply({
            "IMMICH_URL": url,
            "IMMICH_API_KEY": api_key,
            "IMMICH_ALBUM_NAME": b.album_name.strip() or "Aşkımız ❤️",
            "INSTANT_ALBUM_NAME": b.instant_album_name.strip() or "Aşkımız-Gelenler",
        })
        return {"ok": True}

    @app.post("/api/settings/immich/test")
    def test_immich(b: ImmichSettings, r: Request):
        core.auth(r)
        who = setup_wizard.immich_check(b.url, b.api_key.strip() or core.IMMICH_API_KEY)
        return {"ok": True, "message": f"Bağlantı başarılı: {who}"}

    def smtp_values(b: SmtpSettings):
        return setup_wizard.SmtpSettings(
            host=b.host.strip(), port=b.port or 587, user=b.user.strip(),
            password=b.password or core.SMTP_PASS,
        )

    @app.put("/api/settings/smtp")
    def put_smtp(b: SmtpSettings, r: Request):
        core.auth(r)
        ensure_unlocked("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS")
        if not b.host.strip():
            apply({"SMTP_HOST": "", "SMTP_USER": "", "SMTP_PASS": "", "SMTP_PORT": ""})
            return {"ok": True}
        s = smtp_values(b)
        if not s.user or not s.password:
            raise HTTPException(400, "Kullanıcı adı ve şifre gerekli.")
        apply({"SMTP_HOST": s.host, "SMTP_PORT": s.port, "SMTP_USER": s.user, "SMTP_PASS": s.password})
        return {"ok": True}

    @app.post("/api/settings/smtp/test")
    def test_smtp(b: SmtpTest, r: Request):
        who = core.auth(r)
        s = smtp_values(b)
        if not s.host or not s.user or not s.password:
            raise HTTPException(400, "Sunucu, kullanıcı adı ve şifre gerekli.")
        to = b.test_to.strip() or core.PEOPLE.get(who, "") or s.user
        setup_wizard.smtp_check(s, to)
        return {"ok": True, "message": f"Test e-postası gönderildi ({to})."}

    @app.post("/api/settings/password")
    def change_password(b: PasswordChange, r: Request, resp: Response):
        who = core.auth(r)
        ensure_unlocked("LOGIN_CREDENTIALS")
        index = next((i for i, person in enumerate(core.USERS) if person[0] == who), None)
        user = next((u for u in core.LOGIN_USERS if u["site_index"] == index), None)
        if user is None:
            raise HTTPException(404, "Kullanıcı bulunamadı.")
        candidate = hashlib.pbkdf2_hmac("sha256", b.current.encode("utf-8"), user["salt"], user["iterations"])
        if not hmac.compare_digest(candidate, user["digest"]):
            raise HTTPException(400, "Mevcut şifre hatalı.")
        if len(b.new) < 8:
            raise HTTPException(400, "Yeni şifre en az 8 karakter olmalı.")

        encoded = setup_wizard.hash_password(b.new)
        parts = encoded.split(":")
        user.update({
            "encoded": encoded,
            "iterations": int(parts[1]),
            "salt": bytes.fromhex(parts[2]),
            "digest": bytes.fromhex(parts[3]),
        })
        line = ",".join(f"{u['username']}:{u['encoded']}" for u in core.LOGIN_USERS)
        config_store.update_config({"LOGIN_CREDENTIALS": line})
        # Oturum imzası şifreye bağlı: diğer cihazlar çıkış yapar, bu cihaz açık kalır.
        core.issue_session(resp, r, index)
        return {"ok": True}

    @app.post("/api/settings/photo")
    def upload_photo(b: PhotoUpload, r: Request):
        who = core.auth(r)
        prefix = "data:image/png;base64,"
        if not b.photo.startswith(prefix):
            raise HTTPException(400, "Fotoğraf okunamadı.")
        try:
            raw = base64.b64decode(b.photo[len(prefix):], validate=True)
        except ValueError:
            raise HTTPException(400, "Fotoğraf okunamadı.")
        if len(raw) > setup_wizard.MAX_PHOTO_BYTES or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(400, "Fotoğraf geçersiz ya da çok büyük.")
        slug = setup_wizard.profile_slug(who)
        if not slug:
            raise HTTPException(400, "Bu ad için fotoğraf kaydedilemiyor.")
        config_store.ensure_data_dir()
        config_store.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        (config_store.PROFILE_DIR / f"{slug}.png").write_bytes(raw)
        return {"ok": True}

    @app.delete("/api/settings/photo")
    def delete_photo(r: Request):
        who = core.auth(r)
        slug = setup_wizard.profile_slug(who)
        removed = False
        for d in (config_store.PROFILE_DIR, core.HERE / "profile-images"):
            p = d / f"{slug}.png"
            if slug and p.is_file():
                p.unlink()
                removed = True
        return {"ok": True, "removed": removed}
