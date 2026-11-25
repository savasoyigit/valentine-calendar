from datetime import date
import os, sqlite3, hmac, hashlib, time, secrets, smtplib, threading, html
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qs
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import HTTPError, URLError
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel, field_validator
from fastapi.exceptions import RequestValidationError

import validation
from pywebpush import webpush, WebPushException
import json
import re

import config_store

SECRET = os.getenv("SITE_SECRET", "bunu-degistir")
DB = os.getenv("DB_PATH", str(Path(__file__).parent / "askimiz.db"))
HERE = Path(__file__).parent
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "")
IMMICH_URL = os.getenv("IMMICH_URL", "http://127.0.0.1:2283").rstrip("/")
IMMICH_API_KEY = os.getenv("IMMICH_API_KEY", "")
IMMICH_ALBUM_NAME = os.getenv("IMMICH_ALBUM_NAME", "Aşkımız ❤️")
INSTANT_ALBUM_NAME = os.getenv("INSTANT_ALBUM_NAME", "Aşkımız-Gelenler")
# Birlikteliğin başladığı gün (YYYY-MM-DD). Boşsa ana sayfadaki sayaç gizlenir.
RELATIONSHIP_START = os.getenv("RELATIONSHIP_START", "").strip()

# LOGIN_CREDENTIALS biçimi: kullanıcıadı:pbkdf2_sha256:iterasyon:salt:özet
def load_login_credentials():
    raw = os.getenv("LOGIN_CREDENTIALS", "").strip()
    if not raw:
        raise RuntimeError("LOGIN_CREDENTIALS .env içinde tanımlanmalı")

    result = []
    seen = set()

    for item in raw.split(","):
        username, encoded = item.split(":", 1)
        username = username.strip().lower()

        if not username or username in seen:
            raise RuntimeError("Giriş kullanıcı adları boş veya tekrarlı olamaz")

        parts = encoded.split(":")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            raise RuntimeError(f"{username} için parola özeti biçimi geçersiz")

        iterations = int(parts[1])
        salt_hex, digest_hex = parts[2], parts[3]

        if iterations < 310000 or len(bytes.fromhex(salt_hex)) < 16:
            raise RuntimeError(f"{username} için parola özeti ayarları geçersiz")
        if len(bytes.fromhex(digest_hex)) != 32:
            raise RuntimeError(f"{username} için parola özeti uzunluğu geçersiz")

        result.append({
            "username": username,
            "encoded": encoded,
            "iterations": iterations,
            "salt": bytes.fromhex(salt_hex),
            "digest": bytes.fromhex(digest_hex),
        })
        seen.add(username)

    if len(result) != 2:
        raise RuntimeError("Tam olarak iki giriş hesabı tanımlanmalı")

    return result

LOGIN_USERS = load_login_credentials()

# Eski kullanıcı kimliklerini koru: isim, e-posta, eski oturum imza değeri.
USERS = [
    item.split(":", 2)
    for item in os.getenv("PEOPLE", "").split(",")
    if item.strip()
]

if len(USERS) != 2 or any(len(user) != 3 for user in USERS):
    raise RuntimeError(
        "PEOPLE ortam değişkeni iki adet isim:e-posta:değer kaydı içermeli"
    )

PEOPLE = {name: email for name, email, _ in USERS}

for login_user in LOGIN_USERS:
    matches = [
        i for i, (name, _, _) in enumerate(USERS)
        if name.strip().lower() == login_user["username"]
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"{login_user['username']} PEOPLE içinde tek bir kişiyle eşleşmeli"
        )
    login_user["site_index"] = matches[0]

if len({u["site_index"] for u in LOGIN_USERS}) != len(USERS):
    raise RuntimeError("Giriş hesapları farklı site kullanıcılarına eşleşmeli")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
esc = html.escape

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

import security  # noqa: E402

security.add_security_headers(app)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    first = (exc.errors() or [{}])[0]
    msg = str(first.get("msg", "Geçersiz istek.")).removeprefix("Value error, ")
    return JSONResponse({"detail": msg}, status_code=422)


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


with db() as c:
    c.executescript("""
    create table if not exists events(id integer primary key, title text, date text,
        time text default '', place text default '', done integer default 0);
    create table if not exists notes(id integer primary key, date text, author text,
        text text, created text default current_timestamp);
    create table if not exists history(id integer primary key, event_id integer, who text,
        text text, created text default current_timestamp);
    """)
    for col in ["status text default 'accepted'", "proposed_by text default ''", "token text default ''"]:
        try:
            c.execute("alter table events add column " + col)
        except sqlite3.OperationalError:
            pass
    c.executescript("""
    create table if not exists shared_notes(id integer primary key, title text default '', text text, author text, created text default current_timestamp, updated text default current_timestamp);
    create table if not exists shopping_lists(id integer primary key, title text not null, created_by text default '', created text default current_timestamp);
    create table if not exists shopping_items(id integer primary key, text text, added_by text, done integer default 0, created text default current_timestamp);
    create table if not exists wishlist_items(id integer primary key, title text, url text default '', price text default '', priority text default 'normal', added_by text, done integer default 0, created text default current_timestamp);
    create table if not exists notifications(id integer primary key, recipient text, actor text, type text, title text, text text, target text default '', read integer default 0, created text default current_timestamp);
    create table if not exists push_subscriptions(id integer primary key,user text not null,endpoint text not null unique,p256dh text not null,auth text not null,created text default current_timestamp);
    """)
    # Alışveriş listesi geçişi: eski ürünleri varsayılan listeye taşı, durumlarını koru.
    try:
        c.execute("alter table shopping_items add column list_id integer")
    except sqlite3.OperationalError:
        pass
    default_list = c.execute("select id from shopping_lists order by id limit 1").fetchone()
    if not default_list:
        c.execute("insert into shopping_lists(title,created_by) values(?,?)", ("Genel Alışveriş", ""))
        default_list_id = c.execute("select last_insert_rowid()").fetchone()[0]
    else:
        default_list_id = default_list["id"]
    c.execute("update shopping_items set list_id=? where list_id is null", (default_list_id,))
    try:
        c.execute("alter table wishlist_items add column currency text default 'TRY'")
    except sqlite3.OperationalError:
        pass

with db() as c:
    c.execute("""
        create table if not exists instant_photo_messages(
            id integer primary key,
            asset_id text not null unique,
            sender text not null,
            created text default current_timestamp
        )
    """)
    # Sonradan eklenen özellik tabloları. Mevcut veritabanlarında zaten
    # varsa dokunulmaz; temiz kurulumda ilk açılışta oluşturulur.
    c.executescript("""
    create table if not exists special_days(
        id integer primary key,
        title text not null,
        date text not null,
        recurring integer default 1,
        icon text default '❤️',
        note text default '',
        created_by text default '',
        created_at text default current_timestamp
    );
    create table if not exists watchlist_items(
        id integer primary key,
        title text not null,
        media_type text default 'movie',
        status text default 'planned',
        genre text default '',
        poster_url text default '',
        rating real,
        note text default '',
        added_by text default '',
        watched_at text,
        created_at text default current_timestamp
    );
    create table if not exists memories(
        id integer primary key,
        title text not null,
        date text not null,
        description text default '',
        location text default '',
        created_by text default '',
        created_at text default current_timestamp,
        updated_at text default current_timestamp
    );
    create table if not exists memory_photos(
        id integer primary key,
        memory_id integer not null,
        asset_id text not null,
        sort_order integer default 0,
        created_at text default current_timestamp
    );
    """)


SESSION_MAX_AGE = 365 * 24 * 3600
DEVICE_MAX_AGE = 5 * 365 * 24 * 3600

# Her giriş sunucuda ayrı bir oturum kaydıdır. Çerezde rastgele bir anahtar durur,
# veritabanında yalnızca onun SHA-256 özeti saklanır. Çıkış o kaydı siler; böylece
# kopyalanmış bir çerez de anında geçersizleşir.
with db() as c:
    c.execute("""create table if not exists sessions(
        token_hash text primary key,
        user_index integer not null,
        pw_fp text not null,
        created integer not null,
        last_seen integer not null,
        user_agent text default ''
    )""")
    c.execute("drop table if exists session_versions")


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def login_user_for(i):
    return next((u for u in LOGIN_USERS if u["site_index"] == i), None)


def password_fingerprint(i):
    """Şifre özetinin parmak izi. Şifre değişince (uygulamadan ya da .env'den)
    o kişinin tüm eski oturumları geçersizleşir."""
    user = login_user_for(i)
    return hashlib.sha256(f"{SECRET}:{user['encoded'] if user else ''}".encode()).hexdigest()[:32]


def issue_session(resp: Response, r: Request, i: int):
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with db() as c:
        c.execute("delete from sessions where created < ?", (now - SESSION_MAX_AGE,))
        c.execute("insert into sessions(token_hash,user_index,pw_fp,created,last_seen,user_agent) values(?,?,?,?,?,?)",
                  (_hash_token(token), i, password_fingerprint(i), now, now,
                   r.headers.get("user-agent", "")[:200]))
    resp.set_cookie("t", token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=is_https(r))
    return token


def current_token_hash(r: Request):
    token = r.cookies.get("t", "")
    return _hash_token(token) if token else ""


