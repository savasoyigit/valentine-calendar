"""Kendi alanım modu.

Bir kişi "biraz kendi alanımdayım" dediğinde:
  * karşı taraf bunu ana sayfada görür ve bir kez haber alır,
  * o kişiye giden anlık (push) bildirimler susturulur; uygulama içi
    bildirimler yine kaydedilir, döndüğünde toplu görür,
  * süre verildiyse süre dolunca kendiliğinden döner.

Zamanlar UTC ISO-8601 olarak saklanır; böylece sunucunun saat dilimi önemsizdir.
"""
import threading
import time
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from pydantic import BaseModel


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class SpaceIn(BaseModel):
    away: bool
    note: str = ""
    until: str = ""   # UTC ISO; boş = süresiz


def register(app, core):
    with core.db() as c:
        c.execute("""
            create table if not exists personal_space(
                user text primary key,
                since text not null,
                until text default '',
                note text default ''
            )
        """)

    def active(c, user):
        row = c.execute("select * from personal_space where user=?", (user,)).fetchone()
        if not row:
            return None
        until = parse_iso(row["until"]) if row["until"] else None
        if until and until <= utc_now():
            return None
        return dict(row)

    def is_away(user):
        with core.db() as c:
            return active(c, user) is not None

    # Kendi alanındaki kişiye push gönderme; uygulama içi kayıt yine yapılır.
    original_send_push = core.send_push

    def send_push_respecting_space(recipient, *args, **kwargs):
        if is_away(recipient):
            print(f"[PUSH] {recipient} kendi alanında, push gönderilmedi.", flush=True)
            return
        return original_send_push(recipient, *args, **kwargs)

    core.send_push = send_push_respecting_space

    def come_back(user, automatic=False):
        """Durumu kaldırır; kaldırılacak bir durum varsa karşı tarafa haber verir."""
        with core.db() as c:
            cur = c.execute("delete from personal_space where user=?", (user,))
            changed = cur.rowcount > 0
        if changed:
            core.notify_other(user, "space", f"{user} döndü ☀️",
                              "Süresi doldu, yeniden burada." if automatic else "Yeniden burada.", "space:")
        return changed

    def describe(c, user):
        row = active(c, user)
        if not row:
            return {"name": user, "away": False}
        return {"name": user, "away": True, "since": row["since"], "until": row["until"], "note": row["note"]}

    @app.get("/api/space")
    def get_space(r: Request):
        who = core.auth(r)
        with core.db() as c:
            return {
                "me": describe(c, who),
                "partner": describe(c, core.other(who)),
            }

    @app.put("/api/space")
    def put_space(b: SpaceIn, r: Request):
        who = core.auth(r)
        if not b.away:
            with core.db() as c:
                row = c.execute("select since from personal_space where user=?", (who,)).fetchone()
                missed = 0
                if row:
                    since = parse_iso(row["since"])
                    # notifications.created SQLite'ta UTC "YYYY-MM-DD HH:MM:SS" biçimindedir
                    missed = c.execute(
                        "select count(*) n from notifications where recipient=? and read=0 and created>=?",
                        (who, since.strftime("%Y-%m-%d %H:%M:%S")),
                    ).fetchone()["n"]
            come_back(who)
            return {"ok": True, "missed": missed}

        note = b.note.strip()[:80]
        until = ""
        if b.until:
            dt = parse_iso(b.until)
            if not dt or dt <= utc_now():
                raise HTTPException(400, "Bitiş zamanı gelecekte olmalı.")
            until = iso(dt)
        with core.db() as c:
            was_away = active(c, who) is not None
            c.execute("""
                insert into personal_space(user, since, until, note) values(?,?,?,?)
                on conflict(user) do update set until=excluded.until, note=excluded.note
            """, (who, iso(utc_now()), until, note))
        if not was_away:
            core.notify_other(who, "space", f"{who} biraz kendi alanında 🌙",
                              note or "Açıklama gerekmez, döndüğünde haber vereceğiz.", "space:")
        return {"ok": True}

    def expire_due():
        """Süresi dolan durumları kaldırır ve karşı tarafa 'döndü' der."""
        now = utc_now()
        with core.db() as c:
            rows = c.execute("select user, until from personal_space where until!=''").fetchall()
        for row in rows:
            until = parse_iso(row["until"])
            if until and until <= now:
                come_back(row["user"], automatic=True)

    def loop():
        while True:
            try:
                expire_due()
            except Exception as ex:  # zamanlayıcı asla durmamalı
                print("[SPACE] zamanlayıcı hatası:", ex, flush=True)
            time.sleep(60)

    threading.Thread(target=loop, name="space-expiry", daemon=True).start()
    app.state.space_expire_due = expire_due
