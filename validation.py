"""Kullanıcıdan gelen adres ve kimliklerin doğrulanması."""
import re
from urllib.parse import urlparse

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Tarayıcıların kullandığı Web Push servisleri. Abonelik adresi bunlardan biri
# değilse sunucu oraya istek göndermez.
PUSH_HOSTS = (
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "push.services.mozilla.com",
    "web.push.apple.com",
    ".push.apple.com",
    ".notify.windows.com",
)


def clean_http_url(value: str, max_len: int = 500) -> str:
    """Boş ya da http(s) adresi döndürür; başka her şeyde ValueError.
    javascript:, data: gibi şemalar bu yüzden hiç kaydedilemez."""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) > max_len:
        raise ValueError("Bağlantı çok uzun.")
    if "://" not in value and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError("Bağlantı http:// veya https:// ile başlamalı.")
    return value


def is_uuid(value: str) -> bool:
    return bool(UUID_RE.match(value or ""))


def is_push_endpoint(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return False
    return any(host == h or (h.startswith(".") and host.endswith(h)) for h in PUSH_HOSTS)