def auth_index(r: Request):
    """Geçerli oturumun kişi sırasını döndürür, geçersizse 401."""
    token_hash = current_token_hash(r)
    if token_hash:
        now = int(time.time())
        with db() as c:
            row = c.execute("select * from sessions where token_hash=?", (token_hash,)).fetchone()
            if row and now - row["created"] < SESSION_MAX_AGE and 0 <= row["user_index"] < len(USERS) \
                    and hmac.compare_digest(row["pw_fp"], password_fingerprint(row["user_index"])):
                if now - row["last_seen"] > 3600:
                    c.execute("update sessions set last_seen=? where token_hash=?", (now, token_hash))
                return row["user_index"]
    raise HTTPException(401)


def auth(r: Request):
    """Çerezden kullanıcı adını döndürür, geçersizse 401."""
    return USERS[auth_index(r)][0]


def end_other_sessions(r: Request, i: int, keep_current=True):
    with db() as c:
        if keep_current:
            c.execute("delete from sessions where user_index=? and token_hash!=?", (i, current_token_hash(r)))
        else:
            c.execute("delete from sessions where user_index=?", (i,))


def device_value(i):
    return f"{i}.{hmac.new(SECRET.encode(), f'device:{i}'.encode(), hashlib.sha256).hexdigest()}"


def is_known_device(r: Request, i: int):
    """Bu tarayıcıdan daha önce bu kişi olarak başarıyla giriş yapıldı mı?"""
    return hmac.compare_digest(r.cookies.get("d", ""), device_value(i))


fails = {}


def is_https(r: Request):
    """Çerez yalnızca https bağlantıda 'Secure' işaretlenir; böylece ev ağında
    http://192.168.x.x üzerinden de giriş yapılabilir. Ters vekil arkasında şema,
    uvicorn --proxy-headers ile yalnızca güvenilir vekilden gelen başlıktan çözülür."""
    return r.url.scheme == "https"


class Login(BaseModel):
    username: str
    password: str

class Event(BaseModel):
    title: str
    date: str
    time: str = ""
    place: str = ""
    proposed_by: str = ""


class Note(BaseModel):
    date: str
    author: str = ""
    text: str


class SharedNote(BaseModel):
    title: str = ""
    text: str


class ShoppingItem(BaseModel):
    text: str


class ShoppingList(BaseModel):
    title: str


class WishlistItem(BaseModel):
    title: str
    url: str = ""
    price: str = ""
    currency: str = "TRY"
    priority: str = "normal"

    _check_url = field_validator("url")(lambda cls, v: validation.clean_http_url(v))

class SpecialDay(BaseModel):
    title: str
    date: str
    recurring: bool = True
    icon: str = "❤️"
    note: str = ""


class WatchlistItem(BaseModel):
    title: str
    media_type: str = "movie"
    status: str = "planned"
    genre: str = ""
    poster_url: str = ""
    rating: float | None = None
    note: str = ""

    _check_poster = field_validator("poster_url")(lambda cls, v: validation.clean_http_url(v, 1000))


class Memory(BaseModel):
    title: str
    date: str
    description: str = ""
    location: str = ""


class MemoryPhoto(BaseModel):
    asset_id: str
    sort_order: int = 0

    @field_validator("asset_id")
    @classmethod
    def _check_asset(cls, v):
        if not validation.is_uuid(v):
            raise ValueError("Geçersiz fotoğraf kimliği.")
        return v

class PushSubscription(BaseModel):
    endpoint: str
    keys: dict

# ---------- mail ----------
AY = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
GUN = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


def send_mail(to, subj, body, html_body=None):
    if not SMTP_HOST:
        print(f"[MAIL -> {to or '?'}] {subj}\n{body}\n", flush=True)
        return
    if not to:
        return

    def run():
        try:
            m = EmailMessage()
            m["From"], m["To"], m["Subject"] = SMTP_USER, to, subj
            m.set_content(body)
            if html_body:
                m.add_alternative(html_body, subtype="html")
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(m)
        except Exception as ex:
            print("mail hatası:", ex, flush=True)
    threading.Thread(target=run, daemon=True).start()


def other(name):
    return next((n for n in PEOPLE if n != name), "")


def when(e):
    return e["date"] + (" " + e["time"] if e["time"] else "") + (f" · 📍{e['place']}" if e["place"] else "")


def dt_txt(d, t=""):
    try:
        x = date.fromisoformat(d)
        s = f"{x.day} {AY[x.month - 1]} {x.year}, {GUN[x.weekday()]}"
    except ValueError:
        s = d
    return s + (f" · {t}" if t else "")


def get_ev(i):
    with db() as c:
        return c.execute("select * from events where id=?", (i,)).fetchone()


def add_hist(c, i, who, text):
    c.execute("insert into history(event_id,who,text) values(?,?,?)", (i, who, text))


def mail_html(head, lead, rows, btn, url):
    """Site renklerinde, e-posta istemcilerine uygun (tablo + satır içi stil) HTML."""
    rr = "".join(
        f'<tr><td style="padding:8px 0;color:#8f6f7c;font-size:13px;width:105px;vertical-align:top">{k}</td>'
        f'<td style="padding:8px 0;color:#3a1a2a;font-size:15px;line-height:1.4">{v}</td></tr>' for k, v in rows)
    return f"""<!DOCTYPE html><html lang="tr"><body style="margin:0;padding:0;background:#fbf3f1">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#fbf3f1"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="480" cellpadding="0" cellspacing="0" style="max-width:480px;width:100%;font-family:-apple-system,'Segoe UI',Arial,sans-serif">
<tr><td style="padding:0 4px 14px;font-family:Georgia,'Times New Roman',serif;font-style:italic;font-size:26px;color:#3a1a2a"><span style="color:#d9466f">&#9829;</span><span style="color:#ffb38a">&#9829;</span>&nbsp; Aşkımız</td></tr>
<tr><td bgcolor="#6b2143" style="background:#6b2143;border-radius:20px;padding:26px 24px;color:#fff1ec">
<div style="font-family:Georgia,'Times New Roman',serif;font-size:23px;line-height:1.25;font-weight:bold;color:#fff1ec">{head}</div>
<div style="margin-top:8px;font-size:15px;line-height:1.5;color:#f3d9d6">{lead}</div></td></tr>
<tr><td height="12" style="font-size:0;line-height:0">&nbsp;</td></tr>
<tr><td bgcolor="#ffffff" style="background:#ffffff;border:1px solid #efdde0;border-radius:18px;padding:12px 20px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rr}</table></td></tr>
<tr><td align="center" style="padding:22px 0 10px"><table role="presentation" cellpadding="0" cellspacing="0"><tr>
<td bgcolor="#d9466f" style="background:#d9466f;border-radius:12px"><a href="{url}" style="display:inline-block;padding:14px 32px;color:#ffffff;font-weight:bold;font-size:16px;text-decoration:none">{btn}</a></td></tr></table></td></tr>
<tr><td align="center" style="padding:6px 12px;color:#8f6f7c;font-size:12px;line-height:1.6">Buton çalışmazsa bu adresi tarayıcına yapıştırabilirsin:<br><a href="{url}" style="color:#8f6f7c;word-break:break-all">{url}</a></td></tr>
</table></td></tr></table></body></html>"""


last_note_mail = {}


def notify_ask(i, old=None, reason=None):
    """Karşı tarafa yanıt isteği: yeni öneri ya da revize (old verilirse)."""
    e = get_ev(i)
    if not e:
        return
    frm, to = e["proposed_by"], other(e["proposed_by"])
    url = f"{BASE_URL}/r/{e['token']}"
    new = dt_txt(e["date"], e["time"])
    with db() as c:
        h = c.execute("select text from history where event_id=? order by id desc limit 1", (i,)).fetchone()
    rows = [("Plan", f"<b>{esc(e['title'])}</b>")]
    if old:
        rows += [("Önceki gün", f'<span style="text-decoration:line-through;color:#8f6f7c">{esc(old)}</span>'),
                 ("Yeni gün", f'<b style="color:#d9466f">{esc(new)}</b>')]
        if reason:
            rows.append(("Neden", f"“{esc(reason)}”"))
        head, subj, btn = f"{esc(frm)} başka bir gün önerdi", f"{frm} başka bir gün önerdi 🗓️", "Yeni güne bak"
        lead = f"Selam {esc(to)}, bu sefer tutmadı ama pes yok! {esc(frm)} yeni bir gün buldu, bir göz atsana."
        text = (f"Selam {to}, bu sefer tutmadı ama pes yok!\n{frm} başka bir gün önerdi:\n{e['title']}\n"
                f"Önceki gün: {old}\nYeni gün: {new}\nNeden: {reason}\n\nYeni güne bakmak için:\n{url}\n")
    else:
        rows.append(("Gün", f'<b style="color:#d9466f">{esc(new)}</b>'))
        if h and h["text"].startswith("olmaz"):
            rows.append(("Not", esc(h["text"])))
        head, subj, btn = f"{esc(frm)} seni bir buluşmaya çağırıyor", f"{frm} seni bir buluşmaya çağırıyor 💕", "Cevabını ver"
        lead = (f"Selam {esc(to)}! {esc(frm)} bir plan yapmış. Sence de güzel olmaz mı? "
                "Uyuyorsa tek tıkla kabul et, uymuyorsa başka bir gün öner.")
        text = f"Selam {to}! {frm} seni bir buluşmaya çağırıyor:\n{e['title']}\n{new}\n\nCevabını vermek için:\n{url}\n"
    if e["place"]:
        rows.append(("Mekan", esc(e["place"])))
    send_mail(PEOPLE.get(to, ""), subj, text, mail_html(head, lead, rows, btn, url))


