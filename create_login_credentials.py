"""Kurulum sihirbazı yerine .env ile kurulum yapacaklar için yardımcı betik.

İki hesap için LOGIN_CREDENTIALS satırını üretir. Kullanıcı adları,
PEOPLE değişkenindeki adların küçük harfli hâliyle aynı olmalıdır.
"""

import getpass
import hashlib
import secrets
import re

credentials = []

for i in range(2):
    print(f"\n{i + 1}. hesap")
    username = input("Yeni kullanıcı adı: ").strip().lower()

    if not re.fullmatch(r"[a-z0-9._-]{3,40}", username):
        raise SystemExit("Kullanıcı adı geçersiz.")
    if any(username == u for u, _ in credentials):
        raise SystemExit("Kullanıcı adları farklı olmalı.")

    password = getpass.getpass("Yeni parola (en az 8 karakter): ")
    confirm = getpass.getpass("Parolayı tekrar gir: ")

    if len(password) < 8:
        raise SystemExit("Parola en az 8 karakter olmalı.")
    if password != confirm:
        raise SystemExit("Parolalar eşleşmedi.")

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, 310000
    )
    encoded = (
        f"pbkdf2_sha256:310000:{salt.hex()}:{digest.hex()}"
    )
    credentials.append((username, encoded))

print("\n.env dosyasına eklenecek satır:\n")
print(
    "LOGIN_CREDENTIALS="
    + ",".join(f"{u}:{h}" for u, h in credentials)
)
