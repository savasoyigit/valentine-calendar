"""Uygulama ayarlarını okur ve yazar.

Ayarlar iki kaynaktan gelir, öncelik sırasıyla:
  1. Ortam değişkenleri (.env, systemd EnvironmentFile, Docker -e ...)
  2. Kurulum sihirbazının yazdığı  <DATA_DIR>/config.json

Ortam değişkenleri her zaman kazanır; böylece elle .env ile kurulmuş
sunucular sihirbazdan hiç etkilenmez.
"""
import json
import os
import secrets
from pathlib import Path

HERE = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(HERE / "data"))).resolve()
CONFIG_PATH = DATA_DIR / "config.json"
SETUP_TOKEN_PATH = DATA_DIR / ".setup-token"
PROFILE_DIR = DATA_DIR / "profile-images"

# Uygulamanın çalışması için mutlaka gereken anahtarlar.
REQUIRED_KEYS = ("SITE_SECRET", "LOGIN_CREDENTIALS", "PEOPLE")

# config.json içinde saklanabilecek tüm anahtarlar.
KNOWN_KEYS = (
    "SITE_SECRET", "BASE_URL", "DB_PATH", "RELATIONSHIP_START",
    "LOGIN_CREDENTIALS", "PEOPLE",
    "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT",
    "IMMICH_URL", "IMMICH_API_KEY", "IMMICH_ALBUM_NAME", "INSTANT_ALBUM_NAME",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS",
)


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass


def load_file_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: str(v) for k, v in data.items() if k in KNOWN_KEYS and v is not None}
    except FileNotFoundError:
        return {}


def is_configured():
    file_cfg = load_file_config()
    return all(os.getenv(k) or file_cfg.get(k) for k in REQUIRED_KEYS)


def apply_to_env():
    """config.json değerlerini, ortamda zaten olmayanlar için os.environ'a aktarır."""
    for key, value in load_file_config().items():
        if value and not os.getenv(key):
            os.environ[key] = value


def save_config(values: dict):
    """Ayarları atomik olarak ve yalnızca sahibinin okuyabileceği izinlerle yazar."""
    ensure_data_dir()
    clean = {k: str(v) for k, v in values.items() if k in KNOWN_KEYS and v not in (None, "")}
    tmp = CONFIG_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def get_setup_token():
    """Kurulum ekranını yetkisiz kişilere karşı koruyan tek kullanımlık kod."""
    ensure_data_dir()
    if SETUP_TOKEN_PATH.exists():
        token = SETUP_TOKEN_PATH.read_text().strip()
        if token:
            return token
    token = "-".join(secrets.token_hex(2).upper() for _ in range(3))
    fd = os.open(SETUP_TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


def clear_setup_token():
    try:
        SETUP_TOKEN_PATH.unlink()
    except FileNotFoundError:
        pass