def notify_ok(i):
    """Öneri kabul edilince öneri sahibine bilgi."""
    e = get_ev(i)
    who = other(e["proposed_by"])
    rows = [("Plan", f"<b>{esc(e['title'])}</b>"), ("Gün", f'<b style="color:#d9466f">{esc(dt_txt(e["date"], e["time"]))}</b>')]
    if e["place"]:
        rows.append(("Mekan", esc(e["place"])))
    send_mail(PEOPLE.get(e["proposed_by"], ""), f"{who} evet dedi 🎉",
              f"{who} evet dedi! Buluşma tamam:\n{e['title']}\n{dt_txt(e['date'], e['time'])}\n\n{BASE_URL}\n",
              mail_html(f"{esc(who)} evet dedi 🎉", "Buluşma tamam! Takvimde yerini aldı, artık geriye heyecanla beklemek kaldı.",
                        rows, "Takvimi aç", BASE_URL))


def notify_note(author, day):
    """Not bırakılınca diğer kişiye haber (içeriği maile koymuyoruz; aynı kişiden 10 dk içinde tek mail)."""
    if time.time() - last_note_mail.get(author, 0) < 600:
        return
    last_note_mail[author] = time.time()
    to = other(author)
    url = f"{BASE_URL}/?d={day}" if len(day) == 10 and day.replace("-", "").isdigit() else BASE_URL
    send_mail(PEOPLE.get(to, ""), f"{author} sana bir not bıraktı 💌",
              f"Selam {to}, {author} sana bir not bıraktı. Okumak ya da sen de not bırakmak istersen:\n{url}\n",
              mail_html(f"{esc(author)} sana bir not bıraktı 💌",
                        f"Selam {esc(to)}, küçük bir not var. Okumak ya da sen de bir not bırakmak istersen sayfa seni bekliyor.",
                        [("Hangi gün", f"<b>{esc(dt_txt(day))}</b>")], "Notu oku", url))


# ---------- api ----------

@app.get("/api/push/public-key")
def push_public_key(r: Request):
    auth(r)

    if not VAPID_PUBLIC_KEY:
        raise HTTPException(500, "VAPID public key ayarlanmamış")

    return {"publicKey": VAPID_PUBLIC_KEY}


@app.post("/api/push/subscribe")
def push_subscribe(sub: PushSubscription, r: Request):
    who = auth(r)

    p256dh = sub.keys.get("p256dh")
    auth_key = sub.keys.get("auth")

    if not p256dh or not auth_key or len(str(p256dh)) > 200 or len(str(auth_key)) > 100:
        raise HTTPException(400, "Geçersiz push subscription")
    if not validation.is_push_endpoint(sub.endpoint) or len(sub.endpoint) > 1000:
        raise HTTPException(400, "Desteklenmeyen bildirim servisi")

    with db() as c:
        c.execute("""
            insert into push_subscriptions(user, endpoint, p256dh, auth)
            values(?,?,?,?)
            on conflict(endpoint) do update set
                user=excluded.user,
                p256dh=excluded.p256dh,
                auth=excluded.auth
        """, (
            who,
            sub.endpoint,
            p256dh,
            auth_key
        ))

    return {"ok": True}


@app.post("/api/push/unsubscribe")
def push_unsubscribe(sub: PushSubscription, r: Request):
    who = auth(r)

    with db() as c:
        c.execute(
            "delete from push_subscriptions where endpoint=? and user=?",
            (sub.endpoint, who)
        )

    return {"ok": True}




LOGIN_WINDOW = 1800          # 30 dakika
LOGIN_LIMITS = {
    "ipuser": 3,    # aynı IP'den aynı kullanıcı adına
    "ip": 10,       # aynı IP'den toplam
    "user": 30,     # tüm IP'lerden aynı (gerçek) kullanıcı adına; dağıtık saldırıya karşı yedek
}


def _login_keys(ip, username):
    keys = [("ipuser", f"{ip}|{username}"), ("ip", ip)]
    # Sadece gerçek kullanıcı adları için genel sayaç tut: hem sözlüğün sınırsız
    # büyümesini önler hem de bir yabancının tek başına sizi kilitlemesini zorlaştırır.
    if any(u["username"] == username for u in LOGIN_USERS):
        keys.append(("user", username))
    return keys


def _prune_fails(now):
    if len(fails) > 5000:
        for k in [k for k, (_, started, until) in fails.items()
                  if now - started >= LOGIN_WINDOW and now >= until]:
            fails.pop(k, None)


@app.post("/api/login")
def login(b: Login, r: Request, resp: Response):
    now = time.time()
    ip = r.client.host if r.client else "unknown"
    username = b.username.strip().lower()
    user = next((u for u in LOGIN_USERS if u["username"] == username), None)
    keys = _login_keys(ip, username)
    # Daha önce bu kişi olarak giriş yapılmış bir cihaz, dağıtık saldırı sayacından
    # muaftır: saldırı sürerken bile kendi telefonundan girebilirsin.
    if user and is_known_device(r, user["site_index"]):
        keys = [k for k in keys if k[0] != "user"]

    for kind, key in keys:
        n, started, blocked_until = fails.get((kind, key), (0, 0, 0))
        if now < blocked_until:
            raise HTTPException(429, "Çok fazla hatalı deneme. Biraz sonra tekrar deneyin.")

    # Kullanıcı bulunmasa da benzer parola hesaplama maliyeti uygula
    if user is None:
        hashlib.pbkdf2_hmac("sha256", b"invalid-password", bytes(16), 310000)
        valid = False
    else:
        candidate = hashlib.pbkdf2_hmac("sha256", b.password.encode("utf-8"), user["salt"], user["iterations"])
        valid = hmac.compare_digest(candidate, user["digest"])

    if not valid:
        _prune_fails(now)
        for kind, key in keys:
            n, started, _ = fails.get((kind, key), (0, now, 0))
            if now - started >= LOGIN_WINDOW:
                n, started = 0, now
            n += 1
            fails[(kind, key)] = (n, started, now + LOGIN_WINDOW if n >= LOGIN_LIMITS[kind] else 0)
        raise HTTPException(401, "Kullanıcı adı veya parola hatalı")

    # Başarılı girişte bu IP'nin sayaçlarını temizle; genel kullanıcı sayacı
    # saldırı sürüyorsa korumaya devam etsin diye sadece süresi dolunca sıfırlanır.
    for kind, key in keys:
        if kind != "user":
            fails.pop((kind, key), None)

    issue_session(resp, r, user["site_index"])
    resp.set_cookie("d", device_value(user["site_index"]), max_age=DEVICE_MAX_AGE,
                    httponly=True, samesite="lax", secure=is_https(r))
    return {"ok": True}


@app.get("/api/me")
def me(r: Request):
    return {"ok": True, "name": auth(r), "since": RELATIONSHIP_START}


@app.post("/api/logout")
def logout(r: Request, resp: Response):
    """Bu cihazın oturumunu sunucuda da siler; diğer cihazlar açık kalır."""
    token_hash = current_token_hash(r)
    if token_hash:
        with db() as c:
            c.execute("delete from sessions where token_hash=?", (token_hash,))
    resp.delete_cookie("t", samesite="lax", secure=is_https(r), httponly=True)
    return {"ok": True}


@app.post("/api/logout-others")
def logout_others(r: Request):
    """Bu cihaz açık kalır, diğer tüm cihazlardaki oturumlar kapanır."""
    i = auth_index(r)
    end_other_sessions(r, i)
    return {"ok": True}


@app.get("/api/people")
def people(r: Request):
    auth(r)
    return list(PEOPLE)


@app.get("/api/events")
def events(r: Request):
    auth(r)
    with db() as c:
        rows = [dict(x) for x in c.execute(
            "select id,title,date,time,place,done,status,proposed_by from events order by date, time")]
        for x in rows:
            x["hist"] = [dict(h) for h in c.execute(
                "select who,text from history where event_id=? order by id", (x["id"],))]
    return rows


@app.post("/api/events")
def add_event(e: Event, r: Request):
    e.proposed_by = auth(r)
    with db() as c:
        cur = c.execute("insert into events(title,date,time,place,status,proposed_by,token) values(?,?,?,?,?,?,?)",
                        (e.title, e.date, e.time, e.place, "pending", e.proposed_by, secrets.token_urlsafe(16)))
        i = cur.lastrowid
        add_hist(c, i, e.proposed_by, "önerdi")
    notify_ask(i)
    notify_other(e.proposed_by, "event", f"{e.proposed_by} bir buluşma önerdi 💕", e.title, f"calendar:{e.date}")
    return {"id": i}


@app.post("/api/events/{i}/done")
def toggle_done(i: int, r: Request):
    auth(r)
    with db() as c:
        c.execute("update events set done=1-done where id=? and status='accepted'", (i,))
    return {"ok": True}


