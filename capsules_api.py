"""Zaman kapsülü: belirli bir anda açılan mektuplar.

Gizlilik kuralı sunucuda uygulanır: açılma zamanı gelmeden mektubun başlığı
ve içeriği alıcıya hiç gönderilmez. Yazan kişi kendi mektubunu her zaman görür.
Açılma anında alıcıya bildirim gider; alıcı okuyunca yazana haber verilir.
"""
import threading
import time

from fastapi import HTTPException, Request
from pydantic import BaseModel

from space_api import iso, parse_iso, utc_now


class CapsuleIn(BaseModel):
    title: str = ""
    body: str
    open_at: str  # UTC ISO-8601


def register(app, core):
    with core.db() as c:
        c.execute("""
            create table if not exists capsules(
                id integer primary key,
                author text not null,
                recipient text not null,
                title text default '',
                body text not null,
                open_at text not null,
                created_at text default current_timestamp,
                delivered integer default 0,
                opened_at text default ''
            )
        """)

    def is_open(row):
        dt = parse_iso(row["open_at"])
        return bool(dt and dt <= utc_now())

    def view(row, who):
        unlocked = is_open(row)
        mine = row["author"] == who
        item = {
            "id": row["id"],
            "author": row["author"],
            "recipient": row["recipient"],
            "open_at": row["open_at"],
            "created_at": row["created_at"],
            "unlocked": unlocked,
            "opened_at": row["opened_at"],
            "mine": mine,
        }
        if mine or unlocked:
            item["title"] = row["title"]
            item["body"] = row["body"]
        return item

    @app.get("/api/capsules")
    def list_capsules(r: Request):
        who = core.auth(r)
        with core.db() as c:
            rows = c.execute(
                "select * from capsules where author=? or recipient=? order by open_at",
                (who, who),
            ).fetchall()
        return [view(x, who) for x in rows]

    @app.post("/api/capsules")
    def add_capsule(b: CapsuleIn, r: Request):
        who = core.auth(r)
        body = b.body.strip()
        if not 1 <= len(body) <= 20000:
            raise HTTPException(400, "Mektup boş olamaz (en fazla 20.000 karakter).")
        open_at = parse_iso(b.open_at)
        if not open_at or open_at <= utc_now():
            raise HTTPException(400, "Açılma zamanı gelecekte olmalı.")
        recipient = core.other(who)
        with core.db() as c:
            cur = c.execute(
                "insert into capsules(author,recipient,title,body,open_at) values(?,?,?,?,?)",
                (who, recipient, b.title.strip()[:120], body, iso(open_at)),
            )
            i = cur.lastrowid
        # İçerik vermeden, sadece bir mektubun yolda olduğunu haber ver.
        core.notify_user(recipient, who, "capsule", f"{who} sana bir mektup bıraktı 🔒",
                         "Açılma zamanı gelince okuyabileceksin.", f"capsule:{i}")
        return {"id": i}

    @app.delete("/api/capsules/{i}")
    def delete_capsule(i: int, r: Request):
        who = core.auth(r)
        with core.db() as c:
            row = c.execute("select * from capsules where id=?", (i,)).fetchone()
            if not row or row["author"] != who:
                raise HTTPException(404, "Mektup bulunamadı.")
            if is_open(row):
                raise HTTPException(409, "Açılmış bir mektup silinemez; artık onun.")
            c.execute("delete from capsules where id=?", (i,))
        return {"ok": True}

    @app.post("/api/capsules/{i}/read")
    def read_capsule(i: int, r: Request):
        who = core.auth(r)
        with core.db() as c:
            row = c.execute("select * from capsules where id=?", (i,)).fetchone()
            if not row or row["recipient"] != who:
                raise HTTPException(404, "Mektup bulunamadı.")
            if not is_open(row):
                raise HTTPException(403, "Bu mektubun açılma zamanı henüz gelmedi 🔒")
            first = not row["opened_at"]
            if first:
                c.execute("update capsules set opened_at=? where id=?", (iso(utc_now()), i))
        if first:
            core.notify_user(row["author"], who, "capsule", f"{who} mektubunu okudu 💌",
                             row["title"] or "Zaman kapsülü", f"capsule:{i}")
        return view(dict(row), who)

    def deliver_due():
        """Açılma zamanı gelen mektuplar için alıcıya bir kez bildirim gönderir."""
        now = iso(utc_now())
        with core.db() as c:
            due = c.execute(
                "select * from capsules where delivered=0 and open_at<=?", (now,)
            ).fetchall()
        sent = 0
        for row in due:
            with core.db() as c:
                claimed = c.execute(
                    "update capsules set delivered=1 where id=? and delivered=0", (row["id"],)
                ).rowcount
            if claimed:
                core.notify_user(row["recipient"], row["author"], "capsule",
                                 "Sana bir mektup açıldı 💌",
                                 f"{row['author']} yazdı" + (f": {row['title']}" if row["title"] else ""),
                                 f"capsule:{row['id']}")
                sent += 1
        return sent

    def loop():
        while True:
            try:
                deliver_due()
            except Exception as ex:
                print("[CAPSULE] zamanlayıcı hatası:", ex, flush=True)
            time.sleep(30)

    threading.Thread(target=loop, name="capsule-delivery", daemon=True).start()
    app.state.capsules_deliver_due = deliver_due