@app.post("/api/events/{i}/resend")
def resend(i: int, r: Request):
    auth(r)
    e = get_ev(i)
    if e and e["status"] == "pending":
        notify_ask(i)
    return {"ok": True}


@app.delete("/api/events/{i}")
def del_event(i: int, r: Request):
    auth(r)
    with db() as c:
        c.execute("delete from events where id=?", (i,))
        c.execute("delete from history where event_id=?", (i,))
    return {"ok": True}


@app.get("/api/notes")
def notes(r: Request):
    auth(r)
    with db() as c:
        return [dict(x) for x in c.execute("select * from notes order by id")]


@app.post("/api/notes")
def add_note(n: Note, r: Request):
    n.author = auth(r)
    with db() as c:
        cur = c.execute("insert into notes(date,author,text) values(?,?,?)", (n.date, n.author, n.text))
        i = cur.lastrowid
    notify_note(n.author, n.date)
    notify_other(n.author, "calendar-note", f"{n.author} sana bir takvim notu bıraktı 💌", dt_txt(n.date), f"calendar:{n.date}")
    return {"id": i}


@app.delete("/api/notes/{i}")
def del_note(i: int, r: Request):
    auth(r)
    with db() as c:
        c.execute("delete from notes where id=?", (i,))
    return {"ok": True}


# ---------- ortak alanlar / bildirimler ----------

def send_push(recipient, title, text, target=""):
    if not VAPID_PRIVATE_KEY or not VAPID_SUBJECT:
        print("[PUSH] VAPID ayarları eksik, push gönderilmedi.", flush=True)
        return

    with db() as c:
        subscriptions = c.execute("""
            select id, endpoint, p256dh, auth
            from push_subscriptions
            where user=?
        """, (recipient,)).fetchall()

    payload = json.dumps({
        "title": title,
        "body": text,
        "target": target
    }, ensure_ascii=False)

    for sub in subscriptions:
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {
                "p256dh": sub["p256dh"],
                "auth": sub["auth"]
            }
        }

        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={
                    "sub": VAPID_SUBJECT
                },
                ttl=86400
            )

        except WebPushException as ex:
            print(
                f"[PUSH] Gönderilemedi: {sub['endpoint'][:60]}... -> {ex}",
                flush=True
            )

            # Abonelik artık geçerli değilse temizle
            if ex.response is not None and ex.response.status_code in (404, 410):
                with db() as c:
                    c.execute(
                        "delete from push_subscriptions where id=?",
                        (sub["id"],)
                    )

        except Exception as ex:
            print(f"[PUSH] Hata: {ex}", flush=True)

def notify_user(recipient, actor, kind, title, text, target=""):
    if not recipient or recipient == actor:
        return

    with db() as c:
        c.execute(
            "insert into notifications(recipient,actor,type,title,text,target) values(?,?,?,?,?,?)",
            (recipient, actor, kind, title, text, target)
        )

    send_push(
        recipient,
        title,
        text,
        target
    )


def notify_other(actor, kind, title, text, target=""):
    notify_user(other(actor), actor, kind, title, text, target)

# ============================================================
# IMMICH
# ============================================================

def immich_request(path, method="GET", body=None):
    if not IMMICH_API_KEY:
        raise HTTPException(500, "Immich API anahtarı yapılandırılmamış")

    url = f"{IMMICH_URL}/api/{path.lstrip('/')}"

    headers = {
        "x-api-key": IMMICH_API_KEY,
        "Accept": "application/json",
    }

    data = None

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = UrlRequest(
        url,
        data=data,
        headers=headers,
        method=method
    )

    try:
        with urlopen(req, timeout=15) as response:
            raw = response.read()

            if not raw:
                return {}

            return json.loads(raw.decode("utf-8"))

    except HTTPError as e:
        detail = ""

        try:
            detail = e.read().decode("utf-8")[:500]
        except Exception:
            pass

        raise HTTPException(
            e.code,
            f"Immich API hatası: {detail or e.reason}"
        )

    except URLError as e:
        raise HTTPException(
            502,
            f"Immich'e bağlanılamadı: {e.reason}"
        )

    except Exception as e:
        raise HTTPException(
            502,
            f"Immich bağlantı hatası: {e}"
        )


def get_immich_album():
    albums = immich_request("albums")

    if isinstance(albums, dict):
        albums = albums.get("items", [])

    for album in albums:
        if album.get("albumName") == IMMICH_ALBUM_NAME:
            return album

    return None

@app.get("/api/immich/photos")
def immich_photos(r: Request):
    auth(r)

    album = get_immich_album()

    if not album:
        raise HTTPException(
            404,
            f"Immich albümü bulunamadı: {IMMICH_ALBUM_NAME}"
        )

    album_id = album.get("id")

    result = immich_request(
        "search/metadata",
        method="POST",
        body={
            "albumIds": [album_id],
            "size": 1000
        }
    )

    assets = result.get("assets", {})

    if isinstance(assets, dict):
        assets = assets.get("items", [])

    photos = []

    for asset in assets:
        asset_id = asset.get("id")

        if not asset_id:
            continue

        photos.append({
            "id": asset_id,
            "type": asset.get("type"),
            "filename": asset.get("originalFileName", ""),
            "created": asset.get("fileCreatedAt", ""),
            "thumbnail": f"/api/immich/photos/{asset_id}/thumbnail"
        })

    return {
        "album": {
            "id": album.get("id"),
            "name": album.get("albumName"),
            "asset_count": album.get("assetCount", len(photos))
        },
        "photos": photos
    }


_album_cache = {"at": 0.0, "ids": frozenset()}


def album_asset_ids():
    """Yapılandırılmış albümdeki fotoğraf kimlikleri (60 sn önbellekli)."""
    if time.time() - _album_cache["at"] < 60:
        return _album_cache["ids"]
    ids = frozenset()
    album = get_immich_album()
    if album:
        result = immich_request("search/metadata", method="POST", body={"albumIds": [album.get("id")], "size": 1000})
        assets = result.get("assets", {})
        if isinstance(assets, dict):
            assets = assets.get("items", [])
        ids = frozenset(a.get("id") for a in assets if a.get("id"))
    _album_cache.update(at=time.time(), ids=ids)
    return ids


def require_allowed_asset(asset_id):
    """Sadece paylaşılan albümdeki ya da bir anıya/anlık fotoğrafa eklenmiş
    fotoğraflara erişime izin verir; Immich kütüphanesinin geri kalanı kapalıdır."""
    if not validation.is_uuid(asset_id):
        raise HTTPException(404, "Fotoğraf bulunamadı")
    with db() as c:
        known = c.execute("select 1 from memory_photos where asset_id=? union "
                          "select 1 from instant_photo_messages where asset_id=?", (asset_id, asset_id)).fetchone()
    if known or asset_id in album_asset_ids():
        return asset_id
    _album_cache["at"] = 0.0  # albüme yeni eklenmiş olabilir; bir kez tazele
    if asset_id in album_asset_ids():
        return asset_id
    raise HTTPException(404, "Fotoğraf bulunamadı")


@app.get("/api/immich/photos/{asset_id}/thumbnail")
def immich_thumbnail(asset_id: str, r: Request):
    auth(r)
    asset_id = require_allowed_asset(asset_id)

    if not IMMICH_API_KEY:
        raise HTTPException(500, "Immich API anahtarı yapılandırılmamış")

    url = (
        f"{IMMICH_URL}/api/assets/"
        f"{asset_id}/thumbnail?size=preview"
    )

    req = UrlRequest(
        url,
        headers={
            "x-api-key": IMMICH_API_KEY
        }
    )

    try:
        with urlopen(req, timeout=20) as response:
            content = response.read()
            content_type = response.headers.get(
                "Content-Type",
                "image/jpeg"
            )

            return Response(
                content=content,
                media_type=content_type
            )

    except HTTPError as e:
        raise HTTPException(
            e.code,
            "Immich fotoğrafı alınamadı"
        )

    except URLError as e:
        raise HTTPException(
            502,
            f"Immich'e bağlanılamadı: {e.reason}"
        ) 


def get_or_create_instant_album():
    """Anlık Fotoğraflar için Immich albümünü bulur veya oluşturur."""
    albums = immich_request("albums")
    if isinstance(albums, dict):
        albums = albums.get("items", [])

    for album in albums:
        if album.get("albumName") == INSTANT_ALBUM_NAME:
            return album

    created = immich_request(
        "albums",
        method="POST",
        body={"albumName": INSTANT_ALBUM_NAME, "assetIds": []}
    )
    if not created or not created.get("id"):
        # Bazı Immich sürümleri albüm oluşturma yanıtında tam nesne döndürmeyebilir.
        albums = immich_request("albums")
        if isinstance(albums, dict):
            albums = albums.get("items", [])
        created = next(
            (x for x in albums if x.get("albumName") == INSTANT_ALBUM_NAME),
            None
        )
    if not created:
        raise HTTPException(502, "Immich Aşkımız-Gelenler albümü oluşturulamadı")
    return created


def upload_to_immich(filename: str, content_type: str, content: bytes):
    """Bir fotoğrafı Immich'e yükler ve asset kimliğini döndürür."""
    boundary = "----AskimizUpload" + secrets.token_hex(16)
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    safe_name = Path(filename or "askimiz-fotograf").name.replace('"', "").replace("\r", "").replace("\n", "")
    if not safe_name:
        safe_name = "askimiz-fotograf"
    fields = [
        ("deviceAssetId", secrets.token_hex(16)),
        ("deviceId", "askimiz-site"),
        ("fileCreatedAt", now),
        ("fileModifiedAt", now),
        ("isFavorite", "false"),
    ]
    chunks = []
    for key, value in fields:
        chunks.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    chunks.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="assetData"; filename="{safe_name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
        + content + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    payload = b"".join(chunks)

    req = UrlRequest(
        f"{IMMICH_URL}/api/assets",
        data=payload,
        headers={
            "x-api-key": IMMICH_API_KEY,
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST"
    )
    try:
        with urlopen(req, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:400]
        except Exception:
            pass
        raise HTTPException(502, f"Immich fotoğraf yükleme hatası: {detail or e.reason}")
    except URLError as e:
        raise HTTPException(502, f"Immich'e bağlanılamadı: {e.reason}")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(502, f"Fotoğraf Immich'e yüklenemedi: {e}")

    asset_id = result.get("id") or result.get("assetId")
    if not asset_id:
        raise HTTPException(502, "Immich yükleme yanıtında fotoğraf kimliği bulunamadı")
    return str(asset_id)


@app.get("/api/instant-photos")
def instant_photos(r: Request):
    auth(r)
    with db() as c:
        rows = [
            dict(x) for x in c.execute(
                """select id, asset_id, sender, created
               from instant_photo_messages
               where created >= datetime('now', '-24 hours')
               order by id desc"""
            )
        ]
    for item in rows:
        item["thumbnail"] = f"/api/instant-photos/{item['id']}/thumbnail"
        item["image"] = f"/api/instant-photos/{item['id']}/image"
    return rows


@app.post("/api/instant-photos")
async def add_instant_photo(r: Request, filename: str = "askimiz-fotograf"):
    who = auth(r)
    content_type = (r.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    allowed_types = {
        "image/jpeg", "image/png", "image/webp", "image/gif",
        "image/heic", "image/heif", "image/avif"
    }
    if content_type not in allowed_types:
        raise HTTPException(415, "Lütfen JPEG, PNG, WebP, GIF, HEIC veya AVIF fotoğrafı seç.")
    chunks = []
    total = 0
    async for chunk in r.stream():
        total += len(chunk)
        if total > 15 * 1024 * 1024:
            raise HTTPException(413, "Fotoğraf en fazla 15 MB olabilir.")
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise HTTPException(400, "Fotoğraf dosyası boş.")
    if not IMMICH_API_KEY:
        raise HTTPException(500, "Immich API anahtarı yapılandırılmamış")

    album = get_or_create_instant_album()
    asset_id = upload_to_immich(filename, content_type, content)
    try:
        immich_request(
            f"albums/{album['id']}/assets",
            method="PUT",
            body={"ids": [asset_id]}
        )
    except HTTPException:
        # Fotoğraf Immich'e yüklenmiş olsa da albüme eklenememiş olabilir.
        raise HTTPException(502, "Fotoğraf Immich'e yüklendi ancak Aşkımız-Gelenler albümüne eklenemedi.")

    with db() as c:
        cur = c.execute(
            "insert into instant_photo_messages(asset_id, sender) values(?,?)",
            (asset_id, who)
        )
        message_id = cur.lastrowid

    notify_other(
        who, "instant_photo", f"{who} sana bir anlık fotoğraf gönderdi 📸",
        "Aşkımız-Gelenler albümünde yeni bir fotoğraf var.",
        f"instantphoto:{message_id}"
    )
    return {"ok": True, "id": message_id}


def get_instant_asset(message_id: int):
    with db() as c:
        row = c.execute(
            """select asset_id from instant_photo_messages
               where id=? and created >= datetime('now', '-24 hours')""",
            (message_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Fotoğraf bulunamadı")
    return row["asset_id"]


def immich_image_response(asset_id: str, original: bool):
    if not IMMICH_API_KEY:
        raise HTTPException(500, "Immich API anahtarı yapılandırılmamış")
    path = f"assets/{asset_id}/original" if original else f"assets/{asset_id}/thumbnail?size=preview"
    req = UrlRequest(
        f"{IMMICH_URL}/api/{path}",
        headers={"x-api-key": IMMICH_API_KEY}
    )
    try:
        with urlopen(req, timeout=45) as response:
            content = response.read()
            content_type = response.headers.get("Content-Type", "image/jpeg")
            return Response(content=content, media_type=content_type, headers={"Cache-Control": "private, max-age=300"})
    except HTTPError as e:
        raise HTTPException(e.code, "Immich fotoğrafı alınamadı")
    except URLError as e:
        raise HTTPException(502, f"Immich'e bağlanılamadı: {e.reason}")


@app.get("/api/instant-photos/{message_id}/thumbnail")
def instant_photo_thumbnail(message_id: int, r: Request):
    auth(r)
    return immich_image_response(get_instant_asset(message_id), False)


@app.get("/api/instant-photos/{message_id}/image")
def instant_photo_image(message_id: int, r: Request):
    auth(r)
    return immich_image_response(get_instant_asset(message_id), True)


@app.get("/api/special-days")
def special_days(r: Request):
    auth(r)

    with db() as c:
        return [
            dict(x)
            for x in c.execute("""
                SELECT *
                FROM special_days
                ORDER BY
                    substr(date, 6, 5),
                    date
            """)
        ]


@app.post("/api/special-days")
def add_special_day(item: SpecialDay, r: Request):
    who = auth(r)

    title = item.title.strip()
    day = item.date.strip()

    if not title:
        raise HTTPException(400, "Özel gün adı boş olamaz")

    if not day:
        raise HTTPException(400, "Tarih boş olamaz")

    with db() as c:
        cur = c.execute("""
            INSERT INTO special_days(
                title,
                date,
                recurring,
                icon,
                note,
                created_by
            )
            VALUES(?,?,?,?,?,?)
        """, (
            title,
            day,
            1 if item.recurring else 0,
            item.icon.strip() or "❤️",
            item.note.strip(),
            who
        ))

        i = cur.lastrowid

    notify_other(
        who,
        "special_day",
        f"{who} özel bir gün ekledi ❤️",
        title,
        f"special-day:{i}"
    )

    return {"id": i}


@app.put("/api/special-days/{i}")
def edit_special_day(i: int, item: SpecialDay, r: Request):
    auth(r)

    title = item.title.strip()
    day = item.date.strip()

    if not title:
        raise HTTPException(400, "Özel gün adı boş olamaz")

    with db() as c:
        row = c.execute(
            "SELECT id FROM special_days WHERE id=?",
            (i,)
        ).fetchone()

        if not row:
            raise HTTPException(404, "Özel gün bulunamadı")

        c.execute("""
            UPDATE special_days
            SET title=?,
                date=?,
                recurring=?,
                icon=?,
                note=?
            WHERE id=?
        """, (
            title,
            day,
            1 if item.recurring else 0,
            item.icon.strip() or "❤️",
            item.note.strip(),
            i
        ))

    return {"ok": True}


@app.delete("/api/special-days/{i}")
def delete_special_day(i: int, r: Request):
    auth(r)

    with db() as c:
        row = c.execute(
            "SELECT id FROM special_days WHERE id=?",
            (i,)
        ).fetchone()

        if not row:
            raise HTTPException(404, "Özel gün bulunamadı")

        c.execute(
            "DELETE FROM special_days WHERE id=?",
            (i,)
        )

    return {"ok": True}

@app.get("/api/watchlist")
def watchlist(r: Request):
    auth(r)

    with db() as c:
        return [
            dict(x)
            for x in c.execute("""
                SELECT *
                FROM watchlist_items
                ORDER BY
                    CASE status
                        WHEN 'planned' THEN 0
                        WHEN 'watching' THEN 1
                        WHEN 'watched' THEN 2
                        ELSE 3
                    END,
                    id DESC
            """)
        ]


@app.post("/api/watchlist")
def add_watchlist(item: WatchlistItem, r: Request):
    who = auth(r)

    title = item.title.strip()

    if not title:
        raise HTTPException(400, "İzlenecek içerik adı boş olamaz")

    media_type = (
        item.media_type
        if item.media_type in ("movie", "series")
        else "movie"
    )

    status = (
        item.status
        if item.status in ("planned", "watching", "watched")
        else "planned"
    )

    rating = item.rating

    if rating is not None and not 0 <= rating <= 10:
        raise HTTPException(
            400,
            "Puan 0 ile 10 arasında olmalıdır"
        )

    with db() as c:
        cur = c.execute("""
            INSERT INTO watchlist_items(
                title,
                media_type,
                status,
                genre,
                poster_url,
                rating,
                note,
                added_by,
                watched_at
            )
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (
            title,
            media_type,
            status,
            item.genre.strip(),
            item.poster_url.strip(),
            rating,
            item.note.strip(),
            who,
            date.today().isoformat()
            if status == "watched"
            else None
        ))

        i = cur.lastrowid

    notify_other(
        who,
        "watchlist",
        f"{who} izleneceklere bir şey ekledi 🎬",
        title,
        f"watchlist:{i}"
    )

    return {"id": i}


@app.put("/api/watchlist/{i}")
def edit_watchlist(
    i: int,
    item: WatchlistItem,
    r: Request
):
    auth(r)

    title = item.title.strip()

    if not title:
        raise HTTPException(
            400,
            "İzlenecek içerik adı boş olamaz"
        )

    media_type = (
        item.media_type
        if item.media_type in ("movie", "series")
        else "movie"
    )

    status = (
        item.status
        if item.status in ("planned", "watching", "watched")
        else "planned"
    )

    if item.rating is not None and not 0 <= item.rating <= 10:
        raise HTTPException(
            400,
            "Puan 0 ile 10 arasında olmalıdır"
        )

    with db() as c:
        row = c.execute(
            "SELECT id FROM watchlist_items WHERE id=?",
            (i,)
        ).fetchone()

        if not row:
            raise HTTPException(
                404,
                "İzlenecek içerik bulunamadı"
            )

        c.execute("""
            UPDATE watchlist_items
            SET title=?,
                media_type=?,
                status=?,
                genre=?,
                poster_url=?,
                rating=?,
                note=?,
                watched_at=?
            WHERE id=?
        """, (
            title,
            media_type,
            status,
            item.genre.strip(),
            item.poster_url.strip(),
            item.rating,
            item.note.strip(),
            date.today().isoformat()
            if status == "watched"
            else None,
            i
        ))

    return {"ok": True}

@app.get("/api/memories")
def memories(r: Request):
    auth(r)

    with db() as c:
        rows = c.execute("""
            SELECT
                m.*,
                COUNT(mp.id) AS photo_count
            FROM memories m
            LEFT JOIN memory_photos mp
                ON mp.memory_id = m.id
            GROUP BY m.id
            ORDER BY m.date DESC, m.id DESC
        """).fetchall()

        return [dict(x) for x in rows]


@app.get("/api/memories/{i}")
def memory_detail(i: int, r: Request):
    auth(r)

    with db() as c:
        memory = c.execute(
            "SELECT * FROM memories WHERE id=?",
            (i,)
        ).fetchone()

        if not memory:
            raise HTTPException(404, "Anı bulunamadı")

        photos = c.execute("""
            SELECT
                id,
                asset_id,
                sort_order
            FROM memory_photos
            WHERE memory_id=?
            ORDER BY sort_order, id
        """, (i,)).fetchall()

    result = dict(memory)

    result["photos"] = [
        {
            "id": x["id"],
            "asset_id": x["asset_id"],
            "sort_order": x["sort_order"],
            "thumbnail": (
                f"/api/immich/photos/"
                f"{x['asset_id']}/thumbnail"
            )
        }
        for x in photos
    ]

    return result


@app.post("/api/memories")
def add_memory(item: Memory, r: Request):
    who = auth(r)

    title = item.title.strip()
    day = item.date.strip()

    if not title:
        raise HTTPException(400, "Anı başlığı boş olamaz")

    if not day:
        raise HTTPException(400, "Anı tarihi boş olamaz")

    with db() as c:
        cur = c.execute("""
            INSERT INTO memories(
                title,
                date,
                description,
                location,
                created_by
            )
            VALUES(?,?,?,?,?)
        """, (
            title,
            day,
            item.description.strip(),
            item.location.strip(),
            who
        ))

        i = cur.lastrowid

    notify_other(
        who,
        "memory",
        f"{who} yeni bir anı ekledi ❤️",
        title,
        f"memory:{i}"
    )

    return {"id": i}


@app.put("/api/memories/{i}")
def edit_memory(
    i: int,
    item: Memory,
    r: Request
):
    auth(r)

    title = item.title.strip()
    day = item.date.strip()

    if not title:
        raise HTTPException(
            400,
            "Anı başlığı boş olamaz"
        )

    with db() as c:
        row = c.execute(
            "SELECT id FROM memories WHERE id=?",
            (i,)
        ).fetchone()

        if not row:
            raise HTTPException(
                404,
                "Anı bulunamadı"
            )

        c.execute("""
            UPDATE memories
            SET title=?,
                date=?,
                description=?,
                location=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (
            title,
            day,
            item.description.strip(),
            item.location.strip(),
            i
        ))

    return {"ok": True}


@app.delete("/api/memories/{i}")
def delete_memory(i: int, r: Request):
    auth(r)

    with db() as c:
        row = c.execute(
            "SELECT id FROM memories WHERE id=?",
            (i,)
        ).fetchone()

        if not row:
            raise HTTPException(
                404,
                "Anı bulunamadı"
            )

        c.execute(
            "DELETE FROM memory_photos WHERE memory_id=?",
            (i,)
        )

        c.execute(
            "DELETE FROM memories WHERE id=?",
            (i,)
        )

    return {"ok": True}


@app.post("/api/memories/{i}/photos")
def add_memory_photo(
    i: int,
    photo: MemoryPhoto,
    r: Request
):
    auth(r)
    if photo.asset_id not in album_asset_ids():
        _album_cache["at"] = 0.0
        if photo.asset_id not in album_asset_ids():
            raise HTTPException(400, "Bu fotoğraf paylaşılan Immich albümünde değil.")

    with db() as c:
        row = c.execute(
            "SELECT id FROM memories WHERE id=?",
            (i,)
        ).fetchone()

        if not row:
            raise HTTPException(
                404,
                "Anı bulunamadı"
            )

        existing = c.execute("""
            SELECT id
            FROM memory_photos
            WHERE memory_id=? AND asset_id=?
        """, (
            i,
            photo.asset_id
        )).fetchone()

        if existing:
            return {
                "id": existing["id"],
                "ok": True
            }

        cur = c.execute("""
            INSERT INTO memory_photos(
                memory_id,
                asset_id,
                sort_order
            )
            VALUES(?,?,?)
        """, (
            i,
            photo.asset_id,
            photo.sort_order
        ))

        photo_id = cur.lastrowid

    return {
        "id": photo_id,
        "ok": True
    }


@app.delete("/api/memories/{i}/photos/{photo_id}")
def delete_memory_photo(
    i: int,
    photo_id: int,
    r: Request
):
    auth(r)

    with db() as c:
        c.execute("""
            DELETE FROM memory_photos
            WHERE id=? AND memory_id=?
        """, (
            photo_id,
            i
        ))

    return {"ok": True}    


@app.delete("/api/watchlist/{i}")
def delete_watchlist(i: int, r: Request):
    auth(r)

    with db() as c:
        c.execute(
            "DELETE FROM watchlist_items WHERE id=?",
            (i,)
        )

    return {"ok": True}               

@app.get("/api/dashboard")
def dashboard(r: Request):
    me_name = auth(r)
    today = date.today().isoformat()
    with db() as c:
        upcoming = c.execute("select id,title,date,time,place from events where status='accepted' and done=0 and date>=? order by date,time limit 1", (today,)).fetchone()
        unread = c.execute("select count(*) n from notifications where recipient=? and read=0", (me_name,)).fetchone()["n"]
        notes_count = c.execute("select count(*) n from shared_notes").fetchone()["n"]
        shopping_count = c.execute("select count(*) n from shopping_items where done=0").fetchone()["n"]
        wishlist_count = c.execute("select count(*) n from wishlist_items where done=0").fetchone()["n"]
        recent_notes = [dict(x) for x in c.execute("select id,title,text,author,created from shared_notes order by id desc limit 3")]
        recent_notifications = [dict(x) for x in c.execute("select id,actor,type,title,text,target,read,created from notifications where recipient=? order by id desc limit 5", (me_name,))]
        pending_rows = [dict(x) for x in c.execute(
            "select id,title,date,time,place,proposed_by from events "
            "where status='pending' and done=0 and date>=? order by date,time", (today,))]
    return {"upcoming": dict(upcoming) if upcoming else None, "unread": unread,
            "pending_in": [e for e in pending_rows if e["proposed_by"] != me_name],
            "pending_out": [e for e in pending_rows if e["proposed_by"] == me_name],
            "notes_count": notes_count, "shopping_count": shopping_count, "wishlist_count": wishlist_count,
            "recent_notes": recent_notes, "recent_notifications": recent_notifications}


@app.get("/api/shared-notes")
def shared_notes(r: Request):
    auth(r)
    with db() as c:
        return [dict(x) for x in c.execute("select * from shared_notes order by id desc")]


@app.post("/api/shared-notes")
def add_shared_note(n: SharedNote, r: Request):
    who = auth(r)
    text = n.text.strip()
    title = n.title.strip()
    if not text:
        raise HTTPException(400, "Not boş olamaz")
    with db() as c:
        cur = c.execute("insert into shared_notes(title,text,author) values(?,?,?)", (title, text, who))
        i = cur.lastrowid
    notify_other(who, "note", f"{who} ortak bir not ekledi 📝", title or text[:80], f"notes:{i}")
    return {"id": i}


@app.delete("/api/shared-notes/{i}")
def del_shared_note(i: int, r: Request):
    who = auth(r)
    with db() as c:
        row = c.execute("select author from shared_notes where id=?", (i,)).fetchone()
        if not row:
            raise HTTPException(404)
        if row["author"] != who:
            raise HTTPException(403, "Bu notu sadece oluşturan kişi silebilir")
        c.execute("delete from shared_notes where id=?", (i,))
    return {"ok": True}


@app.get("/api/shopping")
def shopping(r: Request):
    auth(r)
    with db() as c:
        lists = [dict(x) for x in c.execute("""
            select l.id,l.title,l.created_by,l.created,
                   count(i.id) as item_count,
                   sum(case when i.done=0 then 1 else 0 end) as open_count
            from shopping_lists l left join shopping_items i on i.list_id=l.id
            group by l.id order by l.id
        """)]
        items = [dict(x) for x in c.execute("select * from shopping_items order by done,id desc")]
    return {"lists": lists, "items": items}


@app.post("/api/shopping")
def create_shopping_list(item: ShoppingList, r: Request):
    who = auth(r)
    title = item.title.strip()
    if not title:
        raise HTTPException(400, "Liste adı boş olamaz")
    if len(title) > 80:
        raise HTTPException(400, "Liste adı en fazla 80 karakter olabilir")
    with db() as c:
        cur = c.execute("insert into shopping_lists(title,created_by) values(?,?)", (title, who))
        i = cur.lastrowid
    notify_other(who, "shopping", f"{who} yeni bir alışveriş listesi oluşturdu 🛒", title, f"shopping-list:{i}")
    return {"id": i, "title": title}


@app.post("/api/shopping/{list_id}/items")
def add_shopping(list_id: int, item: ShoppingItem, r: Request):
    who = auth(r)
    text = item.text.strip()
    if not text:
        raise HTTPException(400, "Ürün boş olamaz")
    with db() as c:
        if not c.execute("select id from shopping_lists where id=?", (list_id,)).fetchone():
            raise HTTPException(404, "Alışveriş listesi bulunamadı")
        cur = c.execute("insert into shopping_items(text,added_by,list_id) values(?,?,?)", (text, who, list_id))
        i = cur.lastrowid
    notify_other(who, "shopping", f"{who} alışveriş listesine ürün ekledi 🛒", text, f"shopping:{i}")
    return {"id": i}


@app.delete("/api/shopping/lists/{list_id}")
def del_shopping_list(list_id: int, r: Request):
    who = auth(r)
    with db() as c:
        row = c.execute("select title from shopping_lists where id=?", (list_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Alışveriş listesi bulunamadı")
        count = c.execute("select count(*) n from shopping_lists").fetchone()["n"]
        if count <= 1:
            raise HTTPException(400, "Son alışveriş listesini silemezsiniz")
        c.execute("delete from shopping_items where list_id=?", (list_id,))
        c.execute("delete from shopping_lists where id=?", (list_id,))
    notify_other(who, "shopping", f"{who} bir alışveriş listesini sildi 🛒", row["title"], f"shopping-list:{list_id}")
    return {"ok": True}


@app.post("/api/shopping/{i}/toggle")
def toggle_shopping(i: int, r: Request):
    who = auth(r)
    with db() as c:
        row = c.execute("select text,done from shopping_items where id=?", (i,)).fetchone()
        if not row:
            raise HTTPException(404)
        done = 1 - row["done"]
        c.execute("update shopping_items set done=? where id=?", (done, i))
    notify_other(who, "shopping", f"{who} alışveriş listesini güncelledi 🛒", ("Tamamlandı: " if done else "Tekrar alınacak: ") + row["text"], f"shopping:{i}")
    return {"ok": True}


@app.delete("/api/shopping/{i}")
def del_shopping(i: int, r: Request):
    auth(r)
    with db() as c:
        c.execute("delete from shopping_items where id=?", (i,))
    return {"ok": True}


@app.get("/api/wishlist")
def wishlist(r: Request):
    auth(r)
    with db() as c:
        return [dict(x) for x in c.execute("select * from wishlist_items order by done, case priority when 'high' then 0 when 'normal' then 1 else 2 end,id desc")]


@app.post("/api/wishlist")
def add_wishlist(item: WishlistItem, r: Request):
    who = auth(r)

    title = item.title.strip()

    if not title:
        raise HTTPException(400, "İstek boş olamaz")

    priority = (
        item.priority
        if item.priority in ("high", "normal", "low")
        else "normal"
    )

    currency = (
        item.currency
        if item.currency in ("TRY", "EUR", "USD", "GBP")
        else "TRY"
    )

    price = item.price.strip()

    # Fiyat girildiyse sayısal olmalı
    if price:
        try:
            float(price.replace(",", "."))
        except ValueError:
            raise HTTPException(400, "Fiyat sadece sayısal olmalıdır")

    with db() as c:
        cur = c.execute("""
            insert into wishlist_items(
                title,
                url,
                price,
                currency,
                priority,
                added_by
            )
            values(?,?,?,?,?,?)
        """, (
            title,
            item.url.strip(),
            price,
            currency,
            priority,
            who
        ))

        i = cur.lastrowid

    notify_other(
        who,
        "wishlist",
        f"{who} wishlist'e bir şey ekledi 🎁",
        title,
        f"wishlist:{i}"
    )

    return {"id": i}

@app.put("/api/wishlist/{i}")
def edit_wishlist(i: int, item: WishlistItem, r: Request):
    who = auth(r)

    title = item.title.strip()

    if not title:
        raise HTTPException(400, "İstek boş olamaz")

    priority = (
        item.priority
        if item.priority in ("high", "normal", "low")
        else "normal"
    )

    currency = (
        item.currency
        if item.currency in ("TRY", "EUR", "USD", "GBP")
        else "TRY"
    )

    price = item.price.strip()

    if price:
        try:
            float(price.replace(",", "."))
        except ValueError:
            raise HTTPException(400, "Fiyat sadece sayısal olmalıdır")

    with db() as c:
        row = c.execute(
            "select title from wishlist_items where id=?",
            (i,)
        ).fetchone()

        if not row:
            raise HTTPException(404, "Wishlist maddesi bulunamadı")

        c.execute("""
            update wishlist_items
            set title=?,
                url=?,
                price=?,
                currency=?,
                priority=?
            where id=?
        """, (
            title,
            item.url.strip(),
            price,
            currency,
            priority,
            i
        ))

    notify_other(
        who,
        "wishlist",
        f"{who} wishlist'i güncelledi 🎁",
        title,
        f"wishlist:{i}"
    )

    return {"ok": True}    

@app.post("/api/wishlist/{i}/toggle")
def toggle_wishlist(i: int, r: Request):
    who = auth(r)
    with db() as c:
        row = c.execute("select title,done from wishlist_items where id=?", (i,)).fetchone()
        if not row:
            raise HTTPException(404)
        done = 1 - row["done"]
        c.execute("update wishlist_items set done=? where id=?", (done, i))
    notify_other(who, "wishlist", f"{who} wishlist'i güncelledi 🎁", ("Tamamlandı: " if done else "Tekrar istek: ") + row["title"], f"wishlist:{i}")
    return {"ok": True}


@app.delete("/api/wishlist/{i}")
def del_wishlist(i: int, r: Request):
    auth(r)
    with db() as c:
        c.execute("delete from wishlist_items where id=?", (i,))
    return {"ok": True}


@app.get("/api/notifications")
def notifications(r: Request):
    who = auth(r)
    with db() as c:
        return [dict(x) for x in c.execute("select * from notifications where recipient=? order by id desc limit 100", (who,))]


@app.post("/api/notifications/read-all")
def read_all_notifications(r: Request):
    who = auth(r)
    with db() as c:
        c.execute("update notifications set read=1 where recipient=?", (who,))
    return {"ok": True}


@app.post("/api/notifications/{i}/read")
def read_notification(i: int, r: Request):
    who = auth(r)
    with db() as c:
        c.execute("update notifications set read=1 where id=? and recipient=?", (i, who))
    return {"ok": True}


# ---------- mail ile yanıt sayfası (PIN yok, tek kullanımlık gizli link) ----------
CSS = ("body{margin:0;background:#1a0f15;color:#f6e3e8;font:16px/1.45 Montserrat,system-ui,sans-serif;color-scheme:dark}"
       "main{max-width:440px;margin:0 auto;padding:24px 16px}"
       ".c{background:linear-gradient(135deg,#4f1d35,#2b1220);border:1px solid #3a2431;border-radius:20px;padding:20px;margin-bottom:14px}"
       "h1{font-size:1.25rem;margin:0 0 8px}"
       "button{width:100%;border:0;border-radius:12px;background:#d9466f;color:#fff;padding:13px;font:inherit;font-weight:700;margin-top:8px;cursor:pointer}"
       "button.g{background:#3a2230;color:#f6e3e8}"
       "input,textarea{width:100%;box-sizing:border-box;border:1px solid #3a2431;background:#1a0f15;color:#f6e3e8;"
       "border-radius:10px;padding:10px;font:inherit;margin-bottom:6px;color-scheme:dark}"
       "a{color:#ffb38a}")
INVALID = "<div class=c><h1>Bu bağlantı artık geçerli değil 🙈</h1>Yanıt zaten verilmiş olabilir.</div>"


def page(body):
    return HTMLResponse("<!DOCTYPE html><html lang=tr><meta charset=utf-8>"
                        "<meta name=viewport content='width=device-width,initial-scale=1'>"
                        "<meta name=color-scheme content=dark><meta name=theme-color content='#1a0f15'>"
                        f"<title>Aşkımız 💕</title><style>{CSS}</style><main>{body}"
                        "<p style='text-align:center;margin-top:18px'><a href='/'>Uygulamaya dön →</a></p></main></html>")


def pending(tok):
    """Cevap bekleyen öneri; tarihi geçmiş önerilerin bağlantıları artık çalışmaz."""
    if not tok or len(tok) > 64:
        return None
    with db() as c:
        return c.execute("select * from events where token=? and status='pending' and date>=?",
                         (tok, date.today().isoformat())).fetchone()


@app.get("/r/{tok}")
def resp_page(tok: str):
    e = pending(tok)
    if not e:
        return page(INVALID)
    return page(f"""<div class=c><h1>{esc(e['proposed_by'])} bir buluşma önerdi 💕</h1>
<b>{esc(e['title'])}</b><br>{esc(dt_txt(e['date'], e['time']))}{' · 📍' + esc(e['place']) if e['place'] else ''}
<form method=post action=/r/{esc(tok)}/accept><button>Kabul ediyorum 🥰</button></form></div>
<div class=c><h1>Yoksa yok mu?</h1>
<form method=post action=/r/{esc(tok)}/decline>
<textarea name=reason rows=2 placeholder="Neden olmuyor?" required></textarea>
<input type=date name=date required><input type=time name=time value="{esc(e['time'])}">
<button class=g>Bu günü öner</button></form></div>""")


def accept_event(e):
    """Öneriyi kabul eder; öneri sahibine e-posta, uygulama içi bildirim ve push gider."""
    who = other(e["proposed_by"])
    with db() as c:
        c.execute("update events set status='accepted', token='' where id=?", (e["id"],))
        add_hist(c, e["id"], who, "kabul etti")
    notify_ok(e["id"])
    notify_user(e["proposed_by"], who, "accepted", f"{who} evet dedi 🎉",
                f"{e['title']} · {dt_txt(e['date'], e['time'])}", f"calendar:{e['id']}")


CELEBRATE = """<style>.hb{position:fixed;bottom:-40px;font-size:28px;animation:hb 2.6s ease-out forwards;pointer-events:none}
@keyframes hb{to{transform:translateY(-110vh) rotate(var(--r));opacity:0}}
@media(prefers-reduced-motion:reduce){.hb{display:none}}</style>
<script>navigator.vibrate&&navigator.vibrate([30,50,30]);
for(let i=0;i<26;i++){const h=document.createElement('span');h.className='hb';
h.textContent=['❤️','💕','💖','🧡','✨'][i%5];h.style.left=Math.random()*96+'vw';
h.style.animationDelay=Math.random()*.7+'s';h.style.setProperty('--r',(Math.random()*80-40)+'deg');
document.body.appendChild(h);}</script>"""


@app.post("/r/{tok}/accept")
def accept(tok: str):
    e = pending(tok)
    if not e:
        return page(INVALID)
    accept_event(e)
    return page("<div class=c><h1>Harika! Buluşma onaylandı 🎉</h1>Haber verdik.</div>" + CELEBRATE)


def pending_for(i, who):
    e = get_ev(i)
    if not e or e["status"] != "pending":
        raise HTTPException(404, "Bekleyen bir öneri bulunamadı.")
    if e["proposed_by"] == who:
        raise HTTPException(403, "Kendi önerine cevap veremezsin.")
    return e


@app.post("/api/events/{i}/accept")
def accept_in_app(i: int, r: Request):
    who = auth(r)
    accept_event(pending_for(i, who))
    return {"ok": True}


@app.get("/api/events/{i}/respond")
def respond_link(i: int, r: Request):
    """Başka gün önermek için e-postadaki cevap sayfasının adresi."""
    who = auth(r)
    e = pending_for(i, who)
    return {"url": f"/r/{e['token']}"}


@app.post("/r/{tok}/decline")
async def decline(tok: str, r: Request):
    e = pending(tok)
    if not e:
        return page(INVALID)
    if int(r.headers.get("content-length") or 0) > 8192:
        raise HTTPException(413, "İstek çok büyük")
    body = b""
    async for chunk in r.stream():
        body += chunk
        if len(body) > 8192:
            raise HTTPException(413, "İstek çok büyük")
    f = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
    reason, d, tm = f.get("reason", "").strip()[:500], f.get("date", "").strip(), f.get("time", "").strip()
    try:
        valid_date = date.fromisoformat(d) >= date.today()
    except ValueError:
        valid_date = False
    if tm and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", tm):
        tm = ""
    if not reason or not valid_date:
        return page("<div class=c><h1>Neden ve ileri bir tarih gerekli 🙈</h1>Geri dönüp doldurabilirsin.</div>")
    who = other(e["proposed_by"])
    old = dt_txt(e["date"], e["time"])
    with db() as c:
        c.execute("update events set date=?,time=?,proposed_by=?,token=? where id=?",
                  (d, tm, who, secrets.token_urlsafe(16), e["id"]))
        add_hist(c, e["id"], who, f"olmaz: {reason} → yeni öneri {d}{' ' + tm if tm else ''}")
    notify_ask(e["id"], old, reason)
    return page("<div class=c><h1>Yeni tarih iletildi 💌</h1>Onay gelince haber vereceğiz.</div>")


@app.get("/sw.js")
def service_worker():
    js = r"""
self.addEventListener("push", event => {
    if (!event.data) return;

    let data = {};

    try {
        data = event.data.json();
    } catch {
        data = {
            title: "Aşkımız 💕",
            body: event.data.text()
        };
    }

    const title = data.title || "Aşkımız 💕";

    const options = {
        body: data.body || "",
        icon: "/icons/icon-192.png",
        badge: "/icons/icon-192.png",
        data: {
            target: data.target || ""
        },
        vibrate: [200, 100, 200]
    };

    event.waitUntil(
        self.registration.showNotification(title, options)
    );
});


self.addEventListener("notificationclick", event => {
    event.notification.close();

    const target = event.notification.data?.target || "";

    let url = "/";

    if (target.startsWith("calendar:")) {
        url = "/?open=calendar";
    } else if (target.startsWith("notes:")) {
        url = "/?open=notes";
    } else if (target.startsWith("shopping:")) {
        url = "/?open=shopping";
    } else if (target.startsWith("wishlist:")) {
        url = "/?open=wishlist";
    }

    event.waitUntil(
        clients.matchAll({
            type: "window",
            includeUncontrolled: true
        }).then(windowClients => {

            for (const client of windowClients) {
                if ("focus" in client) {
                    client.navigate(url);
                    return client.focus();
                }
            }

            if (clients.openWindow) {
                return clients.openWindow(url);
            }
        })
    );
});
"""

    return Response(
        js,
        media_type="application/javascript"
    )

@app.get("/manifest.json")
def manifest():
    return JSONResponse({
        "name": "Aşkımız", "short_name": "Aşkımız", "start_url": "/", "display": "standalone",
        "background_color": "#1a0f15", "theme_color": "#1a0f15",
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icons/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        ]})


ICON_FILES = {"icon.svg", "icon-192.png", "icon-512.png", "apple-touch-icon.png", "ares.svg"}


@app.get("/icon.svg")
def icon():
    return FileResponse(HERE / "icons" / "icon.svg", media_type="image/svg+xml")


@app.get("/icons/{name}")
def icons(name: str):
    if name not in ICON_FILES:
        raise HTTPException(404)
    return FileResponse(HERE / "icons" / name, headers={"Cache-Control": "public, max-age=604800"})


@app.get("/profile-images/{filename}")
def profile_image(filename: str, r: Request):
    auth(r)
    # Only serve user-supplied PNGs from the dedicated profile-images directory.
    # The filename is deliberately restricted to avoid path traversal and other files.
    if not filename.endswith(".png"):
        raise HTTPException(404, "Profil fotoğrafı bulunamadı")
    stem = filename[:-4]
    if not stem or any(not (ch.isascii() and (ch.isalnum() or ch in "-_")) for ch in stem):
        raise HTTPException(404, "Profil fotoğrafı bulunamadı")
    # Önce kurulum sihirbazının kaydettiği klasöre, sonra proje içindekine bak.
    for profile_dir in (config_store.PROFILE_DIR.resolve(), (HERE / "profile-images").resolve()):
        image_path = (profile_dir / filename).resolve()
        if image_path.parent == profile_dir and image_path.is_file():
            break
    else:
        raise HTTPException(404, "Profil fotoğrafı bulunamadı")
    return FileResponse(image_path, media_type="image/png", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/")
def index():
    # Her açılışta ETag ile yeniden doğrulansın: arayüz güncellemesi telefonlara gecikmeden ulaşır
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


# ---------- ayarlar ----------
import sys  # noqa: E402
import settings_api  # noqa: E402

settings_api.register(app, sys.modules[__name__])

import places_api  # noqa: E402

places_api.register(app, sys.modules[__name__])

import space_api  # noqa: E402
import capsules_api  # noqa: E402

space_api.register(app, sys.modules[__name__])
capsules_api.register(app, sys.modules[__name__])
